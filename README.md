# Along-Trader

基于 Tushare Pro + ai4trade.ai 的 4+1 多市场 AI 自动交易系统，运行在阿里云 ECS (Ubuntu 22.04)。

## 架构

```
                         ┌─────────────────────┐
                         │  along-scheduler     │  智能调度器（systemd 守护）
                         │  /opt/scheduler.py   │  30s 巡检 · 错峰启动 · 孤儿清理
                         └──────┬──────────────┘
                ┌───────────────┼───────────────┬──────────────┐
                ▼               ▼               ▼              ▼
        ┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────────┐
        │ US Agent │    │ A-share  │    │   ETF    │    │  Bond    │
        │ 21:25→   │    │ 09:25→   │    │ 09:25→   │    │ 09:25→   │
        │  05:30   │    │  15:45   │    │  15:45   │    │  15:45   │
        └────┬─────┘    └────┬─────┘    └────┬─────┘    └────┬─────┘
             │               │               │               │
             ▼               ▼               ▼               ▼
        Tushare Pro ─── 行情数据 ─── ai4trade.ai ─── QQ 邮箱 SMTP
        (付费)          (日线+实时)     (模拟交易)        (HTML 日报)
```

## Agent 一览

| Agent | 市场 | 时段 (北京时间) | 候选池 | 上限 | 每只上限 |
|-------|------|---------------|--------|------|---------|
| US | 美股 S&P 500 | 21:25→05:30 | 500 只成分股 | 10 只 | $10,000 |
| A-share | 沪深 300 | 09:25→15:45 | 300 只成分股 | 6 只 | ¥50,000 |
| ETF | 全市场股票/指数 ETF | 09:25→15:45 | 2,085 只 | 6 只 | ¥50,000 |
| Bond | 可转债 | 09:25→15:45 | 643 只 | 长期4+活跃3 | ¥40,000/¥25,000 |

## 数据源

Tushare Pro（付费），端点使用情况：

| 端点 | 批量 | 用于 |
|------|------|------|
| `pro.daily()` | ✅ 逗号分隔 | A-share 行情 |
| `pro.us_daily()` | ✅ 逗号分隔 | US 行情 |
| `pro.fund_daily()` | ❌ 逐代码 | ETF 行情（3min 刷新节流） |
| `pro.cb_daily()` | ❌ 逐代码 | Bond 行情（3min 刷新节流） |
| `pro.cb_basic()` | ✅ | Bond 候选池 + 到期过滤 |

每日 API 请求约 28,000 次，Tushare Pro 完全覆盖。

## 日报

每个交易日收盘后 30 分钟自动生成 HTML 日报（持仓明细、当日成交、盈亏、止损止盈触发），通过 QQ 邮箱 SMTP 发送。可转债日报额外包含到期提醒。

## 部署路径

```
VPS: Alibaba Cloud ECS, Ubuntu 22.04 (1C1G)

/opt/ai-trader-agent/    # 美股
/opt/ai-trader-ashare/   # A 股
/opt/ai-trader-etf/      # ETF
/opt/ai-trader-bond/     # 债券
/opt/scheduler.py        # 智能调度器
```

### 环境变量 (`.env`)

每个 Agent 目录下 `.env` 需配置：

```bash
TUSHARE_TOKEN=your_token
SMTP_HOST=smtp.qq.com
SMTP_PORT=465
SMTP_USER=your_email@qq.com
SMTP_PASS=your_auth_code
REPORT_EMAIL=your_email@qq.com
INITIAL_CAPITAL=200000
LOOP_INTERVAL=60
```

## 已解决的坑

| 问题 | 修复 |
|------|------|
| `pro.daily()` 无 `start_date` 返回半年前 QFQ 脏数据 | 显式日期范围 |
| `fund_daily`/`cb_daily` 不支持批量 | 3 分钟刷新节流 |
| 到期债/停牌债名称显示为代码 | `_BOND_NAMES` + Tushare 直查兜底 |
| `daily_start_equity` 只取现金忽略持仓 | 改为 `现金 + 持仓市值` |
| 到期日当天买入到期债（remain_years=0） | 移除 `>0` 守卫 |
| 调度器 end_time 早于日报发送 | end_time 延后 15-45 分钟 |

## 免责声明

本项目仅供学习研究，所有交易决策由 AI Agent 自动生成，不构成投资建议。

## License

MIT — 继承自 [AI-Trader](https://github.com/HKUDS/AI-Trader)
