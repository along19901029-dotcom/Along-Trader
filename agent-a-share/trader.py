"""
A股 AI-Trader Agent — A 股模拟交易脚本
基于 Tushare Pro 行情数据 + 本地模拟撮合，支持 T+1、涨跌停、分时段交易。
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
from datetime import datetime, timezone, timedelta, date
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path
from typing import Optional

import requests
import tushare as ts
from dotenv import load_dotenv

# DeepSeek LLM 共享模块
sys.path.insert(0, "/opt/ai-trader-common")
from llm_client import deepseek_ask

# ── Config ──────────────────────────────────────────────
load_dotenv(Path(__file__).parent / ".env")

AGENT_NAME = os.getenv("AGENT_NAME", "AShareAgent")
INITIAL_CAPITAL = float(os.getenv("INITIAL_CAPITAL", "100000"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOOP_INTERVAL = int(os.getenv("LOOP_INTERVAL", "60"))

# Tushare
TUSHARE_TOKEN = os.getenv("TUSHARE_TOKEN", "")
ts.set_token(TUSHARE_TOKEN)
pro = ts.pro_api()
_STOCK_NAMES: dict = {}

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
MAX_POSITION_VALUE = float(os.getenv("MAX_POSITION_VALUE", "10000"))
MAX_POSITIONS = int(os.getenv("MAX_POSITIONS", "5"))

# 筛选参数
MIN_VOLUME_RATIO = float(os.getenv("MIN_VOLUME_RATIO", "1.5"))  # 量比下限
UNIVERSE_INDEX = os.getenv("UNIVERSE_INDEX", "000300")  # 候选池指数: 000300=沪深300, 000905=中证500, 000852=中证1000

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
        "locked_shares": {},  # T+1: {symbol: quantity} today's buys
    }


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str))


# ── Market Clock ────────────────────────────────────────


def is_trading_day() -> bool:
    t = datetime.now()
    if t.weekday() >= 5:
        return False
    try:
        today_str = t.strftime("%Y%m%d")
        cal = pro.trade_cal(exchange="SSE", start_date=today_str,
                           end_date=today_str, is_open="1")
        return len(cal) > 0
    except Exception:
        return True  # fallback: assume trading day


def trading_session() -> str:
    """返回当前交易时段: 'morning', 'afternoon', 'closed'"""
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


# ── Market Data (Tushare Pro) ───────────────────────────

_price_cache: dict = {}
_price_date: str = ""
_PREV_CLOSE_CACHE: dict = {}
_universe_cache: list = []
_universe_date: str = ""
_last_trade_date: str = ""


def _sina_to_ts(code: str) -> str:
    """sh600519 → 600519.SH, sz000001 → 000001.SZ"""
    if code.startswith("sh"):
        return f"{code[2:]}.SH"
    elif code.startswith("sz"):
        return f"{code[2:]}.SZ"
    return code


def _ts_to_sina(code: str) -> str:
    """600519.SH → sh600519, 000001.SZ → sz000001"""
    if code.endswith(".SH"):
        return "sh" + code[:6]
    elif code.endswith(".SZ"):
        return "sz" + code[:6]
    return code


def _get_latest_trade_date() -> str:
    """获取最近一个交易日。"""
    global _last_trade_date
    if _last_trade_date:
        return _last_trade_date
    try:
        cal = pro.trade_cal(exchange="SSE", start_date="20260101",
                           end_date=datetime.now().strftime("%Y%m%d"), is_open="1")
        if len(cal) > 0:
            _last_trade_date = str(cal.iloc[-1]["cal_date"])
            return _last_trade_date
    except Exception:
        pass
    return datetime.now().strftime("%Y%m%d")


def _load_stock_names(ts_codes: list):
    """懒加载股票名称。"""
    global _STOCK_NAMES
    missing = [c for c in ts_codes if c not in _STOCK_NAMES]
    if not missing:
        return
    try:
        codes_str = ",".join(missing[:100])
        df = pro.stock_basic(ts_code=codes_str, fields="ts_code,name")
        for _, row in df.iterrows():
            _STOCK_NAMES[row["ts_code"]] = row["name"]
    except Exception:
        pass


def get_universe() -> list:
    """获取候选股票池（CSI 300 成分股，每日刷新一次）。"""
    global _universe_cache, _universe_date
    today = datetime.now().strftime("%Y-%m-%d")
    if _universe_cache and _universe_date == today:
        return _universe_cache
    try:
        trade_date = _get_latest_trade_date()
        df = pro.index_weight(index_code=f"{UNIVERSE_INDEX}.SH",
                              trade_date=trade_date)
        codes = []
        for _, row in df.iterrows():
            ts_code = row["con_code"]  # e.g. "600519.SH"
            codes.append(_ts_to_sina(ts_code))
        _universe_cache = codes
        _universe_date = today
        log.info("候选池已刷新: %s, %d 只标的", UNIVERSE_INDEX, len(codes))
        return codes
    except Exception as e:
        log.warning("获取指数成分股失败: %s，使用缓存", e)
        return _universe_cache if _universe_cache else []


def get_candidates_for_llm(state: dict) -> list:
    """给 LLM 提供候选标的原始数据（不做打分筛选）。"""
    positions = state.get("positions", {})
    candidates = []
    for code, info in _price_cache.items():
        if code in positions:
            continue
        price = info.get("price", 0)
        if price is None or price <= 0:
            continue
        if price >= info.get("high_limit", 9999) * 0.999:
            continue  # 涨停不推荐
        lot_cost = price * 100
        if lot_cost > MAX_POSITION_VALUE:
            continue
        chg_pct = (price - info["prev_close"]) / info["prev_close"] if info.get("prev_close") else 0
        candidates.append({
            "symbol": code,
            "name": info.get("name", code),
            "price": round(price, 2),
            "prev_close": info.get("prev_close", 0),
            "change_pct": round(chg_pct * 100, 2),
            "volume": info.get("volume", 0),
        })
    candidates.sort(key=lambda x: x["change_pct"], reverse=True)
    return candidates[:30]


def refresh_prices(symbols: list):
    """从 Tushare 批量获取最新日线行情（最多 50 只/次）。"""
    global _price_cache, _PREV_CLOSE_CACHE, _price_date
    if not symbols:
        return

    ts_codes = [_sina_to_ts(s) for s in symbols]
    _load_stock_names(ts_codes)

    try:
        codes_str = ",".join(ts_codes[:50])
        end_date = datetime.now().strftime("%Y%m%d")
        start_date = (datetime.now() - timedelta(days=10)).strftime("%Y%m%d")
        df = pro.daily(ts_code=codes_str, start_date=start_date, end_date=end_date,
                       fields="ts_code,trade_date,open,high,low,close,pre_close,vol")

        # 只在第一行设置最新数据日期
        if len(df) > 0:
            _price_date = str(df.iloc[0]["trade_date"])

        for _, row in df.iterrows():
            ts_code = row["ts_code"]
            code = _ts_to_sina(ts_code)
            close = float(row["close"])
            pre_close = float(row["pre_close"])
            is_cyb_or_kcb = ts_code.startswith("300") or ts_code.startswith("688")
            limit_pct = 0.2 if is_cyb_or_kcb else 0.1
            _price_cache[code] = {
                "code": code,
                "name": _STOCK_NAMES.get(ts_code, code),
                "price": close,
                "prev_close": pre_close,
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "high_limit": round(pre_close * (1 + limit_pct), 2),
                "low_limit": round(pre_close * (1 - limit_pct), 2),
                "volume": float(row["vol"]),
            }
            _PREV_CLOSE_CACHE[code] = pre_close

    except Exception as e:
        log.warning("行情获取失败: %s", e)


def fetch_price(code: str) -> Optional[float]:
    info = _price_cache.get(code)
    return info["price"] if info else None


def get_stock_name(code: str) -> str:
    info = _price_cache.get(code)
    return info["name"] if info else code


def is_at_limit_up(code: str) -> bool:
    info = _price_cache.get(code)
    if info:
        return info["price"] >= info["high_limit"] * 0.999
    return False


def is_at_limit_down(code: str) -> bool:
    info = _price_cache.get(code)
    if info:
        return info["price"] <= info["low_limit"] * 1.001
    return False


# ── LLM System Prompt ────────────────────────────────────

LLM_SYSTEM_PROMPT = """你是A股AI交易员，管理一个自动化交易账户。交易标的是沪深300指数成分股。

## 交易规则
- T+1（当日买入次日才能卖出），有涨跌停限制（主板10%，创业板/科创板20%）
- 交易时段：上午 9:30-11:30，下午 13:00-15:00
- 单只股票最大持仓金额 ￥MAX_POS_VALUE，最大持仓数量 MAX_POS_COUNT 只
- 佣金万三，卖出有千一印花税，最小交易单位 100 股

## 决策框架
你需要根据当前持仓状态和候选标的数据，做出买卖决策：
1. 卖出已持有的标的（注意 T+1 限制：locked_shares 中的标的当日不能卖出）
2. 买入新的候选标的
3. 哪些标的继续持有

## 分析维度
- 持仓标的：看浮动盈亏、T+1锁定状态、是否需要止盈/止损
- 候选标的：看涨跌幅、成交量（vol>10万手）、行业分散度
- 风险控制：涨停不追（接近high_limit）、避免过度集中、单票仓位不宜过重
- 创业板/科创板（300/688开头）涨跌停 20%，价格弹性更大

## 输出格式（严格JSON）
{
  "reasoning": "简要分析逻辑（中文，100字以内）",
  "sells": [{"symbol": "sh600519", "reason": "技术面走弱，获利了结"}],
  "buys": [{"symbol": "sz000001", "reason": "量价齐升，突破前高", "quantity_percent": 80}],
  "hold": ["sh600036"]
}

注意：
- quantity_percent 范围 10-100，表示使用单只上限金额的百分比
- symbol 使用 sh/sz 前缀格式（如 sh600519、sz000001）
- 不要买入涨停标的
- 不要卖出 locked_shares 中的标的
- sells 和 buys 可以为空数组"""


def _build_llm_context(state: dict) -> dict:
    """组装 LLM 输入上下文。"""
    positions = state.get("positions", {})
    locked = state.get("locked_shares", {})
    cash = state.get("cash", 0)

    pos_list = []
    for code, p in positions.items():
        cur_price = fetch_price(code) or p.get("entry_price", 0)
        entry = p.get("entry_price", 0)
        qty = p.get("quantity", 0)
        pnl_pct = (cur_price - entry) / entry if entry else 0
        pos_list.append({
            "symbol": code,
            "name": get_stock_name(code),
            "quantity": qty,
            "entry_price": round(entry, 2),
            "current_price": round(cur_price, 2),
            "pnl_pct": round(pnl_pct, 4),
            "market_value": round(cur_price * qty, 2),
            "t1_locked": code in locked and locked[code] > 0,
        })

    candidates = get_candidates_for_llm(state)
    total_market_value = sum(p["market_value"] for p in pos_list)

    return {
        "market": "A股 沪深300",
        "session": trading_session(),
        "portfolio": {
            "total_equity": round(cash + total_market_value, 2),
            "cash": round(cash, 2),
            "positions_value": round(total_market_value, 2),
            "positions": pos_list,
        },
        "candidates": candidates,
        "constraints": {
            "max_positions": MAX_POSITIONS,
            "max_per_position": MAX_POSITION_VALUE,
            "market_type": "A-share",
            "t1_rule": True,
        },
    }


# ── Trade Execution ─────────────────────────────────────


def execute_buy(code: str) -> bool:
    """模拟买入（100 股/手，向下取整手）。"""
    state = load_state()
    price = fetch_price(code)
    if not price:
        return False

    lot_size = 100
    max_shares = int(
        MAX_POSITION_VALUE / price / lot_size
    ) * lot_size
    if max_shares < lot_size:
        log.warning("资金不足以买入 %s 一手", code)
        return False

    cost = max_shares * price
    fee = cost * 0.0003  # 佣金万三
    total_cost = cost + fee

    if state["cash"] < total_cost:
        max_shares = int(
            (state["cash"] / (price * 1.0003)) / lot_size
        ) * lot_size
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
    # T+1 锁定
    state.setdefault("locked_shares", {})[code] = (
        state["locked_shares"].get(code, 0) + max_shares
    )

    record_trade(state, "buy", code, max_shares, price, "signal")
    save_state(state)
    log.info(
        "买入 %s(%s) x%s @ ￥%.2f 佣金￥%.2f",
        get_stock_name(code), code, max_shares, price, fee,
    )
    return True


def execute_sell(code: str, reason: str = "signal") -> bool:
    state = load_state()
    pos = state.get("positions", {}).get(code)
    if not pos:
        log.warning("无 %s 持仓", code)
        return False

    qty = pos["quantity"]
    price = fetch_price(code) or pos["entry_price"]
    proceeds = qty * price
    fee = proceeds * 0.0003
    stamp_tax = proceeds * 0.001  # 印花税千一
    net = proceeds - fee - stamp_tax

    state["cash"] += net
    del state["positions"][code]
    # 从锁定中移除
    if code in state.get("locked_shares", {}):
        del state["locked_shares"][code]

    record_trade(state, "sell", code, qty, price, reason)
    save_state(state)

    reason_cn = {"stop_loss": "止损", "take_profit": "止盈", "force_sell": "清仓"}.get(
        reason, "信号"
    )
    log.info(
        "卖出 %s(%s) x%s @ ￥%.2f 净收入￥%.2f [%s]",
        get_stock_name(code), code, qty, price, net, reason_cn,
    )
    return True


def record_trade(state, action, symbol, quantity, price, reason):
    state.setdefault("daily_trades", []).append({
        "time": datetime.now().strftime("%H:%M:%S"),
        "action": action,
        "symbol": symbol,
        "quantity": quantity,
        "price": round(price, 2),
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


# ── Data Freshness ──────────────────────────────────────


def is_price_data_today() -> bool:
    if not _price_date:
        return False
    today = datetime.now().strftime('%Y%m%d')
    if _price_date == today:
        return True
    try:
        cal = pro.trade_cal(exchange='SSE', start_date=_price_date,
                           end_date=today, is_open='1')
        if len(cal) == 0:
            return False
        last_date = str(cal.iloc[-1]['cal_date'])
        if last_date == today and len(cal) >= 2:
            last_date = str(cal.iloc[-2]['cal_date'])
        return _price_date == last_date
    except Exception:
        cutoff = (datetime.now() - timedelta(days=3)).strftime('%Y%m%d')
        return _price_date >= cutoff
def minutes_after_market_open() -> int:
    """开盘后已过多少分钟。未开盘返回 -1。"""
    if not is_market_open():
        return -1
    t = datetime.now()
    mkt_open = t.replace(hour=9, minute=30, second=0, microsecond=0)
    delta = (t - mkt_open).total_seconds()
    return int(delta // 60) if delta >= 0 else -1


# ── Main Loop ───────────────────────────────────────────


def run_loop():
    log.info("══ A股 AI-Trader Agent 启动 ══")
    log.info("初始资金: ￥%s", INITIAL_CAPITAL)
    log.info("候选池: %s 指数成分股 | 持仓上限 %s 只 | 单只 ￥%s",
             UNIVERSE_INDEX, MAX_POSITIONS, int(MAX_POSITION_VALUE))
    log.info("决策引擎: DeepSeek-v4-pro LLM 自主决策")
    log.info("交易规则: T+1 | 涨跌停限制 | 佣金万三 | 印花税千一(卖)")

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

            # 获取候选池 + 当前持仓
            universe = get_universe()
            holdings = list(state.get("positions", {}).keys())
            all_symbols = list(set(holdings + universe[:50]))  # 首批 50 只候选
            refresh_prices(all_symbols)

            if is_market_open():
                # 开盘冷却期 + 数据新鲜度检查
                min_after_open = minutes_after_market_open()
                data_fresh = is_price_data_today()
                can_trade = min_after_open >= 5 and data_fresh

                if not can_trade:
                    if min_after_open < 5:
                        log.info("⏳ 开盘冷却期（%d/5 分钟），跳过交易决策", min_after_open)
                    if not data_fresh:
                        log.info("⏳ 行情数据日期=%s 非今日，等待 Tushare 更新...", _price_date)
                else:
                    # LLM 自主决策
                    user_msg = _build_llm_context(state)
                    user_msg["trading_context"] = {
                        "minutes_after_open": min_after_open,
                        "data_date": _price_date,
                    }
                    decisions = deepseek_ask(LLM_SYSTEM_PROMPT, user_msg)

                    if decisions:
                        log.info("LLM 决策: %s", decisions.get("reasoning", "")[:200])

                        # 执行卖出（带护栏）
                        locked = state.get("locked_shares", {})
                        for item in decisions.get("sells", []):
                            sym = str(item.get("symbol", ""))
                            reason = item.get("reason", "LLM signal")
                            if sym not in state.get("positions", {}):
                                log.warning("护栏拦截: %s 未持仓", sym)
                                continue
                            if sym in locked and locked[sym] > 0:
                                log.warning("护栏拦截: %s T+1 锁定", sym)
                                continue
                            if is_at_limit_down(sym):
                                log.warning("护栏拦截: %s 跌停无法卖出", sym)
                                continue
                            execute_sell(sym, reason)
                            state = load_state()
                            time.sleep(1)

                        # 执行买入（带护栏）
                        state = load_state()
                        current_count = len(state.get("positions", {}))
                        for item in decisions.get("buys", []):
                            sym = str(item.get("symbol", ""))
                            reason = item.get("reason", "LLM signal")
                            qty_pct = max(10, min(100, int(item.get("quantity_percent", 100))))

                            if sym in state.get("positions", {}):
                                log.warning("护栏拦截: %s 已持仓", sym)
                                continue
                            if current_count >= MAX_POSITIONS:
                                log.warning("护栏拦截: 已达最大持仓 %s", MAX_POSITIONS)
                                break
                            if is_at_limit_up(sym):
                                log.warning("护栏拦截: %s 涨停无法买入", sym)
                                continue
                            price = fetch_price(sym)
                            if not price or price <= 0:
                                log.warning("护栏拦截: %s 无有效价格", sym)
                                continue

                            # 根据 quantity_percent 调整买入金额
                            adjusted_max = MAX_POSITION_VALUE * qty_pct / 100
                            _orig = MAX_POSITION_VALUE
                            globals()["MAX_POSITION_VALUE"] = adjusted_max
                            ok = execute_buy(sym)
                            globals()["MAX_POSITION_VALUE"] = _orig

                            if ok:
                                current_count += 1
                                time.sleep(1)

                        state = load_state()
                        state["_llm_reasoning"] = decisions.get("reasoning", "")
                        save_state(state)
                    else:
                        log.warning("LLM 调用失败，跳过本周期交易")
            else:
                # 休市快照
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

            # 日报已发送 + 非交易时段 → 退出
            if state.get("report_sent_date") == today_str and not is_market_open():
                log.info("日报已发送，今日任务完成，退出进程")
                save_state(state)
                break

            # 非交易日空闲检测：连续 3 个周期无操作 → 退出
            if not is_trading_day() and not is_market_open():
                idle = state.get("_idle_cycles", 0) + 1
                state["_idle_cycles"] = idle
                if idle >= 3:
                    log.info("非交易日，退出进程")
                    save_state(state)
                    break
            else:
                state["_idle_cycles"] = 0

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
    stock_value = total_eq - cash
    llm_reasoning = state.get("_llm_reasoning", "")

    # 成交
    trade_rows = ""
    if trades:
        for t in trades:
            a = "买" if t["action"] == "buy" else "卖"
            r = {"signal": "策略", "stop_loss": "止损", "take_profit": "止盈",
                 "force_sell": "清仓"}.get(t.get("reason", ""), "")
            trade_rows += (
                f"<tr><td>{t['time']}</td><td>{a}</td><td>{t['symbol']}</td>"
                f"<td>{t['quantity']}</td><td>￥{t['price']:.2f}</td><td>{r}</td></tr>"
            )
    else:
        trade_rows = '<tr><td colspan="6" style="text-align:center;color:#888">今日无成交</td></tr>'

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
                f"<tr><td>{get_stock_name(s)}</td><td>{s}</td><td>{qty}</td>"
                f"<td>￥{entry:.2f}</td><td>￥{cur:.2f}</td><td>￥{mv:,.0f}</td>"
                f"<td style='color:{c}'>￥{pnl:+,.0f} ({pnl_pct:+.2f}%)</td></tr>"
            )
    else:
        pos_rows = '<tr><td colspan="7" style="text-align:center;color:#888">空仓</td></tr>'

    summary = _gen_summary(trades, positions, daily_pnl, llm_reasoning)

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
    body {{ font-family: -apple-system, 'Microsoft YaHei', sans-serif; background: #f5f6fa; padding: 20px; color: #2c3e50; }}
    .container {{ max-width: 700px; margin: 0 auto; background: #fff; border-radius: 12px; padding: 32px; box-shadow: 0 2px 12px rgba(0,0,0,.08); }}
    h1 {{ font-size: 22px; border-bottom: 2px solid #e74c3c; padding-bottom: 12px; }}
    h2 {{ font-size: 16px; color: #e74c3c; margin-top: 28px; }}
    table {{ width: 100%; border-collapse: collapse; margin: 12px 0; font-size: 13px; }}
    th {{ background: #fef5f5; padding: 10px 8px; text-align: left; }}
    td {{ padding: 8px; border-bottom: 1px solid #eee; }}
    .metrics {{ display: flex; gap: 16px; flex-wrap: wrap; }}
    .metric {{ flex: 1; min-width: 130px; background: #fef5f5; border-radius: 8px; padding: 16px; text-align: center; }}
    .metric .label {{ font-size: 12px; color: #7f8c8d; }}
    .metric .value {{ font-size: 20px; font-weight: 600; margin-top: 4px; }}
    .summary {{ background: #fef9e7; border-left: 4px solid #f39c12; padding: 14px 18px; margin-top: 24px; font-size: 14px; line-height: 1.7; border-radius: 4px; }}
    .footer {{ text-align: center; color: #aaa; font-size: 12px; margin-top: 24px; }}
</style></head><body>
<div class="container">
<h1>A股 AI-Trader 投资日报 {today_str}</h1>

<h2>资产概览</h2>
<div class="metrics">
    <div class="metric"><div class="label">总资产</div><div class="value">￥{total_eq:,.0f}</div></div>
    <div class="metric"><div class="label">现金</div><div class="value">￥{cash:,.0f}</div></div>
    <div class="metric"><div class="label">持仓市值</div><div class="value">￥{stock_value:,.0f}</div></div>
    <div class="metric"><div class="label">当日盈亏</div>
        <div class="value" style="color:{'#e74c3c' if daily_pnl<0 else '#27ae60'}">{daily_pnl:+,.0f}</div></div>
</div>

<h2>当日成交</h2>
<table><thead><tr><th>时间</th><th>方向</th><th>代码</th><th>数量(股)</th><th>价格</th><th>原因</th></tr></thead>
<tbody>{trade_rows}</tbody></table>

<h2>当前持仓</h2>
<table><thead><tr><th>名称</th><th>代码</th><th>数量</th><th>买入价</th><th>现价</th><th>市值</th><th>浮动盈亏</th></tr></thead>
<tbody>{pos_rows}</tbody></table>

<h2>AI 决策思路</h2>
<div class="summary"><strong>LLM 复盘</strong><br>{summary}</div>

<div class="footer">{AGENT_NAME} · DeepSeek-v4-pro 驱动</div>
</div></body></html>"""


def _gen_summary(trades, positions, daily_pnl, llm_reasoning: str):
    parts = []
    buys = sum(1 for t in trades if t["action"] == "buy")
    sells = sum(1 for t in trades if t["action"] == "sell")

    if buys + sells == 0:
        parts.append("今日无操作。")
    else:
        parts.append(f"成交 {buys} 笔买入、{sells} 笔卖出。")
    if buys:
        parts.append("新开仓: " + "、".join(t["symbol"] for t in trades if t["action"] == "buy") + "。")
    if sells:
        parts.append("平仓: " + "、".join(t["symbol"] for t in trades if t["action"] == "sell") + "。")
    if llm_reasoning:
        parts.append(f"AI 决策逻辑: {llm_reasoning}")
    if len(positions) == 0:
        parts.append("空仓观望，等待 DeepSeek LLM 判断交易机会。")
    else:
        parts.append(f"持有 {len(positions)} 只标的，由 DeepSeek-v4-pro 持续监控决策。")
    return "".join(parts)


def send_report_email(html_content: str):
    if not SMTP_USER or not SMTP_PASS:
        log.warning("SMTP 未配置，跳过发送")
        return
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"A股 AI-Trader 投资日报 {datetime.now().strftime('%Y-%m-%d')}"
    msg["From"] = SMTP_USER
    msg["To"] = REPORT_EMAIL
    msg.attach(MIMEText(html_content, "html", "utf-8"))
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=15) as s:
        s.login(SMTP_USER, SMTP_PASS)
        s.sendmail(SMTP_USER, [REPORT_EMAIL], msg.as_string())


# ── Entry ───────────────────────────────────────────────


def main():
    log.info("A股 AI-Trader 启动...")
    log.info("行情源: Tushare Pro | 交易模拟: 本地撮合")
    run_loop()


if __name__ == "__main__":
    main()
