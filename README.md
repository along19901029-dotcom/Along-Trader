# Along-Trader

基于 DeepSeek + Tushare Pro 的 LLM 量化交易实验项目。

**核心发现：LLM 不擅长择时和选股，机械策略比你聪明。**

## 五策略回测对比 (388天)

```
策略                    收益      最大回撤    夏普    交易    AI成本
──────────────────────────────────────────────────────────────
永久组合 (25/25/25/25)  +20.4%     -7.9%    1.50     59      零
ETF LLM (30只候选)       +9.9%    -11.0%    0.52    384      高
A股 LLM (200只候选)      +0.0%     -7.3%    0.07    178      高
纯择时 (只做HS300)        -1.8%     -3.7%   -0.50     14      高
──────────────────────────────────────────────────────────────
HS300 买入持有           +22.9%    -26.4%      —      0      零
```

> 择时自由度越大，收益越差。不折腾 = 赢。

## 永久投资组合 (Harry Browne)

```
25% 股票 — 510300.SH  沪深300ETF    繁荣期
25% 债券 — 511010.SH  国债ETF       衰退期
25% 黄金 — 518880.SH  黄金ETF       通胀期
25% 现金 — 511880.SH  银华日利       通缩期

规则：任一资产偏离 ±5% 触发再平衡 (卖涨买跌)
结果：388天 +20.4%，夏普 1.50，59笔低摩擦交易
```

## 架构

```
本地 (Windows)
├── backtest/             回测框架 (SQLite + LLM 缓存)
├── data/                 数据加载 (Tushare → SQLite)
└── agent-combine/        永久组合 Agent (本地版)

VPS: Alibaba Cloud ECS (1C1G)
├── ai-trader-agent        美股 LLM Agent
├── ai-trader-ashare       A股 LLM Agent (含财务数据)
├── ai-trader-common       LLM 客户端 + 孙子兵法 Prompt
└── monitor.py             健康监控 (cron 每5分钟)
```

## 快速开始 (回测)

```bash
cd backtest

# 永久组合 (秒级，无 LLM)
python runner_sqlite.py -a combine --start 20241025 --end 20260602

# ETF LLM 回测
python runner_sqlite.py -a etf --start 20241025 --end 20260602

# A股 LLM 回测
python runner_sqlite.py -a ashare --start 20250919 --end 20260602

# HS300 纯择时
python runner_sqlite.py -a etf --benchmark --start 20241025 --end 20260602
```

## 数据源

| 端点 | 用于 | 限制 |
|------|------|------|
| `pro.daily()` | A股日线 | 200元/年, 约9个月历史 |
| `pro.fund_daily()` | ETF日线 | 逐代码查询 |
| `pro.cb_daily()` | 可转债日线 | 逐代码查询 |
| `pro.daily_basic()` | PE/PB/ROE | 批量查询 |
| `pro.fina_indicator()` | 财务指标 | 逐代码查询 |

本地 SQLite (`D:/ai-trader-data/market.db`) 作为数据缓存。

## 环境变量

```bash
TUSHARE_TOKEN=your_token
AGENT_NAME=PermanentPortfolio
INITIAL_CAPITAL=200000
SMTP_HOST=smtp.qq.com
SMTP_PORT=465
SMTP_USER=your_email@qq.com
SMTP_PASS=your_auth_code
REPORT_EMAIL=your_email@qq.com
```

## 免责声明

本项目仅供学习研究，所有交易决策不构成投资建议。

## License

MIT
