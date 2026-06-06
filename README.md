# Along-Trader

LLM 量化交易实验项目。用 AI 做投资决策，回测验证，实盘对比。

## 项目结构

```
AI-trader/
├── agent/                  美股 LLM Agent (VPS 生产版)
│   ├── trader.py             主交易脚本 (DeepSeek 驱动)
│   └── intraday_module.py    盘中数据模块
├── agent-a-share/          A股 LLM Agent (VPS 生产版)
│   ├── trader.py             主交易脚本 (DeepSeek + 孙子兵法 + 十五五规划)
│   └── financials_helper.py  财务数据查找模块
├── agent-combine/          永久投资组合 Agent (本地 + VPS)
│   ├── trader.py             机械再平衡 (零 LLM 成本)
│   └── agent.py              本地测试版
├── agent-common/           共享模块
│   ├── llm_client.py         DeepSeek API 客户端
│   ├── suntzu_prompt.py      孙子兵法投资原则
│   └── financials_helper.py  财务指标查询
├── backtest/               回测框架
│   ├── runner_sqlite.py      主回测引擎 (SQLite + LLM 缓存)
│   ├── replay_snapshot.py    缓存重放工具
│   ├── time_machine.py       时间 Mock
│   └── config.py / metrics.py 配置与指标
├── data/                   数据工程
│   ├── build_db.py           构建 A 股日线数据库
│   ├── load_etf_bond.py      加载 ETF/可转债数据
│   └── load_financials.py    加载财务指标 (营收增速/利润增速/负债率)
├── send_briefing.py        美股投资简报
├── ashare_report.py        A股投资日报
└── monitor.py              健康监控 (VPS cron 每5分钟巡检)
```

## Agent 一览

### 美股 Agent (`agent/`)
- **策略**: DeepSeek-v4-flash 自主决策
- **候选池**: S&P 500 成分股 (静态列表 267 只)
- **约束**: 最多 10 只，单只上限 $10,000
- **运行时段**: 北京时间 21:45 → 次日 04:15
- **特色**: 含孙子兵法投资原则，HTML 日报自动发送

### A股 Agent (`agent-a-share/`)
- **策略**: DeepSeek-v4-flash 自主决策
- **候选池**: 沪深 300 成分股 + 100 只上交所 + 100 只深交所
- **约束**: 最多 10 只，单只上限 ¥50,000，T+1，含印花税
- **运行时段**: 北京时间 09:35 → 15:15
- **特色**: 含孙子兵法 + 十五五规划宏观背景，财务指标标注 (营收增速/利润增速/负债率)

### 永久投资组合 (`agent-combine/`)
- **策略**: 哈利·布朗 25/25/25/25 机械再平衡
- **资产**: 510300 (沪深300) / 511010 (国债) / 518880 (黄金) / 511880 (银华日利)
- **规则**: 任一资产偏离 ±5% 触发再平衡，90 天强制再平衡
- **特色**: 零 LLM 成本，59 笔交易/388天，夏普 1.50

## 数据架构

```
Tushare Pro API → 本地 SQLite (D:/ai-trader-data/market.db)
                        │
                        ├── stock_basic    股票基本信息 (5,525只)
                        ├── daily          A股日线 (2025/9起)
                        ├── daily_basic    PE/PB/ROE (2025/9起)
                        ├── etf_daily      ETF日线 (44只, 2024/10起)
                        ├── bond_daily     可转债日线 (30只, 2019起)
                        ├── fin_indicator  财务指标 (1,500只, 2018起)
                        └── trade_cal      交易日历
```

Tushare Pro 付费版（200 元/年）：200 次/分钟，日线数据约 9 个月历史。

## 投资策略

### 孙子兵法三原则
1. **先为不可胜**（先守后攻）：单笔亏损 ≤5%
2. **胜而后战**（趋势+估值+风向缺一不可）
3. **兵贵神速**（盈利超 8% 必收，不贪）

### 十五五规划主线 (2026-2030)
- 科技自立自强：AI 芯片、算力基础设施
- 绿色低碳转型：电气化、储能、新能源
- 新质生产力：低空经济、智能机器人、生物医药

## 回测结果 (388天, 2024/10/25 → 2026/06/02)

```
策略                    收益      最大回撤    夏普    交易    成本
──────────────────────────────────────────────────────────────
永久组合 (机械)          +20.4%     -7.9%    1.50     59      零
ETF LLM (30只候选)       +9.9%    -11.0%    0.52    384      ¥3
A股 LLM (200只候选)      +0.0%     -7.3%    0.07    178      ¥3
纯择时 (只做HS300)        -1.8%     -3.7%   -0.50     14      ¥3
──────────────────────────────────────────────────────────────
HS300 买入持有           +22.9%    -26.4%      —      0      零
中证500 买入持有          +43.1%       —        —      0      零
上证50 买入持有            +7.3%       —        —      0      零
```

**结论**: LLM 择时能力为零，择股能力有限。不做预测的机械策略跑赢所有 AI 策略。大道至简。

## 快速开始

```bash
# 安装依赖
pip install tushare requests python-dotenv akshare tqdm

# 运行回测（永久组合，秒级完成）
cd backtest
python runner_sqlite.py -a combine --start 20241025 --end 20260602

# 运行回测（ETF LLM，首跑约 1.5 小时，缓存后秒级）
python runner_sqlite.py -a etf --start 20241025 --end 20260602

# 运行回测（A股 LLM，首跑约 30 分钟）
python runner_sqlite.py -a ashare --start 20250919 --end 20260602

# HS300 纯择时
python runner_sqlite.py -a etf --benchmark --start 20241025 --end 20260602
```

## VPS 部署

```bash
# 阿里云 ECS Ubuntu 22.04 (1C1G)
# Agent 通过 systemd 管理，monitor.py 通过 cron 每5分钟巡检

# 查看运行状态
ssh root@<vps-ip> "ps aux | grep trader"

# 查看日志
ssh root@<vps-ip> "tail -50 /opt/ai-trader-agent/logs/trader.log"
```

## 环境变量 (`.env`)

```bash
AGENT_NAME=AgentName
INITIAL_CAPITAL=200000
TUSHARE_TOKEN=your_tushare_token
SMTP_HOST=smtp.qq.com
SMTP_PORT=465
SMTP_USER=your_email@qq.com
SMTP_PASS=your_smtp_auth_code
REPORT_EMAIL=your_email@qq.com
LOOP_INTERVAL=60
```

## 免责声明

本项目仅供学习研究。所有回测结果均为历史数据模拟，不构成投资建议。AI 决策有风险，实盘需谨慎。

## License

MIT
