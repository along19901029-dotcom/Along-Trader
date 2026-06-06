"""Fix US agent: _price_date + is_price_data_today"""
fpath = '/opt/ai-trader-agent/trader.py'
with open(fpath, 'r') as f:
    content = f.read()

# Fix 1: _price_date inside loop -> set before loop from all_dates.max()
old1 = '        if df is not None and len(df) > 0:\n            latest = df.sort_values("trade_date").groupby("ts_code").last()\n            for ts_code, row in latest.iterrows():\n                sym = ts_code.replace(".US", "").upper()\n                close = float(row["close"])\n                pre_close = float(row.get("pre_close", close))\n                trade_date = str(row.get("trade_date", ""))\n                if close > 0:\n                    _price_cache[sym] = close\n                    _PREV_CLOSE_CACHE[sym] = pre_close\n                    if trade_date:\n                        _price_date = trade_date  # 记录最新数据日期'

new1 = '        if df is not None and len(df) > 0:\n            # 从原始df取最新交易日期（不在循环中覆盖）\n            all_dates = df["trade_date"].dropna()\n            if len(all_dates) > 0:\n                _price_date = str(all_dates.max())\n            latest = df.sort_values("trade_date").groupby("ts_code").last()\n            for ts_code, row in latest.iterrows():\n                sym = ts_code.replace(".US", "").upper()\n                close = float(row["close"])\n                pre_close = float(row.get("pre_close", close))\n                if close > 0:\n                    _price_cache[sym] = close\n                    _PREV_CLOSE_CACHE[sym] = pre_close'

c1 = content.count(old1)
if c1 == 1:
    content = content.replace(old1, new1)
    print(f'Fix 1 (_price_date): applied (found {c1})')
else:
    print(f'Fix 1: count={c1}, skipping')
    idx = content.find('latest = df.sort_values')
    if idx >= 0:
        print(content[idx:idx+500])

# Fix 2: is_price_data_today (US: 3-day cutoff, no trade_cal available)
old2 = 'def is_price_data_today() -> bool:\n    """检查价格缓存是否包含今日数据。"""\n    if not _price_date:\n        return False\n    today = datetime.now().strftime("%Y%m%d")\n    return _price_date == today'

new2 = 'def is_price_data_today() -> bool:\n    if not _price_date:\n        return False\n    today = datetime.now().strftime("%Y%m%d")\n    if _price_date == today:\n        return True\n    # 美股日线数据收盘后发布，盘中允许3日内数据（覆盖周末）\n    cutoff = (datetime.now() - timedelta(days=3)).strftime("%Y%m%d")\n    return _price_date >= cutoff'

c2 = content.count(old2)
if c2 == 1:
    content = content.replace(old2, new2)
    print(f'Fix 2 (is_price_data_today): applied (found {c2})')
else:
    print(f'Fix 2: count={c2}, skipping')
    idx = content.find('def is_price_data_today')
    if idx >= 0:
        print(repr(content[idx:idx+200]))

with open(fpath, 'w') as f:
    f.write(content)
print('Saved.')
