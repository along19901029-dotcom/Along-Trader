"""Load ETF and convertible bond daily data into SQLite."""
import sqlite3
import time
from datetime import datetime, timedelta

import tushare as ts

DB_PATH = "D:/ai-trader-data/market.db"
TUSHARE_TOKEN = "your_tushare_token_here"
pro = ts.pro_api(TUSHARE_TOKEN)


def init_tables(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS etf_basic (
            ts_code TEXT PRIMARY KEY, name TEXT, management TEXT,
            fund_type TEXT, benchmark TEXT, list_date TEXT
        );
        CREATE TABLE IF NOT EXISTS etf_daily (
            ts_code TEXT, trade_date TEXT, open REAL, high REAL, low REAL,
            close REAL, pre_close REAL, vol REAL,
            PRIMARY KEY (ts_code, trade_date)
        );
        CREATE TABLE IF NOT EXISTS bond_daily (
            ts_code TEXT, trade_date TEXT, open REAL, high REAL, low REAL,
            close REAL, pre_close REAL, vol REAL, amount REAL,
            PRIMARY KEY (ts_code, trade_date)
        );
        CREATE INDEX IF NOT EXISTS idx_etf_daily_date ON etf_daily(trade_date);
        CREATE INDEX IF NOT EXISTS idx_bond_daily_date ON bond_daily(trade_date);
    """)
    conn.commit()


def load_etf_data(conn, start_date, end_date):
    """Load top ETFs - fund_daily is single-symbol only."""
    # Get ETF list
    print("Loading ETF list...")
    df = pro.fund_basic(market="E", fields="ts_code,name,management,fund_type,benchmark,list_date")
    if df is None or len(df) == 0:
        print("  No ETFs found")
        return

    # Filter: stock/index ETFs, listed before 2025
    etfs = []
    for _, r in df.iterrows():
        code = r["ts_code"]
        list_d = str(r.get("list_date", ""))
        if list_d and list_d > "20250901":
            continue  # too new
        etfs.append(code)

    # Insert basic info
    rows = [(r["ts_code"], r["name"], r.get("management", ""),
             r.get("fund_type", ""), r.get("benchmark", ""),
             str(r.get("list_date", ""))) for _, r in df.iterrows()]
    conn.executemany("INSERT OR REPLACE INTO etf_basic VALUES (?,?,?,?,?,?)", rows)
    conn.commit()
    print("  {} ETFs in list, loading daily for top 40...".format(len(etfs)))

    # Pick top 40 by code (rough proxy for popularity: 51xxxx major ETFs)
    target = [c for c in etfs if c.startswith("51")][:20] + \
             [c for c in etfs if c.startswith("15")][:10] + \
             [c for c in etfs if c.startswith("56")][:10]

    total = 0
    for i, code in enumerate(target):
        try:
            df2 = pro.fund_daily(ts_code=code, start_date=start_date, end_date=end_date,
                                 fields="ts_code,trade_date,open,high,low,close,pre_close,vol")
            if df2 is None or len(df2) == 0:
                continue
            rows = [(r["ts_code"], str(r["trade_date"]),
                     float(r["open"]), float(r["high"]), float(r["low"]),
                     float(r["close"]), float(r["pre_close"]), float(r["vol"]))
                    for _, r in df2.iterrows()]
            conn.executemany("INSERT OR REPLACE INTO etf_daily VALUES (?,?,?,?,?,?,?,?)", rows)
            conn.commit()
            total += len(rows)
            if (i + 1) % 10 == 0:
                print("  ETF {}/{}: {} rows".format(i + 1, len(target), total))
        except Exception as e:
            print("  ETF {} error: {}".format(code, str(e)[:50]))
        time.sleep(0.5)
    print("  ETF total: {} rows".format(total))


def load_bond_data(conn, start_date, end_date):
    """Load convertible bonds - cb_daily is single-symbol only."""
    print("Loading bond list...")
    df = pro.cb_basic(fields="ts_code,bond_short_name,list_date")
    if df is None or len(df) == 0:
        print("  No bonds found")
        return

    bonds = []
    for _, r in df.iterrows():
        code = r["ts_code"]
        list_d = str(r.get("list_date", ""))
        if list_d and list_d > "20250901":
            continue
        bonds.append(code)

    # Take top 50 bonds
    target = bonds[:50]
    print("  {} bonds total, loading daily for {}...".format(len(bonds), len(target)))

    total = 0
    for i, code in enumerate(target):
        try:
            df2 = pro.cb_daily(ts_code=code, start_date=start_date, end_date=end_date,
                               fields="ts_code,trade_date,open,high,low,close,pre_close,vol,amount")
            if df2 is None or len(df2) == 0:
                continue
            rows = []
            for _, r in df2.iterrows():
                rows.append((r["ts_code"], str(r["trade_date"]),
                             float(r["open"]), float(r["high"]), float(r["low"]),
                             float(r["close"]), float(r["pre_close"]),
                             float(r["vol"]), float(r.get("amount", 0) or 0)))
            conn.executemany("INSERT OR REPLACE INTO bond_daily VALUES (?,?,?,?,?,?,?,?,?)", rows)
            conn.commit()
            total += len(rows)
            if (i + 1) % 10 == 0:
                print("  Bond {}/{}: {} rows".format(i + 1, len(target), total))
        except Exception as e:
            print("  Bond {} error: {}".format(code, str(e)[:50]))
        time.sleep(0.5)
    print("  Bond total: {} rows".format(total))


if __name__ == "__main__":
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    init_tables(conn)

    start = "20160101"
    end = "20260602"

    load_etf_data(conn, start, end)

    print()
    load_bond_data(conn, start, end)

    size_mb = __import__("os").path.getsize(DB_PATH) / (1024 * 1024)
    print("\nDatabase size: {:.1f} MB".format(size_mb))
    conn.close()
