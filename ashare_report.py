"""Send A-share investment daily report."""
import json, smtplib
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

SMTP_HOST = 'smtp.qq.com'
SMTP_PORT = 465
SMTP_USER = 'your_email@qq.com'
SMTP_PASS = 'your_smtp_password_here'

s = json.load(open('/opt/ai-trader-ashare/state.json'))
pos = s.get('positions', {})
cash = s['cash']
now = datetime.now().strftime('%Y-%m-%d %H:%M')
start = s.get('daily_start_equity', 200000)
reasoning = s.get('_llm_reasoning', '')
trades = s.get('daily_trades', [])
locked = s.get('locked_shares', {})

# Use closing price if available, otherwise entry price
cost_val = 0
total_pnl = 0
pos_rows = ''
for code, p in pos.items():
    qty = p['quantity']
    entry = p['entry_price']
    cur = p.get('close_price', entry)
    mv = qty * cur
    pnl_stock = (cur - entry) * qty
    pnl_pct = (cur - entry) / entry * 100 if entry else 0
    cost_val += mv
    total_pnl += pnl_stock
    name = p.get('name', code)
    has_lock = ' [T+1锁定]' if locked.get(code, 0) > 0 else ''
    c = '#e74c3c' if pnl_stock < 0 else ('#27ae60' if pnl_stock > 0 else '#888')
    pos_rows += '<tr><td>{0}{1}</td><td>{2}</td><td>{3}</td><td>¥{4:.2f}</td><td>¥{5:.2f}</td><td>¥{6:,.0f}</td><td style="color:{7}">¥{8:,.0f} ({9:+.1f}%)</td></tr>'.format(
        name, has_lock, code, qty, entry, cur, mv, c, pnl_stock, pnl_pct)

total = cash + cost_val
pnl = total - start

trade_rows = ''
for t in trades:
    act = '买入' if t['action'] == 'buy' else '卖出'
    trade_rows += '<tr><td>{0}</td><td>{1}</td><td>{2}</td><td>{3}</td><td>¥{4:.2f}</td><td>{5}</td></tr>'.format(
        t.get('time', ''), act, t['symbol'], t['quantity'], t['price'], t.get('reason', ''))

color = '#e74c3c' if pnl < 0 else '#27ae60'
trade_html = trade_rows if trade_rows else '<tr><td colspan="6" style="text-align:center;color:#888">今日无成交</td></tr>'
pos_html = pos_rows if pos_rows else '<tr><td colspan="7" style="text-align:center;color:#888">空仓</td></tr>'

html = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
    body {{ font-family: -apple-system, "Microsoft YaHei", sans-serif; background: #f5f6fa; padding: 20px; color: #2c3e50; }}
    .container {{ max-width: 680px; margin: 0 auto; background: #fff; border-radius: 12px; padding: 32px; box-shadow: 0 2px 12px rgba(0,0,0,.08); }}
    h1 {{ font-size: 22px; border-bottom: 2px solid #e74c3c; padding-bottom: 12px; }}
    h2 {{ font-size: 16px; color: #e74c3c; margin-top: 28px; }}
    table {{ width: 100%; border-collapse: collapse; margin: 12px 0; font-size: 14px; }}
    th {{ background: #f0f3f8; padding: 10px 8px; text-align: left; }}
    td {{ padding: 8px; border-bottom: 1px solid #eee; }}
    .metrics {{ display: flex; gap: 16px; flex-wrap: wrap; }}
    .metric {{ flex: 1; min-width: 140px; background: #f0f3f8; border-radius: 8px; padding: 16px; text-align: center; }}
    .metric .label {{ font-size: 12px; color: #7f8c8d; }}
    .metric .value {{ font-size: 22px; font-weight: 600; margin-top: 4px; }}
    .summary {{ background: #fef9e7; border-left: 4px solid #f39c12; padding: 14px 18px; margin-top: 24px; font-size: 14px; line-height: 1.7; border-radius: 4px; }}
    .footer {{ text-align: center; color: #aaa; font-size: 12px; margin-top: 24px; }}
</style></head><body>
<div class="container">
<h1>AI-Trader A股投资日报</h1>
<p style="color:#888;font-size:13px">{now} (北京) | T+1交易规则</p>

<h2>资产概览</h2>
<div class="metrics">
    <div class="metric"><div class="label">总资产</div><div class="value">¥{total:,.0f}</div></div>
    <div class="metric"><div class="label">现金</div><div class="value">¥{cash:,.0f}</div></div>
    <div class="metric"><div class="label">持仓市值</div><div class="value">¥{cost_val:,.0f}</div></div>
    <div class="metric"><div class="label">浮动盈亏</div><div class="value" style="color:{color}">{pnl:+,.0f}</div></div>
</div>

<h2>当日成交</h2>
<table><thead><tr><th>时间</th><th>方向</th><th>标的</th><th>数量</th><th>价格</th><th>原因</th></tr></thead>
<tbody>{trade_html}</tbody></table>

<h2>当前持仓</h2>
<table><thead><tr><th>名称</th><th>代码</th><th>数量</th><th>买入价</th><th>现价</th><th>市值</th><th>浮动盈亏</th></tr></thead>
<tbody>{pos_html}</tbody></table>

<h2>AI 决策思路</h2>
<div class="summary"><strong>LLM 研判</strong><br>{reasoning}</div>

<div class="footer">AI-Trader Agent · DeepSeek-v4-pro 驱动 · 自动发送</div>
</div></body></html>""".format(
    now=now, total=total, cash=cash, cost_val=cost_val,
    color=color, pnl=pnl, trade_html=trade_html,
    pos_html=pos_html, reasoning=reasoning or '暂无复盘信息')

msg = MIMEMultipart('alternative')
msg['Subject'] = 'AI-Trader A股投资日报 ' + datetime.now().strftime('%Y-%m-%d')
msg['From'] = SMTP_USER
msg['To'] = SMTP_USER
msg.attach(MIMEText(html, 'html', 'utf-8'))

with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=15) as smtp:
    smtp.login(SMTP_USER, SMTP_PASS)
    smtp.sendmail(SMTP_USER, [SMTP_USER], msg.as_string())
print('A股日报已发送')
