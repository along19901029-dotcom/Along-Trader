# Along-Trader

基于 [AI-Trader](https://github.com/HKUDS/AI-Trader) 平台的多市场 AI 自动交易系统，包含四个独立运作的 Agent，覆盖美股、A股、ETF 和中国可转债。

## 项目结构

```
Along-Trader/
├── agent/              # 美股 Agent — S&P 500 动态筛选 + Stooq 行情
├── agent-a-share/      # A股 Agent — CSI 300 成分股轮动 + 新浪行情
├── agent-etf/          # ETF Agent — 股票型/指数型 ETF 动量策略
├── agent-bond/         # 债券 Agent — 可转债双策略 + 国债逆回购
├── service/            # AI-Trader 平台后端与前端
├── skills/             # Agent 技能定义
└── docs/               # API 文档
```

## 运行机制

每个 Agent 独立循环运行：

```
心跳 → 同步持仓 → 获取行情 → 止损/止盈检查 → 筛选买入 → 日报发送
```

- **交易时段限制**：仅在真实市场交易时段执行买卖，非交易时段跳过操作
- **本地模拟撮合**：A股/ETF/债券使用新浪行情 API 本地计算，美股通过 ai4trade.ai 平台执行
- **日报投递**：每个交易日收盘后 30 分钟自动生成 HTML 日报，通过 QQ 邮箱 SMTP 发送
- **持久化状态**：持仓、现金、当日成交记录持久化到本地 `state.json`

---

## 四大 Agent

### 美股 Agent (`agent/`)

| 项目 | 说明 |
|---|---|
| 候选池 | S&P 500 成分股（Wikipedia 动态抓取，每日刷新） |
| 行情源 | Stooq API |
| 交易时段 | 美东 9:30-16:00（含美国节假日判断） |
| 执行方式 | ai4trade.ai 平台 `/api/signals/realtime` |
| 止损/止盈 | -10% / +30%（可在 `.env` 调整） |
| 持仓上限 | 5 只，单只 $10,000 |

### A股 Agent (`agent-a-share/`)

| 项目 | 说明 |
|---|---|
| 候选池 | CSI 300 成分股（AKShare 动态获取） |
| 行情源 | 新浪财经 API (`hq.sinajs.cn`) |
| 交易时段 | 9:30-11:30 / 13:00-15:00（北京时间） |
| 交易规则 | T+1，涨跌停限制，佣金万三，印花税千一（卖出） |
| 止损/止盈 | -10% / +30% |
| 选股逻辑 | 动量得分 = 涨跌幅 × 0.6 + 日内涨幅 × 0.4 |
| 持仓上限 | 5 只，单只 ¥50,000 |

### ETF Agent (`agent-etf/`)

| 项目 | 说明 |
|---|---|
| 候选池 | 股票型/指数型 ETF（AKShare → Eastmoney → 122只精选三级降级） |
| 行情源 | 新浪财经 API |
| 交易规则 | T+1，万三佣金，免印花税 |
| 止损/止盈 | -8% / +20% |
| 选股逻辑 | 同动量得分模型，过滤低成交额品种（最低 ¥5,000万） |
| 持仓上限 | 5 只，单只 ¥50,000 |

### 债券 Agent (`agent-bond/`)

| 项目 | 说明 |
|---|---|
| 品种 | 中国可转债（T+0，10张/手，万二佣金，免印花税） |
| 数据源 | AKShare 集思录 → Eastmoney → 205只精选三级降级 |
| 策略一（60%） | **中长期持有**：优选高评级、正 YTM、价格近面值（100-118）的可转债 |
| 策略二（40%） | **灵活交易**：高流动性波段操作，T+0 随时买卖 |
| 逆回购 | 尾盘 14:55 自动买入 R-001（sz131810）管理灵活交易闲置资金 |
| 中长期止盈 | +20% 或价格触及 130（强赎触发价） |
| 灵活止损/止盈 | -3% / +8% |
| 中长期上限 | 4 只，单只 ¥40,000 |
| 灵活上限 | 3 只，单只 ¥25,000 |

**中长期筛选因子**：到期收益率（YTM）× 5 × 0.5 + 信用评级归一化 × 0.3 + 纯债保护率 × 0.2

---

## 快速部署

### 1. 环境要求

- Python 3.8+
- Linux VPS（推荐 Ubuntu 20.04+）

### 2. 克隆项目

```bash
git clone https://github.com/<your-username>/Along-Trader.git
cd Along-Trader
```

### 3. 安装依赖

```bash
pip install requests python-dotenv akshare
```

### 4. 配置 `.env`

每个 agent 目录下均有 `.env` 文件，需要填入：

```bash
# 必填：邮箱 SMTP（用于日报投递）
SMTP_HOST=smtp.qq.com
SMTP_PORT=465
SMTP_USER=your_email@qq.com
SMTP_PASS=your_smtp_auth_code    # QQ邮箱 → 设置 → 账户 → SMTP 授权码
REPORT_EMAIL=your_email@qq.com

# 美股 Agent 额外需要 ai4trade.ai 账号
AGENT_EMAIL=your_email@example.com
AGENT_PASSWORD=your_password
```

### 5. 运行

```bash
# 美股 Agent
cd agent && python trader.py

# A股 Agent
cd agent-a-share && python trader.py

# ETF Agent
cd agent-etf && python trader.py

# 债券 Agent
cd agent-bond && python trader.py
```

### 6. systemd 持久化运行（推荐）

```bash
cat > /etc/systemd/system/along-trader-{name}.service << 'SVC'
[Unit]
Description=Along-Trader {Name} Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/Along-Trader/agent-{name}
ExecStart=/usr/bin/python3 trader.py
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
SVC

systemctl daemon-reload
systemctl enable along-trader-{name}
systemctl start along-trader-{name}
```

将 `{name}` 替换为 `agent`、`ashare`、`etf` 或 `bond`。

---

## 免责声明

本项目仅供学习和研究使用。所有交易决策由 AI Agent 自动生成，不构成任何投资建议。投资有风险，入市需谨慎。

## License

MIT — 继承自 [AI-Trader](https://github.com/HKUDS/AI-Trader)
