"""
债券 AI-Trader Agent — 中国可转债模拟交易脚本
数据源：Tushare Pro（cb_basic + cb_daily），本地模拟撮合。
双策略并行：
  - 60% 中长期持有：优选高评级、正收益率的可转债，买入后长期持有
  - 40% 灵活交易：高流动性可转债波段操作，T+0 随时买卖
"""
import json
import logging
import os
import signal
import smtplib
import sys
import time
from datetime import datetime, timedelta, date
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path
from typing import Optional

import tushare as ts
from dotenv import load_dotenv

# DeepSeek LLM 共享模块
sys.path.insert(0, "/opt/ai-trader-common")
from llm_client import deepseek_ask

# ── Config ──────────────────────────────────────────────
load_dotenv(Path(__file__).parent / ".env")

AGENT_NAME = os.getenv("AGENT_NAME", "BondAgent")
INITIAL_CAPITAL = float(os.getenv("INITIAL_CAPITAL", "200000"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOOP_INTERVAL = int(os.getenv("LOOP_INTERVAL", "60"))

TUSHARE_TOKEN = os.getenv("TUSHARE_TOKEN", "")
ts.set_token(TUSHARE_TOKEN)
pro = ts.pro_api()
_BOND_NAMES: dict = {}

# 预算约束（护栏用，非策略规则）
LONG_TERM_BUDGET = 120000     # 中长期策略资金上限
ACTIVE_BUDGET = 80000         # 灵活交易资金上限
MAX_LONG_TERM_PER_BOND = 40000
MAX_LONG_TERM_POSITIONS = 4
MAX_ACTIVE_PER_BOND = 25000
MAX_ACTIVE_POSITIONS = 3

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


# ── State ───────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {
        "positions": {},
        "cash": INITIAL_CAPITAL,
        "repos": [],               # 国债逆回购持仓
        "repo_interest_earned": 0.0,
        "daily_trades": [],
        "daily_start_equity": INITIAL_CAPITAL,
        "report_sent_date": "",
        "stop_loss_hits": [],
        "take_profit_hits": [],
        "long_term_cost": 0.0,
        "active_cost": 0.0,
    }


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str))


# ── Market Clock ────────────────────────────────────────

def is_trading_day() -> bool:
    today_str = datetime.now().strftime("%Y%m%d")
    try:
        cal = pro.trade_cal(exchange="SSE", start_date=today_str,
                            end_date=today_str, is_open="1")
        return len(cal) > 0
    except Exception:
        return datetime.now().weekday() < 5


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


# ── Reverse Repo（国债逆回购） ──────────────────────────


def mature_repos(state: dict):
    """到期自动赎回国债逆回购。"""
    today = datetime.now().strftime("%Y-%m-%d")
    matured = 0
    for repo in list(state.get("repos", [])):
        if repo["mature_date"] <= today:
            principal = repo["amount"]
            interest = repo["amount"] * repo["rate"] / 100 / 365
            state["cash"] += principal + interest
            state["repo_interest_earned"] = state.get("repo_interest_earned", 0) + interest
            matured += 1
    if matured:
        state["repos"] = [r for r in state["repos"] if r["mature_date"] > today]
        log.info("逆回购到期赎回 %s 笔", matured)


def buy_reverse_repo(state: dict, amount: float) -> bool:
    """买入 1 天期国债逆回购 R-001(sz131810)，门槛 ￥1,000。"""
    if amount < 1000:
        return False

    rate = fetch_price("sz131810")  # 返回年化利率
    if not rate or rate <= 0:
        return False

    # R-001: 10 张/手 × ￥100/张 = ￥1,000/手
    lots = int(amount / 1000) * 10  # 10张为单位
    if lots < 10:
        return False
    cost = lots * 100

    if state["cash"] < cost:
        lots = int(state["cash"] / 1000) * 10
        if lots < 10:
            return False
        cost = lots * 100

    commission = max(cost * 0.00001, 1)  # 十万分之一，最低 1 分
    total = cost + commission

    if state["cash"] < total:
        return False

    state["cash"] -= total
    today = datetime.now().strftime("%Y-%m-%d")
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    interest = cost * rate / 100 / 365

    state.setdefault("repos", []).append({
        "amount": cost,
        "rate": rate,
        "buy_date": today,
        "mature_date": tomorrow,
    })

    log.info("逆回购 R-001: ￥%.0f @ %.2f%% 预估利息￥%.2f 佣金￥%.2f",
             cost, rate, interest, commission)
    return True


# ── Market Data ─────────────────────────────────────────

_price_cache: dict = {}     # raw_code → {price, prev_close, open, volume, ...}
_price_date: str = ""
_bond_analytics: dict = {}  # raw_code → {ytm, pure_bond_value, conv_premium, rating, remain_years, ...}
_universe_cache: list = []
_universe_date: str = ""
_last_trade_date: str = ""


def _bond_code_to_ts(code: str) -> str:
    code = str(code).strip()
    if code.startswith("sh"):
        return f"{code[2:]}.SH"
    if code.startswith("sz"):
        return f"{code[2:]}.SZ"
    if code.startswith("11"):
        return f"{code}.SH"
    if code.startswith("12"):
        return f"{code}.SZ"
    if code.startswith(("0", "2", "3")):
        return f"{code}.SZ"
    return f"{code}.SH"


def _ts_to_bond_code(ts_code: str) -> str:
    return ts_code.split(".")[0]

    r = rating.strip().upper()
    for i, level in enumerate(["AAA", "AA+", "AA", "AA-", "A+", "A", "A-"]):
        if level in r:
            return 7 - i
    return 0


def _get_latest_trade_date() -> str:
    global _last_trade_date
    if _last_trade_date:
        return _last_trade_date
    cal = pro.trade_cal(exchange="SSE", start_date="20260101",
                        end_date=datetime.now().strftime("%Y%m%d"), is_open="1")
    if len(cal) > 0:
        _last_trade_date = str(cal.iloc[-1]["cal_date"])
        return _last_trade_date
    return datetime.now().strftime("%Y%m%d")


def get_universe() -> list:
    global _universe_cache, _bond_analytics, _universe_date
    today = datetime.now().strftime("%Y-%m-%d")
    if _universe_cache and _universe_date == today:
        return _universe_cache

    bonds = []

    try:
        today_str = datetime.now().strftime("%Y%m%d")
        df = pro.cb_basic(
            fields="ts_code,bond_short_name,stk_code,list_date,conv_price,rate,maturity_date,newest_rating,cb_type"
        )
        for _, row in df.iterrows():
            ts_code = row["ts_code"]
            code = _ts_to_bond_code(ts_code)
            name = str(row.get("bond_short_name", "") or "")
            # 仅保留未到期的可转债（排除可交换债 EB）
            cb_type = str(row.get("cb_type", "") or "")
            if cb_type == "EB":
                continue
            maturity_str = str(row.get("maturity_date", "") or "")
            if not maturity_str or maturity_str in ("nan", "None", ""):
                continue
            if maturity_str < today_str:
                continue  # 已到期，跳过

            conv_price = float(row.get("conv_price", 0) or 0)
            rate_raw = float(row.get("rate", 0) or 0)
            rate = rate_raw / 100.0 if rate_raw > 1 else rate_raw
            maturity = datetime.strptime(maturity_str[:8], "%Y%m%d")
            remain_years = max(0.0, (maturity - datetime.now()).days / 365.25)
            rating = str(row.get("newest_rating", "") or "")

            _bond_analytics[code] = {
                "name": name,
                "ytm": 0.0,
                "pure_bond_value": 0.0,
                "conv_premium": 0.0,
                "rating": rating,
                "remain_years": remain_years,
                "turnover": 0.0,
                "conv_price": conv_price,
                "rate": rate,
            }
            _BOND_NAMES[code] = name
            bonds.append(code)
    except Exception as e:
        log.warning("Tushare 可转债列表获取失败: %s", e)

    if bonds:
        _universe_cache = bonds
        _universe_date = today
        log.info("可转债候选池已刷新: %s 只标的", len(bonds))
    elif not _universe_cache:
        log.info("使用内置可转债精选列表")
        fallback = [
            "113013", "113016", "113021", "113024", "113030", "113033",
            "113037", "113039", "113042", "113043", "113044", "113045",
            "113046", "113048", "113049", "113050", "113051", "113052",
            "113053", "113055", "113056", "113058", "113059", "113060",
            "113062", "113063", "113064", "113065", "113066", "113068",
            "113516", "113519", "113521", "113524", "113525", "113527",
            "113530", "113532", "113534", "113537", "113541", "113545",
            "113549", "113550", "113551", "113552", "113553", "113554",
            "118001", "118003", "118004", "118005", "118006", "118007",
            "118008", "118009", "118010", "118011", "118012", "118013",
            "118014", "118015", "118016", "118017", "118018", "118019",
            "118020", "118021", "118022", "118023", "118024", "118025",
            "118026", "118027", "118028", "118029", "118030", "118031",
            "118032", "118033", "118034", "118035", "118036", "118037",
            "118038", "118039", "118040", "118041", "118042", "118043",
            "118044", "118045",
            "123107", "123108", "123109", "123110", "123111", "123112",
            "123113", "123114", "123115", "123116", "123117", "123118",
            "127001", "127002", "127003", "127004", "127005", "127006",
            "127007", "127008", "127009", "127010", "127011", "127012",
            "127013", "127014", "127015", "127016", "127017", "127018",
            "127020", "127022", "127023", "127024", "127026", "127029",
            "127030", "127032", "127033", "127035", "127037", "127039",
            "127042", "127045", "127048", "127051", "127054", "127057",
            "127060", "127063", "127066", "127069", "127072", "127075",
            "127078", "127081", "127084", "127087", "127089", "127092",
            "128001", "128002", "128003", "128004", "128005", "128006",
            "128007", "128008", "128009", "128010", "128011", "128012",
            "128013", "128014", "128015", "128017", "128019", "128020",
            "128021", "128023", "128025", "128026", "128029", "128033",
            "128036", "128039", "128042", "128045", "128048", "128051",
            "128053", "128056", "128058", "128063", "128066", "128069",
            "128072", "128075", "128078", "128081", "128084", "128087",
            "128090", "128093", "128095", "128097", "128101", "128106",
            "128109", "128111", "128116", "128119", "128123",
        ]
        _universe_cache = fallback
        _universe_date = today
        return fallback
    else:
        log.info("使用缓存的 %s 只可转债", len(_universe_cache))

    return _universe_cache


def refresh_prices(symbols: list):
    global _price_cache, _price_date
    if not symbols:
        return
    ts_codes = [_bond_code_to_ts(s) for s in symbols]
    end_date = datetime.now().strftime("%Y%m%d")
    start_date = (datetime.now() - timedelta(days=5)).strftime("%Y%m%d")
    for ts_code in ts_codes:
        code = _ts_to_bond_code(ts_code)
        try:
            df = pro.cb_daily(ts_code=ts_code, start_date=start_date, end_date=end_date,
                              fields="ts_code,trade_date,open,high,low,close,pre_close,vol,amount")
            if len(df) == 0:
                continue
            row = df.iloc[-1]  # 取最新一行
            trade_date = str(row.get("trade_date", ""))
            if trade_date and (not _price_date or trade_date > _price_date):
                _price_date = trade_date
            close = float(row["close"])
            pre_close = float(row["pre_close"])
            vol = float(row["vol"])
            amount = float(row.get("amount", 0) or 0)

            _price_cache[code] = {
                "code": code,
                "name": _BOND_NAMES.get(code, code),
                "price": close,
                "prev_close": pre_close,
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "volume": vol,
            }

            ana = _bond_analytics.get(code, {})
            ana["turnover"] = amount
            rate = ana.get("rate", 0)
            remain = ana.get("remain_years", 0)
            if rate > 0 and remain > 0 and close > 0:
                annual_coupon = 100 * rate
                ytm = (annual_coupon + (100 - close) / remain) / ((100 + close) / 2)
                ana["ytm"] = ytm
            if code not in _bond_analytics:
                _bond_analytics[code] = {
                    "name": _BOND_NAMES.get(code, code),
                    "ytm": 0.0, "pure_bond_value": 0.0, "conv_premium": 0.0,
                    "rating": "", "remain_years": 0.0, "turnover": amount,
                }
        except Exception as e:
            log.debug("行情获取失败 %s: %s", ts_code, e)


def fetch_price(code: str) -> Optional[float]:
    info = _price_cache.get(code)
    return info["price"] if info else None


def get_bond_name(code: str) -> str:
    info = _price_cache.get(code)
    if info and info.get("name"):
        return info["name"]
    meta = _bond_analytics.get(code, {})
    return meta.get("name", code)


def get_analytics(code: str) -> dict:
    return _bond_analytics.get(code, {})


# ── LLM System Prompt ────────────────────────────────────

LLM_SYSTEM_PROMPT = """你是可转债AI交易员，管理一个双策略可转债交易账户。

## 双策略并行
1. **中长期持有策略（~60%资金）**：优选高评级（AA-及以上）、正YTM、价格接近面值（100-118元）的可转债。买入后长期持有至强赎或到期，关注信用资质、纯债价值和收益率。最多4只，单只上限￥40,000。
2. **灵活交易策略（~40%资金）**：高流动性可转债波段操作，T+0随时买卖。关注成交额（>1000万）、价格动量、市场情绪。最多3只，单只上限￥25,000。

## 交易规则
- T+0（当天可买可卖），无涨跌停限制
- 佣金万二，免印花税，最小交易单位 10 张（1手）
- 交易时段：上午 9:30-11:30，下午 13:00-15:00
- 价格接近 130 元可能触发强赎，面值低于 100 可能存在信用风险
- 剩余年限 < 0.25 年（3个月）的转债应清仓

## 分析维度
- **中长期标的**：YTM（到期收益率）、评级（AAA/AA+/AA/AA-/A+）、纯债价值、剩余年限
- **灵活标的**：成交额（turnover）、涨跌幅、日内动量
- **风险控制**：评级下调→清仓；临近到期→清仓；强赎触发→清仓

## 输出格式（严格JSON）
{
  "reasoning": "简要分析逻辑（中文，150字以内）",
  "sells": [{"symbol": "113013", "reason": "评级下调至A+，信用风险上升", "strategy": "long_term"}],
  "buys": [{"symbol": "110059", "reason": "YTM 3.2% 评级AAA 纯债保护充足", "strategy": "long_term", "quantity_percent": 80}],
  "hold": ["128108"]
}

注意：
- 每个 buy/sell 必须标注 "strategy": "long_term" 或 "active"
- quantity_percent 范围 10-100，表示使用该类策略单只上限金额的百分比
- 中长期策略不应频繁交易，灵活策略可以日内买卖
- sells 和 buys 可以为空数组"""


def get_candidates_for_llm(state: dict) -> list:
    """给 LLM 提供可转债候选数据（含 YTM/评级等分析字段）。"""
    positions = state.get("positions", {})
    candidates = []
    for code, info in _price_cache.items():
        if code in positions:
            continue
        price = info.get("price", 0)
        if price is None or price <= 0:
            continue
        ana = get_analytics(code)
        chg_pct = (price - info["prev_close"]) / info["prev_close"] if info.get("prev_close") else 0
        candidates.append({
            "symbol": code,
            "name": get_bond_name(code),
            "price": round(price, 2),
            "prev_close": info.get("prev_close", 0),
            "change_pct": round(chg_pct * 100, 2),
            "volume": info.get("volume", 0),
            "ytm": round(ana.get("ytm", 0) * 100, 2),  # 百分比
            "rating": ana.get("rating", ""),
            "remain_years": round(ana.get("remain_years", 0), 2),
            "pure_bond_value": round(ana.get("pure_bond_value", 0), 2),
            "conv_premium": round(ana.get("conv_premium", 0) * 100, 2),
            "turnover": round(ana.get("turnover", 0), 0),
        })
    candidates.sort(key=lambda x: x["change_pct"], reverse=True)
    return candidates[:30]


def _build_llm_context(state: dict) -> dict:
    """组装 LLM 输入上下文。"""
    positions = state.get("positions", {})
    cash = state.get("cash", 0)

    pos_list = []
    for code, p in positions.items():
        cur_price = fetch_price(code) or p.get("entry_price", 0)
        entry = p.get("entry_price", 0)
        qty = p.get("quantity", 0)
        pnl_pct = (cur_price - entry) / entry if entry else 0
        ana = get_analytics(code)
        pos_list.append({
            "symbol": code,
            "name": get_bond_name(code),
            "strategy": p.get("strategy", "active"),
            "quantity": qty,
            "entry_price": round(entry, 2),
            "current_price": round(cur_price, 2),
            "pnl_pct": round(pnl_pct, 4),
            "market_value": round(cur_price * qty, 2),
            "ytm": round(ana.get("ytm", 0) * 100, 2),
            "rating": ana.get("rating", ""),
            "remain_years": round(ana.get("remain_years", 0), 2),
        })

    candidates = get_candidates_for_llm(state)
    total_market_value = sum(p["market_value"] for p in pos_list)
    lt_count = sum(1 for p in pos_list if p.get("strategy") == "long_term")
    ac_count = len(pos_list) - lt_count

    return {
        "market": "中国可转债",
        "session": trading_session(),
        "portfolio": {
            "total_equity": round(cash + total_market_value, 2),
            "cash": round(cash, 2),
            "positions_value": round(total_market_value, 2),
            "positions": pos_list,
            "long_term_count": lt_count,
            "active_count": ac_count,
        },
        "candidates": candidates,
        "constraints": {
            "long_term_budget": LONG_TERM_BUDGET,
            "active_budget": ACTIVE_BUDGET,
            "max_long_term_positions": MAX_LONG_TERM_POSITIONS,
            "max_active_positions": MAX_ACTIVE_POSITIONS,
            "max_per_bond_long_term": MAX_LONG_TERM_PER_BOND,
            "max_per_bond_active": MAX_ACTIVE_PER_BOND,
            "market_type": "Bond",
            "t0_rule": True,
        },
    }



# ── Trade Execution ─────────────────────────────────────

LOT_SIZE = 10  # 可转债 10 张/手


def execute_buy(code: str, strategy: str) -> bool:
    """买入可转债。strategy: 'long_term' 或 'active'。"""
    state = load_state()
    price = fetch_price(code)
    if not price:
        return False

    positions = state.get("positions", {})

    # 确定买入金额上限
    if strategy == "long_term":
        per_bond_max = MAX_LONG_TERM_PER_BOND
        max_pos = MAX_LONG_TERM_POSITIONS
    else:
        per_bond_max = MAX_ACTIVE_PER_BOND
        max_pos = MAX_ACTIVE_POSITIONS

    same_count = sum(1 for p in positions.values() if p.get("strategy") == strategy)
    if same_count >= max_pos and code not in positions:
        return False

    # 计算可买数量
    max_lots = int(per_bond_max / (price * LOT_SIZE))
    if max_lots < 1:
        max_lots = 1
    max_shares = max_lots * LOT_SIZE

    cost = max_shares * price
    fee = cost * 0.0002  # 可转债佣金万二（免印花税）
    total = cost + fee

    if state["cash"] < total:
        max_lots = int(state["cash"] / (price * LOT_SIZE * 1.0002))
        if max_lots < 1:
            return False
        max_shares = max_lots * LOT_SIZE
        cost = max_shares * price
        fee = cost * 0.0002
        total = cost + fee

    state["cash"] -= total

    # 更新持仓（支持分批买入同一债券）
    if code in positions:
        old_qty = positions[code]["quantity"]
        old_cost = positions[code]["entry_price"] * old_qty
        new_total_qty = old_qty + max_shares
        positions[code]["quantity"] = new_total_qty
        positions[code]["entry_price"] = (old_cost + cost) / new_total_qty
    else:
        positions[code] = {
            "quantity": max_shares,
            "entry_price": price,
            "strategy": strategy,
        }

    # 更新资金占用
    if strategy == "long_term":
        state["long_term_cost"] = state.get("long_term_cost", 0) + cost
    else:
        state["active_cost"] = state.get("active_cost", 0) + cost

    record_trade(state, "buy", code, max_shares, price, f"{strategy}_signal")
    save_state(state)
    log.info(
        "买入 %s(%s) x%s张 @ ￥%.3f [%s] 佣金￥%.2f",
        get_bond_name(code), code, max_shares, price, strategy, fee,
    )
    return True


def execute_sell(code: str, reason: str = "signal") -> bool:
    """卖出可转债（万二佣金，免印花税，T+0 支持）。"""
    state = load_state()
    pos = state.get("positions", {}).get(code)
    if not pos:
        log.warning("无 %s 持仓", code)
        return False

    qty = pos["quantity"]
    strategy = pos.get("strategy", "active")
    price = fetch_price(code) or pos["entry_price"]
    proceeds = qty * price
    fee = proceeds * 0.0002  # 万二佣金（可转债免印花税）
    net = proceeds - fee

    state["cash"] += net
    del state["positions"][code]

    # 更新资金占用
    cost_at_entry = pos["entry_price"] * qty
    if strategy == "long_term":
        state["long_term_cost"] = max(0, state.get("long_term_cost", 0) - cost_at_entry)
    else:
        state["active_cost"] = max(0, state.get("active_cost", 0) - cost_at_entry)

    record_trade(state, "sell", code, qty, price, reason)
    save_state(state)

    reason_cn = {
        "stop_loss": "止损", "take_profit": "止盈",
        "rating_downgrade": "评级下调", "near_maturity": "临近到期",
        "force_sell": "清仓",
    }.get(reason, "信号")
    log.info(
        "卖出 %s(%s) x%s张 @ ￥%.3f 净收入￥%.2f [%s]",
        get_bond_name(code), code, qty, price, net, reason_cn,
    )
    return True


def record_trade(state, action, symbol, quantity, price, reason):
    state.setdefault("daily_trades", []).append({
        "time": datetime.now().strftime("%H:%M:%S"),
        "action": action,
        "symbol": symbol,
        "quantity": quantity,
        "price": round(price, 3),
        "reason": reason,
    })


# ── Daily State ─────────────────────────────────────────

def ensure_daily_state(state: dict):
    today_str = datetime.now().strftime("%Y-%m-%d")
    if state.get("_date") != today_str:
        mature_repos(state)  # 到期逆回购自动赎回
        state["daily_trades"] = []
        state["stop_loss_hits"] = []
        state["take_profit_hits"] = []
        state["daily_start_equity"] = _calc_total_equity(state)
        state["_date"] = today_str


def _calc_total_equity(state: dict) -> float:
    total = state["cash"]
    for code, pos in state.get("positions", {}).items():
        price = fetch_price(code) or pos.get("entry_price", 0)
        total += price * pos["quantity"]
    for repo in state.get("repos", []):
        total += repo["amount"]
    return total


# ── Main Loop ───────────────────────────────────────────

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
    if not is_market_open():
        return -1
    t = datetime.now()
    mkt_open = t.replace(hour=9, minute=30, second=0, microsecond=0)
    delta = (t - mkt_open).total_seconds()
    return int(delta // 60) if delta >= 0 else -1


def run_loop():
    log.info("══ 债券 AI-Trader Agent 启动 ══")
    log.info("初始资金: ￥%s", INITIAL_CAPITAL)
    log.info("策略: 中长期持有 + 灵活交易（双策略 LLM 自主分配）")
    log.info("中长期: 单只上限￥%s | 最大%s只 | 关注YTM/评级/纯债价值",
             int(MAX_LONG_TERM_PER_BOND), MAX_LONG_TERM_POSITIONS)
    log.info("灵活: 单只上限￥%s | 最大%s只 | T+0",
             int(MAX_ACTIVE_PER_BOND), MAX_ACTIVE_POSITIONS)
    log.info("决策引擎: DeepSeek-v4-pro LLM 自主决策")
    log.info("品种: 可转债 | 佣金万二 | 免印花税 | 10张/手")

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

            # 获取行情（cb_daily 仅支持单券查询，限制批量大小）
            universe = get_universe()
            holdings = list(state.get("positions", {}).keys())
            all_symbols = list(set(holdings + universe[:30]))
            refresh_prices(all_symbols)

            if is_market_open():
                # 开盘冷却期 + 数据新鲜度
                min_after_open = minutes_after_market_open()
                data_fresh = is_price_data_today()
                can_trade = min_after_open >= 5 and data_fresh

                if not can_trade:
                    if min_after_open < 5:
                        log.info("⏳ 开盘冷却期（%d/5 分钟），跳过交易", min_after_open)
                    if not data_fresh:
                        log.info("⏳ 行情数据日期=%s 非今日，等待 Tushare 更新...", _price_date)
                else:
                    # LLM 自主决策（一次调用同时决定中长期和灵活的买卖）
                    user_msg = _build_llm_context(state)
                    user_msg["trading_context"] = {
                        "minutes_after_open": min_after_open,
                        "data_date": _price_date,
                    }
                    decisions = deepseek_ask(LLM_SYSTEM_PROMPT, user_msg)

                    if decisions:
                        log.info("LLM 决策: %s", decisions.get("reasoning", "")[:200])

                        # 执行卖出
                        for item in decisions.get("sells", []):
                            sym = str(item.get("symbol", ""))
                            reason = item.get("reason", "LLM signal")
                            if sym not in state.get("positions", {}):
                                log.warning("护栏拦截: %s 未持仓", sym)
                                continue
                            execute_sell(sym, reason)
                            state = load_state()
                            time.sleep(1)

                        # 执行买入（带护栏和策略校验）
                        state = load_state()
                        positions = state.get("positions", {})
                        lt_count = sum(1 for p in positions.values() if p.get("strategy") == "long_term")
                        ac_count = sum(1 for p in positions.values() if p.get("strategy") == "active")

                        for item in decisions.get("buys", []):
                            sym = str(item.get("symbol", ""))
                            reason = item.get("reason", "LLM signal")
                            strategy = item.get("strategy", "active")
                            qty_pct = max(10, min(100, int(item.get("quantity_percent", 100))))

                            if sym in positions:
                                log.warning("护栏拦截: %s 已持仓", sym)
                                continue
                            if strategy == "long_term" and lt_count >= MAX_LONG_TERM_POSITIONS:
                                log.warning("护栏拦截: 中长期已达上限 %s", MAX_LONG_TERM_POSITIONS)
                                continue
                            if strategy == "active" and ac_count >= MAX_ACTIVE_POSITIONS:
                                log.warning("护栏拦截: 灵活已达上限 %s", MAX_ACTIVE_POSITIONS)
                                continue
                            price = fetch_price(sym)
                            if not price or price <= 0:
                                log.warning("护栏拦截: %s 无有效价格", sym)
                                continue

                            ok = execute_buy(sym, strategy)
                            if ok:
                                if strategy == "long_term":
                                    lt_count += 1
                                else:
                                    ac_count += 1
                                time.sleep(1)

                        state = load_state()
                        state["_llm_reasoning"] = decisions.get("reasoning", "")
                        save_state(state)
                    else:
                        log.warning("LLM 调用失败，跳过本周期交易")

                # ── 4. 尾盘买入逆回购（14:55-15:00）──
                now = datetime.now().time()
                close_cutoff = now.replace(hour=14, minute=55)
                if now >= close_cutoff and session == "afternoon":
                    # 闲置资金 → R-001
                    revenue_idle = state["cash"] - 20000  # 保留2万流动
                    if revenue_idle >= 1000:
                        buy_reverse_repo(state, revenue_idle)
                        state = load_state()
            else:
                pos_count = len(state.get("positions", {}))
                lt_count = sum(
                    1 for p in state.get("positions", {}).values()
                    if p.get("strategy") == "long_term"
                )
                ac_count = pos_count - lt_count
                cash = state["cash"]
                eq = _calc_total_equity(state)
                repo_total = sum(r["amount"] for r in state.get("repos", []))
                log.info("⏸ 休市 | 持仓 %s只(中长期%s/灵活%s) | 现金 ￥%.0f | 逆回购 ￥%.0f | 总资产 ￥%.0f",
                         pos_count, lt_count, ac_count, cash, repo_total, eq)

            # 4. 日报（15:30-16:30）
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
    repos = state.get("repos", [])
    repo_total = sum(r["amount"] for r in repos)
    repo_interest = state.get("repo_interest_earned", 0)
    start_eq = state.get("daily_start_equity", INITIAL_CAPITAL)
    total_eq = _calc_total_equity(state)
    daily_pnl = total_eq - start_eq
    bond_value = total_eq - cash - repo_total
    llm_reasoning = state.get("_llm_reasoning", "")
    lt_cost = state.get("long_term_cost", 0)
    ac_cost = state.get("active_cost", 0)

    trade_rows = ""
    if trades:
        for t in trades:
            a = "买" if t["action"] == "buy" else "卖"
            r = {
                "long_term_signal": "中长期建仓", "active_signal": "灵活建仓",
                "stop_loss": "止损", "take_profit": "止盈",
                "rating_downgrade": "评级下调", "near_maturity": "临近到期",
                "force_sell": "清仓",
            }.get(t.get("reason", ""), t.get("reason", ""))
            trade_rows += (
                f"<tr><td>{t['time']}</td><td>{a}</td><td>{get_bond_name(t['symbol'])}</td>"
                f"<td>{t['symbol']}</td><td>{t['quantity']}张</td><td>￥{t['price']:.3f}</td>"
                f"<td>{r}</td></tr>"
            )
    else:
        trade_rows = '<tr><td colspan="7" style="text-align:center;color:#888">今日无成交</td></tr>'

    pos_rows = ""
    if positions:
        for s, p in positions.items():
            cur = fetch_price(s) or p["entry_price"]
            qty = p["quantity"]
            entry = p["entry_price"]
            mv = cur * qty
            pnl = (cur - entry) * qty
            pnl_pct = (cur - entry) / entry * 100 if entry else 0
            st = p.get("strategy", "active")
            st_label = "中长期" if st == "long_term" else "灵活"
            c = "#e74c3c" if pnl < 0 else "#27ae60"
            ana = get_analytics(s)
            ytm_str = f"{ana.get('ytm', 0)*100:.1f}%" if ana.get("ytm") else "-"
            pos_rows += (
                f"<tr><td>{get_bond_name(s)}</td><td>{s}</td><td>{qty}张</td>"
                f"<td>￥{entry:.3f}</td><td>￥{cur:.3f}</td><td>￥{mv:,.0f}</td>"
                f"<td style='color:{c}'>￥{pnl:+,.0f} ({pnl_pct:+.2f}%)</td>"
                f"<td>{st_label}</td><td>{ytm_str}</td></tr>"
            )
    else:
        pos_rows = '<tr><td colspan="9" style="text-align:center;color:#888">空仓</td></tr>'

    summary = _gen_summary(trades, positions, daily_pnl, llm_reasoning)

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
    body {{ font-family: -apple-system, 'Microsoft YaHei', sans-serif; background: #f5f6fa; padding: 20px; color: #2c3e50; }}
    .container {{ max-width: 750px; margin: 0 auto; background: #fff; border-radius: 12px; padding: 32px; box-shadow: 0 2px 12px rgba(0,0,0,.08); }}
    h1 {{ font-size: 22px; border-bottom: 2px solid #8e44ad; padding-bottom: 12px; }}
    h2 {{ font-size: 16px; color: #8e44ad; margin-top: 28px; }}
    table {{ width: 100%; border-collapse: collapse; margin: 12px 0; font-size: 13px; }}
    th {{ background: #f8f4fc; padding: 10px 8px; text-align: left; }}
    td {{ padding: 8px; border-bottom: 1px solid #eee; }}
    .metrics {{ display: flex; gap: 16px; flex-wrap: wrap; }}
    .metric {{ flex: 1; min-width: 120px; background: #f8f4fc; border-radius: 8px; padding: 16px; text-align: center; }}
    .metric .label {{ font-size: 12px; color: #7f8c8d; }}
    .metric .value {{ font-size: 20px; font-weight: 600; margin-top: 4px; }}
    .summary {{ background: #fef9e7; border-left: 4px solid #f39c12; padding: 14px 18px; margin-top: 24px; font-size: 14px; line-height: 1.7; border-radius: 4px; }}
    .footer {{ text-align: center; color: #aaa; font-size: 12px; margin-top: 24px; }}
</style></head><body>
<div class="container">
<h1>债券 AI-Trader 投资日报 {today_str}</h1>

<h2>资产概览</h2>
<div class="metrics">
    <div class="metric"><div class="label">总资产</div><div class="value">￥{total_eq:,.0f}</div></div>
    <div class="metric"><div class="label">现金</div><div class="value">￥{cash:,.0f}</div></div>
    <div class="metric"><div class="label">债券持仓市值</div><div class="value">￥{bond_value:,.0f}</div></div>
    <div class="metric"><div class="label">逆回购</div><div class="value">￥{repo_total:,.0f}</div></div>
    <div class="metric"><div class="label">中长期成本</div><div class="value">￥{lt_cost:,.0f}</div></div>
    <div class="metric"><div class="label">灵活交易成本</div><div class="value">￥{ac_cost:,.0f}</div></div>
    <div class="metric"><div class="label">逆回购累计利息</div><div class="value">￥{repo_interest:,.2f}</div></div>
    <div class="metric"><div class="label">当日盈亏</div>
        <div class="value" style="color:{'#e74c3c' if daily_pnl<0 else '#27ae60'}">{daily_pnl:+,.0f}</div></div>
</div>

<h2>当日成交</h2>
<table><thead><tr><th>时间</th><th>方向</th><th>名称</th><th>代码</th><th>数量</th><th>价格</th><th>原因</th></tr></thead>
<tbody>{trade_rows}</tbody></table>

<h2>当前持仓</h2>
<table><thead><tr><th>名称</th><th>代码</th><th>数量</th><th>成本价</th><th>现价</th><th>市值</th><th>浮动盈亏</th><th>策略</th><th>YTM</th></tr></thead>
<tbody>{pos_rows}</tbody></table>

<h2>AI 决策思路</h2>
<div class="summary"><strong>LLM 复盘</strong><br>{summary}</div>

<div class="footer">{AGENT_NAME} · DeepSeek-v4-pro 驱动</div>
</div></body></html>"""


def _gen_summary(trades, positions, daily_pnl, llm_reasoning: str):
    parts = []
    buys = sum(1 for t in trades if t["action"] == "buy")
    sells = sum(1 for t in trades if t["action"] == "sell")

    lt_buys = sum(1 for t in trades if t["action"] == "buy" and "long_term" in str(t.get("reason", "")))
    ac_buys = buys - lt_buys

    if buys + sells == 0:
        parts.append("今日无操作。")
    else:
        parts.append(f"成交 {buys} 笔买入（中长期{lt_buys}/灵活{ac_buys}）、{sells} 笔卖出。")
    if buys:
        bought = [get_bond_name(t["symbol"]) for t in trades if t["action"] == "buy"]
        parts.append("新开/加仓: " + "、".join(bought) + "。")
    if sells:
        sold = [get_bond_name(t["symbol"]) for t in trades if t["action"] == "sell"]
        parts.append("平仓: " + "、".join(sold) + "。")
    if llm_reasoning:
        parts.append(f"AI 决策逻辑: {llm_reasoning}")

    lt_count = sum(1 for p in positions.values() if p.get("strategy") == "long_term")
    ac_count = len(positions) - lt_count
    if lt_count:
        parts.append(f"中长期持有 {lt_count} 只，由 DeepSeek LLM 根据YTM/评级/纯债价值监控。")
    if ac_count:
        parts.append(f"灵活交易 {ac_count} 只，T+0 波段，LLM 持续决策。")
    if not positions:
        parts.append("当前空仓，等待 DeepSeek 判断合适的建仓时机。")
    return "".join(parts)


def send_report_email(html_content: str):
    if not SMTP_USER or not SMTP_PASS:
        log.warning("SMTP 未配置，跳过发送")
        return
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"债券 AI-Trader 投资日报 {datetime.now().strftime('%Y-%m-%d')}"
    msg["From"] = SMTP_USER
    msg["To"] = REPORT_EMAIL
    msg.attach(MIMEText(html_content, "html", "utf-8"))
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=15) as s:
        s.login(SMTP_USER, SMTP_PASS)
        s.sendmail(SMTP_USER, [REPORT_EMAIL], msg.as_string())


# ── Entry ───────────────────────────────────────────────

def main():
    log.info("债券 AI-Trader 启动...")
    log.info("数据源: Tushare Pro | 交易模拟: 本地撮合 | 品种: 可转债")
    run_loop()


if __name__ == "__main__":
    main()
