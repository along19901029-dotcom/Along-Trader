"""Pre-load historical price data from Tushare for backtesting."""
import time
from datetime import datetime, timedelta

import tushare as ts

TUSHARE_TOKEN = "your_tushare_token_here"
pro = ts.pro_api(TUSHARE_TOKEN)


def fetch_trading_days(start: str, end: str, exchange: str = "SSE") -> list:
    """Get list of trading days between start and end (YYYYMMDD format)."""
    cal = pro.trade_cal(exchange=exchange, start_date=start, end_date=end, is_open="1")
    if cal is None or len(cal) == 0:
        # fallback: use NYSE for us agent
        cal = pro.trade_cal(exchange="NYSE", start_date=start, end_date=end, is_open="1")
    if cal is None or len(cal) == 0:
        return []
    return sorted(cal["cal_date"].tolist())


def load_price_data(agent_name: str, symbols: list, start_date: str, end_date: str) -> dict:
    """Pre-load daily prices for entire backtest range.

    Returns: {date_str: {symbol: price_or_dict}}
    """
    if not symbols:
        return {}

    all_data = {}
    batch_size = 50
    end_dt = datetime.strptime(end_date, "%Y%m%d")
    start_dt = datetime.strptime(start_date, "%Y%m%d")

    if agent_name == "us":
        _load_us(symbols, start_date, end_date, all_data)
    elif agent_name == "bond":
        _load_bond(symbols, start_date, end_date, all_data)
    else:
        _load_batch(agent_name, symbols, start_date, end_date, all_data)

    return all_data


def _load_us(symbols, start_date, end_date, all_data):
    """US stocks: us_daily batch query, scalar cache."""
    for i in range(0, len(symbols), 50):
        batch = symbols[i:i + 50]
        codes = ",".join(batch)
        df = pro.us_daily(ts_code=codes, start_date=start_date, end_date=end_date,
                          fields="ts_code,trade_date,close")
        if df is None or len(df) == 0:
            continue
        for _, row in df.iterrows():
            sym = row["ts_code"].replace(".US", "").upper()
            date_str = str(row["trade_date"])
            close = float(row["close"])
            all_data.setdefault(date_str, {})[sym] = close
        time.sleep(0.6)


def _load_batch(agent_name, symbols, start_date, end_date, all_data):
    """A-share/ETF: daily/fund_daily batch query, dict cache."""
    func_map = {
        "ashare": pro.daily,
        "etf": pro.fund_daily,
    }
    fn = func_map[agent_name]
    for i in range(0, len(symbols), 50):
        batch = symbols[i:i + 50]
        codes = ",".join(batch)
        df = fn(ts_code=codes, start_date=start_date, end_date=end_date,
                fields="ts_code,trade_date,open,high,low,close,pre_close,vol")
        if df is None or len(df) == 0:
            continue
        for _, row in df.iterrows():
            ts_code = row["ts_code"]
            # Convert ts_code to agent's internal format
            if agent_name == "ashare":
                code = _ts_to_sina(ts_code)
            else:
                code = _ts_to_etf(ts_code)
            date_str = str(row["trade_date"])
            all_data.setdefault(date_str, {})[code] = {
                "price": float(row["close"]),
                "prev_close": float(row["pre_close"]),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "volume": float(row["vol"]),
            }
        time.sleep(0.6)


def _load_bond(symbols, start_date, end_date, all_data):
    """Convertible bonds: cb_daily single-symbol only, loop with delay."""
    for ts_code in symbols:
        df = pro.cb_daily(ts_code=ts_code, start_date=start_date, end_date=end_date,
                          fields="ts_code,trade_date,close,pre_close,vol,amount")
        if df is None or len(df) == 0:
            continue
        code = _ts_to_bond(ts_code)
        for _, row in df.iterrows():
            date_str = str(row["trade_date"])
            all_data.setdefault(date_str, {})[code] = {
                "price": float(row["close"]),
                "prev_close": float(row["pre_close"]),
                "volume": float(row["vol"]),
            }
        time.sleep(0.6)


def _ts_to_sina(ts_code: str) -> str:
    """600519.SH -> sh600519, 000001.SZ -> sz000001"""
    if ts_code.endswith(".SH"):
        return "sh" + ts_code[:6]
    elif ts_code.endswith(".SZ"):
        return "sz" + ts_code[:6]
    return ts_code


def _ts_to_etf(ts_code: str) -> str:
    """510050.SH -> sh510050"""
    if ts_code.endswith(".SH"):
        return "sh" + ts_code[:6]
    elif ts_code.endswith(".SZ"):
        return "sz" + ts_code[:6]
    return ts_code


def _ts_to_bond(ts_code: str) -> str:
    """110092.SH -> sh110092"""
    if ts_code.endswith(".SH"):
        return "sh" + ts_code[:6]
    elif ts_code.endswith(".SZ"):
        return "sz" + ts_code[:6]
    return ts_code


def get_universe_symbols(agent_name: str) -> list:
    """Get candidate universe symbols for backtesting (static lists, no live API)."""
    if agent_name == "us":
        import json
        from backtest.config import AGENTS as _BT_AGENTS
        sp500_file = _BT_AGENTS["us"]["dir"] / "sp500_static.json"
        if sp500_file.exists():
            return json.loads(sp500_file.read_text())
        # Fallback: top 50
        return ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "GOOG", "TSLA",
                "JPM", "V", "UNH", "XOM", "WMT", "JNJ", "MA", "PG", "HD", "CVX",
                "BAC", "LLY", "PFE", "ABBV", "KO", "PEP", "TMO", "AVGO", "COST",
                "MRK", "ABT", "DHR", "ORCL", "NFLX", "ADBE", "CRM", "CSCO", "ACN",
                "CMCSA", "VZ", "NKE", "TXN", "QCOM", "AMD", "INTC", "IBM", "HON",
                "PM", "LIN", "GE", "CAT", "BA"]

    elif agent_name == "ashare":
        # HS300 top 50 in sina format
        return ["sh600519", "sh601318", "sz000858", "sh600036", "sh601166",
                "sz000333", "sh600276", "sz002415", "sh600900", "sh601888",
                "sz000001", "sz000002", "sh600030", "sh601012", "sh600887",
                "sz300750", "sz002594", "sh601398", "sh600809", "sz000651"]

    elif agent_name == "etf":
        return ["sh510050", "sh510300", "sh510500", "sz159919", "sz159915",
                "sh512880", "sh512100", "sh510880", "sh512010", "sz159949"]

    elif agent_name == "bond":
        return ["sh110059", "sh113011", "sh113013", "sh113016", "sh113021",
                "sh113024", "sh113030", "sh113033", "sh113037", "sh113044"]

    return []
