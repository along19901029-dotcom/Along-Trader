"""
ETF AI-Trader Agent — A股 ETF 模拟交易脚本
基于新浪行情 API + 本地模拟撮合，专注 ETF 品种（股票型/指数型），
免印花税，按流动性过滤，动量轮动策略。
"""
import hashlib
import json
import logging
import os
import re
import signal
import smtplib
import sys
import time
from datetime import datetime, timedelta, date
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv

# ── Config ──────────────────────────────────────────────
load_dotenv(Path(__file__).parent / ".env")

AGENT_NAME = os.getenv("AGENT_NAME", "ETFAgent")
INITIAL_CAPITAL = float(os.getenv("INITIAL_CAPITAL", "200000"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOOP_INTERVAL = int(os.getenv("LOOP_INTERVAL", "60"))

# Email
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.qq.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
REPORT_EMAIL = os.getenv("REPORT_EMAIL", "your_email@qq.com")

LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "trader.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
sys.stdout.reconfigure(encoding="utf-8") if hasattr(sys.stdout, "reconfigure") else None
log = logging.getLogger("trader")

STATE_FILE = Path(__file__).parent / "state.json"

# ── Trading Configuration ───────────────────────────────
MAX_POSITION_VALUE = float(os.getenv("MAX_POSITION_VALUE", "50000"))
MAX_POSITIONS = int(os.getenv("MAX_POSITIONS", "5"))
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "-0.08"))
TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", "0.20"))
MIN_DAILY_TURNOVER = float(os.getenv("MIN_DAILY_TURNOVER", "50000000"))
ETF_TYPE_FILTER = os.getenv("ETF_TYPE_FILTER", "股票型,指数型")  # 逗号分隔

# ── State Persistence ───────────────────────────────────


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {
        "positions": {},
        "cash": INITIAL_CAPITAL,
        "daily_trades": [],
        "daily_start_equity": INITIAL_CAPITAL,
        "report_sent_date": "",
        "stop_loss_hits": [],
        "take_profit_hits": [],
        "locked_shares": {},
    }


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str))


# ── Market Clock ────────────────────────────────────────


def is_trading_day() -> bool:
    t = datetime.now()
    if t.weekday() >= 5:
        return False
    return True


def trading_session() -> str:
    if not is_trading_day():
        return "closed"
    t = datetime.now().time()
    if t.replace(9, 30) <= t <= t.replace(11, 30):
        return "morning"
    if t.replace(13, 0) <= t <= t.replace(15, 0):
        return "afternoon"
    return "closed"


def is_market_open() -> bool:
    return trading_session() != "closed"


def minutes_after_close() -> int:
    t = datetime.now()
    close = t.replace(hour=15, minute=0, second=0, microsecond=0)
    delta = (t - close).total_seconds()
    return int(delta // 60) if delta >= 0 else -1


# ── Market Data ─────────────────────────────────────────

_price_cache: dict = {}
_universe_meta: dict = {}  # code → {name, type, turnover, fund_size}
_universe_cache: list = []
_universe_date: str = ""


def _code_to_sina(code: str) -> str:
    """ETF 代码转新浪格式。159xxx → sz, 5xxxxx → sh"""
    code = str(code).strip()
    if code.startswith(("0", "1", "3")):
        return "sz" + code
    return "sh" + code


def _sina_to_code(sina: str) -> str:
    """新浪格式转回 6 位代码。"""
    return sina[2:]


def get_universe() -> list:
    """获取 ETF 候选池（AKShare 全市场 ETF 列表，按类型/流动性过滤，每日刷新）。"""
    global _universe_cache, _universe_meta, _universe_date
    today = datetime.now().strftime("%Y-%m-%d")
    if _universe_cache and _universe_date == today:
        return _universe_cache

    allowed_types = set(t.strip() for t in ETF_TYPE_FILTER.split(","))
    etf_list = []

    def _build_from_rows(rows):
        """从行数据构建 ETF 列表，每行: (code, name, price, turnover, fund_type)"""
        for code, name, price, turnover, fund_type in rows:
            if fund_type not in allowed_types:
                continue
            if turnover < MIN_DAILY_TURNOVER:
                continue
            if price <= 0:
                continue
            sina_code = _code_to_sina(code)
            _universe_meta[sina_code] = {
                "name": name,
                "type": fund_type,
                "turnover": turnover,
            }
            etf_list.append(sina_code)

    # 尝试 AKShare（VPS 上可用）
    try:
        import akshare as ak
        df = ak.fund_etf_spot_em()
        cols = df.columns.tolist()
        # AKShare 列名: 代码, 名称, 最新价, 涨跌幅, ..., 成交额, ..., 基金类型
        rows = []
        for _, row in df.iterrows():
            code = str(row.get("代码", "")).strip()
            name = str(row.get("名称", "")).strip()
            price = float(row.get("最新价", 0))
            turnover = float(row.get("成交额", 0))
            fund_type = str(row.get("基金类型", "")).strip()
            rows.append((code, name, price, turnover, fund_type))
        _build_from_rows(rows)
    except Exception as e:
        log.warning("AKShare ETF 列表获取失败: %s", e)

    # AKShare 失败则尝试 Eastmoney API 直连
    if not etf_list:
        try:
            rows = []
            for page in range(1, 15):
                r = requests.get(
                    "https://push2.eastmoney.com/api/qt/clist/get",
                    params={
                        "pn": str(page), "pz": "100", "po": "1", "np": "1",
                        "fltt": "2", "invt": "2", "fid": "f3",
                        "fs": "b:MK0021,b:MK0022,b:MK0023,b:MK0024",
                        "fields": "f2,f6,f12,f14,f402",
                    },
                    timeout=15,
                )
                data = r.json()
                for d in data.get("data", {}).get("diff", []):
                    if d.get("f12") and d.get("f2"):
                        code = str(d["f12"]).strip()
                        name = d.get("f14", "")
                        price = float(d["f2"])
                        turnover = float(d.get("f6", 0))
                        fund_type = str(d.get("f402", "")).strip()
                        rows.append((code, name, price, turnover, fund_type))
                if not data.get("data", {}).get("diff"):
                    break
            _build_from_rows(rows)
        except Exception as e:
            log.warning("Eastmoney ETF 列表获取失败: %s", e)

    # 最终回退：高流动性 ETF 精选列表（~180 只，覆盖全品类）
    if not etf_list and not _universe_cache:
        log.info("使用内置 ETF 精选列表")
        fallback = [
            # ── 宽基指数 ──
            "510050", "510300", "510500", "510880", "510210", "510310",
            "512100", "512200", "512400", "512500", "512510", "512580",
            "159919", "159915", "159922", "159925", "159949",
            "588000", "588050", "588080", "588200", "588300", "588400",
            # ── 行业：证券/金融 ──
            "512880", "512000", "512070", "512800", "512690", "512990",
            "159940", "159841", "159851",
            # ── 行业：科技/TMT ──
            "512480", "512720", "512760", "512930", "515050", "515880",
            "159995", "159996", "159998", "159997", "159801", "159807",
            "159939", "159994",
            # ── 行业：医药/消费 ──
            "512010", "512120", "512170", "512290", "512600", "512710",
            "159938", "159929", "159992", "159883", "159645",
            # ── 行业：军工/制造 ──
            "512660", "512670", "512680", "512810",
            "159611", "159638", "159967",
            # ── 行业：新能源/周期 ──
            "516160", "516510", "516970", "516520", "516110", "516020",
            "159865", "159766", "159781", "159790", "159875", "159609",
            # ── 行业：基建/地产/交通 ──
            "516950", "516650", "516970", "516880", "516180",
            "159787", "159845",
            # ── 风格/策略 ──
            "512890", "515080", "515100", "515180", "515450",
            "159905", "159906", "159910", "159825",
            # ── 跨境/港股/QDII ──
            "513100", "513500", "513050", "513180", "513060", "513120",
            "159601", "159605", "159607", "159612", "159615", "159688",
            "159696", "159699", "159741", "159792",
            # ── 商品/黄金 ──
            "518880", "518600", "518680",
            "159980", "159981", "159985",
            # ── 债券/货币（低风险备选）──
            "511010", "511020", "511030", "511260", "511360",
            "159649", "159650", "159651",
        ]
        fallback_sina = [_code_to_sina(c) for c in fallback]
        _universe_cache = fallback_sina
        _universe_date = today
        return fallback_sina

    if etf_list:
        _universe_cache = etf_list
        _universe_date = today
        log.info("ETF 候选池已刷新: %s 只标的 (%s)", len(etf_list), ETF_TYPE_FILTER)
    else:
        log.info("使用缓存的 %s 只 ETF", len(_universe_cache))

    return _universe_cache


def screen_buy_candidates(state: dict) -> list:
    """从 ETF 候选池筛选可买入标的，按动量排序。"""
    positions = state.get("positions", {})
    candidates = []
    for code in _price_cache:
        if code in positions:
            continue
        info = _price_cache[code]
        price = info["price"]
        prev = info["prev_close"]
        vol = info.get("volume", 0)
        if price <= 0 or prev <= 0:
            continue
        # 涨停/跌停过滤
        if price >= info["high_limit"] * 0.999:
            continue
        if price <= info["low_limit"] * 1.001:
            continue
        lot_cost = price * 100
        if lot_cost > state["cash"] * 0.25:
            continue
        if lot_cost > MAX_POSITION_VALUE:
            continue
        if vol < 100000:  # 最少 10 万手
            continue
        # 动量得分：涨跌幅 + 日内强势
        chg_pct = (price - prev) / prev
        intraday_pct = (price - info["open"]) / info["open"] if info["open"] > 0 else 0
        score = chg_pct * 0.6 + intraday_pct * 0.4
        candidates.append((code, score, price))
    candidates.sort(key=lambda x: x[1], reverse=True)
    return candidates


def refresh_prices(symbols: list):
    """从新浪行情 API 批量获取 ETF 实时价格。"""
    global _price_cache
    if not symbols:
        return
    sina_codes = ",".join(symbols)
    try:
        r = requests.get(
            f"https://hq.sinajs.cn/list={sina_codes}",
            headers={"Referer": "https://finance.sina.com.cn"},
            timeout=10,
        )
        r.encoding = "gbk"
        for line in r.text.strip().split("\n"):
            m = re.search(r'hq_str_(\w+)="(.+)"', line)
            if not m:
                continue
            code = m.group(1)
            fields = m.group(2).split(",")
            if len(fields) < 32:
                continue
            try:
                name = fields[0]
                prev_close = float(fields[2])
                current = float(fields[3])
                high_limit = float(fields[9]) if fields[9] else prev_close * 1.1
                low_limit = float(fields[10]) if fields[10] else prev_close * 0.9
                _price_cache[code] = {
                    "code": code,
                    "name": name,
                    "price": current,
                    "prev_close": prev_close,
                    "open": float(fields[1]),
                    "high": float(fields[4]),
                    "low": float(fields[5]),
                    "high_limit": high_limit,
                    "low_limit": low_limit,
                    "volume": float(fields[8]),
                }
                # 补充 ETF 元信息中的名称
                if code not in _universe_meta:
                    _universe_meta[code] = {"name": name, "type": "", "turnover": 0}
            except (ValueError, IndexError):
                continue
    except Exception as e:
        log.warning("行情获取失败: %s", e)


def fetch_price(code: str) -> Optional[float]:
    info = _price_cache.get(code)
    return info["price"] if info else None


def get_etf_name(code: str) -> str:
    """获取 ETF 名称，优先 Sina 实时数据，其次 AKShare 元信息。"""
    info = _price_cache.get(code)
    if info and info.get("name"):
        return info["name"]
    meta = _universe_meta.get(code, {})
    return meta.get("name", code)


# ── Strategy ────────────────────────────────────────────


def should_buy(code: str, state: dict) -> bool:
    positions = state.get("positions", {})
    if len(positions) >= MAX_POSITIONS:
        return False
    if code in positions:
        return False
    info = _price_cache.get(code)
    if info and info["price"] >= info["high_limit"] * 0.999:
        return False
    return True


def should_sell(code: str, state: dict):
    """返回 (是否卖出, 原因)。"""
    positions = state.get("positions", {})
    if code not in positions:
        return False, ""

    # T+1：当日买入不能卖出
    locked = state.get("locked_shares", {})
    if code in locked and locked[code] > 0:
        return False, ""

    entry = positions[code]
    entry_price = entry["entry_price"]
    price = fetch_price(code)
    if not price:
        return False, ""
    pnl_pct = (price - entry_price) / entry_price

    if pnl_pct <= STOP_LOSS_PCT:
        return True, "stop_loss"
    if pnl_pct >= TAKE_PROFIT_PCT:
        return True, "take_profit"

    info = _price_cache.get(code)
    if info and info["price"] <= info["low_limit"] * 1.001:
        return False, ""
    return False, ""


# ── Trade Execution ─────────────────────────────────────


def execute_buy(code: str) -> bool:
    """模拟买入 ETF（100 份/手，万三佣金，无印花税）。"""
    state = load_state()
    price = fetch_price(code)
    if not price:
        return False

    lot_size = 100
    max_shares = int(MAX_POSITION_VALUE / price / lot_size) * lot_size
    if max_shares < lot_size:
        log.warning("资金不足以买入 %s 一手", get_etf_name(code))
        return False

    cost = max_shares * price
    fee = cost * 0.0003  # 佣金万三（ETF 免印花税）
    total_cost = cost + fee

    if state["cash"] < total_cost:
        max_shares = int((state["cash"] / (price * 1.0003)) / lot_size) * lot_size
        if max_shares < lot_size:
            return False
        cost = max_shares * price
        fee = cost * 0.0003
        total_cost = cost + fee

    state["cash"] -= total_cost
    state.setdefault("positions", {})[code] = {
        "quantity": max_shares,
        "entry_price": price,
    }
    state.setdefault("locked_shares", {})[code] = (
        state["locked_shares"].get(code, 0) + max_shares
    )

    record_trade(state, "buy", code, max_shares, price, "signal")
    save_state(state)
    log.info(
        "买入 %s(%s) x%s 份 @ ￥%.3f 佣金￥%.2f",
        get_etf_name(code), code, max_shares, price, fee,
    )
    return True


def execute_sell(code: str, reason: str = "signal") -> bool:
    """模拟卖出 ETF（万三佣金，免印花税）。"""
    state = load_state()
    pos = state.get("positions", {}).get(code)
    if not pos:
        log.warning("无 %s 持仓", code)
        return False

    qty = pos["quantity"]
    price = fetch_price(code) or pos["entry_price"]
    proceeds = qty * price
    fee = proceeds * 0.0003  # 万三佣金（ETF 免印花税）
    net = proceeds - fee

    state["cash"] += net
    del state["positions"][code]
    if code in state.get("locked_shares", {}):
        del state["locked_shares"][code]

    record_trade(state, "sell", code, qty, price, reason)
    save_state(state)

    reason_cn = {"stop_loss": "止损", "take_profit": "止盈", "force_sell": "清仓"}.get(
        reason, "信号"
    )
    log.info(
        "卖出 %s(%s) x%s 份 @ ￥%.3f 净收入￥%.2f [%s]",
        get_etf_name(code), code, qty, price, net, reason_cn,
    )
    return True


def record_trade(state, action, symbol, quantity, price, reason):
    state.setdefault("daily_trades", []).append({
        "time": datetime.now().strftime("%H:%M:%S"),
        "action": action,
        "symbol": symbol,
        "quantity": quantity,
        "price": round(price, 3),  # ETF 价格精度到厘
        "reason": reason,
    })


# ── Daily State ─────────────────────────────────────────


def ensure_daily_state(state: dict):
    today_str = datetime.now().strftime("%Y-%m-%d")
    if state.get("_date") != today_str:
        state["daily_trades"] = []
        state["stop_loss_hits"] = []
        state["take_profit_hits"] = []
        state["locked_shares"] = {}
        state["daily_start_equity"] = _calc_total_equity(state)
        state["_date"] = today_str


def _calc_total_equity(state: dict) -> float:
    total = state["cash"]
    for code, pos in state.get("positions", {}).items():
        price = fetch_price(code) or pos.get("entry_price", 0)
        total += price * pos["quantity"]
    return total


# ── Main Loop ───────────────────────────────────────────


def run_loop():
    log.info("══ ETF AI-Trader Agent 启动 ══")
    log.info("初始资金: ￥%s", INITIAL_CAPITAL)
    log.info("ETF 类型: %s | 最低日均成交额: ￥%.0f万",
             ETF_TYPE_FILTER, MIN_DAILY_TURNOVER / 10000)
    log.info("止损 %.0f%% | 止盈 %.0f%% | 持仓上限 %s 只 | 单只 ￥%s",
             STOP_LOSS_PCT * 100, TAKE_PROFIT_PCT * 100,
             MAX_POSITIONS, int(MAX_POSITION_VALUE))
    log.info("交易规则: T+1 | 万三佣金 | 免印花税 | 按流动性过滤")

    state = load_state()
    cycle = 0
    shutdown = False

    def handle_signal(sig, frame):
        nonlocal shutdown
        log.info("收到退出信号，安全关闭...")
        shutdown = True

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    while not shutdown:
        cycle += 1
        t0 = time.time()

        session = trading_session()
        session_cn = {"morning": "早盘", "afternoon": "午盘"}.get(session, "休市")
        log.info("── 周期 #%s [%s] ──", cycle, session_cn)

        try:
            ensure_daily_state(state)

            # 获取 ETF 候选池 + 当前持仓
            universe = get_universe()
            holdings = list(state.get("positions", {}).keys())
            all_symbols = list(set(holdings + universe[:50]))
            refresh_prices(all_symbols)

            if is_market_open():
                # 1. 止损/止盈
                for code in list(state.get("positions", {}).keys()):
                    do_sell, reason = should_sell(code, state)
                    if do_sell:
                        if reason == "stop_loss":
                            log.info("🛑 %s 触发止损", get_etf_name(code))
                            state.setdefault("stop_loss_hits", []).append(code)
                        elif reason == "take_profit":
                            log.info("🎯 %s 触达止盈", get_etf_name(code))
                            state.setdefault("take_profit_hits", []).append(code)
                        execute_sell(code, reason)
                        state = load_state()

                # 2. 买入（动量轮动）
                if len(state.get("positions", {})) < MAX_POSITIONS:
                    if len(universe) > 50:
                        refresh_prices(universe[50:150])
                    candidates = screen_buy_candidates(state)
                    for code, score, price in candidates:
                        if len(state.get("positions", {})) >= MAX_POSITIONS:
                            break
                        if should_buy(code, state):
                            log.info("🔍 筛选买入 %s(%s) score=%.4f",
                                     get_etf_name(code), code, score)
                            execute_buy(code)
                            state = load_state()
                            time.sleep(1)
            else:
                pos_count = len(state.get("positions", {}))
                cash = state["cash"]
                eq = _calc_total_equity(state)
                log.info("⏸ 休市 | 持仓 %s 只 | 现金 ￥%.0f | 总资产 ￥%.0f",
                         pos_count, cash, eq)

            # 3. 日报（15:30-16:30 发送）
            min_after = minutes_after_close()
            today_str = datetime.now().strftime("%Y-%m-%d")
            if (
                30 <= min_after < 90
                and state.get("report_sent_date") != today_str
                and is_trading_day()
            ):
                log.info("📧 收盘后 %d 分钟，发送日报...", min_after)
                try:
                    report = generate_report(state)
                    send_report_email(report)
                    state["report_sent_date"] = today_str
                    save_state(state)
                except Exception as e:
                    log.warning("日报发送失败: %s", e)

            save_state(state)

        except Exception as e:
            log.exception("周期异常: %s", e)

        elapsed = time.time() - t0
        sleep_time = max(5, LOOP_INTERVAL - elapsed)
        time.sleep(sleep_time)

    log.info("Agent 已安全退出")


# ── Report ──────────────────────────────────────────────


def generate_report(state: dict) -> str:
    today_str = datetime.now().strftime("%Y-%m-%d")
    trades = state.get("daily_trades", [])
    positions = state.get("positions", {})
    cash = state["cash"]
    start_eq = state.get("daily_start_equity", INITIAL_CAPITAL)
    total_eq = _calc_total_equity(state)
    daily_pnl = total_eq - start_eq
    etf_value = total_eq - cash
    stop_hits = state.get("stop_loss_hits", [])
    profit_hits = state.get("take_profit_hits", [])

    # 成交
    trade_rows = ""
    if trades:
        for t in trades:
            a = "买" if t["action"] == "buy" else "卖"
            r = {"signal": "策略", "stop_loss": "止损", "take_profit": "止盈",
                 "force_sell": "清仓"}.get(t.get("reason", ""), "")
            trade_rows += (
                f"<tr><td>{t['time']}</td><td>{a}</td><td>{get_etf_name(t['symbol'])}</td>"
                f"<td>{t['symbol']}</td><td>{t['quantity']}</td><td>￥{t['price']:.3f}</td>"
                f"<td>{r}</td></tr>"
            )
    else:
        trade_rows = '<tr><td colspan="7" style="text-align:center;color:#888">今日无成交</td></tr>'

    # 持仓
    pos_rows = ""
    if positions:
        for s, p in positions.items():
            cur = fetch_price(s) or p["entry_price"]
            qty = p["quantity"]
            entry = p["entry_price"]
            mv = cur * qty
            pnl = (cur - entry) * qty
            pnl_pct = (cur - entry) / entry * 100 if entry else 0
            c = "#e74c3c" if pnl < 0 else "#27ae60"
            pos_rows += (
                f"<tr><td>{get_etf_name(s)}</td><td>{s}</td><td>{qty}</td>"
                f"<td>￥{entry:.3f}</td><td>￥{cur:.3f}</td><td>￥{mv:,.0f}</td>"
                f"<td style='color:{c}'>￥{pnl:+,.0f} ({pnl_pct:+.2f}%)</td></tr>"
            )
    else:
        pos_rows = '<tr><td colspan="7" style="text-align:center;color:#888">空仓</td></tr>'

    sl_text = "、".join(get_etf_name(s) for s in stop_hits) if stop_hits else "无"
    tp_text = "、".join(get_etf_name(s) for s in profit_hits) if profit_hits else "无"

    summary = _gen_summary(trades, positions, daily_pnl, stop_hits, profit_hits)

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
    body {{ font-family: -apple-system, 'Microsoft YaHei', sans-serif; background: #f5f6fa; padding: 20px; color: #2c3e50; }}
    .container {{ max-width: 700px; margin: 0 auto; background: #fff; border-radius: 12px; padding: 32px; box-shadow: 0 2px 12px rgba(0,0,0,.08); }}
    h1 {{ font-size: 22px; border-bottom: 2px solid #2ecc71; padding-bottom: 12px; }}
    h2 {{ font-size: 16px; color: #2ecc71; margin-top: 28px; }}
    table {{ width: 100%; border-collapse: collapse; margin: 12px 0; font-size: 13px; }}
    th {{ background: #f0faf4; padding: 10px 8px; text-align: left; }}
    td {{ padding: 8px; border-bottom: 1px solid #eee; }}
    .metrics {{ display: flex; gap: 16px; flex-wrap: wrap; }}
    .metric {{ flex: 1; min-width: 130px; background: #f0faf4; border-radius: 8px; padding: 16px; text-align: center; }}
    .metric .label {{ font-size: 12px; color: #7f8c8d; }}
    .metric .value {{ font-size: 20px; font-weight: 600; margin-top: 4px; }}
    .summary {{ background: #fef9e7; border-left: 4px solid #f39c12; padding: 14px 18px; margin-top: 24px; font-size: 14px; line-height: 1.7; border-radius: 4px; }}
    .footer {{ text-align: center; color: #aaa; font-size: 12px; margin-top: 24px; }}
</style></head><body>
<div class="container">
<h1>ETF AI-Trader 投资日报 {today_str}</h1>

<h2>资产概览</h2>
<div class="metrics">
    <div class="metric"><div class="label">总资产</div><div class="value">￥{total_eq:,.0f}</div></div>
    <div class="metric"><div class="label">现金</div><div class="value">￥{cash:,.0f}</div></div>
    <div class="metric"><div class="label">ETF 持仓市值</div><div class="value">￥{etf_value:,.0f}</div></div>
    <div class="metric"><div class="label">当日盈亏</div>
        <div class="value" style="color:{'#e74c3c' if daily_pnl<0 else '#27ae60'}">{daily_pnl:+,.0f}</div></div>
</div>

<h2>当日成交</h2>
<table><thead><tr><th>时间</th><th>方向</th><th>名称</th><th>代码</th><th>数量(份)</th><th>价格</th><th>原因</th></tr></thead>
<tbody>{trade_rows}</tbody></table>

<h2>当前持仓</h2>
<table><thead><tr><th>名称</th><th>代码</th><th>数量</th><th>买入价</th><th>现价</th><th>市值</th><th>浮动盈亏</th></tr></thead>
<tbody>{pos_rows}</tbody></table>

<h2>止损/止盈触发</h2>
<table><thead><tr><th>类型</th><th>触发标的</th></tr></thead>
<tbody><tr><td>止损 (-{abs(STOP_LOSS_PCT)*100:.0f}%)</td><td>{sl_text}</td></tr>
    <tr><td>止盈 (+{TAKE_PROFIT_PCT*100:.0f}%)</td><td>{tp_text}</td></tr></tbody></table>

<div class="summary"><strong>今日复盘</strong><br>{summary}</div>

<div class="footer">{AGENT_NAME} · ETF 动量轮动 · 自动化系统</div>
</div></body></html>"""


def _gen_summary(trades, positions, daily_pnl, stop_hits, profit_hits):
    parts = []
    buys = sum(1 for t in trades if t["action"] == "buy")
    sells = sum(1 for t in trades if t["action"] == "sell")

    if buys + sells == 0:
        parts.append("今日无操作。")
    else:
        parts.append(f"成交 {buys} 笔买入、{sells} 笔卖出。")
    if buys:
        parts.append("新开仓: " + "、".join(
            get_etf_name(t["symbol"]) for t in trades if t["action"] == "buy"
        ) + "。")
    if sells:
        parts.append("平仓: " + "、".join(
            get_etf_name(t["symbol"]) for t in trades if t["action"] == "sell"
        ) + "。")
    if stop_hits:
        parts.append(f"触发止损: {'、'.join(get_etf_name(s) for s in stop_hits)}。")
    if profit_hits:
        parts.append(f"触发止盈: {'、'.join(get_etf_name(s) for s in profit_hits)}。")
    if len(positions) == 0:
        parts.append("空仓观望。")
    else:
        parts.append(f"持有 {len(positions)} 只 ETF，按 T+1 动量轮动策略跟踪。")
    return "".join(parts)


def send_report_email(html_content: str):
    if not SMTP_USER or not SMTP_PASS:
        log.warning("SMTP 未配置，跳过发送")
        return
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"ETF AI-Trader 投资日报 {datetime.now().strftime('%Y-%m-%d')}"
    msg["From"] = SMTP_USER
    msg["To"] = REPORT_EMAIL
    msg.attach(MIMEText(html_content, "html", "utf-8"))
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=15) as s:
        s.login(SMTP_USER, SMTP_PASS)
        s.sendmail(SMTP_USER, [REPORT_EMAIL], msg.as_string())


# ── Entry ───────────────────────────────────────────────


def main():
    log.info("ETF AI-Trader 启动...")
    log.info("行情源: 新浪财经 | ETF 数据: AKShare | 交易模拟: 本地撮合")
    run_loop()


if __name__ == "__main__":
    main()
