"""SQLite-based data loader for backtesting — reads from local market.db."""
import sqlite3
from datetime import datetime, timedelta

DB_PATH = "D:/ai-trader-data/market.db"


def fetch_trading_days(start: str, end: str, exchange: str = "SSE") -> list:
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT cal_date FROM trade_cal WHERE cal_date BETWEEN ? AND ? AND is_open=1 AND exchange=? ORDER BY cal_date",
        (start, end, exchange)
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


def load_price_data(agent_name: str, symbols: list, start_date: str, end_date: str) -> dict:
    """Load daily OHLCV from SQLite for entire range. Returns {date_str: {code: val_or_dict}}."""
    if not symbols:
        return {}

    conn = sqlite3.connect(DB_PATH)

    if agent_name == "ashare":
        ts_codes = [_sina_to_ts(s) for s in symbols]
        _load_ashare(conn, ts_codes, start_date, end_date)
    elif agent_name == "etf":
        ts_codes = [_etf_to_ts(s) for s in symbols]
        _load_etf(conn, ts_codes, start_date, end_date)
    elif agent_name == "bond":
        _load_bond(conn, symbols, start_date, end_date)
    else:
        _load_generic(conn, symbols, start_date, end_date)

    # Now read back from DB
    return _read_from_db(conn, symbols, start_date, end_date, agent_name)


def _read_from_db(conn, symbols, start_date, end_date, agent_name) -> dict:
    """Read pre-loaded data back into the format backtest expects."""
    result = {}
    codes = [_sina_to_ts(s) for s in symbols] if agent_name in ("ashare", "etf") else symbols

    placeholders = ",".join("?" * len(codes))
    rows = conn.execute(
        "SELECT ts_code, trade_date, open, high, low, close, pre_close, vol FROM daily "
        "WHERE ts_code IN ({}) AND trade_date BETWEEN ? AND ? ORDER BY trade_date".format(placeholders),
        codes + [start_date, end_date]
    ).fetchall()

    for row in rows:
        ts_code, date_str, o, h, l, c, pc, v = row
        code = _ts_to_sina(ts_code) if agent_name in ("ashare", "etf") else ts_code
        result.setdefault(date_str, {})[code] = {
            "price": c, "prev_close": pc, "open": o,
            "high": h, "low": l, "volume": v,
        }

    # Also load daily_basic for ashare
    if agent_name == "ashare":
        basic_rows = conn.execute(
            "SELECT ts_code, trade_date, pe, pb, roe, total_mv FROM daily_basic "
            "WHERE ts_code IN ({}) AND trade_date BETWEEN ? AND ?".format(placeholders),
            codes + [start_date, end_date]
        ).fetchall()
        for row in basic_rows:
            ts_code, date_str, pe, pb, roe, mv = row
            code = _ts_to_sina(ts_code)
            if date_str in result and code in result[date_str]:
                result[date_str][code].update({
                    "pe": pe or 0, "pb": pb or 0,
                    "roe": roe or 0, "total_mv": mv or 0,
                })

    conn.close()
    return result


def get_universe_symbols(agent_name: str) -> list:
    """Get candidate universe for backtesting from DB."""
    conn = sqlite3.connect(DB_PATH)
    if agent_name == "ashare":
        # HS300 constituents in sina format
        rows = conn.execute(
            "SELECT ts_code FROM stock_basic WHERE industry != '' AND delist_date = '' LIMIT 100"
        ).fetchall()
        symbols = [_ts_to_sina(r[0]) for r in rows]
    elif agent_name == "etf":
        symbols = ["sh510050", "sh510300", "sh510500", "sz159919", "sz159915",
                   "sh512880", "sh512100", "sh510880", "sh512010", "sz159949",
                   "sh512660", "sh516160", "sz159766", "sh159745", "sh516970",
                   "sh159611", "sh159819", "sh159995", "sh516390"]
    elif agent_name == "bond":
        rows = conn.execute(
            "SELECT ts_code FROM stock_basic WHERE ts_code LIKE '11%' OR ts_code LIKE '12%' LIMIT 30"
        ).fetchall()
        symbols = [r[0] for r in rows]
    else:
        symbols = []
    conn.close()
    return symbols


def _sina_to_ts(code: str) -> str:
    if code.startswith("sh"): return code[2:] + ".SH"
    if code.startswith("sz"): return code[2:] + ".SZ"
    return code

def _ts_to_sina(ts_code: str) -> str:
    if ts_code.endswith(".SH"): return "sh" + ts_code[:6]
    if ts_code.endswith(".SZ"): return "sz" + ts_code[:6]
    return ts_code

def _etf_to_ts(code: str) -> str:
    return _sina_to_ts(code)

# Stub functions for bond (not using DB for bonds yet)
def _load_ashare(conn, codes, start, end): pass
def _load_etf(conn, codes, start, end): pass
def _load_bond(conn, codes, start, end): pass
def _load_generic(conn, codes, start, end): pass
