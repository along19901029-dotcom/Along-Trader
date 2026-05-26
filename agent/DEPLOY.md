# AI-Trader Agent — VPS 部署指南

## 快速部署（Ubuntu/Debian）

### 1. 上传文件到 VPS

```bash
scp -r agent/ user@your-server:/opt/ai-trader-agent/
```

### 2. 安装依赖

```bash
ssh user@your-server
cd /opt/ai-trader-agent
pip install requests python-dotenv
```

### 3. 编辑配置

```bash
nano /opt/ai-trader-agent/.env
```

填写 `AGENT_EMAIL`、`AGENT_PASSWORD`，替换 Alpha Vantage API Key。

### 4. 试运行

```bash
python /opt/ai-trader-agent/trader.py
```

确认 Agent 成功注册/登录后 Ctrl+C 退出。

### 5. 注册为系统服务（开机自启、崩溃自动重启）

```bash
sudo nano /etc/systemd/system/ai-trader-agent.service
```

写入以下内容：

```ini
[Unit]
Description=AI-Trader Auto Trading Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=nobody
WorkingDirectory=/opt/ai-trader-agent
ExecStart=/usr/bin/python3 /opt/ai-trader-agent/trader.py
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

启动服务：

```bash
sudo systemctl daemon-reload
sudo systemctl enable ai-trader-agent
sudo systemctl start ai-trader-agent
```

### 6. 查看运行状态

```bash
# 服务状态
sudo systemctl status ai-trader-agent

# 实时日志
tail -f /opt/ai-trader-agent/logs/trader.log

# 最近 50 条日志
journalctl -u ai-trader-agent -n 50
```

## 最小 VPS 配置推荐

| 平台 | 规格 | 月费 |
|------|------|------|
| AWS Lightsail | 512MB RAM, 1vCPU | ~$3.5 |
| 阿里云 ECS | 1C 1G | ~¥34 |
| 腾讯云轻量 | 1C 1G | ~¥28 |
| Hetzner CX22 | 2GB RAM, 1vCPU | ~€4 |

## 文件结构

```
agent/
├── trader.py      # 主交易脚本
├── .env           # 配置文件（敏感，勿提交 git）
├── .token         # 自动生成的 token 缓存
├── state.json     # 自动生成的持仓状态
└── logs/
    └── trader.log # 运行日志
```
