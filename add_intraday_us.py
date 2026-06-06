"""Add Tencent intraday prices to US agent. Run on VPS: python3 /tmp/add_intraday_us.py"""
fpath = '/opt/ai-trader-agent/trader.py'
with open(fpath, 'r') as f:
    content = f.read()

# 1. Add curl_cffi import
content = content.replace(
    'import tushare as ts\n',
    'import tushare as ts\nfrom curl_cffi import requests as cffi_requests\n'
)

# 2. Add intraday cache vars
old = '_price_cache: dict = {}\n_PREV_CLOSE_CACHE: dict = {}\n_price_date: str = ""  # 缓存数据的最新交易日期'
new = '_price_cache: dict = {}\n_PREV_CLOSE_CACHE: dict = {}\n_intraday_cache: dict = {}  # 腾讯财经实时价\n_intraday_time: float = 0.0\n_price_date: str = ""  # 缓存数据的最新交易日期'
content = content.replace(old, new)
print('1. Cache vars:', 'OK' if '_intraday_cache' in content else 'FAIL')

# 3. Add refresh_intraday function + update fetch_price
old2 = '\n\ndef fetch_price(symbol: str) -> Optional[float]:\n    """获取标的最新价格。"""\n    return _price_cache.get(symbol.upper())'

new2 = '''

def refresh_intraday(symbols: list):
    """从腾讯财经获取美股实时价格（模拟Chrome指纹，30秒内缓存）。"""
    global _intraday_cache, _intraday_time
    if not symbols:
        return
    now_ts = time.time()
    if _intraday_cache and (now_ts - _intraday_time) < 30:
        return
    try:
        q = ','.join(['us' + s.upper() for s in symbols[:30]])
        url = 'https://qt.gtimg.cn/q=' + q
        resp = cffi_requests.get(url, impersonate='chrome120', timeout=8)
        for line in resp.text.split(';'):
            line = line.strip()
            if not line or '=' not in line:
                continue
            parts = line.split('~')
            if len(parts) < 10:
                continue
            key = line.split('=')[0]
            sym = key.replace('v_us', '').replace('_', '')
            try:
                _intraday_cache[sym.upper()] = {
                    'price': round(float(parts[3]), 2),
                    'prev_close': float(parts[4]),
                    'chg_pct': round((float(parts[3]) - float(parts[4])) / float(parts[4]) * 100, 2),
                }
            except (ValueError, IndexError):
                pass
        _intraday_time = now_ts
    except Exception as e:
        log.debug('实时行情获取失败: %s', e)


def fetch_price(symbol: str) -> Optional[float]:
    """获取标的最新价格（优先实时价）。"""
    live = _intraday_cache.get(symbol.upper(), {})
    if live and live.get('price'):
        return live['price']
    return _price_cache.get(symbol.upper())'

content = content.replace(old2, new2)
print('2. refresh_intraday:', 'OK' if 'def refresh_intraday' in content else 'FAIL')
print('3. fetch_price:', 'OK' if 'live and live.get' in content else 'FAIL')

# 4. Call refresh_intraday in run_loop
old3 = 'refresh_prices(all_symbols)\n\n            if is_market_open():'
new3 = 'refresh_prices(all_symbols)\n            refresh_intraday(all_symbols)\n\n            if is_market_open():'
content = content.replace(old3, new3)
print('4. run_loop call:', 'OK' if 'refresh_intraday(all_symbols)' in content else 'FAIL')

# 5. Also update _build_llm_context to include intraday data in candidates
old4 = "        candidates.append({\n            \"symbol\": sym,\n            \"price\": round(price, 2),\n        })"
new4 = """        live = _intraday_cache.get(sym.upper(), {})
        intraday_price = live.get('price') if live else None
        intraday_chg = live.get('chg_pct') if live else None
        candidates.append({
            \"symbol\": sym,
            \"price\": round(price, 2),
            \"intraday_price\": intraday_price,
            \"intraday_chg_pct\": intraday_chg,
        })"""
content = content.replace(old4, new4)
print('5. LLM context:', 'OK' if 'intraday_price' in content else 'FAIL')

with open(fpath, 'w') as f:
    f.write(content)
print('\nAll done.')
