"""Send investment briefing for US agent — full HTML template."""
import json, smtplib
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

SMTP_HOST = 'smtp.qq.com'
SMTP_PORT = 465
SMTP_USER = 'your_email@qq.com'
SMTP_PASS = 'your_smtp_password_here'
STATE_FILE = '/opt/ai-trader-agent/state.json'

state = json.load(open(STATE_FILE))
positions = state.get('positions', {})
trades = state.get('daily_trades', [])
cash = state['cash']
today_str = datetime.now().strftime('%Y-%m-%d')
now_str = datetime.now().strftime('%Y-%m-%d %H:%M')
start_equity = state.get('daily_start_equity', 100000)

# Try to get intraday prices for real P&L
intraday_prices = {}
try:
    import sys; sys.path.insert(0, '/opt/ai-trader-agent')
    from intraday_module import refresh_intraday, get_intraday_price
    symbols = list(positions.keys())
    refresh_intraday(symbols)
    for sym in symbols:
        p = get_intraday_price(sym)
        if p:
            intraday_prices[sym] = p
except Exception:
    pass

# Calculate with real-time prices where available
position_value = 0
for sym, p in positions.items():
    cur_price = intraday_prices.get(sym, p['entry_price'])
    position_value += p['quantity'] * cur_price
total = cash + position_value
daily_pnl = total - start_equity

# ── Trade rows ──
trade_rows = ''
if trades:
    for t in trades:
        reason_cn = {'signal':'策略信号','stop_loss':'止损','take_profit':'止盈','force_sell':'清仓'}.get(t.get('reason',''), t.get('reason',''))
        trade_rows += f"""<tr>
            <td>{t['time']}</td><td>{'买入' if t['action']=='buy' else '卖出'}</td><td>{t['symbol']}</td>
            <td>{t['quantity']}</td><td>${t['price']:.2f}</td><td>{reason_cn}</td>
        </tr>"""
else:
    trade_rows = '<tr><td colspan="6" style="text-align:center;color:#888">今日无成交</td></tr>'

# ── Position rows ──
pos_rows = ''
if positions:
    for s, p in positions.items():
        entry = p['entry_price']
        qty = p['quantity']
        cur = intraday_prices.get(s, entry)
        mv = cur * qty
        pnl = (cur - entry) * qty
        pnl_pct = (cur - entry) / entry * 100 if entry else 0
        color = '#e74c3c' if pnl < 0 else ('#27ae60' if pnl > 0 else '#888')
        pos_rows += f"""<tr>
            <td>{s}</td><td>{qty}</td><td>${entry:.2f}</td><td>${cur:.2f}</td>
            <td>${mv:,.0f}</td><td style="color:{color}">${pnl:,.0f} ({pnl_pct:+.1f}%)</td>
        </tr>"""
else:
    pos_rows = '<tr><td colspan="6" style="text-align:center;color:#888">空仓</td></tr>'

# ── Summary ──
llm_reasoning = state.get('_llm_reasoning', '')
summary = llm_reasoning if llm_reasoning else '暂无复盘信息'

html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
    body {{ font-family: -apple-system, 'Microsoft YaHei', sans-serif; background: #f5f6fa;
           padding: 20px; color: #2c3e50; }}
    .container {{ max-width: 680px; margin: 0 auto; background: #fff; border-radius: 12px;
                  padding: 32px; box-shadow: 0 2px 12px rgba(0,0,0,.08); }}
    h1 {{ font-size: 22px; border-bottom: 2px solid #3498db; padding-bottom: 12px; }}
    h2 {{ font-size: 16px; color: #3498db; margin-top: 28px; }}
    table {{ width: 100%; border-collapse: collapse; margin: 12px 0; font-size: 14px; }}
    th {{ background: #f0f3f8; padding: 10px 8px; text-align: left; }}
    td {{ padding: 8px; border-bottom: 1px solid #eee; }}
    .metrics {{ display: flex; gap: 16px; flex-wrap: wrap; }}
    .metric {{ flex: 1; min-width: 140px; background: #f0f3f8; border-radius: 8px;
               padding: 16px; text-align: center; }}
    .metric .label {{ font-size: 12px; color: #7f8c8d; }}
    .metric .value {{ font-size: 22px; font-weight: 600; margin-top: 4px; }}
    .summary {{ background: #fef9e7; border-left: 4px solid #f39c12; padding: 14px 18px;
                margin-top: 24px; font-size: 14px; line-height: 1.7; border-radius: 4px; }}
    .footer {{ text-align: center; color: #aaa; font-size: 12px; margin-top: 24px; }}
</style></head><body>
<div class="container">
<h1>AI-Trader 美股投资简报</h1>
<p style="color:#888;font-size:13px">生成时间: {now_str} (北京) | 数据截止: {today_str}</p>

<h2>资产概览</h2>
<div class="metrics">
    <div class="metric"><div class="label">总资产</div><div class="value">${total:,.0f}</div></div>
    <div class="metric"><div class="label">现金</div><div class="value">${cash:,.0f}</div></div>
    <div class="metric"><div class="label">持仓市值</div><div class="value">${position_value:,.0f}</div></div>
    <div class="metric"><div class="label">浮动盈亏</div>
        <div class="value" style="color:{'#e74c3c' if daily_pnl < 0 else '#27ae60'}">{daily_pnl:+,.0f}</div></div>
</div>

<h2>当日成交</h2>
<table><thead><tr><th>时间</th><th>方向</th><th>标的</th><th>数量</th><th>价格</th><th>原因</th></tr></thead>
<tbody>{trade_rows}</tbody></table>

<h2>当前持仓</h2>
<table><thead><tr><th>标的</th><th>数量</th><th>成本价</th><th>现价(估)</th><th>市值</th><th>浮动盈亏</th></tr></thead>
<tbody>{pos_rows}</tbody></table>

<h2>AI 决策思路</h2>
<div class="summary"><strong>LLM 最新研判</strong><br>{summary}</div>

<div class="footer">AI-Trader Agent · DeepSeek-v4-pro 驱动 · 自动发送</div>
</div></body></html>"""

msg = MIMEMultipart('alternative')
msg['Subject'] = f'AI-Trader 美股投资简报 {today_str}'
msg['From'] = SMTP_USER
msg['To'] = SMTP_USER
msg.attach(MIMEText(html, 'html', 'utf-8'))

with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=15) as s:
    s.login(SMTP_USER, SMTP_PASS)
    s.sendmail(SMTP_USER, [SMTP_USER], msg.as_string())
print('简报已发送')
