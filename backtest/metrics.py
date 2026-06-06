"""Performance metrics calculation for backtesting results."""
import math


def compute_metrics(snapshots: list, initial_capital: float,
                    risk_free_rate: float = 0.03) -> dict:
    """Calculate standard portfolio performance metrics.

    Args:
        snapshots: list of {date, total_value, cash, positions_value, trades, ...}
        initial_capital: starting portfolio value
        risk_free_rate: annual risk-free rate (default 3%)

    Returns:
        dict with all metrics
    """
    n = len(snapshots)
    if n == 0:
        return _empty_metrics()

    values = [s["total_value"] for s in snapshots]

    # Daily returns
    daily_returns = []
    prev = initial_capital
    for v in values:
        ret = (v - prev) / prev if prev != 0 else 0
        daily_returns.append(ret)
        prev = v

    final_value = values[-1]
    total_return = (final_value - initial_capital) / initial_capital
    annualized_return = (1 + total_return) ** (252 / n) - 1 if n > 0 else 0

    # Sharpe ratio
    mean_ret = sum(daily_returns) / n if n > 0 else 0
    variance = sum((r - mean_ret) ** 2 for r in daily_returns) / n if n > 1 else 0
    std_ret = math.sqrt(variance)
    daily_rf = risk_free_rate / 252
    sharpe = (mean_ret - daily_rf) / std_ret * math.sqrt(252) if std_ret > 0 else 0

    # Max drawdown
    peak = values[0]
    max_dd = 0.0
    for v in values:
        if v > peak:
            peak = v
        dd = (v - peak) / peak if peak > 0 else 0
        if dd < max_dd:
            max_dd = dd

    # Win rate
    win_days = sum(1 for r in daily_returns if r > 0)
    win_rate = win_days / n if n > 0 else 0

    # Turnover rate (annualized)
    total_trades = sum(len(s.get("trades", [])) for s in snapshots)
    total_traded_value = 0.0
    for s in snapshots:
        for t in s.get("trades", []):
            total_traded_value += t.get("price", 0) * t.get("quantity", 0)
    avg_equity = sum(values) / n if n > 0 else initial_capital
    turnover = (total_traded_value / avg_equity) * (252 / n) if n > 0 and avg_equity > 0 else 0

    # Volatility (annualized)
    volatility = std_ret * math.sqrt(252) if std_ret > 0 else 0

    # Calmar ratio
    calmar = annualized_return / abs(max_dd) if max_dd != 0 else 0

    return {
        "trading_days": n,
        "initial_capital": initial_capital,
        "final_value": round(final_value, 2),
        "total_return_pct": round(total_return * 100, 2),
        "annualized_return_pct": round(annualized_return * 100, 2),
        "sharpe_ratio": round(sharpe, 2),
        "max_drawdown_pct": round(max_dd * 100, 2),
        "volatility_pct": round(volatility * 100, 2),
        "calmar_ratio": round(calmar, 2),
        "win_rate_pct": round(win_rate * 100, 1),
        "total_trades": total_trades,
        "turnover_rate": round(turnover, 2),
    }


def _empty_metrics() -> dict:
    return {
        "trading_days": 0,
        "initial_capital": 0,
        "final_value": 0,
        "total_return_pct": 0,
        "annualized_return_pct": 0,
        "sharpe_ratio": 0,
        "max_drawdown_pct": 0,
        "volatility_pct": 0,
        "calmar_ratio": 0,
        "win_rate_pct": 0,
        "total_trades": 0,
        "turnover_rate": 0,
    }
