"""Along-Trader Smart Scheduler — 智能进程调度器，按交易时段启动/守护 Agent。

白天 (北京时间): A 股 + ETF + 债券 (3 进程)
晚上 (北京时间): 美股 (1 进程)
错峰启动，避免内存峰值叠加。

调度器自身内存占用 ~5MB，全天候运行。
"""

import os
import sys
import time
import signal
import logging
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path

BEIJING_TZ = timezone(timedelta(hours=8))
LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "scheduler.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("scheduler")


# ── Agent 定义 ──────────────────────────────────────────────
# 每个 agent: (路径, 开市北京时间, 收市北京时间)
# 收市时间可能跨天 (如美股 21:30 → 次日 04:00)

AGENTS = {
    "us": {
        "dir": "/opt/ai-trader-agent",
        "start": (21, 25),   # 美东 9:25 ≈ 北京 21:25 (夏令时) / 22:25 (冬令时)
        "end":   (5, 30),    # 留足时间给收盘后日报发送 (夏令04:30/冬令05:30) + 优雅退出
    },
    "ashare": {
        "dir": "/opt/ai-trader-ashare",
        "start": (9,  25),   # 北京 9:25
        "end":   (15, 45),   # 留足时间给收盘后日报发送 (15:30) + 优雅退出
    },
    "etf": {
        "dir": "/opt/ai-trader-etf",
        "start": (9,  25),
        "end":   (15, 45),
    },
    "bond": {
        "dir": "/opt/ai-trader-bond",
        "start": (9,  25),
        "end":   (15, 45),
    },
}

# 每个 agent 的运行状态
state: dict[str, subprocess.Popen | None] = {name: None for name in AGENTS}
# 退出后不再自动重启的倒计时 (防止 agent 频繁 crash-restart 循环)
crash_count: dict[str, int] = {name: 0 for name in AGENTS}


def _time_to_minutes(h: int, m: int) -> int:
    return h * 60 + m


def _check_window(start: tuple[int, int], end: tuple[int, int]) -> tuple[bool, bool]:
    """返回 (is_past_start, is_before_end) 两个布尔值。

    支持跨天时间窗口 (如 21:30 → 04:00)。
    """
    now = datetime.now(BEIJING_TZ)
    current = _time_to_minutes(now.hour, now.minute)
    s = _time_to_minutes(*start)
    e = _time_to_minutes(*end)

    if s <= e:
        # 同一天: start=9:25, end=15:05
        return current >= s, current < e
    else:
        # 跨天: start=21:25, end=04:05
        # past_start = now >= start OR now < end (because if it's 03:00, it's before end which is 04:05)
        # before_end = now < end (if before midnight) OR now >= start (after midnight, already past end for that day)
        # Actually: in range = (current >= s) OR (current < e)
        past_start = current >= s
        before_end = current < e
        return past_start, before_end


def should_run(agent_name: str) -> bool:
    """判断 agent 当前是否应该在运行。"""
    cfg = AGENTS[agent_name]
    past_start, before_end = _check_window(cfg["start"], cfg["end"])

    start_h, start_m = cfg["start"]
    end_h, end_m = cfg["end"]
    s = _time_to_minutes(start_h, start_m)
    e = _time_to_minutes(end_h, end_m)

    if s <= e:
        # 同一天窗口
        return past_start and before_end
    else:
        # 跨天窗口: 在 start 之后 OR 在 end 之前 即为窗口内
        return past_start or before_end


def is_running(agent_name: str) -> bool:
    """检查 agent 进程是否在运行。"""
    proc = state[agent_name]
    if proc is None:
        return False
    if proc.poll() is not None:
        state[agent_name] = None
        return False
    return True


def start_agent(agent_name: str):
    """启动 agent 进程。"""
    cfg = AGENTS[agent_name]
    agent_dir = cfg["dir"]
    log_file = f"{agent_dir}/logs/trader.log"

    try:
        proc = subprocess.Popen(
            ["/usr/bin/python3", "trader.py"],
            cwd=agent_dir,
            stdout=open(log_file, "a"),
            stderr=subprocess.STDOUT,
            preexec_fn=os.setsid,
        )
        state[agent_name] = proc
        log.info("[%s] 已启动 (PID %d)", agent_name, proc.pid)
    except Exception as e:
        log.error("[%s] 启动失败: %s", agent_name, e)


def stop_agent(agent_name: str, force: bool = False):
    """停止 agent 进程。"""
    proc = state[agent_name]
    if proc is None:
        return

    try:
        if force:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except ProcessLookupError:
        pass

    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)

    state[agent_name] = None


def shutdown_all():
    """优雅关闭所有 agent。"""
    log.info("调度器退出，关闭所有 agent...")
    for name in AGENTS:
        if is_running(name):
            log.info("[%s] 正在关闭...", name)
            stop_agent(name, force=False)
            time.sleep(1)
    log.info("所有 agent 已关闭")


def main():
    log.info("══ Along-Trader 智能调度器启动 ══")

    # 清理孤儿进程：杀掉所有 trader.py 残留（防止重启/崩溃残留）
    result = subprocess.run(
        ["ps", "aux"], capture_output=True, text=True
    )
    for line in result.stdout.split("\n"):
        if "trader.py" in line and "grep" not in line:
            parts = line.split()
            if len(parts) >= 2:
                try:
                    pid = int(parts[1])
                    os.kill(pid, signal.SIGKILL)  # SIGKILL 确保孤儿进程立即终止
                    log.info("清理孤儿进程 PID %d", pid)
                except (ValueError, ProcessLookupError):
                    pass
    import time as _time
    _time.sleep(2)

    for name, cfg in AGENTS.items():
        sh, sm = cfg["start"]
        eh, em = cfg["end"]
        log.info("  %s: %02d:%02d → %02d:%02d  %s",
                 name, sh, sm, eh, em, cfg["dir"])

    signal.signal(signal.SIGTERM, lambda *_: shutdown_all() or sys.exit(0))
    signal.signal(signal.SIGINT,  lambda *_: shutdown_all() or sys.exit(0))

    # 调度主循环
    orphan_scan_counter = 0
    last_startup_time = 0.0  # 错峰启动：距上次启动至少间隔 30s
    while True:
        for name in AGENTS:
            active = is_running(name)
            needed = should_run(name)

            if needed and not active:
                # 应在运行但未运行 → 错峰启动（间隔 ≥ 30s，避免内存峰值叠加）
                if time.time() - last_startup_time >= 30:
                    start_agent(name)
                    last_startup_time = time.time()
                    crash_count[name] = 0
                # 否则等下一轮循环再启动

            elif needed and active:
                # 运行中 — 一切正常，重置崩溃计数
                crash_count[name] = 0

            elif not needed and active:
                # 不应运行但还在跑 → 发送 SIGTERM (agent 会在当前循环结束后退出)
                # 如果超出结束时间 30 分钟还没退出 → 强制 kill
                cfg = AGENTS[name]
                now = datetime.now(BEIJING_TZ)
                current = _time_to_minutes(now.hour, now.minute)
                e = _time_to_minutes(*cfg["end"])
                overdue = (current - e) % (24 * 60)

                if overdue > 15:
                    log.warning("[%s] 超出关闭窗口 %d 分钟，强制终止", name, overdue)
                    stop_agent(name, force=True)
                # 否则让它自己优雅退出

        # 每 10 次循环（5 分钟）扫描一次孤儿进程
        orphan_scan_counter += 1
        if orphan_scan_counter >= 10:
            orphan_scan_counter = 0
            my_pid = os.getpid()
            result = subprocess.run(["ps", "aux"], capture_output=True, text=True)
            for line in result.stdout.split("\n"):
                if "trader.py" in line and "grep" not in line:
                    parts = line.split()
                    if len(parts) >= 2:
                        try:
                            pid = int(parts[1])
                            # 检查是否被当前调度器追踪
                            tracked = any(
                                p is not None and p.pid == pid
                                for p in state.values()
                            )
                            if not tracked:
                                os.kill(pid, signal.SIGKILL)
                                log.warning("清理孤儿进程 PID %d (%s)", pid, parts[10][:50] if len(parts) > 10 else "")
                        except (ValueError, ProcessLookupError):
                            pass

        time.sleep(30)  # 每 30 秒检查一次


if __name__ == "__main__":
    main()
