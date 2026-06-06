"""Test Tencent Finance US stock real-time API and parse response."""
from curl_cffi import requests as cr

symbols = ['AAPL','NVDA','GOOG','JPM','PG','XOM','UNH','V','WMT','HD',
           'MSFT','TSLA','AMZN','META','BRK-B']
q = ','.join(['us' + s for s in symbols])
url = 'https://qt.gtimg.cn/q=' + q
resp = cr.get(url, impersonate='chrome120', timeout=10)

prices = {}
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
        price = float(parts[3])
        prev_close = float(parts[4])
        chg_pct = (price - prev_close) / prev_close * 100 if prev_close else 0
        prices[sym] = {
            'price': round(price, 2),
            'prev_close': prev_close,
            'chg_pct': round(chg_pct, 2),
            'name': parts[1],
        }
    except (ValueError, IndexError):
        pass

print('Got {0} real-time prices:'.format(len(prices)))
for sym, p in sorted(prices.items()):
    print('  {0} ({1}) price={2} chg={3:+.2f}%'.format(
        sym, p['name'], p['price'], p['chg_pct']))
