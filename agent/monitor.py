"""
AI-Trader Monitor — 查询 Agent 交易纪录和累计盈亏
用法: python monitor.py
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

BASE_URL = os.getenv("BASE_URL", "https://ai4trade.ai")
TOKEN_FILE = Path(__file__).parent / ".token"

if not TOKEN_FILE.exists():
    print("❌ 未找到 token 文件，请先运行 trader.py 注册/登录")
    sys.exit(1)

TOKEN = TOKEN_FILE.read_text().strip()
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


def api_get(path, params=None):
    try:
        r = requests.get(f"{BASE_URL}{path}", headers=HEADERS, params=params, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"  ⚠️  请求失败: {e}")
        return {}


def section(title):
    print(f"\n{'━' * 50}")
    print(f"  {title}")
    print(f"{'━' * 50}")


# ── Agent 总览 ──
section("Agent 账户总览")

me = api_get("/api/claw/agents/me")
if me.get("id"):
    print(f"  ID:       {me['id']}")
    print(f"  名称:     {me['name']}")
    print(f"  现金余额: ${me.get('cash', 0):,.2f}")
    print(f"  积分:     {me.get('points', 0)}")
    print(f"  声誉:     {me.get('reputation_score', 0)}")

# ── 持仓 & 实时 PnL ──
section("当前持仓及浮动盈亏")

positions = api_get("/api/positions")
if positions.get("positions"):
    total_pnl = 0
    for p in positions["positions"]:
        sym = p["symbol"]
        qty = p["quantity"]
        entry = p.get("entry_price", 0)
        current = p.get("current_price", 0)
        pnl = p.get("pnl", 0) or ((current - entry) * qty if current and entry else 0)
        total_pnl += pnl
        pnl_sign = "+" if pnl >= 0 else ""
        print(f"  {sym:8s}  {qty:>6.2f}股  "
              f"均价 ${entry:>10.2f}  现价 ${current:>10.2f}  "
              f"PnL {pnl_sign}${pnl:,.2f}")
    print(f"  {'─' * 40}")
    print(f"  持仓浮动盈亏合计: {'+' if total_pnl >= 0 else ''}${total_pnl:,.2f}")
    cash = positions.get("cash", me.get("cash", 0))
    print(f"  现金: ${cash:,.2f}")
    print(f"  总资产: ${cash + total_pnl:,.2f}")
else:
    print("  当前空仓")

# ── 最近交易纪录 ──
section("最近 10 笔交易")

feed = api_get("/api/signals/feed", {"limit": 20, "message_type": "operation"})
trades = [
    s for s in feed.get("signals", [])
    if s.get("type") in ("trade", "position")
]
if trades:
    for t in trades[:10]:
        ts = t.get("timestamp")
        if ts:
            ts = datetime.utcfromtimestamp(ts).strftime("%m-%d %H:%M")
        side = t.get("side", "?")
        sym = t.get("symbol", "?")
        price = t.get("entry_price") or t.get("price") or 0
        qty = t.get("quantity", 0)
        print(f"  {ts}  {side:>5s} {sym:8s}  "
              f"${price:>10.2f} × {qty}")
else:
    print("  暂无交易纪录")

# ── 我的交易信号（含 PnL 记录） ──
section("我的信号 PnL 汇总")

my_discussions = api_get("/api/signals/my/discussions")
# 从 grouped 中提取自己的 PnL
grouped = api_get("/api/signals/grouped", {"limit": 50})
agent_id = me.get("id")

my_agent = None
for a in grouped.get("agents", []):
    if a.get("agent_id") == agent_id:
        my_agent = a
        break

if my_agent:
    print(f"  历史信号数:  {my_agent.get('signal_count', 0)}")
    print(f"  信号累计 PnL: {'+' if my_agent.get('total_pnl', 0) >= 0 else ''}${my_agent.get('total_pnl', 0):,.2f}")
    print(f"  持仓浮动 PnL: {'+' if my_agent.get('position_pnl', 0) >= 0 else ''}${my_agent.get('position_pnl', 0):,.2f}")
    total = my_agent.get('total_pnl', 0) + my_agent.get('position_pnl', 0)
    print(f"  合计:         {'+' if total >= 0 else ''}${total:,.2f}")
else:
    print("  暂无数据")

print()
print("┌─────────────────────────────────────────────────┐")
print("│  Web 看板: https://ai4trade.ai                  │")
print("│  本地看板: http://localhost:3000                 │")
print("│  Agent 日志: cat agent/logs/trader.log          │")
print("└─────────────────────────────────────────────────┘")
