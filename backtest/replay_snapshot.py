"""Replay cached decisions to reconstruct current portfolio snapshot."""
import sqlite3, json, hashlib
from pathlib import Path

DB_PATH = "D:/ai-trader-data/market.db"
CACHE_FILE = Path(__file__).parent.parent / "backtest_cache" / "sqlite_backtest.json"
cache = json.loads(CACHE_FILE.read_text()) if CACHE_FILE.exists() else {}

# Load trading days
conn = sqlite3.connect(DB_PATH)
days = [r[0] for r in conn.execute(
    "SELECT cal_date FROM trade_cal WHERE cal_date BETWEEN '20250919' AND '20260602' AND is_open=1 AND exchange='SSE' ORDER BY cal_date"
).fetchall()]

# Load all ETF price data
codes = [r[0] for r in conn.execute(
    "SELECT DISTINCT ts_code FROM etf_daily WHERE trade_date BETWEEN '20250919' AND '20260602' LIMIT 40"
).fetchall()]

price_data = {}
for code in codes:
    rows = conn.execute(
        "SELECT trade_date, open, high, low, close, pre_close, vol FROM etf_daily "
        "WHERE ts_code=? AND trade_date BETWEEN ? AND ? ORDER BY trade_date",
        (code, '20250919', '20260602')).fetchall()
    for row in rows:
        date_str, o, h, l, c, pc, v = row
        sym = "sh" + code[:6] if code.endswith(".SH") else "sz" + code[:6]
        price_data.setdefault(date_str, {})[sym] = {
            "price": c, "prev_close": pc, "open": o, "high": h, "low": l, "volume": v,
        }
conn.close()

# Simulate with cached decisions
state = {"positions": {}, "cash": 200000, "locked_shares": {}, "daily_trades": []}
snapshots = []
cfg = {"initial": 200000, "lot": 100, "max_positions": 10, "max_per": 50000,
       "buy_fee": 0.0003, "sell_fee": 0.0003, "stamp": 0.0, "t_plus": 1}

for i, date_str in enumerate(days):
    day_data = price_data.get(date_str, {})
    state["locked_shares"] = {}
    state["daily_trades"] = []

    # Build context identical to runner
    pos_list = []
    pos_value = 0
    for sym, p in state["positions"].items():
        cur = day_data.get(sym, {}).get("price", p["entry_price"])
        mv = cur * p["quantity"]
        pnl_pct = (cur - p["entry_price"]) / p["entry_price"] if p["entry_price"] else 0
        pos_list.append({
            "symbol": sym, "quantity": p["quantity"],
            "entry_price": round(p["entry_price"], 2),
            "current_price": round(cur, 2),
            "pnl_pct": round(pnl_pct, 4),
            "market_value": round(mv, 2),
        })
        pos_value += mv

    held = set(state["positions"].keys())
    candidates = []
    for sym, info in sorted(day_data.items(), key=lambda x: x[1].get("volume", 0), reverse=True):
        if sym in held:
            continue
        if info.get("price", 0) <= 0:
            continue
        c = {"symbol": sym, "price": round(info["price"], 2)}
        candidates.append(c)
        if len(candidates) >= 20:
            break

    total_equity = state["cash"] + pos_value

    context = {
        "market": "A股",
        "date": date_str,
        "portfolio": {
            "total_equity": round(total_equity, 2),
            "cash": round(state["cash"], 2),
            "positions_value": round(pos_value, 2),
            "positions": pos_list,
        },
        "candidates": candidates,
        "constraints": {
            "max_positions": cfg["max_positions"],
            "max_per_position": cfg["max_per"],
            "t_plus": cfg["t_plus"],
            "lot_size": cfg["lot"],
        },
    }

    # Look up cache
    key = hashlib.md5(
        ("etf" + date_str + json.dumps(context, sort_keys=True, default=str)).encode()
    ).hexdigest()
    decisions = cache.get(key)

    if decisions is None:
        decisions = {"reasoning": "NOT_CACHED", "sells": [], "buys": [], "hold": []}

    # Execute sells
    for item in decisions.get("sells", []):
        sym = item["symbol"]
        if sym not in state["positions"]:
            continue
        p = state["positions"][sym]
        qty = p["quantity"]
        price = day_data.get(sym, {}).get("price", p["entry_price"])
        proceeds = qty * price
        fee = proceeds * cfg["sell_fee"]
        stamp = proceeds * cfg["stamp"]
        state["cash"] += proceeds - fee - stamp
        del state["positions"][sym]
        state["daily_trades"].append({
            "time": "09:30:00", "action": "sell", "symbol": sym,
            "quantity": qty, "price": round(price, 2),
            "reason": item.get("reason", ""),
        })

    # Execute buys
    current_count = len(state["positions"])
    for item in decisions.get("buys", []):
        sym = item["symbol"]
        if current_count >= cfg["max_positions"]:
            break
        if sym in state["positions"]:
            continue
        info = day_data.get(sym, {})
        price = info.get("price", 0)
        if price <= 0:
            continue

        qty_pct = max(10, min(100, int(item.get("quantity_percent", 100))))
        max_val = cfg["max_per"] * qty_pct / 100
        shares = int(max_val / price / cfg["lot"]) * cfg["lot"]
        if shares < cfg["lot"]:
            continue

        cost = shares * price
        fee = cost * cfg["buy_fee"]
        total = cost + fee
        if state["cash"] < total:
            shares = int((state["cash"] / (price * (1 + cfg["buy_fee"]))) / cfg["lot"]) * cfg["lot"]
            if shares < cfg["lot"]:
                continue
            cost = shares * price
            fee = cost * cfg["buy_fee"]
            total = cost + fee

        state["cash"] -= total
        state["positions"][sym] = {"quantity": shares, "entry_price": price}
        current_count += 1
        state["daily_trades"].append({
            "time": "09:30:01", "action": "buy", "symbol": sym,
            "quantity": shares, "price": round(price, 2),
            "reason": item.get("reason", ""),
        })

    # Snapshot
    pos_val = sum(
        day_data.get(s, {}).get("price", p["entry_price"]) * p["quantity"]
        for s, p in state["positions"].items()
    )
    snapshots.append({
        "date": date_str,
        "total": round(state["cash"] + pos_val, 2),
        "trades": len(state["daily_trades"]),
        "positions": len(state["positions"]),
        "cash": round(state["cash"], 2),
        "reasoning": decisions.get("reasoning", ""),
    })

# Report
cached_days = sum(1 for s in snapshots if s["reasoning"] != "NOT_CACHED")
print("=" * 60)
print(f"ETF Backtest Progress: {cached_days}/{len(days)} = {cached_days / len(days) * 100:.0f}%")
print(f"Current Equity: CNY {snapshots[-1]['total']:,.0f}")
print(f"Cash: CNY {snapshots[-1]['cash']:,.0f}")
print(f"Positions: {snapshots[-1]['positions']}")
print(f"Latest Date: {snapshots[-1]['date']}")
print("=" * 60)

# Recent 10 days
print("\nLast 10 Trading Days:")
for s in snapshots[-10:]:
    ret = (s["total"] - 200000) / 200000 * 100
    has_llm = "OK" if s["reasoning"] != "NOT_CACHED" else "??"
    print(f"  {s['date']}  [{has_llm}]  CNY {s['total']:>12,.0f}  {ret:+.2f}%  pos={s['positions']}  trades={s['trades']}")

# Position details
print("\nCurrent Positions:")
for sym, p in state["positions"].items():
    cur_p = p["entry_price"]
    for d in reversed(days):
        if d in price_data and sym in price_data[d]:
            cur_p = price_data[d][sym]["price"]
            break
    mv = cur_p * p["quantity"]
    pnl = (cur_p - p["entry_price"]) / p["entry_price"] * 100
    print(f"  {sym}  cost={p['entry_price']:.3f}  now={cur_p:.3f}  qty={p['quantity']}  val={mv:,.0f}  pnl={pnl:+.2f}%")

if not state["positions"]:
    print("  (empty)")

# Calculate metrics
values = [s["total"] for s in snapshots]
peak = values[0]
max_dd = 0.0
for v in values:
    if v > peak:
        peak = v
    dd = (v - peak) / peak if peak else 0
    if dd < max_dd:
        max_dd = dd

daily_rets = [(values[i] - values[i-1]) / values[i-1] if i > 0 and values[i-1] else 0
              for i in range(len(values))]
mean_ret = sum(daily_rets) / len(daily_rets) if daily_rets else 0
win_days = sum(1 for r in daily_rets if r > 0)
std_ret = (sum((r - mean_ret) ** 2 for r in daily_rets) / len(daily_rets)) ** 0.5 if len(daily_rets) > 1 else 0
sharpe = (mean_ret / std_ret) * (252 ** 0.5) if std_ret > 0 else 0

print(f"\nSharpe: {sharpe:.2f}  Max DD: {max_dd * 100:.1f}%  Win Rate: {win_days / len(daily_rets) * 100:.0f}%")
print(f"Total Return: {(values[-1] - 200000) / 200000 * 100:+.2f}%")

# Equity milestones
print("\nEquity Milestones:")
for s in snapshots:
    if len(snapshots) > 0:
        pass  # placeholder
# Show every 20th day
for i, s in enumerate(snapshots):
    if i % 20 == 0 or i == len(snapshots) - 1:
        ret = (s["total"] - 200000) / 200000 * 100
        print(f"  Day {i:3d}  {s['date']}  CNY {s['total']:>12,.0f}  {ret:+.2f}%")

# Recent trades
print("\nRecent Reasoning (last 5 cached days):")
for s in snapshots[-15:]:
    if s["reasoning"] != "NOT_CACHED" and len(s["reasoning"]) > 5:
        r = s["reasoning"][:200]
        print(f"  [{s['date']}] {r}")
