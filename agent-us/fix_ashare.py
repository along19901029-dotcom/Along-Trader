with open("/opt/ai-trader-ashare/trader.py", "r") as f:
    content = f.read()

old_func = """def get_stock_name(code: str) -> str:
    info = _price_cache.get(code)
    return info["name"] if info else code"""

new_func = """def get_stock_name(code: str) -> str:
    info = _price_cache.get(code)
    if info and info.get("name"):
        return info["name"]
    ts_code = _sina_to_ts(code)
    name = _STOCK_NAMES.get(ts_code)
    if name:
        return name
    return code"""

if old_func in content:
    content = content.replace(old_func, new_func)
    with open("/opt/ai-trader-ashare/trader.py", "w") as f:
        f.write(content)
    print("get_stock_name fix APPLIED")
else:
    print("NOT FOUND")
    for i, line in enumerate(content.split("\n")):
        if "def get_stock_name" in line:
            print(f"Line {i}: {repr(line)}")
            for j in range(1, 4):
                print(f"  Line {i+j}: {repr(content.split(chr(10))[i+j])}")
