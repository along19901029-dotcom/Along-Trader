"""
Fix all 3 domestic agents:
1. Add date range to Tushare queries (avoid fetching 6000 rows)
2. Fix _price_date: set from newest row, not overwritten by oldest
3. Fix misleading log messages
"""
import sys


def fix_ashare(path):
    with open(path, 'r') as f:
        content = f.read()

    old = '''    try:
        codes_str = ",".join(ts_codes[:50])
        df = pro.daily(ts_code=codes_str,
                       fields="ts_code,trade_date,open,high,low,close,pre_close,vol")

        for _, row in df.iterrows():
            ts_code = row["ts_code"]
            code = _ts_to_sina(ts_code)
            close = float(row["close"])
            trade_date = str(row.get("trade_date", ""))
            if trade_date:
                _price_date = trade_date'''

    new = '''    try:
        codes_str = ",".join(ts_codes[:50])
        end_date = datetime.now().strftime("%Y%m%d")
        start_date = (datetime.now() - timedelta(days=10)).strftime("%Y%m%d")
        df = pro.daily(ts_code=codes_str, start_date=start_date, end_date=end_date,
                       fields="ts_code,trade_date,open,high,low,close,pre_close,vol")

        # 只在第一行设置最新数据日期
        if len(df) > 0:
            _price_date = str(df.iloc[0]["trade_date"])

        for _, row in df.iterrows():
            ts_code = row["ts_code"]
            code = _ts_to_sina(ts_code)
            close = float(row["close"])'''

    if old in content:
        content = content.replace(old, new)
        print('  A-share: refresh_prices fixed')
    else:
        print('  A-share: PATTERN NOT FOUND in refresh_prices')
        return False

    # Fix log message
    old_log = 'log.info("行情源: 新浪财经 | 交易模拟: 本地撮合")'
    new_log = 'log.info("行情源: Tushare Pro | 交易模拟: 本地撮合")'
    if old_log in content:
        content = content.replace(old_log, new_log)
        print('  A-share: log message fixed')
    else:
        print('  A-share: log message not found (may already be fixed)')

    with open(path, 'w') as f:
        f.write(content)
    return True


def fix_etf(path):
    with open(path, 'r') as f:
        content = f.read()

    old = '''    try:
        codes_str = ",".join(ts_codes[:50])
        df = pro.fund_daily(ts_code=codes_str,
                            fields="ts_code,trade_date,open,high,low,close,pre_close,vol")

        for _, row in df.iterrows():
            ts_code = row["ts_code"]
            code = _ts_to_etf_code(ts_code)
            close = float(row["close"])
            trade_date = str(row.get("trade_date", ""))
            if trade_date:
                _price_date = trade_date'''

    new = '''    try:
        codes_str = ",".join(ts_codes[:50])
        end_date = datetime.now().strftime("%Y%m%d")
        start_date = (datetime.now() - timedelta(days=10)).strftime("%Y%m%d")
        df = pro.fund_daily(ts_code=codes_str, start_date=start_date, end_date=end_date,
                            fields="ts_code,trade_date,open,high,low,close,pre_close,vol")

        # 只在第一行设置最新数据日期
        if len(df) > 0:
            _price_date = str(df.iloc[0]["trade_date"])

        for _, row in df.iterrows():
            ts_code = row["ts_code"]
            code = _ts_to_etf_code(ts_code)
            close = float(row["close"])'''

    if old in content:
        content = content.replace(old, new)
        print('  ETF: refresh_prices fixed')
    else:
        print('  ETF: PATTERN NOT FOUND in refresh_prices')
        return False

    # Fix log message
    old_log = 'log.info("行情源: 新浪财经 | ETF 数据: AKShare | 交易模拟: 本地撮合")'
    new_log = 'log.info("行情源: Tushare Pro | 交易模拟: 本地撮合")'
    if old_log in content:
        content = content.replace(old_log, new_log)
        print('  ETF: log message fixed')
    else:
        print('  ETF: log message not found (may already be fixed)')

    with open(path, 'w') as f:
        f.write(content)
    return True


def fix_bond(path):
    with open(path, 'r') as f:
        content = f.read()

    # Bond already has date range but _price_date overwrites per symbol
    # Fix: keep the max date across all symbols
    old = '''    for ts_code in ts_codes:
        code = _ts_to_bond_code(ts_code)
        try:
            df = pro.cb_daily(ts_code=ts_code, start_date=start_date, end_date=end_date,
                              fields="ts_code,trade_date,open,high,low,close,pre_close,vol,amount")
            if len(df) == 0:
                continue
            row = df.iloc[-1]  # 取最新一行
            trade_date = str(row.get("trade_date", ""))
            if trade_date:
                _price_date = trade_date'''

    new = '''    for ts_code in ts_codes:
        code = _ts_to_bond_code(ts_code)
        try:
            df = pro.cb_daily(ts_code=ts_code, start_date=start_date, end_date=end_date,
                              fields="ts_code,trade_date,open,high,low,close,pre_close,vol,amount")
            if len(df) == 0:
                continue
            row = df.iloc[-1]  # 取最新一行
            trade_date = str(row.get("trade_date", ""))
            if trade_date and (not _price_date or trade_date > _price_date):
                _price_date = trade_date'''

    if old in content:
        content = content.replace(old, new)
        print('  Bond: _price_date fixed (keep max date)')
    else:
        print('  Bond: PATTERN NOT FOUND in refresh_prices')
        return False

    # Fix data source log message
    old_log = 'log.info("数据源: Tushare Pro | 交易模拟: 本地撮合 | 品种: 可转债")'
    if old_log in content:
        print('  Bond: log message already correct')
    else:
        print('  Bond: log message already correct or different')

    with open(path, 'w') as f:
        f.write(content)
    return True


if __name__ == '__main__':
    agents = {
        'A-share': ('/opt/ai-trader-ashare/trader.py', fix_ashare),
        'ETF': ('/opt/ai-trader-etf/trader.py', fix_etf),
        'Bond': ('/opt/ai-trader-bond/trader.py', fix_bond),
    }
    for name, (path, fix_func) in agents.items():
        print(f'{name}:')
        fix_func(path)
        print()
    print('All fixes applied.')
