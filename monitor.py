"""
Agent 健康监控 — 明天首次运行，遇异常立即邮件报警。
部署: /opt/monitor.py，cron 每 5 分钟执行一次。
"""
import smtplib, os, sys
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path

SMTP_HOST = "smtp.qq.com"
SMTP_PORT = 465
SMTP_USER = "your_email@qq.com"
SMTP_PASS = "your_smtp_password_here"
REPORT_EMAIL = "your_email@qq.com"

AGENTS = {
    "US":        {"dir": "/opt/ai-trader-agent",    "start": "21:25", "end": "05:30+1"},
    "A-share":   {"dir": "/opt/ai-trader-ashare",   "start": "09:25", "end": "15:45"},
    "ETF":       {"dir": "/opt/ai-trader-etf",      "start": "09:25", "end": "15:45"},
    "Bond":      {"dir": "/opt/ai-trader-bond",     "start": "09:25", "end": "15:45"},
}

alerts = []

now = datetime.now()
today_str = now.strftime("%Y-%m-%d")
time_str = now.strftime("%H:%M")


def send_alert(subject: str, body: str):
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = SMTP_USER
        msg["To"] = REPORT_EMAIL
        msg.attach(MIMEText(body, "plain", "utf-8"))
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=15) as s:
            s.login(SMTP_USER, SMTP_PASS)
            s.sendmail(SMTP_USER, [REPORT_EMAIL], msg.as_string())
        print(f"[{time_str}] Alert sent: {subject}")
    except Exception as e:
        print(f"[{time_str}] Alert FAILED: {e}")


def check_log(path: str, minutes: int = 10) -> list:
    """检查最近 N 分钟的日志是否有致命错误。"""
    logfile = Path(path) / "logs" / "trader.log"
    if not logfile.exists():
        return [f"{logfile} 不存在"]
    errors = []
    try:
        lines = logfile.read_text(encoding="utf-8").split("\n")
        # 只看最近的日志行（粗略判断，简单取最后 100 行）
        recent = lines[-100:]
        for line in recent:
            if not line.strip():
                continue
            # 提取时间戳
            try:
                ts = line[:19]  # "2026-05-31 10:11:42"
                log_time = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
                if (now - log_time).total_seconds() > minutes * 60:
                    continue
            except (ValueError, IndexError):
                continue
            # 检查错误关键字
            if any(kw in line for kw in ["ERROR", "Traceback", "NameError", "ImportError",
                                          "ModuleNotFoundError", "KeyError", "PermissionError"]):
                errors.append(line.strip())
    except Exception as e:
        errors.append(f"读取日志失败: {e}")
    return errors


def check_process(cfg: dict) -> bool:
    """检查 Agent 进程是否在运行（通过 /proc/<pid>/cwd 匹配目录）。"""
    try:
        import subprocess, os
        result = subprocess.run(["pgrep", "-f", "python3.*trader.py"],
                              capture_output=True, text=True, timeout=5)
        if result.returncode != 0 or not result.stdout.strip():
            return False
        for pid in result.stdout.strip().split():
            try:
                cwd = os.readlink(f"/proc/{pid}/cwd")
                if cwd.rstrip("/") == cfg["dir"].rstrip("/"):
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def is_in_time_window(start: str, end: str) -> bool:
    """检查当前是否在时间窗口内（支持跨天如 21:25-05:30）。"""
    sh, sm = map(int, start.split(":"))
    if "+" in end:  # 跨天
        end_clean = end.split("+")[0]
        eh, em = map(int, end_clean.split(":"))
        start_t = sh * 60 + sm
        end_t = eh * 60 + em
        curr_t = now.hour * 60 + now.minute
        # 跨天窗口：当前时间 >= start 或 <= end（第二天）
        return curr_t >= start_t or curr_t <= end_t
    else:
        eh, em = map(int, end.split(":"))
        start_t = sh * 60 + sm
        end_t = eh * 60 + em
        curr_t = now.hour * 60 + now.minute
        return start_t <= curr_t <= end_t


# ── 交易日判断（简单版：周末跳过。假日列表后续完善）─
def is_trading_day() -> bool:
    wd = now.weekday()  # 0=Mon, 6=Sun
    if wd >= 5:
        return False
    return True


# ── 主检查 ──────────────────────────────────────────────
if not is_trading_day():
    print(f"[{time_str}] 非交易日，跳过监控")
    sys.exit(0)

# 先检查调度器是否在运行
try:
    import subprocess
    r = subprocess.run(["systemctl", "is-active", "along-scheduler"],
                      capture_output=True, text=True, timeout=5)
    if "active" not in r.stdout:
        alerts.append("🚨 调度器已停止！")
except Exception:
    pass

import hashlib, json

STATE_FILE = "/opt/monitor_state.json"
DEDUP_MINUTES = 30  # 同一问题 30 分钟内不重复告警

def load_alert_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def save_alert_state(s):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(s, f)
    except Exception:
        pass

alert_state = load_alert_state()

for name, cfg in AGENTS.items():
    in_window = is_in_time_window(cfg["start"], cfg["end"])
    has_process = check_process(cfg)
    errors = check_log(cfg["dir"], minutes=10)

    # 在交易窗口内但没有进程 → 告警
    if in_window and not has_process:
        key = f"{name}_process_down"
        last_alert = alert_state.get(key, 0)
        try:
            last_dt = datetime.fromisoformat(last_alert)
            if (now - last_dt).total_seconds() < DEDUP_MINUTES * 60:
                print(f"[{time_str}] {name} 进程未运行告警已发送过，跳过重复")
            else:
                alerts.append(f"🚨 {name} Agent 交易窗口内({cfg['start']}-{cfg['end']})进程未运行")
                alert_state[key] = now.isoformat()
        except (ValueError, TypeError):
            alerts.append(f"🚨 {name} Agent 交易窗口内({cfg['start']}-{cfg['end']})进程未运行")
            alert_state[key] = now.isoformat()

    # 有致命错误（仅告警最近 5 条）
    if errors:
        recent_errors = errors[-5:]
        error_hash = hashlib.md5("\n".join(recent_errors).encode()).hexdigest()
        key = f"{name}_errors_{error_hash}"
        last_alert = alert_state.get(key, 0)
        try:
            last_dt = datetime.fromisoformat(last_alert)
            if (now - last_dt).total_seconds() < DEDUP_MINUTES * 60:
                print(f"[{time_str}] {name} 相同错误已告警过，跳过重复")
            else:
                alerts.append(f"⚠ {name} Agent 近10分钟 {len(errors)} 个错误:\n  " + "\n  ".join(recent_errors))
                alert_state[key] = now.isoformat()
        except (ValueError, TypeError):
            alerts.append(f"⚠ {name} Agent 近10分钟 {len(errors)} 个错误:\n  " + "\n  ".join(recent_errors))
            alert_state[key] = now.isoformat()

# 清理过期告警记录（超过 60 分钟的删掉）
alert_state = {k: v for k, v in alert_state.items()
               if (now - datetime.fromisoformat(v)).total_seconds() < 3600}

save_alert_state(alert_state)

# ── 发送 ──────────────────────────────────────────────
if alerts:
    body = f"监控时间: {today_str} {time_str}\n\n" + "\n\n".join(alerts)
    body += "\n\n---\nAI-Trader 监控系统自动发送"
    send_alert(f"🚨 AI-Trader 告警 - {today_str} {time_str}", body)
else:
    print(f"[{time_str}] All agents healthy")
