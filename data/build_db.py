"""Build SQLite database from Tushare for A-share backtesting.

Database: D:/ai-trader-data/market.db
Tables: stock_basic, daily, daily_basic, fina_indicator, trade_cal

Run: python data/build_db.py --years 2016-2026
"""
import sqlite3
import time
import argparse
from pathlib import Path
from datetime import datetime, timedelta

import tushare as ts

DB_PATH = Path("D:/ai-trader-data/market.db")
TUSHARE_TOKEN = "your_tushare_token_here"
pro = ts.pro_api(TUSHARE_TOKEN)


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=OFF")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS stock_basic (
            ts_code TEXT PRIMARY KEY,
            name TEXT,
            industry TEXT,
            list_date TEXT,
            exchange TEXT,
            delist_date TEXT
        );
        CREATE TABLE IF NOT EXISTS daily (
            ts_code TEXT,
            trade_date TEXT,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            pre_close REAL,
            vol REAL,
            amount REAL,
            PRIMARY KEY (ts_code, trade_date)
        );
        CREATE TABLE IF NOT EXISTS daily_basic (
            ts_code TEXT,
            trade_date TEXT,
            pe REAL,
            pb REAL,
            ps REAL,
            roe REAL,
            total_mv REAL,
            circ_mv REAL,
            PRIMARY KEY (ts_code, trade_date)
        );
        CREATE TABLE IF NOT EXISTS fina_indicator (
            ts_code TEXT,
            end_date TEXT,
            roe REAL,
            roa REAL,
            grossprofit_margin REAL,
            netprofit_margin REAL,
            debt_to_assets REAL,
            PRIMARY KEY (ts_code, end_date)
        );
        CREATE TABLE IF NOT EXISTS trade_cal (
            cal_date TEXT PRIMARY KEY,
            exchange TEXT,
            is_open INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_daily_date ON daily(trade_date);
        CREATE INDEX IF NOT EXISTS idx_daily_basic_date ON daily_basic(trade_date);
    """)
    conn.commit()
    return conn


def load_stock_basic(conn):
    """Load all A-share stocks including delisted (avoid survivorship bias)."""
    print("Loading stock_basic (all statuses)...")
    all_stocks = []
    for status in ['L', 'D', 'P']:
        df = pro.stock_basic(exchange='', list_status=status,
                             fields='ts_code,name,industry,list_date,delist_date')
        if df is None or len(df) == 0:
            continue
        for _, r in df.iterrows():
            exchange = "SH" if r['ts_code'].endswith(".SH") else "SZ"
            all_stocks.append((r['ts_code'], r['name'], r.get('industry', ''),
                               str(r.get('list_date', '')), exchange,
                               str(r.get('delist_date', ''))))
        print("  {} ({})".format(status, len(df)))
    conn.executemany(
        "INSERT OR REPLACE INTO stock_basic VALUES (?,?,?,?,?,?)", all_stocks)
    conn.commit()


def load_daily(conn, start_date: str, end_date: str, batch_size: int = 50):
    """Load daily OHLCV for all stocks over date range."""
    print("Loading daily data {} to {}...".format(start_date, end_date))
    stocks = [r[0] for r in conn.execute("SELECT ts_code FROM stock_basic")]
    total = 0
    for i in range(0, len(stocks), batch_size):
        batch = stocks[i:i + batch_size]
        codes = ",".join(batch)
        try:
            df = pro.daily(ts_code=codes, start_date=start_date, end_date=end_date,
                           fields="ts_code,trade_date,open,high,low,close,pre_close,vol,amount")
            if df is None or len(df) == 0:
                continue
            rows = []
            for _, r in df.iterrows():
                rows.append((r['ts_code'], str(r['trade_date']),
                             float(r['open']), float(r['high']), float(r['low']),
                             float(r['close']), float(r['pre_close']),
                             float(r['vol']), float(r.get('amount', 0) or 0)))
            conn.executemany(
                "INSERT OR REPLACE INTO daily VALUES (?,?,?,?,?,?,?,?,?)", rows)
            conn.commit()
            total += len(rows)
            if (i // batch_size + 1) % 50 == 0:
                print("  {} stocks done, {} rows...".format(
                    min(i + batch_size, len(stocks)), total))
        except Exception as e:
            print("  Error batch {}: {}".format(i, str(e)[:60]))
        time.sleep(0.3)
    print("  Total: {} rows".format(total))


def load_daily_basic(conn, start_date: str, end_date: str):
    """Load daily fundamental data (PE, PB, ROE, market cap)."""
    print("Loading daily_basic {} to {}...".format(start_date, end_date))
    trading_days = [r[0] for r in conn.execute(
        "SELECT DISTINCT trade_date FROM daily WHERE trade_date BETWEEN ? AND ? ORDER BY trade_date",
        (start_date, end_date))]
    total = 0
    for i, td in enumerate(trading_days):
        try:
            df = pro.daily_basic(trade_date=td,
                                 fields="ts_code,trade_date,pe,pb,ps,roe,total_mv,circ_mv")
            if df is None or len(df) == 0:
                continue
            rows = []
            for _, r in df.iterrows():
                rows.append((r['ts_code'], str(r['trade_date']),
                             float(r.get('pe', 0) or 0), float(r.get('pb', 0) or 0),
                             float(r.get('ps', 0) or 0), float(r.get('roe', 0) or 0),
                             float(r.get('total_mv', 0) or 0),
                             float(r.get('circ_mv', 0) or 0)))
            conn.executemany(
                "INSERT OR REPLACE INTO daily_basic VALUES (?,?,?,?,?,?,?,?)", rows)
            conn.commit()
            total += len(rows)
            if (i + 1) % 50 == 0:
                print("  {} / {} days, {} rows...".format(i + 1, len(trading_days), total))
        except Exception as e:
            print("  Error {}: {}".format(td, str(e)[:60]))
        time.sleep(0.4)
    print("  Total: {} rows".format(total))


def load_trade_cal(conn, start_date: str, end_date: str):
    """Load trading calendar."""
    print("Loading trade_cal...")
    df = pro.trade_cal(exchange="SSE", start_date=start_date, end_date=end_date)
    if df is None or len(df) == 0:
        print("  No data")
        return
    rows = [(str(r['cal_date']), str(r.get('exchange', 'SSE')),
             int(r['is_open'])) for _, r in df.iterrows()]
    conn.executemany("INSERT OR REPLACE INTO trade_cal VALUES (?,?,?)", rows)
    conn.commit()
    open_days = sum(1 for r in rows if r[2] == 1)
    print("  {} days ({} open)".format(len(rows), open_days))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="20160101", help="Start date YYYYMMDD")
    parser.add_argument("--end", default="20260602", help="End date YYYYMMDD")
    parser.add_argument("--skip-daily", action="store_true")
    parser.add_argument("--skip-fundamentals", action="store_true")
    args = parser.parse_args()

    conn = init_db()
    print("Database: {}".format(DB_PATH))

    load_stock_basic(conn)
    load_trade_cal(conn, args.start, args.end)

    if not args.skip_daily:
        load_daily(conn, args.start, args.end)
    if not args.skip_fundamentals:
        load_daily_basic(conn, args.start, args.end)

    # Report size
    size_mb = DB_PATH.stat().st_size / (1024 * 1024)
    print("\nDatabase size: {:.1f} MB".format(size_mb))
    conn.close()


if __name__ == "__main__":
    main()
