"""Simple SQLite-based backtest for domestic agents. No module loading needed."""
import sqlite3, json, time, hashlib
from datetime import datetime, timedelta
from pathlib import Path

import requests

DB_PATH = "D:/ai-trader-data/market.db"
API_KEY = "your_deepseek_api_key_here"
BASE_URL = "https://api.deepseek.com/v1/chat/completions"
MODEL = "deepseek-v4-flash"
CACHE_DIR = Path(__file__).parent.parent / "backtest_cache"
CACHE_DIR.mkdir(exist_ok=True)

# ── Agent configs ──
CONFIGS = {
    "ashare": {
        "initial": 200000, "currency": "CNY", "lot": 100,
        "buy_fee": 0.0003, "sell_fee": 0.0003, "stamp": 0.001,
        "max_positions": 10, "max_per": 50000,
        "t_plus": 1, "system_prompt": "ashare",
    },
    "etf": {
        "initial": 200000, "currency": "CNY", "lot": 100,
        "buy_fee": 0.0003, "sell_fee": 0.0003, "stamp": 0.0,
        "max_positions": 10, "max_per": 50000,
        "t_plus": 1, "system_prompt": "etf",
    },
    "bond": {
        "initial": 200000, "currency": "CNY", "lot": 10,
        "buy_fee": 0.0002, "sell_fee": 0.0002, "stamp": 0.0,
        "max_positions": 10, "max_per": 40000,
        "t_plus": 0, "system_prompt": "bond",
    },
    "combine": {
        "initial": 200000, "currency": "CNY", "lot": 100,
        "buy_fee": 0.0003, "sell_fee": 0.0003, "stamp": 0.0,
        "max_positions": 3, "max_per": 200000,
        "t_plus": 1, "system_prompt": "combine",
    },
}

# Permanent Portfolio assets for combine agent
PP_ASSETS = {
    "stock":  {"code": "510300.SH", "name": "沪深300ETF", "target": 0.25},
    "bond":   {"code": "511010.SH", "name": "国债ETF",    "target": 0.25},
    "gold":   {"code": "518880.SH", "name": "黄金ETF",    "target": 0.25},
    "cash":   {"code": "511880.SH",  "name": "银华日利",   "target": 0.25},
}
PP_REBALANCE_UPPER = 0.30
PP_REBALANCE_LOWER = 0.20
PP_FORCE_DAYS = 90

SUNTZU = """
## 投资兵法
孙子曰：先为不可胜，以待敌之可胜。你是战场上的将军，资金是你的兵。
一曰先守后攻：首重保本，单笔亏损不超过5%。仓促出兵，十战九败。
二曰胜而后战：趋势+估值+风向三者缺一，按兵不动。1-3%波动乃噪音，非战之机。
三曰兵贵神速：盈利超8%而不收兵，是为贪。市场转向如敌军突袭，速退勿疑。
善战者之胜也，无智名，无勇功——稳，即是赢。"""

PLAN155 = """
## 宏观背景：十五五规划（2026-2030）
国家核心战略：以科技创新引领新质生产力。三大主线：
1. 科技自立自强：集成电路、AI芯片、算力基础设施、AI+行动全面铺开
2. 绿色低碳转型：碳达峰冲刺，电气化率目标28.9%，锂电池/储能核心受益
3. 新质生产力：六大新兴支柱产业（集成电路/航空航天/生物医药/低空经济/新型储能/智能机器人）
确定性方向：算力/AI基础设施(超长期国债支持)、城市更新(15-20万亿市场)、电气化产业链
风险提示：警惕概念炒作，区分真受益与蹭热点。决策时自问：是否属于政策主线？资金是否已到位？估值是否已反映预期？"""

SYSTEM_PROMPTS = {
    "ashare": """你是A股AI交易员，管理沪深300成分股组合。T+1，佣金万三，印花税千一(卖)。100股/手。""" + SUNTZU + PLAN155 + """
输出JSON: {"reasoning":"...", "sells":[{"symbol":"sh600519","reason":"止盈"}], "buys":[{"symbol":"sz000001","reason":"突破","quantity_percent":80}], "hold":["sh000001"]}""",

    "etf": """你是ETF AI交易员，管理A股股票型/指数型ETF组合。T+1，佣金万三，免印花税。100份/手。""" + SUNTZU + PLAN155 + """
行业ETF映射：算力/AI:159819(人工智能ETF),159995(芯片ETF)；低空经济/军工:512660,512670；新能源/储能:516160,159766；城市更新/基建:159745,516970
输出JSON: {"reasoning":"...", "sells":[...], "buys":[...], "hold":[...]}""",

    "bond": """你是可转债AI交易员。T+0，佣金万二，免印花税。10张/手。""" + SUNTZU + """
十五五债市影响：超长期国债大规模发行，利率债供给增加关注久期风险；城市更新/新基建企业转债优先关注；电气化/新能源产业链转债政策确定性高；央国企转债有改革+估值重塑催化。
输出JSON: {"reasoning":"...", "sells":[{"symbol":"sh113013","reason":"止盈"}], "buys":[{"symbol":"sh110059","reason":"YTM高","quantity_percent":80}], "hold":[...]}""",
}


def load_trading_days(start, end):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT cal_date FROM trade_cal WHERE cal_date BETWEEN ? AND ? AND is_open=1 AND exchange='SSE' ORDER BY cal_date",
        (start, end)).fetchall()
    conn.close()
    return [r[0] for r in rows]


def get_price_data(agent, start_date, end_date):
    """Load daily OHLCV + basic from SQLite."""
    conn = sqlite3.connect(DB_PATH)
    result = {}

    if agent == "ashare":
        table = "daily"
        sz = [r[0] for r in conn.execute(
            "SELECT DISTINCT ts_code FROM {} WHERE trade_date BETWEEN ? AND ? AND ts_code LIKE '%.SZ' ORDER BY ts_code LIMIT 100".format(table),
            (start_date, end_date)).fetchall()]
        sh = [r[0] for r in conn.execute(
            "SELECT DISTINCT ts_code FROM {} WHERE trade_date BETWEEN ? AND ? AND ts_code LIKE '%.SH' ORDER BY ts_code LIMIT 100".format(table),
            (start_date, end_date)).fetchall()]
        codes = sz + sh
    elif agent == "etf":
        table = "etf_daily"
        codes = [r[0] for r in conn.execute(
            "SELECT DISTINCT ts_code FROM {} WHERE trade_date BETWEEN ? AND ? ORDER BY ts_code LIMIT 30".format(table),
            (start_date, end_date)).fetchall()]
    else:  # bond
        table = "bond_daily"
        codes = [r[0] for r in conn.execute(
            "SELECT DISTINCT ts_code FROM {} WHERE trade_date BETWEEN ? AND ? ORDER BY ts_code LIMIT 30".format(table),
            (start_date, end_date)).fetchall()]

    for code in codes:
        rows = conn.execute(
            "SELECT trade_date, open, high, low, close, pre_close, vol FROM {} WHERE ts_code=? AND trade_date BETWEEN ? AND ? ORDER BY trade_date".format(table),
            (code, start_date, end_date)).fetchall()
        for row in rows:
            date_str, o, h, l, c, pc, v = row
            if code.endswith(".SH"):
                sym = "sh" + code[:6]
            else:
                sym = "sz" + code[:6]
            result.setdefault(date_str, {})[sym] = {
                "price": c, "prev_close": pc, "open": o, "high": h, "low": l, "volume": v,
            }

    # Add PE/PB for ashare
    if agent == "ashare":
        for code in codes[:100]:
            rows = conn.execute(
                "SELECT trade_date, pe, pb, roe, total_mv FROM daily_basic WHERE ts_code=? AND trade_date BETWEEN ? AND ?",
                (code, start_date, end_date)).fetchall()
            for row in rows:
                date_str, pe, pb, roe, mv = row
                sym = "sh" + code[:6] if code.endswith(".SH") else "sz" + code[:6]
                if date_str in result and sym in result[date_str]:
                    result[date_str][sym].update({
                        "pe": pe or 0, "pb": pb or 0, "roe": roe or 0, "total_mv": mv or 0,
                    })

    conn.close()
    return result


def load_financial_lookup(codes, end_date):
    """Build a lookup: {ts_code: [(ann_date, indicators), ...]} sorted newest first."""
    conn = sqlite3.connect(DB_PATH)
    lookup = {}
    for code in codes:
        rows = conn.execute(
            "SELECT ann_date, roe, debt_to_assets, or_yoy, netprofit_yoy, eps "
            "FROM fin_indicator WHERE ts_code=? AND ann_date <= ? "
            "ORDER BY ann_date DESC",
            (code, end_date)
        ).fetchall()
        if rows:
            lookup[code] = [
                {"ann_date": r[0], "roe": r[1], "debt_to_assets": r[2],
                 "rev_growth": r[3], "profit_growth": r[4], "eps": r[5]}
                for r in rows
            ]
    conn.close()
    return lookup


def get_financial(ts_code, trade_date, lookup):
    """Return latest financial indicators for a stock on trade_date, or empty dict."""
    reports = lookup.get(ts_code, [])
    for r in reports:
        if r["ann_date"] <= trade_date:
            result = {}
            if r.get("rev_growth") is not None:
                result["rev_growth"] = round(r["rev_growth"], 1)
            if r.get("profit_growth") is not None:
                result["profit_growth"] = round(r["profit_growth"], 1)
            if r.get("debt_to_assets") is not None:
                result["debt_ratio"] = round(r["debt_to_assets"], 1)
            return result
    return {}
    return result


def call_llm(system_prompt, context, agent_name, date_str):
    cache_file = CACHE_DIR / "sqlite_backtest.json"
    cache = {}
    if cache_file.exists():
        try:
            cache = json.loads(cache_file.read_text())
        except (json.JSONDecodeError, FileNotFoundError):
            print("Cache corrupted, starting fresh")
            cache = {}
            cache_file.write_text("{}")

    key = hashlib.md5((agent_name + date_str + json.dumps(context, sort_keys=True, default=str)).encode()).hexdigest()
    if key in cache:
        cached = cache[key]
        if isinstance(cached, dict):
            return cached

    headers = {"Authorization": "Bearer " + API_KEY, "Content-Type": "application/json"}
    payload = {
        "model": MODEL, "max_tokens": 4096, "temperature": 0.3,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(context, ensure_ascii=False, default=str)},
        ],
    }
    for attempt in range(3):
        try:
            resp = requests.post(BASE_URL, headers=headers, json=payload, timeout=90)
            resp.raise_for_status()
            body = resp.json()
            if not isinstance(body, dict):
                raise ValueError("non-dict response")
            content = body.get("choices", [{}])[0].get("message", {}).get("content", "")
            if not content or not content.strip():
                raise ValueError("empty content")
            result = json.loads(content)
            if not isinstance(result, dict):
                result = {"reasoning": str(result), "sells": [], "buys": [], "hold": []}
            cache[key] = result
            cache_file.write_text(json.dumps(cache, ensure_ascii=False))
            return result
        except Exception:
            if attempt < 2:
                time.sleep(2 ** attempt)
    cache_file.write_text(json.dumps(cache, ensure_ascii=False))
    return None


def run_backtest(agent_name, start_date, end_date, verbose=True):
    cfg = CONFIGS[agent_name]
    trading_days = load_trading_days(start_date, end_date)
    if not trading_days:
        print("No trading days found")
        return

    print("Loading price data...")
    price_data = get_price_data(agent_name, start_date, end_date)
    print("  {} dates, {} symbols max/date".format(len(price_data),
          max(len(v) for v in price_data.values()) if price_data else 0))

    # Pre-load financial indicators for A-share
    fin_lookup = {}
    if agent_name == "ashare":
        # Collect all ts_codes that appear in price_data
        all_codes = set()
        for day_vals in price_data.values():
            for sym in day_vals:
                ts_code = sym[2:] + (".SH" if sym.startswith("sh") else ".SZ")
                all_codes.add(ts_code)
        fin_lookup = load_financial_lookup(list(all_codes), end_date)
        if fin_lookup:
            print("  Financial data loaded for {} stocks".format(len(fin_lookup)))

    state = {"positions": {}, "cash": cfg["initial"], "locked_shares": {}, "daily_trades": []}
    snapshots = []
    total_llm_calls = 0
    total_trades = 0

    from tqdm import tqdm

    pbar = tqdm(total=len(trading_days), desc=agent_name.upper(), unit="day", ncols=80)
    for i, date_str in enumerate(trading_days):
        day_data = price_data.get(date_str, {})
        if not day_data:
            snapshots.append({"date": date_str, "total": cfg["initial"] + sum(
                p["quantity"] * p["entry_price"] for p in state["positions"].values()
            ), "trades": 0})
            continue

        # Clear T+1 locks
        state["locked_shares"] = {}
        state["daily_trades"] = []

        # Build positions context
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

        # Build candidates (top 20 by volume, excluding positions)
        held = set(state["positions"].keys())
        candidates = []
        for sym, info in sorted(day_data.items(), key=lambda x: (-x[1].get("volume", 0), x[0])):
            if sym in held:
                continue
            if info.get("price", 0) <= 0:
                continue
            c = {"symbol": sym, "price": round(info["price"], 2)}
            if "pe" in info:
                c["pe"] = round(info["pe"], 1)
            if "pb" in info:
                c["pb"] = round(info["pb"], 1)
            if "roe" in info:
                c["roe"] = round(info["roe"], 1)
            # Add financial indicators if available (A-share only)
            if fin_lookup:
                ts_code = sym[2:] + (".SH" if sym.startswith("sh") else ".SZ")
                f = get_financial(ts_code, date_str, fin_lookup)
                if f:
                    c.update(f)
            candidates.append(c)
            if len(candidates) >= 20:
                break

        total_equity = state["cash"] + pos_value

        context = {
            "market": "A股" if agent_name != "bond" else "可转债",
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

        decisions = call_llm(SYSTEM_PROMPTS[agent_name], context, agent_name, date_str)
        total_llm_calls += 1

        if decisions is None:
            decisions = {"reasoning": "LLM failed", "sells": [], "buys": [], "hold": []}

        # Normalize: LLM sometimes returns string instead of {symbol,reason} dict
        sells = []
        for item in decisions.get("sells", []):
            if isinstance(item, str):
                sells.append({"symbol": item, "reason": ""})
            else:
                sells.append(item)
        buys = []
        for item in decisions.get("buys", []):
            if isinstance(item, str):
                buys.append({"symbol": item, "reason": "", "quantity_percent": 80})
            else:
                buys.append(item)
        decisions = {**decisions, "sells": sells, "buys": buys}

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
                "quantity": qty, "price": round(price, 2), "reason": item.get("reason", "signal"),
            })
            total_trades += 1

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
            if cfg["t_plus"] > 0:
                state["locked_shares"][sym] = state["locked_shares"].get(sym, 0) + shares
            state["daily_trades"].append({
                "time": "09:30:01", "action": "buy", "symbol": sym,
                "quantity": shares, "price": round(price, 2), "reason": item.get("reason", "signal"),
            })
            current_count += 1
            total_trades += 1

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

        pbar.update(1)
        pbar.set_postfix(equity="{:,.0f}".format(snapshots[-1]["total"]),
                         pos=len(state["positions"]),
                         trades=total_trades)

        if verbose and (i + 1) % 20 == 0:
            tqdm.write("  {:3d}/{}  {}  equity={:,.0f}  pos={}".format(
                i + 1, len(trading_days), date_str,
                snapshots[-1]["total"], len(state["positions"])))

    # Metrics
    values = [s["total"] for s in snapshots]
    final = values[-1] if values else cfg["initial"]
    total_ret = (final - cfg["initial"]) / cfg["initial"] * 100
    daily_rets = [(values[i] - values[i-1]) / values[i-1] if i > 0 and values[i-1] else 0 for i in range(len(values))]
    mean_ret = sum(daily_rets) / len(daily_rets) if daily_rets else 0
    std_ret = (sum((r - mean_ret)**2 for r in daily_rets) / len(daily_rets)) ** 0.5 if len(daily_rets) > 1 else 0
    sharpe = (mean_ret / std_ret) * (252 ** 0.5) if std_ret > 0 else 0

    peak = values[0] if values else cfg["initial"]
    max_dd = 0.0
    for v in values:
        if v > peak: peak = v
        dd = (v - peak) / peak if peak else 0
        if dd < max_dd: max_dd = dd

    win_days = sum(1 for r in daily_rets if r > 0)
    win_rate = win_days / len(daily_rets) * 100 if daily_rets else 0

    pbar.close()
    print()
    print("=" * 60)
    print("  {} 回测   {} ~ {}   ({} 天)".format(agent_name.upper(), trading_days[0], trading_days[-1], len(trading_days)))
    print("=" * 60)
    print("  Initial: {:,.0f}  Final: {:,.0f}  Return: {:+.1f}%".format(cfg["initial"], final, total_ret))
    print("  夏普:  {:.2f}    最大回撤: {:.1f}%    胜率: {:.0f}%".format(sharpe, max_dd * 100, win_rate))
    print("  交易:  {} 笔    LLM调用: {} 次".format(total_trades, total_llm_calls))
    print("=" * 60)

    return {"snapshots": snapshots, "final": final, "return": total_ret,
            "sharpe": sharpe, "max_dd": max_dd * 100, "win_rate": win_rate,
            "trades": total_trades, "llm_calls": total_llm_calls}


def run_combine(start_date, end_date):
    """Permanent Portfolio mechanical backtest — no LLM."""
    cfg = CONFIGS["combine"]
    trading_days = load_trading_days(start_date, end_date)
    if not trading_days:
        print("No trading days found")
        return

    # Load prices for the 3 PP ETFs
    conn = sqlite3.connect(DB_PATH)
    price_data = {}
    for key, asset in PP_ASSETS.items():
        code = asset["code"]
        if code is None:
            continue
        rows = conn.execute(
            "SELECT trade_date, close, pre_close FROM etf_daily WHERE ts_code=? AND trade_date BETWEEN ? AND ? ORDER BY trade_date",
            (code, start_date, end_date)
        ).fetchall()
        for row in rows:
            date_str, c, pc = row
            price_data.setdefault(date_str, {})[code] = {"price": c, "prev_close": pc}
    conn.close()
    print("  PP ETFs loaded: {} dates".format(len(price_data)))

    state = {"cash": cfg["initial"], "positions": {}, "last_rebalance": None, "rebalance_count": 0}
    snapshots = []
    total_trades = 0
    lot_size = cfg["lot"]

    from tqdm import tqdm
    pbar = tqdm(total=len(trading_days), desc="COMBINE", unit="day", ncols=80)

    for i, date_str in enumerate(trading_days):
        day_data = price_data.get(date_str, {})
        if not day_data:
            pos_val = sum(
                day_data.get(s.get("code", ""), {}).get("price", p["entry_price"]) * p["quantity"]
                if s else p["quantity"] * p["entry_price"]
                for s, p in [(PP_ASSETS.get(k), p) for k, p in state["positions"].items()]
                if isinstance(p, dict) and "quantity" in p
            ) if state["positions"] else 0
            # Simpler: just use last snapshot
            prev = snapshots[-1]["total"] if snapshots else cfg["initial"]
            snapshots.append({"date": date_str, "total": prev, "trades": 0, "positions": len(state["positions"])})
            pbar.update(1)
            continue

        # Calculate total equity and allocations
        pos_values = {}
        pos_value_total = 0
        for key, pos in state["positions"].items():
            asset = PP_ASSETS.get(key)
            if not asset or not asset["code"]:
                continue
            code = asset["code"]
            cur = day_data.get(code, {}).get("price", pos["entry_price"])
            val = cur * pos["quantity"]
            pos_values[key] = val
            pos_value_total += val

        total = state["cash"] + pos_value_total

        # First run: initial allocation
        if not state["positions"] and state["cash"] == cfg["initial"]:
            for key, asset in PP_ASSETS.items():
                code = asset["code"]
                if code is None:
                    continue
                price = day_data.get(code, {}).get("price")
                if not price:
                    continue
                target_val = total * asset["target"]
                shares = int(target_val / price / lot_size) * lot_size
                if shares < lot_size:
                    continue
                cost = shares * price
                fee = cost * cfg["buy_fee"]
                state["cash"] -= cost + fee
                state["positions"][key] = {"quantity": shares, "entry_price": price}
                state["daily_trades"] = [{
                    "time": "09:30:00", "action": "buy", "symbol": code,
                    "quantity": shares, "price": round(price, 2), "reason": "初始建仓",
                }]
                total_trades += 1
            state["last_rebalance"] = date_str

        # Check rebalance
        allocs = {}
        for key in PP_ASSETS:
            if key in state["positions"] and key in pos_values:
                allocs[key] = pos_values[key] / total if total > 0 else 0
            else:
                allocs[key] = 0

        needs_reb = False
        for key, pct in allocs.items():
            if pct > PP_REBALANCE_UPPER or pct < PP_REBALANCE_LOWER:
                needs_reb = True
                break

        # Force rebalance every 90 days
        if state.get("last_rebalance"):
            try:
                last = datetime.strptime(state["last_rebalance"], "%Y%m%d")
                if (datetime.strptime(date_str, "%Y%m%d") - last).days >= PP_FORCE_DAYS:
                    needs_reb = True
            except ValueError:
                pass

        state["daily_trades"] = []

        if needs_reb:
            for key, asset in PP_ASSETS.items():
                code = asset["code"]
                if code is None:
                    continue
                price = day_data.get(code, {}).get("price")
                if not price:
                    continue

                target_val = total * asset["target"]
                current_qty = state["positions"].get(key, {}).get("quantity", 0)
                current_val = current_qty * price
                diff = target_val - current_val

                if abs(diff) < price * lot_size:
                    continue

                shares = int(abs(diff) / price / lot_size) * lot_size
                if shares < lot_size:
                    continue

                if diff > 0:
                    cost = shares * price
                    fee = cost * cfg["buy_fee"]
                    if state["cash"] < cost + fee:
                        continue
                    state["cash"] -= cost + fee
                    if key not in state["positions"]:
                        state["positions"][key] = {"quantity": shares, "entry_price": price}
                    else:
                        old_qty = state["positions"][key]["quantity"]
                        old_entry = state["positions"][key]["entry_price"]
                        new_qty = old_qty + shares
                        state["positions"][key] = {
                            "quantity": new_qty,
                            "entry_price": (old_entry * old_qty + price * shares) / new_qty,
                        }
                    state["daily_trades"].append({
                        "time": "09:30:00", "action": "buy", "symbol": code,
                        "quantity": shares, "price": round(price, 2), "reason": "再平衡买入",
                    })
                    total_trades += 1
                else:
                    if current_qty < shares:
                        shares = current_qty - (current_qty % lot_size)
                        if shares < lot_size:
                            continue
                    proceeds = shares * price
                    fee = proceeds * cfg["sell_fee"]
                    state["cash"] += proceeds - fee
                    new_qty = state["positions"][key]["quantity"] - shares
                    if new_qty <= 0:
                        del state["positions"][key]
                    else:
                        state["positions"][key]["quantity"] = new_qty
                    state["daily_trades"].append({
                        "time": "09:30:00", "action": "sell", "symbol": code,
                        "quantity": shares, "price": round(price, 2), "reason": "再平衡卖出",
                    })
                    total_trades += 1

            state["rebalance_count"] = state.get("rebalance_count", 0) + 1
            state["last_rebalance"] = date_str

        # Snapshot
        snapshots.append({
            "date": date_str, "total": round(total, 2),
            "trades": len(state["daily_trades"]),
            "positions": len(state["positions"]),
            "cash": round(state["cash"], 2),
        })

        pbar.update(1)
        pbar.set_postfix(equity="{:,.0f}".format(snapshots[-1]["total"]),
                         pos=len(state["positions"]),
                         trades=total_trades)

        if (i + 1) % 40 == 0:
            tqdm.write("  {:3d}/{}  {}  equity={:,.0f}  reb={}  stock={:.0f}% bond={:.0f}% gold={:.0f}%".format(
                i + 1, len(trading_days), date_str, snapshots[-1]["total"],
                state.get("rebalance_count", 0),
                allocs.get("stock", 0) * 100, allocs.get("bond", 0) * 100, allocs.get("gold", 0) * 100))

    # Metrics
    values = [s["total"] for s in snapshots]
    final = values[-1] if values else cfg["initial"]
    total_ret = (final - cfg["initial"]) / cfg["initial"] * 100
    daily_rets = [(values[i] - values[i-1]) / values[i-1] if i > 0 and values[i-1] else 0 for i in range(len(values))]
    mean_ret = sum(daily_rets) / len(daily_rets) if daily_rets else 0
    std_ret = (sum((r - mean_ret)**2 for r in daily_rets) / len(daily_rets)) ** 0.5 if len(daily_rets) > 1 else 0
    sharpe = (mean_ret / std_ret) * (252 ** 0.5) if std_ret > 0 else 0

    peak = values[0] if values else cfg["initial"]
    max_dd = 0.0
    for v in values:
        if v > peak: peak = v
        dd = (v - peak) / peak if peak else 0
        if dd < max_dd: max_dd = dd

    win_days = sum(1 for r in daily_rets if r > 0)
    win_rate = win_days / len(daily_rets) * 100 if daily_rets else 0

    pbar.close()
    print()
    print("=" * 60)
    print("  PERMANENT PORTFOLIO 回测   {} ~ {}   ({} 天)".format(trading_days[0], trading_days[-1], len(trading_days)))
    print("=" * 60)
    print("  Initial: {:,.0f}  Final: {:,.0f}  Return: {:+.1f}%".format(cfg["initial"], final, total_ret))
    print("  夏普:  {:.2f}    最大回撤: {:.1f}%    胜率: {:.0f}%".format(sharpe, max_dd * 100, win_rate))
    print("  交易:  {} 笔    再平衡: {} 次".format(total_trades, state.get("rebalance_count", 0)))
    print("=" * 60)

    return {"snapshots": snapshots, "final": final, "return": total_ret,
            "sharpe": sharpe, "max_dd": max_dd * 100, "win_rate": win_rate,
            "trades": total_trades, "llm_calls": 0}


def run_backtest_benchmark(agent_name, start_date, end_date):
    """Like run_backtest but only allowing HS300 ETF (510300.SH) as candidate."""
    cfg = CONFIGS[agent_name]
    trading_days = load_trading_days(start_date, end_date)
    if not trading_days:
        print("No trading days found")
        return

    # Load only 510300 data
    conn = sqlite3.connect(DB_PATH)
    price_data = {}
    rows = conn.execute(
        "SELECT trade_date, open, high, low, close, pre_close, vol FROM etf_daily "
        "WHERE ts_code='510300.SH' AND trade_date BETWEEN ? AND ? ORDER BY trade_date",
        (start_date, end_date)
    ).fetchall()
    for row in rows:
        date_str, o, h, l, c, pc, v = row
        price_data.setdefault(date_str, {})["sh510300"] = {
            "price": c, "prev_close": pc, "open": o, "high": h, "low": l, "volume": v,
        }
    conn.close()
    print("  Benchmark mode: HS300 ETF only, {} dates".format(len(price_data)))

    state = {"positions": {}, "cash": cfg["initial"], "locked_shares": {}, "daily_trades": []}
    snapshots = []
    total_llm_calls = 0
    total_trades = 0

    from tqdm import tqdm
    pbar = tqdm(total=len(trading_days), desc=agent_name.upper() + "-BM", unit="day", ncols=80)

    for i, date_str in enumerate(trading_days):
        day_data = price_data.get(date_str, {})
        if not day_data:
            snapshots.append({"date": date_str, "total": cfg["initial"] + sum(
                p["quantity"] * p["entry_price"] for p in state["positions"].values()
            ), "trades": 0})
            continue

        state["locked_shares"] = {}
        state["daily_trades"] = []

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
        for sym, info in sorted(day_data.items(), key=lambda x: (-x[1].get("volume", 0), x[0])):
            if sym in held:
                continue
            if info.get("price", 0) <= 0:
                continue
            candidates.append({"symbol": sym, "price": round(info["price"], 2)})
            # Only one candidate: HS300 ETF

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
            "note": "你能交易的唯一标的是沪深300ETF(sh510300)。你的任务是在正确的时间买入和卖出它。买入持有或空仓观望，二选一。",
        }

        decisions = call_llm(SYSTEM_PROMPTS[agent_name], context, agent_name + "_bm", date_str)
        total_llm_calls += 1

        if decisions is None:
            decisions = {"reasoning": "LLM failed", "sells": [], "buys": [], "hold": []}

        sells = []
        for item in decisions.get("sells", []):
            if isinstance(item, str):
                sells.append({"symbol": item, "reason": ""})
            else:
                sells.append(item)
        buys = []
        for item in decisions.get("buys", []):
            if isinstance(item, str):
                buys.append({"symbol": item, "reason": "", "quantity_percent": 80})
            else:
                buys.append(item)
        decisions = {**decisions, "sells": sells, "buys": buys}

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
                "quantity": qty, "price": round(price, 2), "reason": item.get("reason", "signal"),
            })
            total_trades += 1

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
            if cfg["t_plus"] > 0:
                state["locked_shares"][sym] = state["locked_shares"].get(sym, 0) + shares
            state["daily_trades"].append({
                "time": "09:30:01", "action": "buy", "symbol": sym,
                "quantity": shares, "price": round(price, 2), "reason": item.get("reason", "signal"),
            })
            current_count += 1
            total_trades += 1

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

        pbar.update(1)
        pbar.set_postfix(equity="{:,.0f}".format(snapshots[-1]["total"]),
                         pos=len(state["positions"]),
                         trades=total_trades)

        if (i + 1) % 40 == 0:
            tqdm.write("  {:3d}/{}  {}  equity={:,.0f}  pos={}  {}".format(
                i + 1, len(trading_days), date_str,
                snapshots[-1]["total"], len(state["positions"]),
                decisions.get("reasoning", "")[:80]))

    values = [s["total"] for s in snapshots]
    final = values[-1] if values else cfg["initial"]
    total_ret = (final - cfg["initial"]) / cfg["initial"] * 100
    daily_rets = [(values[i] - values[i-1]) / values[i-1] if i > 0 and values[i-1] else 0 for i in range(len(values))]
    mean_ret = sum(daily_rets) / len(daily_rets) if daily_rets else 0
    std_ret = (sum((r - mean_ret)**2 for r in daily_rets) / len(daily_rets)) ** 0.5 if len(daily_rets) > 1 else 0
    sharpe = (mean_ret / std_ret) * (252 ** 0.5) if std_ret > 0 else 0

    peak = values[0] if values else cfg["initial"]
    max_dd = 0.0
    for v in values:
        if v > peak: peak = v
        dd = (v - peak) / peak if peak else 0
        if dd < max_dd: max_dd = dd

    win_days = sum(1 for r in daily_rets if r > 0)
    win_rate = win_days / len(daily_rets) * 100 if daily_rets else 0

    pbar.close()
    print()
    print("=" * 60)
    print("  {} BENCHMARK HS300 only  {} ~ {}   ({} 天)".format(agent_name.upper(), trading_days[0], trading_days[-1], len(trading_days)))
    print("=" * 60)
    print("  Initial: {:,.0f}  Final: {:,.0f}  Return: {:+.1f}%".format(cfg["initial"], final, total_ret))
    print("  夏普:  {:.2f}    最大回撤: {:.1f}%    胜率: {:.0f}%".format(sharpe, max_dd * 100, win_rate))
    print("  交易:  {} 笔    LLM调用: {} 次".format(total_trades, total_llm_calls))
    print("  HS300买入持有: +22.9%  最大回撤: -26.4%  (基准)")
    print("=" * 60)

    return {"snapshots": snapshots, "final": final, "return": total_ret,
            "sharpe": sharpe, "max_dd": max_dd * 100, "win_rate": win_rate,
            "trades": total_trades, "llm_calls": total_llm_calls}


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--agent", "-a", choices=["ashare", "etf", "bond", "combine"], default="ashare")
    p.add_argument("--start", default="20250919")
    p.add_argument("--end", default="20260602")
    p.add_argument("--benchmark", action="store_true", help="Only allow HS300 ETF (510300.SH)")
    args = p.parse_args()

    if args.agent == "combine":
        run_combine(args.start, args.end)
    else:
        if args.benchmark:
            run_backtest_benchmark(args.agent, args.start, args.end)
        else:
            run_backtest(args.agent, args.start, args.end)
