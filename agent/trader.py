"""
AI-Trader Agent — 美股自动化交易脚本
数据源：Tushare Pro（us_daily），本地模拟撮合，DeepSeek-v4-pro LLM 自主决策。
T+0 无涨跌停，1 股起交易，免印花税。
"""

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
import tushare as ts
from intraday_module import refresh_intraday, get_intraday, get_intraday_price
from curl_cffi import requests as cffi_requests
from dotenv import load_dotenv

# DeepSeek LLM 共享模块
sys.path.insert(0, "/opt/ai-trader-common")
from llm_client import deepseek_ask, apply_decisions

# ── Config ──────────────────────────────────────────────
load_dotenv(Path(__file__).parent / ".env")

AGENT_NAME = os.getenv("AGENT_NAME", "USTrader_DeepSeek")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# Tushare Pro
TUSHARE_TOKEN = os.getenv("TUSHARE_TOKEN", "")
ts.set_token(TUSHARE_TOKEN)
pro = ts.pro_api()

# Email (QQ SMTP)
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.qq.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
REPORT_EMAIL = os.getenv("REPORT_EMAIL", "your_email@qq.com")
LOOP_INTERVAL = int(os.getenv("LOOP_INTERVAL", "60"))  # seconds

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
# Force UTF-8 on stdout to avoid emoji encoding issues on Windows
sys.stdout.reconfigure(encoding="utf-8") if hasattr(sys.stdout, "reconfigure") else None
log = logging.getLogger("trader")

STATE_FILE = Path(__file__).parent / "state.json"

# ── Trading Configuration ───────────────────────────────
INITIAL_CAPITAL = float(os.getenv("INITIAL_CAPITAL", "100000"))
MAX_POSITION_VALUE = float(os.getenv("MAX_POSITION_VALUE", "10000"))
MAX_POSITIONS = int(os.getenv("MAX_POSITIONS", "5"))
UNIVERSE_INDEX = os.getenv("UNIVERSE_INDEX", "sp500")  # sp500 / nasdaq100

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
    }


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str))

# ── Market Clock ────────────────────────────────────────


def _get_et_now() -> datetime:
    """当前美东时间（自动处理夏令时）。"""
    utc = datetime.now(timezone.utc)
    # 美国夏令时：3月第二个周日 至 11月第一个周日
    today = utc.astimezone(timezone(timedelta(hours=-5)))  # 先按 EST
    year = today.year
    # 3月第二个周日
    mar_second_sun = date(year, 3, 1)
    while mar_second_sun.weekday() != 6:
        mar_second_sun = mar_second_sun + timedelta(days=1)
    mar_second_sun = mar_second_sun + timedelta(days=7)
    # 11月第一个周日
    nov_first_sun = date(year, 11, 1)
    while nov_first_sun.weekday() != 6:
        nov_first_sun = nov_first_sun + timedelta(days=1)
    dst = mar_second_sun <= date(year, today.month, today.day) < nov_first_sun
    offset = timedelta(hours=-4 if dst else -5)
    return utc.astimezone(timezone(offset))


_US_HOLIDAYS_2026 = {
    date(2026, 1, 1),   # New Year's Day
    date(2026, 1, 19),  # MLK Day
    date(2026, 2, 16),  # Presidents Day
    date(2026, 4, 3),   # Good Friday
    date(2026, 5, 25),  # Memorial Day
    date(2026, 6, 19),  # Juneteenth
    date(2026, 7, 3),   # Independence Day (observed, 7/4 is Saturday)
    date(2026, 9, 7),   # Labor Day
    date(2026, 11, 26), # Thanksgiving
    date(2026, 12, 25), # Christmas
}


def is_market_open() -> bool:
    """检查美股当前是否在交易时段。"""
    et = _get_et_now()
    if et.weekday() >= 5:  # Sat=5, Sun=6
        return False
    if et.date() in _US_HOLIDAYS_2026:
        return False
    t = et.time()
    open_t = t.replace(hour=9, minute=30, second=0)
    close_t = t.replace(hour=16, minute=0, second=0)
    return open_t <= t <= close_t


def market_closed_today() -> bool:
    """今天美股是否休市（非交易日）。"""
    et = _get_et_now()
    if et.weekday() >= 5:
        return True
    if et.date() in _US_HOLIDAYS_2026:
        return True
    return False


def minutes_after_market_close() -> int:
    """收盘后已过多少分钟。未收盘返回 -1。"""
    et = _get_et_now()
    close = et.replace(hour=16, minute=0, second=0, microsecond=0)
    delta = (et - close).total_seconds()
    if delta < 0:
        return -1
    return int(delta // 60)


# ── Market Data ─────────────────────────────────────────

_price_cache: dict = {}
_PREV_CLOSE_CACHE: dict = {}
_price_date: str = ""  # 缓存数据的最新交易日期


def refresh_prices(symbols: list):
    """从 Tushare Pro 批量获取美股最新日线行情（支持多代码逗号拼接）。"""
    global _price_cache, _price_date
    if not symbols:
        return

    try:
        end_date = datetime.now().strftime("%Y%m%d")
        start_date = (datetime.now() - timedelta(days=10)).strftime("%Y%m%d")
        codes_str = ",".join(symbols[:50])
        df = pro.us_daily(ts_code=codes_str, start_date=start_date, end_date=end_date,
                          fields="ts_code,trade_date,close,pre_close,vol")

        if df is not None and len(df) > 0:
            # 从原始df取最新交易日期（不在循环中覆盖）
            all_dates = df["trade_date"].dropna()
            if len(all_dates) > 0:
                _price_date = str(all_dates.max())
            latest = df.sort_values("trade_date").groupby("ts_code").last()
            log.info("日线收盘价日期: %s (%d只标的)", _price_date, len(latest))
            for ts_code, row in latest.iterrows():
                sym = ts_code.replace(".US", "").upper()
                close = float(row["close"])
                pre_close = float(row.get("pre_close", close))
                if close > 0:
                    _price_cache[sym] = close
                    _PREV_CLOSE_CACHE[sym] = pre_close
        # 记录持仓股收盘价
        held = load_state().get("positions", {})
        if held:
            closes = []
            for sym in held:
                if sym in _price_cache:
                    closes.append("{}={}".format(sym, _price_cache[sym]))
            if closes:
                log.info("持仓收盘价: %s", " ".join(closes))
    except Exception as e:
        log.warning("美股行情获取失败: %s", e)


def fetch_price(symbol: str) -> Optional[float]:
    """获取标的最新价格（优先腾讯实时价）。"""
    live = get_intraday_price(symbol)
    if live:
        return live
    return _price_cache.get(symbol.upper())


def fetch_prev_close(symbol: str) -> float:
    """获取前一交易日收盘价。"""
    return _PREV_CLOSE_CACHE.get(symbol.upper(), 0)


def is_price_data_today() -> bool:
    if not _price_date:
        return False
    today = datetime.now().strftime("%Y%m%d")
    if _price_date == today:
        return True
    # 美股日线数据收盘后发布，盘中允许3日内数据（覆盖周末）
    cutoff = (datetime.now() - timedelta(days=3)).strftime("%Y%m%d")
    return _price_date >= cutoff


def minutes_after_market_open() -> int:
    """开盘后已过多少分钟。未开盘或非交易时段返回 -1。"""
    et = _get_et_now()
    if et.weekday() >= 5 or et.date() in _US_HOLIDAYS_2026:
        return -1
    mkt_open = et.replace(hour=9, minute=30, second=0, microsecond=0)
    delta = (et - mkt_open).total_seconds()
    return int(delta // 60) if delta >= 0 else -1


_universe_cache: list = []
_universe_date: str = ""


def get_universe() -> list:
    """获取候选池（Tushare 指数成分股 + 静态文件兜底，不依赖外网）。"""
    global _universe_cache, _universe_date
    today = datetime.now().strftime("%Y-%m-%d")
    if _universe_cache and _universe_date == today:
        return _universe_cache

    tickers = []

    # 1. 主渠道：静态 JSON 文件（国内 VPS 不依赖外网）
    if not tickers:
        try:
            import json
            static_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sp500_static.json")
            with open(static_file, 'r') as fh:
                tickers = json.load(fh)
            log.info("候选池(静态文件): %s 只标的", len(tickers))
        except Exception as e:
            log.warning("静态成分股文件加载失败: %s", e)

    # 3. 最后兜底：内存缓存
    if not tickers and _universe_cache:
        tickers = _universe_cache
        log.warning("使用内存缓存: %s 只标的", len(tickers))

    _universe_cache = tickers
    _universe_date = today
    return tickers


def get_candidates_for_llm(state: dict) -> list:
    """给 LLM 提供原始候选标的数据（不做打分，LLM 自主判断）。"""
    positions = state.get("positions", {})
    candidates = []
    for sym, price in _price_cache.items():
        if sym in positions:
            continue
        if price is None or price <= 0:
            continue
        if price > MAX_POSITION_VALUE * 2:
            continue
        candidates.append({
            "symbol": sym,
            "price": round(price, 2),
        })
    # 取前 30 只送给 LLM（按价格排序方便 LLM 阅读）
    candidates.sort(key=lambda x: x["price"])
    return candidates[:30]


# ── LLM System Prompt ────────────────────────────────────

LLM_SYSTEM_PROMPT = """你是美股AI交易员，管理一个自动化交易账户。你的交易市场是美国股票（S&P 500成分股）。

## 交易规则
- T+0（当天买入当天可卖出），无涨跌停限制
- 交易时段：美东 9:30-16:00（北京时间 21:30-次日 04:00）
- 单只股票最大持仓金额 $MAX_POS_VALUE，最大持仓数量 MAX_POS_COUNT 只

## 交易原则（重要）
- 你是长期稳健型投资者，不是日内交易员。每次买卖产生约0佣金成本。
- 频繁换手（一天内买卖同一只股票）会严重侵蚀收益，应尽量避免。
- 仅在以下情况考虑交易：
  1. 止盈：单只盈利超过8%
  2. 止损：单只亏损超过5%（或基本面恶化）
  3. 调仓：行业过度集中需要分散，且新旧标的有明显优劣差异
- 1-3%的小幅波动属于正常市场噪音，不应触发交易。
- 建仓完成后，优先持有观察，让利润奔跑。

## 决策框架
你需要根据当前持仓状态和候选标的数据，做出以下决策：
1. 是否卖出已持有的标的（止盈、止损、或调仓）
2. 是否买入新的候选标的
3. 哪些标的继续持有

## 分析维度
- 持仓标的：看浮动盈亏、仓位占比、是否需要止盈/止损
- 候选标的：看价格、股票知名度、行业分散度
- 风险控制：避免过度集中，单票仓位不宜过重
- 成本意识：每次交易考虑佣金成本，无充分理由不交易

## 输出格式（严格JSON）
{
  "reasoning": "简要分析当前持仓和市场逻辑（中文，100字以内）",
  "sells": [{"symbol": "AAPL", "reason": "达到预期收益，获利了结"}],
  "buys": [{"symbol": "GOOGL", "reason": "看好AI赛道前景", "quantity_percent": 80}],
  "hold": ["MSFT", "NVDA"]
}

注意：
- quantity_percent 范围 10-100，表示使用单只上限金额的百分比（100=满仓买入单只上限）
- 不要重复买入已持有的标的
- 不要卖出未持有的标的
- sells 和 buys 可以为空数组
- 如果当前持仓合理，可以只输出 hold
- 优先考虑知名蓝筹股，避免投机性标的"""

# ── Trade Execution (Local Simulation) ──────────────────

def record_trade(action: str, symbol: str, quantity: int, price: float, reason: str):
    """记录当日交易到 state。"""
    state = load_state()
    trade = {
        "time": datetime.now().strftime("%H:%M:%S"),
        "action": action,
        "symbol": symbol,
        "quantity": quantity,
        "price": round(price, 2),
        "reason": reason,
    }
    state.setdefault("daily_trades", []).append(trade)
    save_state(state)


def execute_buy(sym: str) -> bool:
    state = load_state()
    price = fetch_price(sym)
    if not price or price <= 0:
        return False
    max_shares = int(MAX_POSITION_VALUE / price)
    if max_shares < 1:
        log.warning('资金不足以买入 %s 1股', sym)
        return False
    cost = max_shares * price
    fee = max(cost * 0.001, 1.0)  # 佣金 0.1%, 最低
    total = cost + fee
    if state['cash'] < total:
        max_shares = int((state['cash'] - 1) / (price * 1.001))
        if max_shares < 1:
            log.warning('现金不足买入 %s', sym)
            return False
        cost = max_shares * price
        fee = max(cost * 0.001, 1.0)
        total = cost + fee
    state['cash'] -= total
    state.setdefault('positions', {})[sym] = {
        'quantity': max_shares,
        'entry_price': price,
    }
    record_trade('buy', sym, max_shares, price, 'signal')
    save_state(state)
    log.info('买入 %s x%s @ $%.2f 佣金$%.2f 余额$%.2f',
             sym, max_shares, price, fee, state['cash'])
    return True


def execute_sell(sym: str, reason: str = 'signal') -> bool:
    state = load_state()
    pos = state.get('positions', {}).get(sym)
    if not pos:
        log.warning('无 %s 持仓', sym)
        return False
    qty = pos['quantity']
    price = fetch_price(sym) or pos['entry_price']
    proceeds = qty * price
    fee = max(proceeds * 0.001, 1.0)  # 佣金 0.1%, 最低
    net = proceeds - fee
    state['cash'] += net
    del state['positions'][sym]
    record_trade('sell', sym, qty, price, reason)
    save_state(state)
    reason_cn = {'stop_loss': '止损', 'take_profit': '止盈', 'force_sell': '清仓'}.get(reason, '信号')
    log.info('卖出 %s x%s @ $%.2f 净收入$%.2f [%s]',
             sym, qty, price, net, reason_cn)
    return True


def _build_llm_context(state: dict) -> dict:
    """组装 LLM 输入上下文：持仓 + 候选标的 + 约束。"""
    positions = state.get("positions", {})
    cash = state.get("cash", 0)

    pos_list = []
    for sym, p in positions.items():
        cur_price = fetch_price(sym) or p.get("entry_price", 0)
        entry = p.get("entry_price", 0)
        qty = p.get("quantity", 0)
        pnl_pct = (cur_price - entry) / entry if entry else 0
        pos_list.append({
            "symbol": sym,
            "quantity": qty,
            "entry_price": round(entry, 2),
            "current_price": round(cur_price, 2),
            "pnl_pct": round(pnl_pct, 4),
            "market_value": round(cur_price * qty, 2),
        })

    candidates = get_candidates_for_llm(state)

    total_market_value = sum(p["market_value"] for p in pos_list)
    total_equity = cash + total_market_value

    return {
        "market": "美股 S&P 500",
        "session": "open" if is_market_open() else "closed",
        "portfolio": {
            "total_equity": round(total_equity, 2),
            "cash": round(cash, 2),
            "positions_value": round(total_market_value, 2),
            "positions": pos_list,
        },
        "candidates": candidates,
        "constraints": {
            "max_positions": MAX_POSITIONS,
            "max_per_position": MAX_POSITION_VALUE,
            "market_type": "US",
        },
    }


def _ensure_daily_state(state: dict):
    """跨日重置每日状态，记录开盘资产。"""
    today_str = datetime.now().strftime("%Y-%m-%d")
    if state.get("report_sent_date") != today_str:
        state["daily_trades"] = []
        state["stop_loss_hits"] = []
        state["take_profit_hits"] = []
    # 记录开盘资产（现金 + 持仓市值）
    if not state.get("daily_start_equity") or state.get("_start_date") != today_str:
        positions_value = sum(
            (fetch_price(sym) or p.get("entry_price", 0)) * p.get("quantity", 0)
            for sym, p in state.get("positions", {}).items()
        )
        state["daily_start_equity"] = state.get("cash", INITIAL_CAPITAL) + positions_value
        state["_start_date"] = today_str


def run_loop():
    """主循环：行情 → LLM 决策 → 执行交易 → 日报。"""
    log.info("══ US Stock Agent 启动 ══")
    log.info("初始资金: $%s | 候选池: %s", INITIAL_CAPITAL, UNIVERSE_INDEX)
    log.info("单笔上限 $%s | 最大持仓 %s 只 | T+0",
             MAX_POSITION_VALUE, MAX_POSITIONS)
    log.info("决策引擎: DeepSeek-v4-pro LLM 自主决策")
    log.info("数据源: Tushare Pro | 本地模拟撮合")
    log.info("交易时段: 美东 9:30-16:00")

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
            # 1. 同步本地状态
            state = load_state()
            _ensure_daily_state(state)
            save_state(state)

            # 3. 批量获取行情（交易时段和非交易时段都需要，用于展示）
            holdings = list(state.get("positions", {}).keys())
            universe = get_universe()
            batch = list(dict.fromkeys(holdings + universe[:50]))  # 持仓优先
            refresh_prices(batch)
            refresh_intraday(batch)

            if is_market_open():
                # 检查是否应该执行交易决策
                min_after_open = minutes_after_market_open()
                data_fresh = is_price_data_today()

                # 开盘冷却期：前 5 分钟不交易（等待行情更新 + 市场情绪稳定）
                if min_after_open < 5:
                    log.info("⏳ 开盘冷却期（%d/5 分钟），等待行情稳定...", min_after_open)

                elif not data_fresh:
                    log.info("⏳ 行情数据日期=%s，非今日数据，等待 Tushare 更新...", _price_date)

                else:
                    # LLM 自主决策（数据新鲜 + 冷却已过）
                    user_msg = _build_llm_context(state)
                    # 每周期记录持仓浮动盈亏（买入价 vs 当前价）
                    user_msg["trading_context"] = {
                        "minutes_after_open": min_after_open,
                        "data_date": _price_date,
                        "note": "数据为当日实时行情，可以放心交易"
                    }
                    decisions = deepseek_ask(LLM_SYSTEM_PROMPT, user_msg)

                    if decisions:
                        log.info("LLM 决策: %s", decisions.get("reasoning", "")[:200])
                        # 执行卖出
                        for item in decisions.get("sells", []):
                            sym = str(item.get("symbol", "")).upper()
                            reason = item.get("reason", "LLM signal")
                            if sym in state.get("positions", {}):
                                execute_sell(sym, reason)
                                time.sleep(1)

                        # 重新加载状态（卖出可能改变了持仓）
                        state = load_state()

                        # 执行买入（带护栏）
                        current_count = len(state.get("positions", {}))
                        for item in decisions.get("buys", []):
                            sym = str(item.get("symbol", "")).upper()
                            reason = item.get("reason", "LLM signal")
                            qty_pct = max(10, min(100, int(item.get("quantity_percent", 100))))

                            # 护栏校验
                            if sym in state.get("positions", {}):
                                log.warning("护栏拦截: %s 已持仓", sym)
                                continue
                            if current_count >= MAX_POSITIONS:
                                log.warning("护栏拦截: 已达最大持仓数 %s", MAX_POSITIONS)
                                break
                            price = fetch_price(sym)
                            if not price or price <= 0:
                                log.warning("护栏拦截: %s 无有效价格", sym)
                                continue
                            if price > MAX_POSITION_VALUE:
                                log.warning("护栏拦截: %s 价格 $%.2f 超单只上限", sym, price)
                                continue

                            # 根据 quantity_percent 调整买入数量
                            adjusted_max = MAX_POSITION_VALUE * qty_pct / 100
                            quantity = max(1, int(adjusted_max / max(price, 1)))

                            # 临时覆盖 execute_buy 的数量计算
                            _orig_max = MAX_POSITION_VALUE
                            globals()["MAX_POSITION_VALUE"] = adjusted_max
                            ok = execute_buy(sym)
                            globals()["MAX_POSITION_VALUE"] = _orig_max

                            if ok:
                                current_count += 1
                                time.sleep(2)

                        state = load_state()
                        # 记录 LLM reasoning 到 state 供日报使用
                        state["_llm_reasoning"] = decisions.get("reasoning", "")
                        save_state(state)
                    else:
                        log.warning("LLM 调用失败，跳过本周期交易决策")

                state = load_state()
            else:
                log.info("⏸ 非交易时段，跳过买卖操作")

            # 7. 检查是否该发日报（收盘后 30-60 分钟之间，每天只发一次）
            min_after = minutes_after_market_close()
            today_str = datetime.now().strftime("%Y-%m-%d")
            if 30 <= min_after < 90 and state.get("report_sent_date") != today_str:
                if not market_closed_today():
                    log.info("📧 收盘后 %d 分钟，准备发送投资日报...", min_after)
                    try:
                        report = generate_report(state)
                        send_report_email(report)
                        state["report_sent_date"] = today_str
                        save_state(state)
                        log.info("📧 日报已发送至 %s", REPORT_EMAIL)
                    except Exception as e:
                        log.warning("日报发送失败: %s", e)

            # 7b. 日报已发送 + 非交易时段 → 退出，由调度器下次启动
            if state.get("report_sent_date") == today_str and not is_market_open():
                log.info("日报已发送，今日任务完成，退出进程")
                save_state(state)
                break

            # 7c. 非交易日空闲检测：连续 3 个周期无操作 → 退出
            if not is_market_open() and not market_closed_today():
                idle = state.get("_idle_cycles", 0) + 1
                state["_idle_cycles"] = idle
                if idle >= 3:
                    log.info("非交易日，退出进程")
                    save_state(state)
                    break
            else:
                state["_idle_cycles"] = 0

            # 8. 持久化状态
            save_state(state)

        except Exception as e:
            log.exception("周期异常: %s", e)

        elapsed = time.time() - t0
        sleep_time = max(5, LOOP_INTERVAL - elapsed)
        log.info("周期完成 (%.1fs), 休眠 %ds", elapsed, int(sleep_time))
        time.sleep(sleep_time)

    log.info("Agent 已安全退出")


# ── Daily Report ────────────────────────────────────────


def generate_report(state: dict) -> str:
    """生成当日投资日报 HTML。"""
    today_str = datetime.now().strftime("%Y-%m-%d")
    trades = state.get("daily_trades", [])
    positions = state.get("positions", {})
    cash = state.get("cash", 0)
    start_equity = state.get("daily_start_equity", 100000)

    # 持仓市值
    position_value = sum(
        fetch_price(s) * p.get("quantity", 0)
        for s, p in positions.items()
        if fetch_price(s)
    ) or 0
    total = cash + position_value
    daily_pnl = total - start_equity

    # 成交表格
    trade_rows = ""
    if trades:
        for t in trades:
            action_emoji = "买入" if t["action"] == "buy" else "卖出"
            reason_map = {
                "signal": "策略信号", "stop_loss": "止损", "take_profit": "止盈",
                "force_sell": "清仓"
            }
            reason_cn = reason_map.get(t.get("reason", ""), t.get("reason", ""))
            trade_rows += f"""<tr>
                <td>{t['time']}</td><td>{action_emoji}</td><td>{t['symbol']}</td>
                <td>{t['quantity']}</td><td>${t['price']:.2f}</td><td>{reason_cn}</td>
            </tr>"""
    else:
        trade_rows = '<tr><td colspan="6" style="text-align:center;color:#888">今日无成交</td></tr>'

    # 持仓表格
    pos_rows = ""
    if positions:
        for s, p in positions.items():
            price = fetch_price(s) or p.get("entry_price", 0)
            qty = p.get("quantity", 0)
            entry = p.get("entry_price", 0)
            mv = price * qty
            pnl = (price - entry) * qty
            pnl_pct = (price - entry) / entry * 100 if entry else 0
            color = "#e74c3c" if pnl < 0 else "#27ae60"
            pos_rows += f"""<tr>
                <td>{s}</td><td>{qty}</td><td>${entry:.2f}</td><td>${price:.2f}</td>
                <td>${mv:,.0f}</td><td style="color:{color}">${pnl:,.0f} ({pnl_pct:+.1f}%)</td>
            </tr>"""
    else:
        pos_rows = '<tr><td colspan="6" style="text-align:center;color:#888">空仓</td></tr>'

    # LLM 决策推理
    llm_reasoning = state.get("_llm_reasoning", "")

    # 投资思路复盘
    summary = _generate_summary(trades, positions, daily_pnl, llm_reasoning)

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
    body {{ font-family: -apple-system, 'Microsoft YaHei', sans-serif; background: #f5f6fa;
           padding: 20px; color: #2c3e50; }}
    .container {{ max-width: 680px; margin: 0 auto; background: #fff; border-radius: 12px;
                  padding: 32px; box-shadow: 0 2px 12px rgba(0,0,0,.08); }}
    h1 {{ font-size: 22px; border-bottom: 2px solid #3498db; padding-bottom: 12px; }}
    h2 {{ font-size: 16px; color: #3498db; margin-top: 28px; }}
    table {{ width: 100%; border-collapse: collapse; margin: 12px 0; font-size: 14px; }}
    th {{ background: #f0f3f8; padding: 10px 8px; text-align: left; }}
    td {{ padding: 8px; border-bottom: 1px solid #eee; }}
    .metrics {{ display: flex; gap: 16px; flex-wrap: wrap; }}
    .metric {{ flex: 1; min-width: 140px; background: #f0f3f8; border-radius: 8px;
               padding: 16px; text-align: center; }}
    .metric .label {{ font-size: 12px; color: #7f8c8d; }}
    .metric .value {{ font-size: 22px; font-weight: 600; margin-top: 4px; }}
    .summary {{ background: #fef9e7; border-left: 4px solid #f39c12; padding: 14px 18px;
                margin-top: 24px; font-size: 14px; line-height: 1.7; border-radius: 4px; }}
    .footer {{ text-align: center; color: #aaa; font-size: 12px; margin-top: 24px; }}
</style></head><body>
<div class="container">
<h1>AI-Trader 投资日报 {today_str}</h1>

<h2>资产概览</h2>
<div class="metrics">
    <div class="metric"><div class="label">总资产</div><div class="value">${total:,.0f}</div></div>
    <div class="metric"><div class="label">现金</div><div class="value">${cash:,.0f}</div></div>
    <div class="metric"><div class="label">持仓市值</div><div class="value">${position_value:,.0f}</div></div>
    <div class="metric"><div class="label">当日盈亏</div>
        <div class="value" style="color:{'#e74c3c' if daily_pnl < 0 else '#27ae60'}">{daily_pnl:+,.0f}</div></div>
</div>

<h2>当日成交</h2>
<table><thead><tr><th>时间</th><th>方向</th><th>标的</th><th>数量</th><th>价格</th><th>原因</th></tr></thead>
<tbody>{trade_rows}</tbody></table>

<h2>当前持仓</h2>
<table><thead><tr><th>标的</th><th>数量</th><th>买入价</th><th>现价</th><th>市值</th><th>浮动盈亏</th></tr></thead>
<tbody>{pos_rows}</tbody></table>

<h2>AI 决策思路</h2>
<div class="summary"><strong>LLM 复盘</strong><br>{summary}</div>

<div class="footer">AI-Trader Agent · DeepSeek-v4-pro 驱动 · 此邮件由自动化系统发送</div>
</div></body></html>"""


def _generate_summary(trades, positions, daily_pnl, llm_reasoning: str) -> str:
    """基于 LLM 推理 + 当日数据生成复盘。"""
    parts = []
    buy_count = sum(1 for t in trades if t["action"] == "buy")
    sell_count = sum(1 for t in trades if t["action"] == "sell")

    if buy_count + sell_count == 0:
        parts.append("今日无交易操作，持仓维持不变。")
    else:
        parts.append(f"今日共完成 {buy_count} 笔买入、{sell_count} 笔卖出。")
        if buy_count > 0:
            bought = [t["symbol"] for t in trades if t["action"] == "buy"]
            parts.append(f"新开仓标的：{'、'.join(bought)}。")
        if sell_count > 0:
            sold = [t["symbol"] for t in trades if t["action"] == "sell"]
            parts.append(f"平仓标的：{'、'.join(sold)}。")

    if daily_pnl > 0:
        parts.append(f"当日整体录得正收益 +${daily_pnl:,.0f}。")
    elif daily_pnl < 0:
        parts.append(f"当日录得亏损 ${daily_pnl:,.0f}。")
    else:
        parts.append("当日整体持平。")

    # LLM 推理
    if llm_reasoning:
        parts.append(f"AI 决策逻辑：{llm_reasoning}")

    if positions:
        parts.append(f"当前持有 {len(positions)} 个标的，由 DeepSeek-v4-pro 持续监控和决策。")
    else:
        parts.append("当前空仓，等待 DeepSeek LLM 判断下一个交易机会。")

    return "".join(parts)


def send_report_email(html_content: str):
    """通过 QQ 邮箱 SMTP 发送日报。"""
    if not SMTP_USER or not SMTP_PASS:
        log.warning("SMTP 未配置，跳过邮件发送")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"AI-Trader 投资日报 {datetime.now().strftime('%Y-%m-%d')}"
    msg["From"] = SMTP_USER
    msg["To"] = REPORT_EMAIL
    msg.attach(MIMEText(html_content, "html", "utf-8"))

    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=15) as server:
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(SMTP_USER, [REPORT_EMAIL], msg.as_string())


# ── Entry Point ─────────────────────────────────────────

def main():
    log.info("正在启动美股 Agent ...")
    log.info("Agent: %s | 初始资金: $%s", AGENT_NAME, INITIAL_CAPITAL)
    log.info("数据源: Tushare Pro | 交易撮合: 本地模拟")
    log.info("AI 引擎: DeepSeek-v4-pro")
    run_loop()


if __name__ == "__main__":
    main()
