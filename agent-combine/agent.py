"""
Permanent Portfolio Agent — 哈利·布朗永久投资组合（国内版）

四分配置：
  25% 股票 — 510300.SH 沪深300ETF
  25% 债券 — 511010.SH 国债ETF
  25% 黄金 — 518880.SH 黄金ETF
  25% 现金 — 货币基金/现金

策略：机械式再平衡。不做预测，不依赖 LLM。
"""
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import tushare as ts
from dotenv import load_dotenv

# ── Config ──────────────────────────────────────────────
load_dotenv(Path(__file__).parent / ".env")

AGENT_NAME = os.getenv("AGENT_NAME", "PermanentPortfolio")
INITIAL_CAPITAL = float(os.getenv("INITIAL_CAPITAL", "200000"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOOP_INTERVAL = int(os.getenv("LOOP_INTERVAL", "60"))

TUSHARE_TOKEN = os.getenv("TUSHARE_TOKEN", "")
if TUSHARE_TOKEN:
    ts.set_token(TUSHARE_TOKEN)
pro = ts.pro_api() if TUSHARE_TOKEN else None

LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "agent.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
log = logging.getLogger("pp")

STATE_FILE = Path(__file__).parent / "state.json"

# ── Permanent Portfolio Assets ───────────────────────────
PORTFOLIO = {
    "stock": {
        "code": "510300.SH",
        "name": "沪深300ETF",
        "target": 0.25,
        "description": "经济繁荣期 — 分享增长红利",
    },
    "bond": {
        "code": "511010.SH",
        "name": "国债ETF",
        "target": 0.25,
        "description": "经济衰退期 — 利率下行时升值",
    },
    "gold": {
        "code": "518880.SH",
        "name": "黄金ETF",
        "target": 0.25,
        "description": "通货膨胀期 — 对冲货币贬值",
    },
    "cash": {
        "code": None,
        "name": "现金",
        "target": 0.25,
        "description": "通缩/危机期 — 流动性与购买力",
    },
}

# Rebalance thresholds: if any asset exceeds these, trigger rebalance
REBALANCE_UPPER = 0.30   # >30% → sell down to 25%
REBALANCE_LOWER = 0.20   # <20% → buy up to 25%
FORCE_REBALANCE_DAYS = 90  # Force rebalance every 90 days regardless

_price_cache: dict = {}
_price_date: str = ""


# ── Price Fetching ───────────────────────────────────────

def _sina_to_ts(code: str) -> str:
    if code.startswith("sh"): return code[2:] + ".SH"
    if code.startswith("sz"): return code[2:] + ".SZ"
    return code


def refresh_prices():
    """Fetch latest prices for all PP assets (fund_daily is single-symbol only)."""
    global _price_cache, _price_date
    codes = [a["code"] for a in PORTFOLIO.values() if a["code"]]

    end_date = datetime.now().strftime("%Y%m%d")
    start_date = (datetime.now() - timedelta(days=7)).strftime("%Y%m%d")
    fetched = 0

    for code in codes:
        try:
            df = pro.fund_daily(ts_code=code, start_date=start_date, end_date=end_date,
                                fields="ts_code,trade_date,close,pre_close")
            if df is not None and len(df) > 0:
                r = df.iloc[0]
                _price_cache[code] = {
                    "price": float(r["close"]),
                    "prev_close": float(r["pre_close"]),
                }
                _price_date = str(r["trade_date"])
                fetched += 1
            time.sleep(0.3)
        except Exception as e:
            log.warning("%s 行情获取失败: %s", code, e)

    if fetched == 0:
        log.warning("无法获取行情数据")
        return False
    log.info("行情更新: %s, %d/%d只ETF", _price_date, fetched, len(codes))
    return True


def fetch_price(code: str) -> Optional[float]:
    info = _price_cache.get(code)
    return info["price"] if info else None


# ── State Management ─────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {
        "cash": INITIAL_CAPITAL,
        "positions": {},
        "initial_capital": INITIAL_CAPITAL,
        "rebalance_count": 0,
        "last_rebalance": None,
        "created_at": datetime.now().strftime("%Y-%m-%d"),
    }


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def calc_total_equity(state: dict) -> float:
    total = state["cash"]
    for asset_key, pos in state.get("positions", {}).items():
        asset = PORTFOLIO[asset_key]
        price = fetch_price(asset["code"]) or pos["entry_price"]
        total += price * pos["quantity"]
    return total


def calc_allocations(state: dict) -> dict:
    """Return current allocation percentages for each asset."""
    total = calc_total_equity(state)
    if total <= 0:
        return {}

    allocs = {}
    for key, asset in PORTFOLIO.items():
        if key == "cash":
            allocs[key] = state["cash"] / total
        else:
            pos = state.get("positions", {}).get(key)
            if pos:
                price = fetch_price(asset["code"]) or pos["entry_price"]
                val = price * pos["quantity"]
                allocs[key] = val / total
            else:
                allocs[key] = 0
    return allocs


# ── Trade Execution ──────────────────────────────────────

def execute_trade(action: str, asset_key: str, quantity: int, price: float, reason: str = ""):
    """Execute a buy or sell for a PP asset."""
    state = load_state()
    asset = PORTFOLIO[asset_key]
    code = asset["code"]
    name = asset["name"]

    if action == "buy":
        cost = quantity * price
        if state["cash"] < cost:
            log.warning("现金不足: 需¥%.0f 仅有¥%.0f", cost, state["cash"])
            return False
        state["cash"] -= cost
        if asset_key not in state["positions"]:
            state["positions"][asset_key] = {"quantity": quantity, "entry_price": price}
        else:
            old_qty = state["positions"][asset_key]["quantity"]
            old_entry = state["positions"][asset_key]["entry_price"]
            new_qty = old_qty + quantity
            new_entry = (old_entry * old_qty + price * quantity) / new_qty
            state["positions"][asset_key] = {"quantity": new_qty, "entry_price": new_entry}
        log.info("买入 %s(%s) x%d @¥%.3f 成本¥%.0f  [%s]", name, code, quantity, price, cost, reason)

    elif action == "sell":
        pos = state.get("positions", {}).get(asset_key)
        if not pos or pos["quantity"] < quantity:
            log.warning("持仓不足: %s 需%d 有%d", name, quantity, pos["quantity"] if pos else 0)
            return False
        proceeds = quantity * price
        state["cash"] += proceeds
        new_qty = pos["quantity"] - quantity
        if new_qty <= 0:
            del state["positions"][asset_key]
        else:
            state["positions"][asset_key]["quantity"] = new_qty
        log.info("卖出 %s(%s) x%d @¥%.3f 收入¥%.0f  [%s]", name, code, quantity, price, proceeds, reason)

    save_state(state)
    return True


# ── Rebalancing Logic ────────────────────────────────────

def needs_rebalance(allocs: dict, force: bool = False) -> bool:
    """Check if rebalancing is needed."""
    if force:
        return True
    if not allocs:
        return False
    for key, pct in allocs.items():
        if pct > REBALANCE_UPPER or pct < REBALANCE_LOWER:
            return True
    return False


def do_rebalance():
    """Execute full rebalance to target 25/25/25/25."""
    state = load_state()
    total = calc_total_equity(state)
    if total <= 0:
        return

    allocs = calc_allocations(state)
    log.info("═" * 50)
    log.info("再平衡检查 — 总资产 ¥%.0f", total)
    for key, pct in allocs.items():
        target = PORTFOLIO[key]["target"]
        flag = " ⚠" if pct > REBALANCE_UPPER or pct < REBALANCE_LOWER else ""
        log.info("  %s: %5.1f%% (目标 %5.1f%%)%s", PORTFOLIO[key]["name"], pct * 100, target * 100, flag)

    if not needs_rebalance(allocs):
        log.info("无需再平衡")
        return

    log.info("开始再平衡...")

    # For each non-cash asset, compute target value and adjust
    for key, asset in PORTFOLIO.items():
        target_val = total * asset["target"]
        if key == "cash":
            continue

        code = asset["code"]
        price = fetch_price(code)
        if not price:
            log.warning("%s 无价格，跳过", asset["name"])
            continue

        current_qty = state.get("positions", {}).get(key, {}).get("quantity", 0)
        current_val = current_qty * price
        diff = target_val - current_val
        lot_size = 100  # ETF lot size

        if abs(diff) < price * lot_size:
            continue  # too small to trade

        shares = int(abs(diff) / price / lot_size) * lot_size
        if shares < lot_size:
            continue

        if diff > 0:
            # Need to buy
            cost = shares * price
            if state["cash"] < cost:
                shares = int(state["cash"] / price / lot_size) * lot_size
                if shares < lot_size:
                    continue
            execute_trade("buy", key, shares, price, "再平衡买入")
        else:
            # Need to sell
            if current_qty < shares:
                shares = current_qty - (current_qty % lot_size)
                if shares < lot_size:
                    continue
            execute_trade("sell", key, shares, price, "再平衡卖出")

        state = load_state()  # refresh after each trade
        time.sleep(0.5)

    # Update state
    state["rebalance_count"] = state.get("rebalance_count", 0) + 1
    state["last_rebalance"] = datetime.now().strftime("%Y-%m-%d")
    save_state(state)

    new_allocs = calc_allocations(state)
    log.info("再平衡完成:")
    for key, pct in new_allocs.items():
        log.info("  %s: %5.1f%%", PORTFOLIO[key]["name"], pct * 100)


# ── Report ───────────────────────────────────────────────

def generate_report(state: dict) -> str:
    today_str = datetime.now().strftime("%Y-%m-%d")
    total = calc_total_equity(state)
    init = state.get("initial_capital", INITIAL_CAPITAL)
    total_ret = (total - init) / init * 100
    allocs = calc_allocations(state)
    reb_count = state.get("rebalance_count", 0)
    last_reb = state.get("last_rebalance", "从未")

    alloc_rows = ""
    for key, pct in allocs.items():
        asset = PORTFOLIO[key]
        target = asset["target"]
        val = total * pct
        diff = (pct - target) * 100
        color = "#e74c3c" if abs(diff) > 5 else "#27ae60"
        alloc_rows += (
            f"<tr><td>{asset['name']}</td><td>{asset['description']}</td>"
            f"<td>¥{val:,.0f}</td>"
            f"<td style='color:{color}'>{pct*100:.1f}% (目标{target*100:.0f}%)</td></tr>"
        )

    pos_rows = ""
    for key, pos in state.get("positions", {}).items():
        asset = PORTFOLIO[key]
        code = asset["code"]
        cur = fetch_price(code) or pos["entry_price"]
        qty = pos["quantity"]
        entry = pos["entry_price"]
        mv = cur * qty
        pnl = (cur - entry) / entry * 100 if entry else 0
        c = "#e74c3c" if pnl < 0 else "#27ae60"
        pos_rows += (
            f"<tr><td>{asset['name']}</td><td>{code}</td><td>{qty}</td>"
            f"<td>¥{entry:.3f}</td><td>¥{cur:.3f}</td><td>¥{mv:,.0f}</td>"
            f"<td style='color:{c}'>{pnl:+.2f}%</td></tr>"
        )

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
    body {{ font-family: -apple-system, 'Microsoft YaHei', sans-serif; background: #f5f6fa; padding: 20px; color: #2c3e50; }}
    .container {{ max-width: 700px; margin: 0 auto; background: #fff; border-radius: 12px; padding: 32px; box-shadow: 0 2px 12px rgba(0,0,0,.08); }}
    h1 {{ font-size: 22px; border-bottom: 2px solid #f39c12; padding-bottom: 12px; }}
    h2 {{ font-size: 16px; color: #f39c12; margin-top: 28px; }}
    table {{ width: 100%; border-collapse: collapse; margin: 12px 0; font-size: 13px; }}
    th {{ background: #fef9e7; padding: 10px 8px; text-align: left; }}
    td {{ padding: 8px; border-bottom: 1px solid #eee; }}
    .metrics {{ display: flex; gap: 16px; flex-wrap: wrap; }}
    .metric {{ flex: 1; min-width: 130px; background: #fef9e7; border-radius: 8px; padding: 16px; text-align: center; }}
    .metric .label {{ font-size: 12px; color: #7f8c8d; }}
    .metric .value {{ font-size: 20px; font-weight: 600; margin-top: 4px; }}
    .tag {{ display: inline-block; padding: 2px 10px; border-radius: 12px; font-size: 11px; font-weight: 600; }}
    .tag-value {{ background: #fef5f5; color: #e74c3c; }}
    .tag-growth {{ background: #eafaf1; color: #27ae60; }}
    .tag-harvest {{ background: #fef9e7; color: #f39c12; }}
    .footer {{ text-align: center; color: #aaa; font-size: 12px; margin-top: 24px; }}
</style></head><body>
<div class="container">
<h1>永久投资组合 每日报告 {today_str}</h1>

<h2>资产概览</h2>
<div class="metrics">
    <div class="metric"><div class="label">总资产</div><div class="value">¥{total:,.0f}</div></div>
    <div class="metric"><div class="label">现金</div><div class="value">¥{state['cash']:,.0f}</div></div>
    <div class="metric"><div class="label">累计收益</div>
        <div class="value" style="color:{'#27ae60' if total_ret >= 0 else '#e74c3c'}">{total_ret:+.2f}%</div></div>
    <div class="metric"><div class="label">再平衡次数</div><div class="value">{reb_count}</div></div>
</div>

<h2>当前配置</h2>
<table><thead><tr><th>资产</th><th>对应环境</th><th>市值</th><th>占比</th></tr></thead>
<tbody>{alloc_rows}</tbody></table>

<h2>持仓明细</h2>
<table><thead><tr><th>名称</th><th>代码</th><th>数量</th><th>成本</th><th>现价</th><th>市值</th><th>盈亏</th></tr></thead>
<tbody>{pos_rows}</tbody></table>

<div style="background:#fef9e7;border-left:4px solid #f39c12;padding:14px 18px;margin-top:24px;font-size:14px;line-height:1.7;border-radius:4px;">
<strong>策略说明</strong><br>
哈利·布朗永久投资组合：25% 股票 + 25% 长期国债 + 25% 黄金 + 25% 现金。<br>
上次再平衡: {last_reb} | 触发条件: 任一资产偏离目标 ±5% 或超过90天未再平衡。<br>
<span style="color:#888">不预测市场，靠分散配置穿越任何经济周期。</span>
</div>

<div class="footer">{AGENT_NAME} · 机械式再平衡 · 零LLM成本</div>
</div></body></html>"""


# ── Main Loop ────────────────────────────────────────────

def is_trading_day() -> bool:
    try:
        today = datetime.now().strftime("%Y%m%d")
        cal = pro.trade_cal(exchange="SSE", start_date=today, end_date=today)
        return len(cal) > 0 and cal.iloc[0]["is_open"] == 1
    except Exception:
        return datetime.now().weekday() < 5


def minutes_after_close() -> int:
    t = datetime.now()
    mkt_close = t.replace(hour=15, minute=0, second=0, microsecond=0)
    delta = (t - mkt_close).total_seconds()
    return int(delta // 60) if delta >= 0 else -1


def run_once():
    """Single run — check prices, rebalance if needed, generate report."""
    log.info("══ 永久投资组合 Agent ══")

    state = load_state()

    # If first run, do initial buy
    if not state.get("positions") and state["cash"] == INITIAL_CAPITAL:
        log.info("首次运行，建立初始仓位...")
        refresh_prices()
        total = calc_total_equity(state)

        for key, asset in PORTFOLIO.items():
            if key == "cash":
                continue
            target_val = total * asset["target"]
            price = fetch_price(asset["code"])
            if not price:
                log.warning("%s 无价格，跳过初始建仓", asset["name"])
                continue
            lot_size = 100
            shares = int(target_val / price / lot_size) * lot_size
            if shares < lot_size:
                continue
            execute_trade("buy", key, shares, price, "初始建仓")
            state = load_state()
            time.sleep(0.5)

        save_state(state)
        log.info("初始建仓完成")

    # Refresh prices
    if not refresh_prices():
        log.warning("行情获取失败，使用缓存价格")

    # Check if force rebalance needed (>90 days)
    state = load_state()
    force = False
    if state.get("last_rebalance"):
        try:
            last = datetime.strptime(state["last_rebalance"], "%Y-%m-%d")
            days_since = (datetime.now() - last).days
            force = days_since >= FORCE_REBALANCE_DAYS
        except ValueError:
            pass

    # Rebalance
    do_rebalance()

    # Generate report
    state = load_state()
    report = generate_report(state)

    # Save report
    report_dir = Path(__file__).parent / "reports"
    report_dir.mkdir(exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    (report_dir / f"{today}.html").write_text(report, encoding="utf-8")
    log.info("报告已保存: reports/%s.html", today)

    # Print summary
    total = calc_total_equity(state)
    allocs = calc_allocations(state)
    log.info("总资产: ¥%.0f | 现金: ¥%.0f", total, state["cash"])
    for key, pct in allocs.items():
        log.info("  %s: %.1f%%", PORTFOLIO[key]["name"], pct * 100)

    return report


def main():
    log.info("永久投资组合 Agent 启动 (本地)...")
    log.info("初始资金: ¥%s | 策略: 25/25/25/25 机械再平衡", INITIAL_CAPITAL)
    run_once()
    log.info("完成")


if __name__ == "__main__":
    main()
