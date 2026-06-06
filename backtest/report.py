"""Console + HTML email reports for backtesting results."""
import smtplib
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

SMTP_HOST = 'smtp.qq.com'
SMTP_PORT = 465
SMTP_USER = 'your_email@qq.com'
SMTP_PASS = 'your_smtp_password_here'


def print_report(result: dict):
    m = result['metrics']
    snaps = result['snapshots']
    if not snaps:
        print('No data')
        return
    cur = '$' if result['agent'] == 'us' else 'Y'
    values = [s['total_value'] for s in snaps]
    print()
    print('=' * 70)
    print('  Backtest — {0} Agent  |  {1} days'.format(result['agent'].upper(), m['trading_days']))
    print('  {0} to {1}'.format(snaps[0]['date'], snaps[-1]['date']))
    print('=' * 70)
    print('  Initial:  {0}{1:,.0f}'.format(cur, m['initial_capital']))
    print('  Final:    {0}{1:,.0f}  ({2:+.1f}%)'.format(cur, m['final_value'], m['total_return_pct']))
    print('  Sharpe:   {0:.2f}  |  MaxDD: {1:.1f}%  |  Win: {2:.0f}%'.format(
        m['sharpe_ratio'], m['max_drawdown_pct'], m['win_rate_pct']))
    print('  Trades:   {0}  |  Turnover: {1:.1f}x/yr'.format(m['total_trades'], m['turnover_rate']))
    print('  Equity:   {0} -> {1}  (hi:{2:,.0f} lo:{3:,.0f})'.format(
        '{:,.0f}'.format(values[0]), '{:,.0f}'.format(values[-1]), max(values), min(values)))
    print('=' * 70)


def send_report_email(result: dict):
    m = result['metrics']
    snaps = result['snapshots']
    agent = result['agent'].upper()
    cur = '$' if result['agent'] == 'us' else 'Y'
    now = datetime.now().strftime('%Y-%m-%d %H:%M')
    start = snaps[0]['date'] if snaps else '?'
    end = snaps[-1]['date'] if snaps else '?'
    values = [s['total_value'] for s in snaps]

    pnl_color = '#e74c3c' if m['total_return_pct'] < 0 else '#27ae60'

    eq_rows = ''
    step = max(1, len(snaps) // 20)
    for i, s in enumerate(snaps):
        if i % step == 0:
            eq_rows += '<tr><td>{0}</td><td>{1}{2:,.0f}</td><td>{3}</td></tr>'.format(
                s['date'], cur, s['total_value'], len(s.get('trades', [])))

    last = snaps[-1] if snaps else {}
    pos_rows = ''
    for sym, p in last.get('positions', {}).items():
        pos_rows += '<tr><td>{0}</td><td>{1}</td><td>{2}{3:.2f}</td></tr>'.format(
            sym, p.get('quantity', 0), cur, p.get('entry_price', 0))

    reason_samples = ''
    for s in snaps[-5:]:
        r = s.get('reasoning', '')
        if r:
            reason_samples += '<tr><td>{0}</td><td>{1}</td></tr>'.format(s['date'], r)

    html = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
    body {{ font-family: -apple-system, "Microsoft YaHei", sans-serif; background: #f5f6fa; padding: 20px; color: #2c3e50; }}
    .container {{ max-width: 680px; margin: 0 auto; background: #fff; border-radius: 12px; padding: 32px; box-shadow: 0 2px 12px rgba(0,0,0,.08); }}
    h1 {{ font-size: 22px; border-bottom: 2px solid #9b59b6; padding-bottom: 12px; }}
    h2 {{ font-size: 16px; color: #9b59b6; margin-top: 28px; }}
    table {{ width: 100%; border-collapse: collapse; margin: 12px 0; font-size: 14px; }}
    th {{ background: #f0f3f8; padding: 10px 8px; text-align: left; }}
    td {{ padding: 8px; border-bottom: 1px solid #eee; }}
    .metrics {{ display: flex; gap: 16px; flex-wrap: wrap; }}
    .metric {{ flex: 1; min-width: 130px; background: #f0f3f8; border-radius: 8px; padding: 16px; text-align: center; }}
    .metric .label {{ font-size: 12px; color: #7f8c8d; }}
    .metric .value {{ font-size: 22px; font-weight: 600; margin-top: 4px; }}
    .footer {{ text-align: center; color: #aaa; font-size: 12px; margin-top: 24px; }}
</style></head><body>
<div class="container">
<h1>AI-Trader 回测报告 — {agent} Agent</h1>
<p style="color:#888;font-size:13px">{start} ~ {end} ({days}天) | 生成于 {now}</p>

<h2>绩效概览</h2>
<div class="metrics">
    <div class="metric"><div class="label">总收益率</div><div class="value" style="color:{pnl_color}">{total_ret:+.1f}%</div></div>
    <div class="metric"><div class="label">夏普比率</div><div class="value">{sharpe:.2f}</div></div>
    <div class="metric"><div class="label">最大回撤</div><div class="value">{max_dd:.1f}%</div></div>
    <div class="metric"><div class="label">胜率</div><div class="value">{win_rate:.0f}%</div></div>
</div>

<h2>关键指标</h2>
<table>
<tr><th>指标</th><th>数值</th></tr>
<tr><td>初始资金</td><td>{cur}{initial:,.0f}</td></tr>
<tr><td>最终资产</td><td>{cur}{final:,.0f}</td></tr>
<tr><td>年化收益</td><td style="color:{pnl_color}">{annual:+.1f}%</td></tr>
<tr><td>年化波动</td><td>{vol:.1f}%</td></tr>
<tr><td>Calmar比率</td><td>{calmar:.2f}</td></tr>
<tr><td>换手率</td><td>{turnover:.1f}x/年</td></tr>
<tr><td>总交易笔数</td><td>{trades}</td></tr>
<tr><td>LLM调用次数</td><td>{llm_calls}</td></tr>
</table>

<h2>资产曲线（抽样）</h2>
<table><tr><th>日期</th><th>总资产</th><th>交易</th></tr>{eq_rows}</table>

<h2>最终持仓</h2>
<table><tr><th>标的</th><th>数量</th><th>成本价</th></tr>{pos_html}</table>

<h2>LLM决策（近5日）</h2>
<table><tr><th>日期</th><th>研判</th></tr>{reason_html}</table>

<div class="footer">AI-Trader 回测系统 · DeepSeek-v4-pro · 自动发送</div>
</div></body></html>""".format(
        agent=agent, start=start, end=end, now=now,
        days=m['trading_days'],
        total_ret=m['total_return_pct'], sharpe=m['sharpe_ratio'],
        max_dd=m['max_drawdown_pct'], win_rate=m['win_rate_pct'],
        pnl_color=pnl_color, cur=cur,
        initial=m['initial_capital'], final=m['final_value'],
        annual=m['annualized_return_pct'], vol=m['volatility_pct'],
        calmar=m['calmar_ratio'], turnover=m['turnover_rate'],
        trades=m['total_trades'], llm_calls=result.get('llm_calls', '?'),
        eq_rows=eq_rows,
        pos_html=pos_rows if pos_rows else '<tr><td colspan="3" style="color:#888;text-align:center">空仓</td></tr>',
        reason_html=reason_samples if reason_samples else '<tr><td colspan="2" style="color:#888;text-align:center">无记录</td></tr>',
    )

    msg = MIMEMultipart('alternative')
    msg['Subject'] = 'AI-Trader 回测报告 {0} {1}~{2}'.format(agent, start, end)
    msg['From'] = SMTP_USER
    msg['To'] = SMTP_USER
    msg.attach(MIMEText(html, 'html', 'utf-8'))

    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=15) as s:
        s.login(SMTP_USER, SMTP_PASS)
        s.sendmail(SMTP_USER, [SMTP_USER], msg.as_string())
    print('回测报告已发送至', SMTP_USER)
