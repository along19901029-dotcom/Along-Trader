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
REPORT_EMAIL = os.getenv("REPORT_EMAIL", "504975497@qq.com")

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
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "-0.10"))
TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", "0.30"))

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


def screen_buy_candidates(state: dict) -> list:
    """从候选池中筛选可买入标的，按动量排序。"""
    positions = state.get("positions", {})
    candidates = []
    for code in _price_cache:
        if code in positions:
            continue
        info = _price_cache[code]
        price = info["price"]
        prev = info["prev_close"]
        vol = info.get("volume", 0)
        # 过滤：涨跌停、价格过高、无成交
        if price <= 0 or prev <= 0:
            continue
        if price >= info["high_limit"] * 0.999:
            continue
        if price <= info["low_limit"] * 1.001:
            continue
        lot_cost = price * 100
        if lot_cost > state["cash"] * 0.25:
            continue
        if lot_cost > MAX_POSITION_VALUE:
            continue
        if vol < 100000:
            continue
        # 动量得分 = 涨跌幅 + 日内涨幅（当日强势优先）
        chg_pct = (price - prev) / prev
        intraday_pct = (price - info["open"]) / info["open"] if info["open"] > 0 else 0
        score = chg_pct * 0.6 + intraday_pct * 0.4
        candidates.append((code, score, price))
    candidates.sort(key=lambda x: x[1], reverse=True)
    return candidates


def refresh_prices(symbols: list):
    """从 Tushare 批量获取最新日线行情（最多 50 只/次）。"""
    global _price_cache, _PREV_CLOSE_CACHE
    if not symbols:
        return

    ts_codes = [_sina_to_ts(s) for s in symbols]
    _load_stock_names(ts_codes)

    try:
        codes_str = ",".join(ts_codes[:50])
        df = pro.daily(ts_code=codes_str,
                       fields="ts_code,trade_date,open,high,low,close,pre_close,vol")

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


# ── Strategy ────────────────────────────────────────────


def should_buy(code: str, state: dict) -> bool:
    positions = state.get("positions", {})
    if len(positions) >= MAX_POSITIONS:
        return False
    if code in positions:
        return False
    if is_at_limit_up(code):
        log.debug("%s 涨停，无法买入", code)
        return False
    return True


def should_sell(code: str, state: dict):
    """返回 (是否卖出, 原因)。"""
    positions = state.get("positions", {})
    if code not in positions:
        return False, ""

    # T+1 检查：当日买入不能卖出
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

    if is_at_limit_down(code):
        log.debug("%s 跌停，无法卖出", code)
        return False, ""
    return False, ""


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


# ── Main Loop ───────────────────────────────────────────


def run_loop():
    log.info("══ A股 AI-Trader Agent 启动 ══")
    log.info("初始资金: ￥%s", INITIAL_CAPITAL)
    log.info("候选池: %s 指数成分股（动态筛选）", UNIVERSE_INDEX)
    log.info("止损 %.0f%% | 止盈 %.0f%% | 持仓上限 %s 只 | 单只 ￥%s",
             STOP_LOSS_PCT * 100, TAKE_PROFIT_PCT * 100,
             MAX_POSITIONS, int(MAX_POSITION_VALUE))
    log.info("交易规则: T+1 | 涨跌停限制 | 佣金万三 | 印花税千一(卖) | 全市场筛选")

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
                # 1. 止损/止盈（持仓标的）
                for code in list(state.get("positions", {}).keys()):
                    do_sell, reason = should_sell(code, state)
                    if do_sell:
                        if reason == "stop_loss":
                            log.info("🛑 %s 触发止损", get_stock_name(code))
                            state.setdefault("stop_loss_hits", []).append(code)
                        elif reason == "take_profit":
                            log.info("🎯 %s 触达止盈", get_stock_name(code))
                            state.setdefault("take_profit_hits", []).append(code)
                        execute_sell(code, reason)
                        state = load_state()

                # 2. 买入（从全市场候选池筛选）
                if len(state.get("positions", {})) < MAX_POSITIONS:
                    # 扩展行情覆盖到更多候选
                    if len(universe) > 50:
                        refresh_prices(universe[50:150])
                    candidates = screen_buy_candidates(state)
                    for code, score, price in candidates:
                        if len(state.get("positions", {})) >= MAX_POSITIONS:
                            break
                        if should_buy(code, state):
                            log.info("🔍 筛选买入 %s(%s) score=%.4f",
                                     get_stock_name(code), code, score)
                            execute_buy(code)
                            state = load_state()
                            time.sleep(1)
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
            if state.get("report_sent_date") == today_str and not is_trading_time():
                log.info("日报已发送，今日任务完成，退出进程")
                save_state(state)
                break

            # 非交易日空闲检测：连续 3 个周期无操作 → 退出
            if not is_trading_day() and not is_trading_time():
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

    sl_text = "、".join(stop_hits) if stop_hits else "无"
    tp_text = "、".join(profit_hits) if profit_hits else "无"

    summary = _gen_summary(trades, positions, daily_pnl, stop_hits, profit_hits)

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

<h2>止损/止盈触发</h2>
<table><thead><tr><th>类型</th><th>触发标的</th></tr></thead>
<tbody><tr><td>止损 (-10%)</td><td>{sl_text}</td></tr><tr><td>止盈 (+30%)</td><td>{tp_text}</td></tr></tbody></table>

<div class="summary"><strong>今日复盘</strong><br>{summary}</div>

<div class="footer">{AGENT_NAME} · 自动化系统</div>
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
        parts.append("新开仓: " + "、".join(t["symbol"] for t in trades if t["action"] == "buy") + "。")
    if sells:
        parts.append("平仓: " + "、".join(t["symbol"] for t in trades if t["action"] == "sell") + "。")
    if stop_hits:
        parts.append(f"触发止损: {'、'.join(stop_hits)}。")
    if profit_hits:
        parts.append(f"触发止盈: {'、'.join(profit_hits)}。")
    if len(positions) == 0:
        parts.append("空仓观望。")
    else:
        parts.append(f"持有 {len(positions)} 只标的，按 T+1 纪律跟踪。")
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
    log.info("行情源: 新浪财经 | 交易模拟: 本地撮合")
    run_loop()


if __name__ == "__main__":
    main()
