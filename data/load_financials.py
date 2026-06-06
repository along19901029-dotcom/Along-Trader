"""Load A-share financial indicators into SQLite for backtest context enrichment."""
import sqlite3
import time
from datetime import datetime

import tushare as ts

DB_PATH = "D:/ai-trader-data/market.db"
TUSHARE_TOKEN = "your_tushare_token_here"
pro = ts.pro_api(TUSHARE_TOKEN)


def init_table(conn):
    conn.executescript("""
        DROP TABLE IF EXISTS fin_indicator;
        CREATE TABLE fin_indicator (
            ts_code TEXT,
            ann_date TEXT,
            end_date TEXT,
            roe REAL,
            debt_to_assets REAL,
            or_yoy REAL,
            netprofit_yoy REAL,
            eps REAL,
            PRIMARY KEY (ts_code, end_date, ann_date)
        );
        CREATE INDEX IF NOT EXISTS idx_fin_ts_code ON fin_indicator(ts_code);
        CREATE INDEX IF NOT EXISTS idx_fin_ann_date ON fin_indicator(ann_date);
    """)
    conn.commit()


def load_financials(conn, start_date="20150101", end_date="20260604"):
    """Pull financial indicators for listed A-share stocks."""
    # Get stock list
    stocks = [r[0] for r in conn.execute(
        "SELECT ts_code FROM stock_basic WHERE delist_date = 'None' ORDER BY ts_code LIMIT 1500"
    ).fetchall()]
    print(f"Target stocks: {len(stocks)}")

    total = 0
    for i, code in enumerate(stocks):
        try:
            df = pro.fina_indicator(
                ts_code=code,
                start_date=start_date,
                end_date=end_date,
                fields="ts_code,ann_date,end_date,roe,debt_to_assets,or_yoy,netprofit_yoy,eps"
            )
            if df is None or len(df) == 0:
                continue

            rows = []
            for _, r in df.iterrows():
                rows.append((
                    r["ts_code"],
                    str(r["ann_date"])[:10] if r["ann_date"] else "",
                    str(r["end_date"])[:10] if r["end_date"] else "",
                    float(r["roe"]) if r["roe"] is not None and r["roe"] != '' else None,
                    float(r["debt_to_assets"]) if r["debt_to_assets"] is not None and r["debt_to_assets"] != '' else None,
                    float(r["or_yoy"]) if r["or_yoy"] is not None and r["or_yoy"] != '' else None,
                    float(r["netprofit_yoy"]) if r["netprofit_yoy"] is not None and r["netprofit_yoy"] != '' else None,
                    float(r["eps"]) if r["eps"] is not None and r["eps"] != '' else None,
                ))

            conn.executemany(
                "INSERT OR REPLACE INTO fin_indicator VALUES (?,?,?,?,?,?,?,?)",
                rows
            )
            conn.commit()
            total += len(rows)

            if (i + 1) % 50 == 0:
                print(f"  {i+1}/{len(stocks)} stocks, {total} rows")
        except Exception as e:
            print(f"  {code} error: {str(e)[:60]}")
        time.sleep(0.3)

    print(f"Total: {total} rows for {len(stocks)} stocks")


if __name__ == "__main__":
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    init_table(conn)

    load_financials(conn, start_date="20180101", end_date="20260604")

    r = conn.execute("SELECT COUNT(*), COUNT(DISTINCT ts_code) FROM fin_indicator").fetchone()
    print(f"\nDB summary: {r[0]} rows, {r[1]} unique stocks")
    conn.close()
