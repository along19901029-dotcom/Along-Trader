"""Core backtest orchestrator: load agent module, patch time, loop through days."""
import importlib.util
import json
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import requests

from .config import AGENTS
from .data_loader import fetch_trading_days, load_price_data, get_universe_symbols
from .llm_cache import LLMCache
from .time_machine import TimeMachine

# DeepSeek API config (same as llm_client)
API_KEY = "your_deepseek_api_key_here"
BASE_URL = "https://api.deepseek.com/v1/chat/completions"
MODEL = "deepseek-v4-pro"


def _load_agent_module(agent_name: str):
    """Import the agent's trader.py as a module."""
    cfg = AGENTS[agent_name]
    trader_path = cfg["dir"] / "trader.py"
    if not trader_path.exists():
        raise FileNotFoundError("Agent not found: {}".format(trader_path))

    # Add agent dir and common dir to path
    agent_dir = str(cfg["dir"])
    if agent_dir not in sys.path:
        sys.path.insert(0, agent_dir)
    common_dir = str(cfg["common_dir"])
    if common_dir not in sys.path:
        sys.path.insert(0, common_dir)

    spec = importlib.util.spec_from_file_location(
        "agent_{}_bt".format(agent_name), str(trader_path)
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _call_llm(system_prompt: str, user_msg: dict) -> Optional[dict]:
    """Call DeepSeek API directly (same as llm_client.deepseek_ask)."""
    headers = {
        "Authorization": "Bearer " + API_KEY,
        "Content-Type": "application/json",
    }
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(user_msg, ensure_ascii=False, default=str)},
        ],
        "max_tokens": 4096,
        "temperature": 0.3,
        "response_format": {"type": "json_object"},
    }
    for attempt in range(3):
        try:
            resp = requests.post(BASE_URL, headers=headers, json=payload, timeout=90)
            resp.raise_for_status()
            body = resp.json()
            content = body["choices"][0]["message"]["content"]
            return json.loads(content)
        except Exception:
            if attempt < 2:
                import time
                time.sleep(2 ** attempt)
    return None


def _simulate_one_day(module, agent_name: str, cfg: dict, date_str: str,
                      state: dict, price_data: dict, llm_cache: LLMCache) -> dict:
    """Simulate one trading day: populate cache, call LLM, execute decisions."""
    # 1. Populate _price_cache from pre-loaded data
    day_data = price_data.get(date_str, {})
    module._price_cache.clear()
    module._PREV_CLOSE_CACHE.clear() if hasattr(module, "_PREV_CLOSE_CACHE") else None
    module._price_date = date_str
    for sym, val in day_data.items():
        if cfg["price_cache_scalar"]:
            module._price_cache[sym] = val
        else:
            # dict format: {price, prev_close, open, ...}
            entry = dict(val)
            if "prev_close" in entry:
                module._PREV_CLOSE_CACHE[sym] = entry["prev_close"]
            module._price_cache[sym] = entry

    # 3. Build LLM context
    context = module._build_llm_context(state)
    context["trading_context"] = {
        "data_date": date_str,
        "backtest_mode": True,
    }

    # 4. Get LLM decision (cached or fresh)
    decisions = llm_cache.get(agent_name, date_str, context)
    llm_fresh = False
    if decisions is None:
        decisions = _call_llm(module.LLM_SYSTEM_PROMPT, context)
        llm_fresh = True
        if decisions is not None:
            llm_cache.set(agent_name, date_str, context, decisions)
    if decisions is None:
        # LLM failed — hold everything
        decisions = {"reasoning": "LLM call failed", "sells": [], "buys": [], "hold": []}

    # 5. Execute sells
    for item in decisions.get("sells", []):
        sym = item["symbol"]
        if agent_name == "bond":
            strategy = item.get("strategy", "active")
            module.execute_sell(sym, item.get("reason", "signal"), strategy)
        else:
            module.execute_sell(sym, item.get("reason", "signal"))
        module.load_state()  # reload after each trade (agent saves stale state)

    # 6. Execute buys
    state = module.load_state()
    current_count = len(state.get("positions", {}))
    max_positions = cfg.get("max_positions", getattr(module, "MAX_POSITIONS", 10))

    for item in decisions.get("buys", []):
        sym = item["symbol"]
        if current_count >= max_positions:
            break
        price = module.fetch_price(sym)
        if not price or price <= 0:
            continue
        if sym in state.get("positions", {}):
            continue

        qty_pct = max(10, min(100, int(item.get("quantity_percent", 100))))
        orig_max = getattr(module, "MAX_POSITION_VALUE", 10000)
        if cfg["price_cache_scalar"]:
            adjusted = orig_max * qty_pct / 100
        else:
            # A-share/ETF/Bond: MAX_POSITION_VALUE is per-position cap
            adjusted = orig_max * qty_pct / 100

        if agent_name == "bond":
            strategy = item.get("strategy", "active")
            ok = module.execute_buy(sym, strategy)
        else:
            # Temporarily override MAX_POSITION_VALUE for quantity_percent
            module.MAX_POSITION_VALUE = adjusted
            ok = module.execute_buy(sym)
            module.MAX_POSITION_VALUE = orig_max

        if ok:
            current_count += 1
            state = module.load_state()  # reload to capture record_trade

    # 7. Record snapshot
    state = module.load_state()
    pos_value = _calc_holdings_value(state, module, cfg)
    total = state["cash"] + pos_value

    return {
        "date": date_str,
        "cash": round(state["cash"], 2),
        "positions_value": round(pos_value, 2),
        "total_value": round(total, 2),
        "positions": {k: dict(v) for k, v in state.get("positions", {}).items()},
        "trades": list(state.get("daily_trades", [])),
        "reasoning": decisions.get("reasoning", ""),
        "llm_fresh": llm_fresh,
    }


def _calc_holdings_value(state: dict, module, cfg: dict) -> float:
    """Calculate total market value of positions."""
    total = 0.0
    for sym, pos in state.get("positions", {}).items():
        price = module.fetch_price(sym)
        if price is None:
            price = pos.get("entry_price", 0)
        elif isinstance(price, dict):
            price = price.get("price", pos.get("entry_price", 0))
        total += price * pos.get("quantity", 0)
    # Bond repos
    for repo in state.get("repos", []):
        total += repo.get("amount", 0)
    return total


def run_backtest(agent_name: str, start_date: str, end_date: str,
                 use_llm: bool = True, cache_dir: str = "backtest_cache",
                 verbose: bool = True) -> dict:
    """Run a full backtest for one agent.

    Args:
        agent_name: 'us', 'ashare', 'etf', or 'bond'
        start_date: 'YYYYMMDD'
        end_date: 'YYYYMMDD'
        use_llm: if False, skip LLM and use cache only
        cache_dir: directory for LLM cache
        verbose: print progress

    Returns:
        {"snapshots": [...], "metrics": {...}, "trades": int, "llm_calls": int}
    """
    cfg = AGENTS[agent_name]
    if verbose:
        print("Loading agent module: {}".format(agent_name))

    module = _load_agent_module(agent_name)

    # Patch time globally for this backtest session
    tm = TimeMachine(module)
    tm.__enter__()

    # Point STATE_FILE to temp file
    tmp_state = Path(tempfile.mktemp(suffix=".json", prefix="bt_state_"))
    module.STATE_FILE = tmp_state

    # Initialize clean state
    state = {
        "positions": {},
        "cash": cfg["initial_capital"],
        "daily_trades": [],
        "daily_start_equity": cfg["initial_capital"],
        "report_sent_date": "",
        "stop_loss_hits": [],
        "take_profit_hits": [],
    }
    module.save_state(state)

    # Get trading days
    exchange = "SSE" if agent_name in ("ashare", "etf", "bond") else "NYSE"
    trading_days = fetch_trading_days(start_date, end_date, exchange)
    if not trading_days:
        # Fallback: all weekdays
        d = datetime.strptime(start_date, "%Y%m%d")
        end = datetime.strptime(end_date, "%Y%m%d")
        while d <= end:
            if d.weekday() < 5:
                trading_days.append(d.strftime("%Y%m%d"))
            d += timedelta(days=1)

    if verbose:
        print("Trading days: {}".format(len(trading_days)))

    # Pre-load price data
    universe = get_universe_symbols(agent_name)
    if verbose:
        print("Loading price data for {} symbols...".format(len(universe)))
    price_data = load_price_data(agent_name, universe, start_date, end_date)
    if verbose:
        print("Got price data for {} dates".format(len(price_data)))

    # LLM cache
    llm_cache = LLMCache(cache_dir)
    if verbose:
        cached, _ = llm_cache.stats()
        print("LLM cache entries: {}".format(cached))

    # Reset module globals
    if hasattr(module, "_universe_cache"):
        module._universe_cache = universe
    if hasattr(module, "_universe_date"):
        module._universe_date = ""

    snapshots = []
    total_llm_calls = 0

    for i, date_str in enumerate(trading_days):
        # Set time to market open
        dt = datetime.strptime(date_str, "%Y%m%d")
        open_time = dt.replace(hour=cfg["start_time"][0], minute=cfg["start_time"][1])
        tm.set(open_time)

        # Daily state reset
        if hasattr(module, "_ensure_daily_state"):
            module._ensure_daily_state(state)
        elif hasattr(module, "ensure_daily_state"):
            module.ensure_daily_state(state)
        module.save_state(state)

        snap = _simulate_one_day(
            module, agent_name, cfg, date_str,
            module.load_state(), price_data, llm_cache
        )

        if snap.get("llm_fresh"):
            total_llm_calls += 1

        snapshots.append(snap)
        state = module.load_state()

        if verbose and (i + 1) % 50 == 0:
            equity = snap["total_value"]
            pnl = equity - cfg["initial_capital"]
            print("  Day {:3d}/{}: {}  equity={:,.0f}  pnl={:+,.0f}  positions={}".format(
                i + 1, len(trading_days), date_str, equity, pnl,
                len(snap["positions"])))

    # Unpatch
    tm.__exit__()
    tmp_state.unlink(missing_ok=True)

    # Calc metrics
    from .metrics import compute_metrics
    metrics = compute_metrics(snapshots, cfg["initial_capital"])

    if verbose:
        print("\nBacktest complete: {} days, {} trades, {} LLM calls".format(
            len(snapshots), metrics.get("total_trades", 0), total_llm_calls))

    return {
        "agent": agent_name,
        "snapshots": snapshots,
        "metrics": metrics,
        "total_trades": metrics.get("total_trades", 0),
        "llm_calls": total_llm_calls,
    }
