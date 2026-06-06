"""Financial indicators lookup for A-share trading agent.
Reads from pre-exported financials.json, with Tushare fallback for missing stocks.
"""
import json
import logging
from pathlib import Path

FINANCIAL_JSON = Path(__file__).parent / "financials.json"
_financial_lookup: dict = {}
_pro = None
_TUSHARE_TOKEN = "your_tushare_token_here"

log = logging.getLogger(__name__) if __name__ != "__main__" else logging.getLogger("financials")


def _ensure_financials():
    global _financial_lookup
    if _financial_lookup:
        return
    if FINANCIAL_JSON.exists():
        with open(FINANCIAL_JSON, "r", encoding="utf-8") as f:
            raw = json.load(f)
        _financial_lookup = raw
        log.debug("Loaded %s stocks from financials.json", len(_financial_lookup))


def _sina_to_ts(code: str) -> str:
    if code.startswith("sh"): return code[2:] + ".SH"
    if code.startswith("sz"): return code[2:] + ".SZ"
    return code


def _get_pro():
    global _pro
    if _pro is None:
        try:
            import tushare as ts
            import os
            token = os.getenv("TUSHARE_TOKEN", _TUSHARE_TOKEN)
            ts.set_token(token)
            _pro = ts.pro_api()
        except Exception:
            return None
    return _pro


def get_financial(code: str) -> dict:
    """Return latest financial indicators: {rev_growth, profit_growth, debt_ratio} or {}."""
    _ensure_financials()
    ts_code = _sina_to_ts(code)

    reports = _financial_lookup.get(ts_code, [])
    if not reports:
        # Fallback: query Tushare API directly
        pro = _get_pro()
        if pro:
            try:
                df = pro.fina_indicator(
                    ts_code=ts_code,
                    fields="ann_date,roe,debt_to_assets,or_yoy,netprofit_yoy,eps",
                    limit=1,
                )
                if df is not None and len(df) > 0:
                    r = df.iloc[0]
                    new_entry = {
                        "a": str(r["ann_date"])[:10],
                        "e": str(r.get("end_date", ""))[:10],
                        "r": round(float(r["roe"]), 2) if r["roe"] is not None and r["roe"] != "" else None,
                        "d": round(float(r["debt_to_assets"]), 2) if r["debt_to_assets"] is not None and r["debt_to_assets"] != "" else None,
                        "rg": round(float(r["or_yoy"]), 2) if r["or_yoy"] is not None and r["or_yoy"] != "" else None,
                        "pg": round(float(r["netprofit_yoy"]), 2) if r["netprofit_yoy"] is not None and r["netprofit_yoy"] != "" else None,
                        "ep": round(float(r["eps"]), 3) if r["eps"] is not None and r["eps"] != "" else None,
                    }
                    _financial_lookup[ts_code] = [new_entry]
                    reports = [new_entry]
            except Exception:
                return {}

    if not reports:
        return {}
    r = reports[0]
    result = {}
    if r.get("rg") is not None:
        result["rev_growth"] = r["rg"]
    if r.get("pg") is not None:
        result["profit_growth"] = r["pg"]
    if r.get("d") is not None:
        result["debt_ratio"] = r["d"]
    return result
