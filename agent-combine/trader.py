"""
Permanent Portfolio Agent — 哈利·布朗永久投资组合（国内版）
VPS 生产版：每日检查 + 再平衡 + 邮件报告

四分配置：
  25% 股票 — 510300.SH 沪深300ETF
  25% 债券 — 511010.SH 国债ETF
  25% 黄金 — 518880.SH 黄金ETF
  25% 现金 — 511880.SH 银华日利（货币ETF）
"""
import json, logging, os, sys, time, smtplib
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path
from typing import Optional

import tushare as ts
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

AGENT_NAME = os.getenv("AGENT_NAME", "PermanentPortfolio")
INITIAL_CAPITAL = float(os.getenv("INITIAL_CAPITAL", "200000"))
TUSHARE_TOKEN = os.getenv("TUSHARE_TOKEN", "")
ts.set_token(TUSHARE_TOKEN)
pro = ts.pro_api()

SMTP_HOST = os.getenv("SMTP_HOST", "smtp.qq.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
REPORT_EMAIL = os.getenv("REPORT_EMAIL", "your_email@qq.com")

LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "trader.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("pp")

STATE_FILE = Path(__file__).parent / "state.json"

PORTFOLIO = {
    "stock": {"code": "510300.SH", "name": "沪深300ETF", "target": 0.25, "desc": "繁荣期"},
    "bond":  {"code": "511010.SH", "name": "国债ETF",    "target": 0.25, "desc": "衰退期"},
    "gold":  {"code": "518880.SH", "name": "黄金ETF",    "target": 0.25, "desc": "通胀期"},
    "cash":  {"code": "511880.SH", "name": "银华日利",   "target": 0.25, "desc": "现金管理"},
}
REBALANCE_UPPER = 0.30
REBALANCE_LOWER = 0.20
FORCE_DAYS = 90

_price_cache: dict = {}
_price_date: str = ""


def refresh_prices():
    global _price_cache, _price_date
    codes = [a["code"] for a in PORTFOLIO.values() if a["code"]]
    end_date = datetime.now().strftime("%Y%m%d")
    start_date = (datetime.now() - timedelta(days=7)).strftime("%Y%m%d")
    fetched = 0
    for code in codes:
        try:
            df = pro.fund_daily(ts_code=code, start_date=start_date, end_date=end_date,
                                fields="ts_code,trade_date,close,pre_close")
            if df is not None and len(df) > 0:
                _price_cache[code] = {"price": float(df.iloc[0]["close"]),
                                       "prev_close": float(df.iloc[0]["pre_close"])}
                _price_date = str(df.iloc[0]["trade_date"])
                fetched += 1
            time.sleep(0.3)
        except Exception as e:
            log.warning("%s 行情失败: %s", code, e)
    return fetched > 0


def fetch_price(code: str) -> Optional[float]:
    info = _price_cache.get(code)
    return info["price"] if info else None


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"cash": INITIAL_CAPITAL, "positions": {}, "initial_capital": INITIAL_CAPITAL,
            "rebalance_count": 0, "last_rebalance": None, "daily_trades": []}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def calc_total(state):
    t = state["cash"]
    for key, pos in state.get("positions", {}).items():
        code = PORTFOLIO[key]["code"]
        price = fetch_price(code) or pos["entry_price"]
        t += price * pos["quantity"]
    return t


def calc_allocs(state):
    total = calc_total(state)
    if total <= 0: return {}
    allocs = {}
    for key, asset in PORTFOLIO.items():
        if key in state.get("positions", {}):
            pos = state["positions"][key]
            price = fetch_price(asset["code"]) or pos["entry_price"]
            allocs[key] = price * pos["quantity"] / total
        else:
            allocs[key] = state["cash"] / total if key == "cash" and state["cash"] > 0 else 0
    return allocs


def record_trade(state, action, key, qty, price, reason):
    asset = PORTFOLIO[key]
    state.setdefault("daily_trades", []).append({
        "time": datetime.now().strftime("%H:%M:%S"),
        "action": action,
        "symbol": asset["code"],
        "name": asset["name"],
        "quantity": qty,
        "price": round(price, 3),
        "reason": reason,
    })


def execute_trade(action, key, qty, price, reason=""):
    state = load_state()
    asset = PORTFOLIO[key]
    code = asset["code"]
    if action == "buy":
        cost = qty * price
        if state["cash"] < cost: return False
        state["cash"] -= cost
        if key not in state["positions"]:
            state["positions"][key] = {"quantity": qty, "entry_price": price}
        else:
            old_qty = state["positions"][key]["quantity"]
            old_entry = state["positions"][key]["entry_price"]
            new_qty = old_qty + qty
            state["positions"][key] = {"quantity": new_qty,
                "entry_price": (old_entry * old_qty + price * qty) / new_qty}
        record_trade(state, "buy", key, qty, price, reason)
        log.info("BUY  %s x%d @%.3f [%s]", asset["name"], qty, price, reason)
    else:
        pos = state.get("positions", {}).get(key)
        if not pos or pos["quantity"] < qty: return False
        state["cash"] += qty * price
        new_qty = pos["quantity"] - qty
        if new_qty <= 0:
            del state["positions"][key]
        else:
            state["positions"][key]["quantity"] = new_qty
        record_trade(state, "sell", key, qty, price, reason)
        log.info("SELL %s x%d @%.3f [%s]", asset["name"], qty, price, reason)
    save_state(state)
    return True


def do_rebalance():
    state = load_state()
    total = calc_total(state)
    allocs = calc_allocs(state)

    needs = False
    for key, pct in allocs.items():
        if pct > REBALANCE_UPPER or pct < REBALANCE_LOWER:
            needs = True
    if state.get("last_rebalance"):
        try:
            last = datetime.strptime(state["last_rebalance"], "%Y-%m-%d")
            if (datetime.now() - last).days >= FORCE_DAYS:
                needs = True
        except: pass

    if not needs:
        return

    log.info("REBALANCE TRIGGERED")
    lot = 100
    for key, asset in PORTFOLIO.items():
        code = asset["code"]
        if code is None:
            continue
        price = fetch_price(code)
        if not price: continue
        target_val = total * asset["target"]
        cur_qty = state.get("positions", {}).get(key, {}).get("quantity", 0)
        diff = target_val - cur_qty * price
        if abs(diff) < price * lot: continue
        shares = int(abs(diff) / price / lot) * lot
        if shares < lot: continue
        if diff > 0:
            execute_trade("buy", key, shares, price, "再平衡")
        else:
            if cur_qty < shares:
                shares = cur_qty - (cur_qty % lot)
                if shares < lot: continue
            execute_trade("sell", key, shares, price, "再平衡")
        state = load_state()
        time.sleep(0.5)

    state["rebalance_count"] = state.get("rebalance_count", 0) + 1
    state["last_rebalance"] = datetime.now().strftime("%Y-%m-%d")
    save_state(state)


def send_report():
    state = load_state()
    total = calc_total(state)
    init = state.get("initial_capital", INITIAL_CAPITAL)
    total_ret = (total - init) / init * 100
    allocs = calc_allocs(state)
    today_str = datetime.now().strftime("%Y-%m-%d")

    # Daily trades
    trades = state.get("daily_trades", [])
    trade_rows = ""
    if trades:
        for t in trades:
            a = "买" if t["action"] == "buy" else "卖"
            trade_rows += (
                f"<tr><td>{t['time']}</td><td>{a}</td>"
                f"<td>{t.get('name', t['symbol'])}</td><td>{t['quantity']}</td>"
                f"<td>{t['price']:.3f}</td><td>{t.get('reason', '')}</td></tr>"
            )
    else:
        trade_rows = '<tr><td colspan="6" style="text-align:center;color:#888">今日无交易</td></tr>'

    # Allocations
    alloc_rows = ""
    for key, pct in allocs.items():
        asset = PORTFOLIO[key]
        diff = (pct - asset["target"]) * 100
        c = "#e74c3c" if abs(diff) > 5 else "#27ae60"
        alloc_rows += f"<tr><td>{asset['name']}</td><td>{asset['desc']}</td><td>{pct*100:.1f}%</td><td style='color:{c}'>{diff:+.1f}%</td></tr>"

    # Positions
    pos_rows = ""
    for key, pos in state.get("positions", {}).items():
        asset = PORTFOLIO[key]
        code = asset["code"]
        cur = fetch_price(code) or pos["entry_price"]
        mv = cur * pos["quantity"]
        pnl = (cur - pos["entry_price"]) / pos["entry_price"] * 100
        c = "#e74c3c" if pnl < 0 else "#27ae60"
        pos_rows += f"<tr><td>{asset['name']}</td><td>{code}</td><td>{pos['quantity']}</td><td>{pos['entry_price']:.3f}</td><td>{cur:.3f}</td><td>{mv:,.0f}</td><td style='color:{c}'>{pnl:+.2f}%</td></tr>"

    pnl_c = "#27ae60" if total >= init else "#e74c3c"

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
body{{font-family:-apple-system,'Microsoft YaHei',sans-serif;background:#f5f6fa;padding:20px;color:#2c3e50}}
.container{{max-width:650px;margin:0 auto;background:#fff;border-radius:12px;padding:28px;box-shadow:0 2px 12px rgba(0,0,0,.08)}}
h1{{font-size:20px;border-bottom:2px solid #f39c12;padding-bottom:10px}}
h2{{font-size:15px;color:#f39c12;margin-top:24px}}
table{{width:100%;border-collapse:collapse;margin:10px 0;font-size:13px}}
th{{background:#fef9e7;padding:8px;text-align:left}}
td{{padding:7px 8px;border-bottom:1px solid #eee}}
.metrics{{display:flex;gap:12px;flex-wrap:wrap}}
.metric{{flex:1;min-width:100px;background:#fef9e7;border-radius:8px;padding:14px;text-align:center}}
.metric .label{{font-size:11px;color:#7f8c8d}}
.metric .value{{font-size:18px;font-weight:600;margin-top:4px}}
.footer{{text-align:center;color:#aaa;font-size:11px;margin-top:20px}}
</style></head><body>
<div class="container">
<h1>永久投资组合 {today_str}</h1>
<div class="metrics">
  <div class="metric"><div class="label">初始资金</div><div class="value">{init:,.0f}</div></div>
  <div class="metric"><div class="label">当前总资产</div><div class="value" style="color:{pnl_c}">{total:,.0f}</div></div>
  <div class="metric"><div class="label">累计收益</div><div class="value" style="color:{pnl_c}">{total_ret:+.2f}%</div></div>
  <div class="metric"><div class="label">再平衡</div><div class="value">{state.get('rebalance_count',0)}次</div></div>
</div>
<h2>当日交易</h2>
<table><tr><th>时间</th><th>方向</th><th>标的</th><th>数量</th><th>价格</th><th>原因</th></tr>{trade_rows}</table>
<h2>资产配置</h2>
<table><tr><th>资产</th><th>环境</th><th>占比</th><th>偏离</th></tr>{alloc_rows}</table>
<h2>持仓明细</h2>
<table><tr><th>名称</th><th>代码</th><th>数量</th><th>成本</th><th>现价</th><th>市值</th><th>盈亏</th></tr>{pos_rows}</table>
<div class="footer">{AGENT_NAME} · 25/25/25/25 机械再平衡 · 零AI成本</div>
</div></body></html>"""

    if SMTP_USER and SMTP_PASS:
        try:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = f"永久投资组合日报 {today_str}"
            msg["From"] = SMTP_USER
            msg["To"] = REPORT_EMAIL
            msg.attach(MIMEText(html, "html", "utf-8"))
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=15) as s:
                s.login(SMTP_USER, SMTP_PASS)
                s.sendmail(SMTP_USER, [REPORT_EMAIL], msg.as_string())
            log.info("Report sent to %s", REPORT_EMAIL)
        except Exception as e:
            log.warning("Email failed: %s", e)


def is_trading_day():
    try:
        today = datetime.now().strftime("%Y%m%d")
        cal = pro.trade_cal(exchange="SSE", start_date=today, end_date=today)
        return len(cal) > 0 and cal.iloc[0]["is_open"] == 1
    except:
        return datetime.now().weekday() < 5


def is_market_open():
    now = datetime.now()
    t = now.hour * 60 + now.minute
    return (570 <= t < 690) or (780 <= t < 900)


def minutes_after_close():
    now = datetime.now()
    close = now.replace(hour=15, minute=0, second=0, microsecond=0)
    return int((now - close).total_seconds() // 60) if now > close else -1


def main():
    log.info("══ 永久投资组合 Agent 启动 ══")
    log.info("初始资金: ￥%s | 策略: 25/25/25/25 机械再平衡", INITIAL_CAPITAL)
    state = load_state()

    while True:
        state = load_state()
        today_str = datetime.now().strftime("%Y-%m-%d")

        # 非交易日：连续3个周期无操作后退出
        if not is_trading_day():
            idle = state.get("_idle_cycles", 0) + 1
            state["_idle_cycles"] = idle
            save_state(state)
            if idle >= 3:
                log.info("非交易日，退出进程")
                break
            time.sleep(60)
            continue

        state["_idle_cycles"] = 0

        # 日报已发送且不在盘中 → 退出
        if state.get("report_sent_date") == today_str and not is_market_open():
            log.info("今日日报已发送，退出进程")
            break

        min_after = minutes_after_close()

        # 收盘后 30-120 分钟 → 发送日报
        if min_after >= 30 and state.get("report_sent_date") != today_str:
            log.info("收盘 %d 分钟，生成日报...", min_after)

            # Initial allocation on first run
            if not state.get("positions") and state["cash"] == INITIAL_CAPITAL:
                log.info("首次运行，初始建仓...")
                state["daily_trades"] = []
                if not refresh_prices():
                    log.error("无法获取行情，等待重试")
                    time.sleep(60)
                    continue
                total = calc_total(state)
                lot = 100
                for key, asset in PORTFOLIO.items():
                    code = asset["code"]
                    if code is None:
                        continue
                    price = fetch_price(code)
                    if not price: continue
                    shares = int(total * asset["target"] / price / lot) * lot
                    if shares < lot: continue
                    execute_trade("buy", key, shares, price, "初始建仓")
                    state = load_state()
                save_state(state)

            refresh_prices()
            do_rebalance()
            send_report()

            state = load_state()
            state["daily_trades"] = []
            state["report_sent_date"] = today_str
            save_state(state)
            log.info("日报已发送")

        # 不在盘中，且不在日报窗口 → 退出（今日无需操作）
        if not is_market_open() and min_after >= 120:
            log.info("已过交易时段，退出")
            break

        # 等待
        if is_market_open():
            log.info("盘中等待...")
        else:
            log.info("盘前等待...")

        save_state(state)
        time.sleep(60)

    save_state(state)
    log.info("Agent 已安全退出")


if __name__ == "__main__":
    main()
