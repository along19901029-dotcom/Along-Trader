"""
DeepSeek-v4-pro LLM 决策客户端 — 4 Agent 共享模块
基于 requests 直接调用（无需额外依赖），部署路径: /opt/ai-trader-common/llm_client.py
"""

import json
import logging
import time
from typing import Optional

import requests

log = logging.getLogger("trader")

# ── 配置 ─────────────────────────────────────────────────
API_KEY = "your_deepseek_api_key_here"
BASE_URL = "https://api.deepseek.com/v1/chat/completions"
MODEL = "deepseek-v4-pro"
MAX_TOKENS = 4096
TEMPERATURE = 0.3
MAX_RETRIES = 2
TIMEOUT = 45


def deepseek_ask(system_prompt: str, user_message: dict, timeout: int = TIMEOUT) -> Optional[dict]:
    """
    调用 DeepSeek API，传入 system prompt + user context dict，
    返回解析后的 JSON dict。失败重试 MAX_RETRIES 次。
    """
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(user_message, ensure_ascii=False, default=str)},
        ],
        "max_tokens": MAX_TOKENS,
        "temperature": TEMPERATURE,
        "response_format": {"type": "json_object"},
    }

    for attempt in range(1 + MAX_RETRIES):
        try:
            log.info("DeepSeek 调用 (attempt %d) ...", attempt + 1)
            t0 = time.time()

            resp = requests.post(BASE_URL, headers=headers, json=payload, timeout=timeout)
            resp.raise_for_status()
            body = resp.json()

            elapsed = time.time() - t0
            usage = body.get("usage", {})
            log.info(
                "DeepSeek OK (%.1fs, in=%d out=%d total=%d)",
                elapsed,
                usage.get("prompt_tokens", 0),
                usage.get("completion_tokens", 0),
                usage.get("total_tokens", 0),
            )

            content = body["choices"][0]["message"]["content"]
            return json.loads(content)

        except json.JSONDecodeError as e:
            log.warning("DeepSeek JSON 解析失败 (attempt %d): %s", attempt + 1, e)
            if attempt < MAX_RETRIES:
                time.sleep(2 ** attempt)
        except requests.RequestException as e:
            log.warning("DeepSeek 请求异常 (attempt %d): %s", attempt + 1, e)
            if attempt < MAX_RETRIES:
                time.sleep(2 ** attempt)
        except (KeyError, IndexError) as e:
            log.warning("DeepSeek 响应格式异常 (attempt %d): %s", attempt + 1, e)
            if attempt < MAX_RETRIES:
                time.sleep(2 ** attempt)

    log.error("DeepSeek 调用全部失败")
    return None


def apply_decisions(
    decisions: dict,
    state: dict,
    market_type: str,
    execute_buy_fn,
    execute_sell_fn,
    fetch_price_fn,
    is_limit_up_fn=None,
    is_limit_down_fn=None,
) -> tuple:
    """
    后置护栏：校验 LLM 决策并执行。

    参数:
        decisions: LLM 返回的 {"sells": [...], "buys": [...], ...}
        state: 当前状态 dict
        market_type: "us" | "ashare" | "etf" | "bond"
        execute_buy_fn / execute_sell_fn: 买卖执行函数
        fetch_price_fn: 获取价格函数
        is_limit_up_fn / is_limit_down_fn: 涨跌停判断（A 股/ETF 需要）

    返回: (executed_sells: int, executed_buys: int, blocked: list)
    """
    positions = state.get("positions", {})
    locked = state.get("locked_shares", {})
    executed_sells = 0
    executed_buys = 0
    blocked = []

    # ── 执行卖出 ─────────────────────────────────────────
    for item in decisions.get("sells", []):
        sym = str(item.get("symbol", ""))
        reason = item.get("reason", "LLM signal")

        if not sym:
            blocked.append("sell: empty symbol")
            continue
        if sym not in positions:
            blocked.append(f"sell: {sym} not held")
            continue
        if sym in locked and locked[sym] > 0:
            blocked.append(f"sell: {sym} T+1 locked")
            continue
        if is_limit_down_fn and is_limit_down_fn(sym):
            blocked.append(f"sell: {sym} limit-down")
            continue

        ok = execute_sell_fn(sym, reason)
        if ok:
            executed_sells += 1

    # ── 执行买入 ─────────────────────────────────────────
    current_pos_count = len(positions)
    max_positions = 10
    max_per_position = 50000

    if market_type == "us":
        max_positions = 10
        max_per_position = 10000
    elif market_type == "ashare":
        max_positions = 10
        max_per_position = 50000
    elif market_type == "etf":
        max_positions = 10
        max_per_position = 50000
    elif market_type == "bond":
        max_positions = 7

    for item in decisions.get("buys", []):
        sym = str(item.get("symbol", ""))
        reason = item.get("reason", "LLM signal")

        if not sym:
            blocked.append("buy: empty symbol")
            continue
        if sym in positions:
            blocked.append(f"buy: {sym} already held")
            continue
        if current_pos_count + executed_buys >= max_positions:
            blocked.append(f"buy: max {max_positions} positions")
            break
        if is_limit_up_fn and is_limit_up_fn(sym):
            blocked.append(f"buy: {sym} limit-up")
            continue

        price = fetch_price_fn(sym)
        if not price or price <= 0:
            blocked.append(f"buy: {sym} no price")
            continue

        ok = execute_buy_fn(sym)
        if ok:
            executed_buys += 1

    if blocked:
        log.warning("护栏拦截 (%d): %s", len(blocked), "; ".join(blocked[:5]))
    log.info("LLM 决策执行: 卖 %d, 买 %d, 拦截 %d", executed_sells, executed_buys, len(blocked))

    return executed_sells, executed_buys, blocked
