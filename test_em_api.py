"""Test EastMoney API for US stock real-time quotes."""
import requests, time

url = "https://push2.eastmoney.com/api/qt/clist/get"

# Test 1: basic batch
t0 = time.time()
params = {
    "fid": "f3", "po": "1", "pz": "5", "pn": "1", "np": "1", "fltt": "2",
    "fs": "m:105,m:106,m:107",
    "fields": "f2,f3,f12,f14,f15,f16,f17",
}
resp = requests.get(url, params=params, timeout=10)
data = resp.json()
stocks = data["data"]["diff"]
print("Batch query: {:.2f}s, total: {}".format(time.time()-t0, data["data"]["total"]))
for s in stocks:
    print("  {:6s} price={:>8s} chg={:>7s}%  {}".format(
        s["f12"], str(s.get("f2","?")), str(s.get("f3","?")), s["f14"]))

# Test 2: Can we filter by specific symbols?
# EastMoney fs format: "m:105" for market, "b:BK0478" for sector
# For individual stocks, we might need to construct fs like:
# "(secid=105.AAPL)(secid=105.MSFT)"
print()
print("=== Try multi-stock filter ===")
targets = ["AAPL", "NVDA", "JPM", "XOM", "GOOG"]
secid_list = ",".join(["105." + t for t in targets])
params2 = {
    "fid": "f3", "po": "1", "pz": str(len(targets)), "pn": "1", "np": "1", "fltt": "2",
    "fs": "m:105,m:106,m:107",
    "fields": "f2,f3,f12,f14",
}
# Also try: directly pass secids parameter
params2["secids"] = secid_list
resp2 = requests.get(url, params=params2, timeout=10)
d2 = resp2.json()
if d2.get("data") and d2["data"].get("diff"):
    print("Got {} stocks:".format(len(d2["data"]["diff"])))
    for s in d2["data"]["diff"]:
        print("  {:6s} ${:>8s}  chg={:>7}%  {}".format(
            s["f12"], str(s.get("f2","?")), str(s.get("f3","?")), s["f14"]))
else:
    print("No data with secids param")
    # Fallback: just fetch all and filter
    print("Fallback: fetch all US stocks and filter locally")
