"""Agent-specific configuration for backtesting.

Each agent has: agent dir, trader.py path, initial capital, fee structure,
lot size, T+ rules, and key function names.
"""

from pathlib import Path

ROOT = Path(__file__).parent.parent

AGENTS = {
    "us": {
        "dir": ROOT / "agent",
        "common_dir": ROOT / "agent_common",
        "initial_capital": 100000.0,
        "currency": "USD",
        "lot_size": 1,
        "t_plus": 0,
        "tushare_func": "us_daily",
        "price_cache_scalar": True,   # _price_cache[sym] = float
        "buy_extra_args": 0,          # execute_buy(sym) only
        "commission_rate": 0.001,     # 0.1%
        "stamp_tax_rate": 0.0,
        "min_commission": 1.0,
        "start_time": (9, 30),        # market open ET
        "end_time": (16, 0),          # market close ET
        "cooling_minutes": 5,
        "timezone": "US/Eastern",
    },
    "ashare": {
        "dir": ROOT / "agent-a-share",
        "common_dir": ROOT / "agent_common",
        "initial_capital": 200000.0,
        "currency": "CNY",
        "lot_size": 100,
        "t_plus": 1,
        "tushare_func": "daily",
        "price_cache_scalar": False,  # _price_cache[code] = dict
        "buy_extra_args": 0,          # execute_buy(code) only
        "commission_rate": 0.0003,    # 0.03%
        "stamp_tax_rate": 0.001,      # 0.1% sell only
        "min_commission": 0.0,
        "start_time": (9, 30),        # morning session
        "end_time": (15, 0),          # afternoon close
        "lunch_start": (11, 30),
        "lunch_end": (13, 0),
        "cooling_minutes": 5,
        "timezone": "Asia/Shanghai",
    },
    "etf": {
        "dir": ROOT / "agent-etf",
        "common_dir": ROOT / "agent_common",
        "initial_capital": 200000.0,
        "currency": "CNY",
        "lot_size": 100,
        "t_plus": 1,
        "tushare_func": "fund_daily",
        "price_cache_scalar": False,
        "buy_extra_args": 0,
        "commission_rate": 0.0003,
        "stamp_tax_rate": 0.0,       # ETF no stamp tax
        "min_commission": 0.0,
        "start_time": (9, 30),
        "end_time": (15, 0),
        "lunch_start": (11, 30),
        "lunch_end": (13, 0),
        "cooling_minutes": 5,
        "timezone": "Asia/Shanghai",
    },
    "bond": {
        "dir": ROOT / "agent-bond",
        "common_dir": ROOT / "agent_common",
        "initial_capital": 200000.0,
        "currency": "CNY",
        "lot_size": 10,
        "t_plus": 0,
        "tushare_func": "cb_daily",
        "price_cache_scalar": False,
        "buy_extra_args": 1,          # execute_buy(code, strategy)
        "commission_rate": 0.0002,    # 0.02%
        "stamp_tax_rate": 0.0,
        "min_commission": 0.0,
        "start_time": (9, 30),
        "end_time": (15, 0),
        "lunch_start": (11, 30),
        "lunch_end": (13, 0),
        "cooling_minutes": 5,
        "timezone": "Asia/Shanghai",
    },
}
