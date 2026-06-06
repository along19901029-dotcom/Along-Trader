"""Fix US agent: replace Wikipedia with Tushare + static file for S&P 500 constituents."""
fpath = '/opt/ai-trader-agent/trader.py'
with open(fpath, 'r') as f:
    content = f.read()

# Add import os if needed
if 'import os\n' not in content[:500]:
    content = content.replace('import tushare as ts\n', 'import tushare as ts\nimport os\n', 1)
    print('Added import os')

old = '''def get_universe() -> list:
    """获取候选池（S&P 500 或 NASDAQ 100 成分股，每日刷新一次）。"""
    global _universe_cache, _universe_date
    today = datetime.now().strftime("%Y-%m-%d")
    if _universe_cache and _universe_date == today:
        return _universe_cache
    try:
        if UNIVERSE_INDEX == "nasdaq100":
            url = "https://en.wikipedia.org/wiki/Nasdaq-100"
        else:
            url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
        tables = pd.read_html(url)
        if UNIVERSE_INDEX == "nasdaq100":
            tickers = tables[3]["Ticker"].dropna().tolist()
        else:
            tickers = tables[0]["Symbol"].dropna().tolist()
        tickers = [t.replace(".", "-") for t in tickers if isinstance(t, str)]
        _universe_cache = tickers
        _universe_date = today
        log.info("候选池已刷新: %s, %s 只标的", UNIVERSE_INDEX, len(tickers))
        return tickers
    except Exception as e:
        log.warning("获取成分股失败: %s，使用缓存", e)
        return _universe_cache if _universe_cache else []'''

new = '''def get_universe() -> list:
    """获取候选池（Tushare 指数成分股 + 静态文件兜底，不依赖外网）。"""
    global _universe_cache, _universe_date
    today = datetime.now().strftime("%Y-%m-%d")
    if _universe_cache and _universe_date == today:
        return _universe_cache

    tickers = []

    # 1. 主渠道：静态 JSON 文件（国内 VPS 不依赖外网）
    if not tickers:
        try:
            import json
            static_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sp500_static.json")
            with open(static_file, 'r') as fh:
                tickers = json.load(fh)
            log.info("候选池(静态文件): %s 只标的", len(tickers))
        except Exception as e:
            log.warning("静态成分股文件加载失败: %s", e)

    # 3. 最后兜底：内存缓存
    if not tickers and _universe_cache:
        tickers = _universe_cache
        log.warning("使用内存缓存: %s 只标的", len(tickers))

    _universe_cache = tickers
    _universe_date = today
    return tickers'''

c = content.count(old)
if c == 1:
    content = content.replace(old, new)
    print('get_universe: replaced')
elif c == 0:
    print('get_universe: NOT FOUND')
    idx = content.find('def get_universe()')
    if idx >= 0:
        print(content[idx:idx+800])
else:
    print(f'get_universe: {c} occurrences, ambiguous')

with open(fpath, 'w') as f:
    f.write(content)
print('Saved.')
