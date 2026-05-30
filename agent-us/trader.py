"""
AI-Trader US Stock Agent — 美股自动化交易脚本
数据源：Stooq（实时价格）+ Wikipedia（S&P 500 成分股每日抓取）
交易执行：ai4trade.ai 模拟交易平台
"""

import hashlib
import json
import logging
import os
import signal
import smtplib
import sys
import time
from datetime import datetime, timezone, timedelta, date
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path
from typing import Optional

import pandas as pd
import requests
import tushare as ts
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

BASE_URL = os.getenv("BASE_URL", "https://ai4trade.ai")
AGENT_NAME = os.getenv("AGENT_NAME", "USTrader")
AGENT_EMAIL = os.getenv("AGENT_EMAIL", "")
AGENT_PASSWORD = os.getenv("AGENT_PASSWORD", "")
TUSHARE_TOKEN = os.getenv("TUSHARE_TOKEN", "")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

pro = ts.pro_api(TUSHARE_TOKEN) if TUSHARE_TOKEN else None

# Email
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.qq.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
REPORT_EMAIL = os.getenv("REPORT_EMAIL", "504975497@qq.com")
LOOP_INTERVAL = int(os.getenv("LOOP_INTERVAL", "60"))

LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
sys.stdout.reconfigure(encoding="utf-8") if hasattr(sys.stdout, "reconfigure") else None
log = logging.getLogger("trader")

TOKEN_FILE = Path(__file__).parent / ".token"
STATE_FILE = Path(__file__).parent / "state.json"

# ── Trading Config ────────────────────────────────────────

MAX_POSITION_VALUE = float(os.getenv("MAX_POSITION_VALUE", "10000"))
MAX_POSITIONS = int(os.getenv("MAX_POSITIONS", "5"))
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "-0.10"))
TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", "0.30"))


# ── Session ───────────────────────────────────────────────

class Session:
    def __init__(self):
        self.token: Optional[str] = None
        self.agent_id: Optional[int] = None
        self.headers: dict = {}

    def set_token(self, token: str):
        self.token = token
        self.headers = {"Authorization": f"Bearer {token}"}

    def post(self, path: str, body: Optional[dict] = None) -> dict:
        try:
            r = requests.post(f"{BASE_URL}{path}", headers=self.headers,
                              json=body, timeout=15)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            log.warning("POST %s failed: %s", path, e)
            return {"success": False, "error": str(e)}

    def get(self, path: str, params: Optional[dict] = None) -> dict:
        try:
            r = requests.get(f"{BASE_URL}{path}", headers=self.headers,
                             params=params, timeout=15)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            log.warning("GET %s failed: %s", path, e)
            return {"success": False, "error": str(e)}


api = Session()


def register_or_login() -> bool:
    if TOKEN_FILE.exists():
        saved = TOKEN_FILE.read_text().strip()
        if saved:
            api.set_token(saved)
            me = api.get("/api/claw/agents/me")
            if me.get("id"):
                api.agent_id = me["id"]
                log.info("Token 恢复成功 — Agent #%s (%s), 现金 $%s",
                         me["id"], me.get("name"), me.get("cash"))
                return True
            log.warning("Token 已过期，重新登录")

    if not AGENT_EMAIL or not AGENT_PASSWORD:
        log.error("请在 .env 中设置 AGENT_EMAIL 和 AGENT_PASSWORD")
        return False

    # 尝试登录
    resp = api.post("/api/claw/agents/login", {
        "name": AGENT_NAME, "email": AGENT_EMAIL, "password": AGENT_PASSWORD,
    })
    if resp.get("token"):
        api.set_token(resp["token"])
        TOKEN_FILE.write_text(resp["token"])
        api.agent_id = resp.get("agent_id")
        log.info("登录成功 — Agent #%s", api.agent_id)
        return True

    # 登录失败则注册新 Agent（初始资金 $100,000）
    resp = api.post("/api/claw/agents/selfRegister", {
        "name": AGENT_NAME, "email": AGENT_EMAIL, "password": AGENT_PASSWORD,
    })
    if resp.get("token"):
        api.set_token(resp["token"])
        TOKEN_FILE.write_text(resp["token"])
        api.agent_id = resp.get("agent_id")
        log.info("注册成功 — Agent #%s (%s), 初始现金 $100,000",
                 api.agent_id, AGENT_NAME)
        return True

    log.error("认证失败: %s", resp)
    return False


# ── State ─────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {
        "positions": {},
        "daily_trades": [],
        "daily_start_equity": 100000,
        "report_sent_date": "",
        "stop_loss_hits": [],
        "take_profit_hits": [],
    }


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str))


# ── Market Clock ──────────────────────────────────────────

def _get_et_now() -> datetime:
    utc = datetime.now(timezone.utc)
    today = utc.astimezone(timezone(timedelta(hours=-5)))
    year = today.year
    mar_second_sun = date(year, 3, 1)
    while mar_second_sun.weekday() != 6:
        mar_second_sun = mar_second_sun + timedelta(days=1)
    mar_second_sun = mar_second_sun + timedelta(days=7)
    nov_first_sun = date(year, 11, 1)
    while nov_first_sun.weekday() != 6:
        nov_first_sun = nov_first_sun + timedelta(days=1)
    dst = mar_second_sun <= date(year, today.month, today.day) < nov_first_sun
    offset = timedelta(hours=-4 if dst else -5)
    return utc.astimezone(timezone(offset))


_US_HOLIDAYS_2026 = {
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16),
    date(2026, 4, 3), date(2026, 5, 25), date(2026, 6, 19),
    date(2026, 7, 3), date(2026, 9, 7), date(2026, 11, 26),
    date(2026, 12, 25),
}


def is_market_open() -> bool:
    et = _get_et_now()
    if et.weekday() >= 5:
        return False
    if et.date() in _US_HOLIDAYS_2026:
        return False
    t = et.time()
    return t >= t.replace(hour=9, minute=30) and t <= t.replace(hour=16, minute=0)


def market_closed_today() -> bool:
    et = _get_et_now()
    if et.weekday() >= 5:
        return True
    if et.date() in _US_HOLIDAYS_2026:
        return True
    return False


def minutes_after_market_close() -> int:
    et = _get_et_now()
    close = et.replace(hour=16, minute=0, second=0, microsecond=0)
    delta = (et - close).total_seconds()
    return int(delta // 60) if delta >= 0 else -1


# ── Market Data ───────────────────────────────────────────

_price_cache: dict = {}
_universe_cache: list = []
_universe_date: str = ""


def refresh_prices(symbols: list):
    global _price_cache
    if not symbols or pro is None:
        return
    end_date = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
    start_date = (datetime.now() - timedelta(days=5)).strftime("%Y%m%d")
    for i in range(0, len(symbols), 50):
        batch = symbols[i:i+50]
        ts_codes = ",".join(batch)
        try:
            df = pro.us_daily(ts_code=ts_codes, start_date=start_date, end_date=end_date,
                             fields="ts_code,trade_date,close")
            if df is None or len(df) == 0:
                continue
            latest = df.sort_values("trade_date", ascending=False).groupby("ts_code").first()
            for code, row in latest.iterrows():
                close = row.get("close")
                if close is not None and close > 0:
                    _price_cache[code.upper()] = float(close)
        except Exception as e:
            log.debug("Tushare 批量获取价格失败 [%d:%d]: %s", i, i+50, e)
            time.sleep(0.5)


def fetch_price(symbol: str) -> Optional[float]:
    return _price_cache.get(symbol.upper())


def get_universe() -> list:
    global _universe_cache, _universe_date
    today = datetime.now().strftime("%Y-%m-%d")
    if _universe_cache and _universe_date == today:
        return _universe_cache
    try:
        url = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/master/data/constituents.csv"
        r = requests.get(url, timeout=15)
        lines = r.text.strip().split("\n")
        tickers = []
        for line in lines[1:]:  # skip header
            cols = line.split(",")
            if cols:
                sym = cols[0].strip()
                if sym and sym.isascii():
                    tickers.append(sym.replace(".", "-"))
        _universe_cache = tickers
        _universe_date = today
        log.info("S&P 500 候选池已刷新: %s 只标的", len(tickers))
        return tickers
    except Exception as e:
        log.warning("获取 S&P 500 成分股失败: %s，使用缓存", e)
        return _universe_cache if _universe_cache else []


def screen_buy_candidates(state: dict) -> list:
    positions = state.get("positions", {})
    candidates = []
    for sym in _price_cache:
        if sym in positions:
            continue
        price = _price_cache[sym]
        if price <= 0 or price > MAX_POSITION_VALUE:
            continue
        candidates.append((sym, 0, price))
    candidates.sort(key=lambda x: x[2])
    return candidates


def generate_uid(prefix: str = "") -> str:
    h = hashlib.sha256(f"{prefix}{time.time()}".encode()).hexdigest()[:12]
    return f"{prefix}{h}"


# ── Strategy ─────────────────────────────────────────────

def should_buy(symbol: str, price: float, state: dict) -> bool:
    positions = state.get("positions", {})
    if len(positions) >= MAX_POSITIONS:
        return False
    if symbol in positions:
        return False
    return True


def should_sell(symbol: str, current_price: float, state: dict):
    positions = state.get("positions", {})
    if symbol not in positions:
        return False, ""
    entry = positions[symbol]
    entry_price = entry["entry_price"]
    pnl_pct = (current_price - entry_price) / entry_price
    if pnl_pct <= STOP_LOSS_PCT:
        return True, "stop_loss"
    if pnl_pct >= TAKE_PROFIT_PCT:
        return True, "take_profit"
    return False, ""


# ── Trade Execution ──────────────────────────────────────

def record_trade(action: str, symbol: str, quantity: int, price: float, reason: str):
    state = load_state()
    trade = {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "action": action,
        "symbol": symbol,
        "quantity": quantity,
        "price": round(price, 2),
        "reason": reason,
    }
    state.setdefault("daily_trades", []).append(trade)
    save_state(state)


def _ensure_daily_state(state: dict):
    today_str = datetime.now().strftime("%Y-%m-%d")
    if state.get("report_sent_date") != today_str:
        if state.get("daily_trades"):
            state["daily_trades"] = []
        if state.get("stop_loss_hits"):
            state["stop_loss_hits"] = []
        if state.get("take_profit_hits"):
            state["take_profit_hits"] = []
    if not state.get("daily_start_equity") or state.get("_start_date") != today_str:
        state["daily_start_equity"] = state.get("cash", 100000)
        state["_start_date"] = today_str


def execute_buy(symbol: str) -> bool:
    price = fetch_price(symbol) or 100
    quantity = max(1, int(MAX_POSITION_VALUE / max(price, 1)))
    resp = api.post("/api/signals/realtime", {
        "market": "us-stock",
        "action": "buy",
        "symbol": symbol,
        "price": 0,
        "quantity": quantity,
        "executed_at": "now",
    })
    if resp.get("success"):
        log.info("买入 %s x%s", symbol, quantity)
        record_trade("buy", symbol, quantity, price, "signal")
        return True
    log.warning("买入失败 %s: %s", symbol, resp)
    return False


def execute_sell(symbol: str, reason: str = "signal") -> bool:
    state = load_state()
    pos = state.get("positions", {}).get(symbol)
    if not pos:
        log.warning("无 %s 持仓，跳过卖出", symbol)
        return False
    price = fetch_price(symbol) or pos.get("entry_price", 0)
    qty = pos["quantity"]
    resp = api.post("/api/signals/realtime", {
        "market": "us-stock",
        "action": "sell",
        "symbol": symbol,
        "price": 0,
        "quantity": qty,
        "executed_at": "now",
    })
    if resp.get("success"):
        log.info("卖出 %s x%s", symbol, qty)
        record_trade("sell", symbol, qty, price, reason)
        del state["positions"][symbol]
        save_state(state)
        if reason == "stop_loss":
            state.setdefault("stop_loss_hits", []).append(symbol)
            save_state(state)
        elif reason == "take_profit":
            state.setdefault("take_profit_hits", []).append(symbol)
            save_state(state)
        return True
    log.warning("卖出失败 %s: %s", symbol, resp)
    return False


# ── Position Sync ────────────────────────────────────────

def sync_positions():
    resp = api.get("/api/positions")
    if not resp or "positions" not in resp:
        return
    state = load_state()
    api_positions = resp.get("positions", [])
    new_positions = {}
    for p in api_positions:
        sym = p["symbol"]
        new_positions[sym] = {
            "quantity": p.get("quantity", 0),
            "entry_price": p.get("entry_price", 0),
            "current_price": p.get("current_price", 0),
            "pnl": p.get("pnl", 0),
        }
    state["positions"] = new_positions
    state["cash"] = resp.get("cash", state.get("cash", 100000))
    save_state(state)
    if new_positions:
        log.info("持仓: %s", ", ".join(
            f"{s}(qty={d['quantity']}, PnL=${d.get('pnl') or 0:.0f})"
            for s, d in new_positions.items()
        ))
    else:
        log.info("空仓")
    position_value = sum(
        (p.get("current_price") or p.get("entry_price") or 0) * abs(p.get("quantity", 0))
        for p in api_positions
    )
    total = resp.get("cash", 0) + position_value
    log.info("现金 $%.0f | 持仓市值 $%.0f | 总资产 $%.0f",
             resp.get("cash", 0), position_value, total)


# ── Main Loop ────────────────────────────────────────────

def run_loop():
    log.info("══ US Stock Agent 启动 ══")
    log.info("候选池: S&P 500 (每日从 GitHub 动态抓取)")
    log.info("止损 %.0f%% | 止盈 +%.0f%% | 单笔上限 $%s | 最大持仓 %s",
             STOP_LOSS_PCT * 100, TAKE_PROFIT_PCT * 100,
             int(MAX_POSITION_VALUE), MAX_POSITIONS)
    log.info("交易时段: 美东 9:30-16:00（开盘时段执行买卖）")

    state = load_state()
    _ensure_daily_state(state)
    save_state(state)
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
        log.info("── 周期 #%s ──", cycle)

        try:
            # 1. 心跳
            api.post("/api/claw/agents/heartbeat")

            # 2. 同步持仓
            sync_positions()
            state = load_state()
            _ensure_daily_state(state)
            save_state(state)

            # 3. 行情
            holdings = list(state.get("positions", {}).keys())
            universe = get_universe()
            batch = list(set(holdings + universe[:50]))
            refresh_prices(batch)

            if is_market_open():
                # 4. 止损止盈检查
                for symbol in list(state.get("positions", {}).keys()):
                    price = fetch_price(symbol)
                    if price:
                        do_sell, reason = should_sell(symbol, price, state)
                        if do_sell:
                            if reason == "stop_loss":
                                log.info("🛑 %s 触发止损", symbol)
                            elif reason == "take_profit":
                                log.info("🎯 %s 触达止盈", symbol)
                            execute_sell(symbol, reason)

                # 5. 买入扫描
                candidates = screen_buy_candidates(state)
                buys_in_cycle = 0
                current_pos_count = len(state.get("positions", {}))
                for symbol, score, price in candidates:
                    if current_pos_count + buys_in_cycle >= MAX_POSITIONS:
                        break
                    if should_buy(symbol, price, state):
                        if execute_buy(symbol):
                            buys_in_cycle += 1
                            state = load_state()
                            time.sleep(2)

                state = load_state()
            else:
                log.info("⏸ 非交易时段，跳过买卖操作")

            # 6. 日报
            min_after = minutes_after_market_close()
            today_str = datetime.now().strftime("%Y-%m-%d")
            if 30 <= min_after < 90 and state.get("report_sent_date") != today_str:
                if not market_closed_today():
                    log.info("📧 收盘后 %d 分钟，发送日报...", min_after)
                    try:
                        report = generate_report(state)
                        send_report_email(report)
                        state["report_sent_date"] = today_str
                        save_state(state)
                        log.info("📧 日报已发送至 %s", REPORT_EMAIL)
                    except Exception as e:
                        log.warning("日报发送失败: %s", e)

            # 日报已发送 + 非交易时段 → 退出
            if state.get("report_sent_date") == today_str and not is_market_open():
                log.info("日报已发送，今日任务完成，退出进程")
                save_state(state)
                break

            # 非交易日空闲 → 退出
            if not is_market_open() and market_closed_today():
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


# ── Report ───────────────────────────────────────────────

def generate_report(state: dict) -> str:
    today_str = datetime.now().strftime("%Y-%m-%d")
    trades = state.get("daily_trades", [])
    positions = state.get("positions", {})
    cash = state.get("cash", 0)
    start_equity = state.get("daily_start_equity", 100000)
    stop_hits = state.get("stop_loss_hits", [])
    profit_hits = state.get("take_profit_hits", [])

    position_value = 0
    for s, p in positions.items():
        price = fetch_price(s) or p.get("current_price") or p.get("entry_price") or 0
        position_value += price * p.get("quantity", 0)
    total = cash + position_value
    daily_pnl = total - start_equity

    trade_rows = ""
    if trades:
        for t in trades:
            a = "买入" if t["action"] == "buy" else "卖出"
            r = {"signal": "策略信号", "stop_loss": "止损",
                 "take_profit": "止盈", "force_sell": "清仓"}.get(t.get("reason", ""), t.get("reason", ""))
            trade_rows += f"<tr><td>{t['time']}</td><td>{a}</td><td>{t['symbol']}</td><td>{t['quantity']}</td><td>${t['price']:.2f}</td><td>{r}</td></tr>"
    else:
        trade_rows = '<tr><td colspan="6" style="text-align:center;color:#888">今日无成交</td></tr>'

    pos_rows = ""
    if positions:
        for s, p in positions.items():
            price = fetch_price(s) or p.get("current_price") or p.get("entry_price") or 0
            qty = p.get("quantity", 0)
            entry = p.get("entry_price", 0)
            mv = price * qty
            pnl = (price - entry) * qty
            pnl_pct = (price - entry) / entry * 100 if entry else 0
            c = "#e74c3c" if pnl < 0 else "#27ae60"
            pos_rows += f"<tr><td>{s}</td><td>{qty}</td><td>${entry:.2f}</td><td>${price:.2f}</td><td>${mv:,.0f}</td><td style='color:{c}'>${pnl:,.0f} ({pnl_pct:+.1f}%)</td></tr>"
    else:
        pos_rows = '<tr><td colspan="6" style="text-align:center;color:#888">空仓</td></tr>'

    sl_text = "、".join(stop_hits) if stop_hits else "无"
    tp_text = "、".join(profit_hits) if profit_hits else "无"
    summary = _gen_summary(trades, positions, daily_pnl, stop_hits, profit_hits)

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
    body {{ font-family: -apple-system, 'Microsoft YaHei', sans-serif; background: #f5f6fa; padding: 20px; color: #2c3e50; }}
    .container {{ max-width: 680px; margin: 0 auto; background: #fff; border-radius: 12px; padding: 32px; box-shadow: 0 2px 12px rgba(0,0,0,.08); }}
    h1 {{ font-size: 22px; border-bottom: 2px solid #3498db; padding-bottom: 12px; }}
    h2 {{ font-size: 16px; color: #3498db; margin-top: 28px; }}
    table {{ width: 100%; border-collapse: collapse; margin: 12px 0; font-size: 14px; }}
    th {{ background: #f0f3f8; padding: 10px 8px; text-align: left; }}
    td {{ padding: 8px; border-bottom: 1px solid #eee; }}
    .metrics {{ display: flex; gap: 16px; flex-wrap: wrap; }}
    .metric {{ flex: 1; min-width: 120px; background: #f0f3f8; border-radius: 8px; padding: 16px; text-align: center; }}
    .metric .label {{ font-size: 12px; color: #7f8c8d; }}
    .metric .value {{ font-size: 20px; font-weight: 600; margin-top: 4px; }}
    .summary {{ background: #fef9e7; border-left: 4px solid #f39c12; padding: 14px 18px; margin-top: 24px; font-size: 14px; line-height: 1.7; border-radius: 4px; }}
    .footer {{ text-align: center; color: #aaa; font-size: 12px; margin-top: 24px; }}
</style></head><body>
<div class="container">
<h1>美股 AI-Trader 投资日报 {today_str}</h1>

<h2>资产概览</h2>
<div class="metrics">
    <div class="metric"><div class="label">总资产</div><div class="value">${total:,.0f}</div></div>
    <div class="metric"><div class="label">现金</div><div class="value">${cash:,.0f}</div></div>
    <div class="metric"><div class="label">持仓市值</div><div class="value">${position_value:,.0f}</div></div>
    <div class="metric"><div class="label">当日盈亏</div><div class="value" style="color:{'#e74c3c' if daily_pnl<0 else '#27ae60'}">{daily_pnl:+,.0f}</div></div>
</div>

<h2>当日成交</h2>
<table><thead><tr><th>时间</th><th>方向</th><th>标的</th><th>数量</th><th>价格</th><th>原因</th></tr></thead>
<tbody>{trade_rows}</tbody></table>

<h2>当前持仓</h2>
<table><thead><tr><th>标的</th><th>数量</th><th>买入价</th><th>现价</th><th>市值</th><th>浮动盈亏</th></tr></thead>
<tbody>{pos_rows}</tbody></table>

<h2>止损/止盈触发</h2>
<table><thead><tr><th>类型</th><th>触发标的</th></tr></thead>
<tbody><tr><td>止损 (-{abs(STOP_LOSS_PCT)*100:.0f}%)</td><td>{sl_text}</td></tr><tr><td>止盈 (+{TAKE_PROFIT_PCT*100:.0f}%)</td><td>{tp_text}</td></tr></tbody></table>

<div class="summary"><strong>投资思路复盘</strong><br>{summary}</div>

<div class="footer">美股 AI-Trader Agent · S&P 500 策略 · 自动化系统</div>
</div></body></html>"""


def _gen_summary(trades, positions, daily_pnl, stop_hits, profit_hits) -> str:
    parts = []
    buys = sum(1 for t in trades if t["action"] == "buy")
    sells = sum(1 for t in trades if t["action"] == "sell")
    if buys + sells == 0:
        parts.append("今日无交易操作，持仓维持不变。")
    else:
        parts.append(f"今日共完成 {buys} 笔买入、{sells} 笔卖出。")
        if buys:
            bought = [t["symbol"] for t in trades if t["action"] == "buy"]
            parts.append("新开仓标的：" + "、".join(bought) + "。")
        if sells:
            sold = [t["symbol"] for t in trades if t["action"] == "sell"]
            parts.append("平仓标的：" + "、".join(sold) + "。")
    if stop_hits:
        parts.append(f"触发止损：{'、'.join(stop_hits)}，已按纪律平仓。")
    if profit_hits:
        parts.append(f"触发止盈：{'、'.join(profit_hits)}，获利了结。")
    if daily_pnl > 0:
        parts.append(f"当日整体录得正收益 +${daily_pnl:,.0f}，策略运行符合预期。")
    elif daily_pnl < 0:
        parts.append(f"当日录得亏损 ${daily_pnl:,.0f}，需关注止损执行和市场波动。")
    else:
        parts.append("当日整体持平。")
    if positions:
        parts.append(f"当前持有 {len(positions)} 个标的，继续按策略跟踪。")
    else:
        parts.append("当前空仓，等待下一个交易机会。")
    return "".join(parts)


def send_report_email(html_content: str):
    if not SMTP_USER or not SMTP_PASS:
        log.warning("SMTP 未配置，跳过邮件发送")
        return
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"美股 AI-Trader 投资日报 {datetime.now().strftime('%Y-%m-%d')}"
    msg["From"] = SMTP_USER
    msg["To"] = REPORT_EMAIL
    msg.attach(MIMEText(html_content, "html", "utf-8"))
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=15) as s:
        s.login(SMTP_USER, SMTP_PASS)
        s.sendmail(SMTP_USER, [REPORT_EMAIL], msg.as_string())


# ── Entry ────────────────────────────────────────────────

def main():
    log.info("正在连接 ai4trade.ai ...")
    if not register_or_login():
        sys.exit(1)
    me = api.get("/api/claw/agents/me")
    log.info("Agent: %s | 现金: $%s | 积分: %s",
             me.get("name"), me.get("cash"), me.get("points"))
    run_loop()


if __name__ == "__main__":
    main()
