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

# 资金分配
LONG_TERM_RATIO = float(os.getenv("LONG_TERM_RATIO", "0.60"))
ACTIVE_RATIO = float(os.getenv("ACTIVE_RATIO", "0.40"))

# 中长期策略
MAX_LONG_TERM_PER_BOND = float(os.getenv("MAX_LONG_TERM_PER_BOND", "40000"))
MAX_LONG_TERM_POSITIONS = int(os.getenv("MAX_LONG_TERM_POSITIONS", "4"))
LONG_TERM_TAKE_PROFIT_PCT = float(os.getenv("LONG_TERM_TAKE_PROFIT_PCT", "0.20"))
LONG_TERM_CALL_PRICE = float(os.getenv("LONG_TERM_CALL_PRICE", "130.0"))
MIN_YTM = float(os.getenv("MIN_YTM", "0.01"))
MAX_ENTRY_PRICE = float(os.getenv("MAX_ENTRY_PRICE", "118.0"))
MIN_REMAINING_YEARS = float(os.getenv("MIN_REMAINING_YEARS", "1.0"))
MIN_BOND_RATING = os.getenv("MIN_BOND_RATING", "AA-")

# 灵活交易策略
MAX_ACTIVE_PER_BOND = float(os.getenv("MAX_ACTIVE_PER_BOND", "25000"))
MAX_ACTIVE_POSITIONS = int(os.getenv("MAX_ACTIVE_POSITIONS", "3"))
ACTIVE_STOP_LOSS_PCT = float(os.getenv("ACTIVE_STOP_LOSS_PCT", "-0.03"))
ACTIVE_TAKE_PROFIT_PCT = float(os.getenv("ACTIVE_TAKE_PROFIT_PCT", "0.08"))
MIN_ACTIVE_TURNOVER = float(os.getenv("MIN_ACTIVE_TURNOVER", "10000000"))

# 通用
MIN_DAILY_TURNOVER = float(os.getenv("MIN_DAILY_TURNOVER", "5000000"))

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


def _rating_score(rating: str) -> int:
    if not rating:
        return 0
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
    global _price_cache
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


# ── Strategy: Long-Term (60%) ───────────────────────────

def screen_long_term_candidates(state: dict) -> list:
    """筛选中长期候选可转债：价格近面值、正YTM、高评级、久期适中。"""
    positions = state.get("positions", {})
    lt_count = sum(1 for p in positions.values() if p.get("strategy") == "long_term")
    if lt_count >= MAX_LONG_TERM_POSITIONS:
        return []

    candidates = []
    for code in _price_cache:
        if code in positions:
            continue
        info = _price_cache[code]
        price = info["price"]
        if price <= 0:
            continue
        # 价格区间：面值附近（100-118）
        if price < 100 or price > MAX_ENTRY_PRICE:
            continue
        # 一手成本
        lot_cost = price * 10
        if lot_cost > state["cash"]:
            continue
        if lot_cost > MAX_LONG_TERM_PER_BOND:
            continue

        ana = get_analytics(code)
        ytm = ana.get("ytm", 0)
        rating = ana.get("rating", "")
        remain = ana.get("remain_years", 0)
        pure_val = ana.get("pure_bond_value", 0)
        turnover = ana.get("turnover", info.get("volume", 0) * price * 100)

        # 过滤（仅当数据已知时应用）
        if ytm != 0 and ytm < MIN_YTM:
            continue
        if remain > 0 and remain < MIN_REMAINING_YEARS:
            continue
        if rating and _rating_score(rating) < _rating_score(MIN_BOND_RATING):
            continue
        if turnover > 0 and turnover < MIN_DAILY_TURNOVER:
            continue

        # 得分：YTM 权重 0.5 + 评级权重 0.3 + 纯债保护权重 0.2
        rating_norm = _rating_score(rating) / 7.0
        pure_protection = (pure_val / price) if pure_val > 0 else 0
        score = ytm * 5 * 0.5 + rating_norm * 0.3 + pure_protection * 0.2
        candidates.append((code, score, price))

    candidates.sort(key=lambda x: x[1], reverse=True)
    return candidates


def should_add_long_term(code: str, state: dict) -> bool:
    """判断是否可在中长期仓位上追加买入。"""
    positions = state.get("positions", {})
    if code in positions:
        pos = positions[code]
        if pos.get("strategy") != "long_term":
            return False
        # 已有仓位，检查是否可追加
        current_cost = pos["quantity"] * fetch_price(code)
        if current_cost + MAX_LONG_TERM_PER_BOND * 0.5 > MAX_LONG_TERM_PER_BOND * 1.5:
            return False
        price = fetch_price(code)
        if price and price > MAX_ENTRY_PRICE:
            return False
        return True
    # 新开仓
    lt_count = sum(1 for p in positions.values() if p.get("strategy") == "long_term")
    return lt_count < MAX_LONG_TERM_POSITIONS


def should_sell_long_term(code: str, state: dict):
    """中长期卖出信号：触发强赎价 / 收益达标 / 评级下调 / 临近到期。"""
    positions = state.get("positions", {})
    if code not in positions:
        return False, ""
    pos = positions[code]
    if pos.get("strategy") != "long_term":
        return False, ""

    price = fetch_price(code)
    if not price:
        return False, ""
    entry = pos["entry_price"]

    # 价格触及 130（多数可转债强赎触发价）
    if price >= LONG_TERM_CALL_PRICE:
        return True, "take_profit"
    # 涨幅超 20%
    if (price - entry) / entry >= LONG_TERM_TAKE_PROFIT_PCT:
        return True, "take_profit"
    # 评级下调至 AA- 以下
    ana = get_analytics(code)
    if ana.get("rating") and _rating_score(ana["rating"]) < _rating_score(MIN_BOND_RATING):
        return True, "rating_downgrade"
    # 剩余年限不足 3 个月
    if ana.get("remain_years") and ana["remain_years"] < 0.25:
        return True, "near_maturity"

    return False, ""


# ── Strategy: Active (40%) ──────────────────────────────

def screen_active_candidates(state: dict) -> list:
    """筛选灵活交易候选可转债：高流动性、动量排名。"""
    positions = state.get("positions", {})
    ac_count = sum(1 for p in positions.values() if p.get("strategy") == "active")
    if ac_count >= MAX_ACTIVE_POSITIONS:
        return []

    candidates = []
    for code in _price_cache:
        if code in positions:
            continue
        info = _price_cache[code]
        price = info["price"]
        prev = info["prev_close"]
        if price <= 0 or prev <= 0:
            continue

        ana = get_analytics(code)
        turnover = ana.get("turnover", info.get("volume", 0) * price * 100)
        if turnover > 0 and turnover < MIN_ACTIVE_TURNOVER:
            continue

        lot_cost = price * 10
        if lot_cost > MAX_ACTIVE_PER_BOND:
            continue
        if lot_cost > state["cash"]:
            continue

        # 动量：涨跌幅 + 日内强度
        chg_pct = (price - prev) / prev
        intraday_pct = (price - info["open"]) / info["open"] if info["open"] > 0 else 0
        score = chg_pct * 0.6 + intraday_pct * 0.4
        candidates.append((code, score, price))

    candidates.sort(key=lambda x: x[1], reverse=True)
    return candidates


def should_sell_active(code: str, state: dict):
    """灵活交易卖出：止损 -3% / 止盈 +8%。"""
    positions = state.get("positions", {})
    if code not in positions:
        return False, ""
    pos = positions[code]
    if pos.get("strategy") != "active":
        return False, ""

    price = fetch_price(code)
    if not price:
        return False, ""
    entry = pos["entry_price"]
    pnl_pct = (price - entry) / entry

    if pnl_pct <= ACTIVE_STOP_LOSS_PCT:
        return True, "stop_loss"
    if pnl_pct >= ACTIVE_TAKE_PROFIT_PCT:
        return True, "take_profit"
    return False, ""


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

def run_loop():
    log.info("══ 债券 AI-Trader Agent 启动 ══")
    log.info("初始资金: ￥%s", INITIAL_CAPITAL)
    log.info("策略: %.0f%% 中长期持有 + %.0f%% 灵活交易",
             LONG_TERM_RATIO * 100, ACTIVE_RATIO * 100)
    log.info("中长期: 止盈 +%.0f%%/￥%.0f | 不设止损 | 单只上限￥%s | 最大%s只",
             LONG_TERM_TAKE_PROFIT_PCT * 100, LONG_TERM_CALL_PRICE,
             int(MAX_LONG_TERM_PER_BOND), MAX_LONG_TERM_POSITIONS)
    log.info("灵活: 止损 %.0f%% | 止盈 +%.0f%% | 单只上限￥%s | 最大%s只 | T+0",
             ACTIVE_STOP_LOSS_PCT * 100, ACTIVE_TAKE_PROFIT_PCT * 100,
             int(MAX_ACTIVE_PER_BOND), MAX_ACTIVE_POSITIONS)
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
                # ── 1. 检查所有持仓的卖出信号 ──
                for code in list(state.get("positions", {}).keys()):
                    pos = state["positions"][code]
                    if pos.get("strategy") == "long_term":
                        do_sell, reason = should_sell_long_term(code, state)
                    else:
                        do_sell, reason = should_sell_active(code, state)

                    if do_sell:
                        name = get_bond_name(code)
                        if reason == "stop_loss":
                            log.info("🛑 %s 触发止损", name)
                            state.setdefault("stop_loss_hits", []).append(code)
                        elif reason == "take_profit":
                            log.info("🎯 %s 触达止盈", name)
                            state.setdefault("take_profit_hits", []).append(code)
                        elif reason == "rating_downgrade":
                            log.info("⚠ %s 评级下调，清仓", name)
                        elif reason == "near_maturity":
                            log.info("📅 %s 临近到期，清仓", name)
                        execute_sell(code, reason)
                        state = load_state()

                # ── 2. 中长期买入（60% 资金，分批建仓）──
                lt_count = sum(
                    1 for p in state.get("positions", {}).values()
                    if p.get("strategy") == "long_term"
                )
                if lt_count < MAX_LONG_TERM_POSITIONS:
                    # 扩展行情覆盖（每批 30 只，控制 API 调用频率）
                    if len(universe) > 30:
                        refresh_prices(universe[30:80])
                    lt_candidates = screen_long_term_candidates(state)
                    for code, score, price in lt_candidates:
                        lt_count = sum(
                            1 for p in state.get("positions", {}).values()
                            if p.get("strategy") == "long_term"
                        )
                        if lt_count >= MAX_LONG_TERM_POSITIONS:
                            break
                        if should_add_long_term(code, state):
                            log.info("🏦 中长期买入 %s(%s) score=%.4f YTM=%.1f%%",
                                     get_bond_name(code), code, score,
                                     get_analytics(code).get("ytm", 0) * 100)
                            execute_buy(code, "long_term")
                            state = load_state()
                            time.sleep(1)

                # ── 3. 灵活交易买入（40% 资金，T+0 波段）──
                ac_count = sum(
                    1 for p in state.get("positions", {}).values()
                    if p.get("strategy") == "active"
                )
                if ac_count < MAX_ACTIVE_POSITIONS:
                    ac_candidates = screen_active_candidates(state)
                    for code, score, price in ac_candidates:
                        ac_count = sum(
                            1 for p in state.get("positions", {}).values()
                            if p.get("strategy") == "active"
                        )
                        if ac_count >= MAX_ACTIVE_POSITIONS:
                            break
                        if code not in state.get("positions", {}):
                            log.info("📈 灵活买入 %s(%s) score=%.4f",
                                     get_bond_name(code), code, score)
                            execute_buy(code, "active")
                            state = load_state()
                            time.sleep(1)

                # ── 4. 尾盘买入逆回购（14:55-15:00）──
                now = datetime.now().time()
                close_cutoff = now.replace(hour=14, minute=55)
                if now >= close_cutoff and session == "afternoon":
                    # 灵活交易闲置资金 → R-001
                    active_positions_value = sum(
                        fetch_price(c) * p["quantity"]
                        for c, p in state.get("positions", {}).items()
                        if p.get("strategy") == "active"
                    )
                    active_idle = state["cash"] * ACTIVE_RATIO - active_positions_value * 0.1
                    if active_idle >= 1000:
                        buy_reverse_repo(state, active_idle)
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
    stop_hits = state.get("stop_loss_hits", [])
    profit_hits = state.get("take_profit_hits", [])
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

    sl_text = "、".join(get_bond_name(s) for s in stop_hits) if stop_hits else "无"
    tp_text = "、".join(get_bond_name(s) for s in profit_hits) if profit_hits else "无"

    summary = _gen_summary(trades, positions, daily_pnl, stop_hits, profit_hits)

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

<h2>止损/止盈触发</h2>
<table><thead><tr><th>类型</th><th>触发标的</th></tr></thead>
<tbody><tr><td>止损 (灵活 -{abs(ACTIVE_STOP_LOSS_PCT)*100:.0f}%)</td><td>{sl_text}</td></tr>
    <tr><td>止盈 (灵活 +{ACTIVE_TAKE_PROFIT_PCT*100:.0f}% / 中长期 +{LONG_TERM_TAKE_PROFIT_PCT*100:.0f}% 或 ￥{LONG_TERM_CALL_PRICE:.0f})</td><td>{tp_text}</td></tr></tbody></table>

<div class="summary"><strong>今日复盘</strong><br>{summary}</div>

<div class="footer">{AGENT_NAME} · 可转债双策略 · 自动化系统</div>
</div></body></html>"""


def _gen_summary(trades, positions, daily_pnl, stop_hits, profit_hits):
    parts = []
    buys = sum(1 for t in trades if t["action"] == "buy")
    sells = sum(1 for t in trades if t["action"] == "sell")

    lt_buys = sum(1 for t in trades if t["action"] == "buy" and "long_term" in t.get("reason", ""))
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
    if stop_hits:
        parts.append(f"触发止损: {'、'.join(get_bond_name(s) for s in stop_hits)}。")
    if profit_hits:
        parts.append(f"触发止盈: {'、'.join(get_bond_name(s) for s in profit_hits)}。")

    lt_count = sum(1 for p in positions.values() if p.get("strategy") == "long_term")
    ac_count = len(positions) - lt_count
    if lt_count:
        parts.append(f"中长期持有 {lt_count} 只，关注收益率稳定性和信用资质变化。")
    if ac_count:
        parts.append(f"灵活交易 {ac_count} 只，T+0 波段操作。")
    if not positions:
        parts.append("当前空仓，等待合适的建仓时机。")
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
