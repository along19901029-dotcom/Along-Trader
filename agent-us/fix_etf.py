with open("/opt/ai-trader-etf/trader.py", "r") as f:
    content = f.read()

old_func = """    try:
        codes_str = ",".join(ts_codes[:50])
        df = pro.fund_daily(ts_code=codes_str,
                            fields="ts_code,trade_date,open,high,low,close,pre_close,vol")

        for _, row in df.iterrows():
            ts_code = row["ts_code"]
            code = _ts_to_etf_code(ts_code)
            close = float(row["close"])
            pre_close = float(row["pre_close"])
            _price_cache[code] = {
                "code": code,
                "name": _ETF_NAMES.get(code, code),
                "price": close,
                "prev_close": pre_close,
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "high_limit": round(pre_close * 1.1, 3),
                "low_limit": round(pre_close * 0.9, 3),
                "volume": float(row["vol"]),
            }

    except Exception as e:
        log.warning("行情获取失败: %s", e)"""

new_func = """    end_date = datetime.now().strftime("%Y%m%d")
    start_date = (datetime.now() - timedelta(days=3)).strftime("%Y%m%d")
    for ts_code in ts_codes:
        code = _ts_to_etf_code(ts_code)
        try:
            df = pro.fund_daily(ts_code=ts_code, start_date=start_date, end_date=end_date,
                                fields="ts_code,trade_date,open,high,low,close,pre_close,vol")
            if df is None or len(df) == 0:
                continue
            row = df.iloc[0]
            close = float(row["close"])
            pre_close = float(row["pre_close"])
            _price_cache[code] = {
                "code": code,
                "name": _ETF_NAMES.get(code, code),
                "price": close,
                "prev_close": pre_close,
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "high_limit": round(pre_close * 1.1, 3),
                "low_limit": round(pre_close * 0.9, 3),
                "volume": float(row["vol"]),
            }
        except Exception as e:
            log.debug("获取 %s 行情失败: %s", code, e)
            continue"""

if old_func in content:
    content = content.replace(old_func, new_func)
    with open("/opt/ai-trader-etf/trader.py", "w") as f:
        f.write(content)
    print("REPLACED OK")
else:
    print("Need exact match - checking whitespace")
    for i, line in enumerate(content.split("\n")):
        if "codes_str = " in line and "join(ts_codes" in line:
            print(f"Line {i}: {repr(line)}")
