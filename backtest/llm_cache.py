"""Disk cache for LLM responses during backtesting.

Keyed by MD5(agent_name + date + context_hash), so re-running the same
backtest date with the same context is free (no API call).
"""
import hashlib
import json
from pathlib import Path


class LLMCache:
    def __init__(self, cache_dir: str = "backtest_cache"):
        self.path = Path(cache_dir) / "llm_responses.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data: dict = {}
        self._dirty = 0
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text())
            except Exception:
                self._data = {}

    def get(self, agent: str, date_str: str, context: dict):
        key = self._key(agent, date_str, context)
        return self._data.get(key)

    def set(self, agent: str, date_str: str, context: dict, decisions: dict):
        key = self._key(agent, date_str, context)
        self._data[key] = decisions
        self._dirty += 1
        if self._dirty >= 10:
            self.flush()

    def flush(self):
        self.path.write_text(json.dumps(self._data, indent=2, ensure_ascii=False, default=str))

    def stats(self) -> tuple:
        return len(self._data), self._dirty

    def _key(self, agent: str, date_str: str, context: dict) -> str:
        # Hash the context to avoid huge keys — only portfolio and candidates matter
        raw = "{}|{}|{}|{}|{}|{}".format(
            agent, date_str,
            context.get("portfolio", {}).get("cash", 0),
            len(context.get("portfolio", {}).get("positions", [])),
            len(context.get("candidates", [])),
            json.dumps(context.get("constraints", {}), sort_keys=True),
        )
        return hashlib.md5(raw.encode()).hexdigest()
