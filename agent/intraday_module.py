import time
try:
    from curl_cffi import requests as cffi_requests
except ImportError:
    cffi_requests = None  # backtesting doesn't need live data
import logging

log = logging.getLogger('intraday')
_intraday_cache = {}
_intraday_time = 0.0

def refresh_intraday(symbols):
    global _intraday_cache, _intraday_time
    if not symbols:
        log.debug('refresh_intraday: empty symbols')
        return
    now_ts = time.time()
    if _intraday_cache and (now_ts - _intraday_time) < 30:
        log.debug('refresh_intraday: throttled (cache age=%.1fs)', now_ts - _intraday_time)
        return
    try:
        batch = [s.upper() for s in symbols[:30]]
        q = ','.join(['us' + s for s in batch])
        log.debug('refresh_intraday: fetching %d symbols', len(batch))
        resp = cffi_requests.get('https://qt.gtimg.cn/q=' + q, impersonate='chrome120', timeout=8)
        count = 0
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
                count += 1
            except (ValueError, IndexError):
                pass
        _intraday_time = now_ts
        log.debug('refresh_intraday: got %d prices', count)
    except Exception as e:
        log.warning('refresh_intraday failed: %s', e)

def get_intraday(sym):
    return _intraday_cache.get(sym.upper(), {})

def get_intraday_price(sym):
    live = _intraday_cache.get(sym.upper(), {})
    return live.get('price')
