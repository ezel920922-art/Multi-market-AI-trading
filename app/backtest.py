"""Conservative, event-driven OHLCV backtest. Research only; never routes orders."""
from __future__ import annotations
from datetime import timedelta
from math import isfinite
from statistics import mean
from app.market_data import Candle, validate_candle_series


def _ema(values: list[float], period: int) -> list[float]:
    alpha = 2.0 / (period + 1.0)
    out = [values[0]]
    for value in values[1:]:
        out.append(alpha * value + (1 - alpha) * out[-1])
    return out


def run_ema_backtest(
    candles: list[Candle], *, fast_period: int = 8, slow_period: int = 21,
    initial_equity: float = 10_000, risk_pct: float = 0.005,
    spread_bps: float = 2, slippage_bps: float = 1, fee_bps: float = 1,
    stop_atr_multiplier: float = 1.5, reward_risk: float = 2.0,
) -> dict:
    if len(candles) < max(30, slow_period + 5):
        raise ValueError("Insufficient candles for the selected EMA periods")
    if not 2 <= fast_period < slow_period <= 100:
        raise ValueError("EMA periods must satisfy 2 <= fast < slow <= 100")
    for name, value in {"initial_equity": initial_equity, "risk_pct": risk_pct,
                        "spread_bps": spread_bps, "slippage_bps": slippage_bps,
                        "fee_bps": fee_bps, "stop_atr_multiplier": stop_atr_multiplier,
                        "reward_risk": reward_risk}.items():
        if not isfinite(float(value)):
            raise ValueError(f"{name} must be finite")
    if initial_equity <= 0 or not 0 < risk_pct <= 0.005:
        raise ValueError("Equity must be positive and risk_pct cannot exceed 0.5%")
    if min(spread_bps, slippage_bps, fee_bps) < 0 or max(spread_bps, slippage_bps, fee_bps) > 100:
        raise ValueError("Cost assumptions must be between 0 and 100 bps")
    if not 0.5 <= stop_atr_multiplier <= 10 or not 0.5 <= reward_risk <= 10:
        raise ValueError("Stop ATR multiplier and reward/risk must be between 0.5 and 10")
    # Historical data is validated against its own final timestamp, not wall-clock now.
    validate_candle_series(candles, now=candles[-1].timestamp + timedelta(seconds=1), max_staleness_seconds=2)
    closes = [float(c.close) for c in candles]
    highs = [float(c.high) for c in candles]
    lows = [float(c.low) for c in candles]
    fast = _ema(closes, fast_period)
    slow = _ema(closes, slow_period)
    equity = float(initial_equity)
    peak = equity
    max_drawdown_pct = 0.0
    trades: list[dict] = []
    equity_curve = [equity]
    daily_pnl: dict[str, float] = {}
    halted_days: set[str] = set()
    i = slow_period
    # Cost charged on each side. Spread is half-spread each side; slippage and fees per side.
    side_cost_rate = (spread_bps / 2 + slippage_bps + fee_bps) / 10_000
    while i < len(candles) - 1:
        direction = None
        if fast[i-1] <= slow[i-1] and fast[i] > slow[i]:
            direction = "buy"
        elif fast[i-1] >= slow[i-1] and fast[i] < slow[i]:
            direction = "sell"
        if direction is None:
            i += 1
            continue
        entry_index = i + 1  # no same-bar look-ahead: enter at the next candle open
        entry_bar = candles[entry_index]
        day = entry_bar.timestamp.date().isoformat()
        if day in halted_days or equity <= 0:
            i += 1
            continue
        true_ranges = []
        for k in range(max(1, entry_index - 14), entry_index):
            prev_close = closes[k-1]
            true_ranges.append(max(highs[k]-lows[k], abs(highs[k]-prev_close), abs(lows[k]-prev_close)))
        if not true_ranges:
            i += 1
            continue
        atr = mean(true_ranges)
        distance = atr * stop_atr_multiplier
        if not isfinite(distance) or distance <= 0:
            i += 1
            continue
        entry_raw = float(entry_bar.open)
        entry = entry_raw * (1 + side_cost_rate) if direction == "buy" else entry_raw * (1 - side_cost_rate)
        stop = entry_raw - distance if direction == "buy" else entry_raw + distance
        target = entry_raw + distance * reward_risk if direction == "buy" else entry_raw - distance * reward_risk
        risk_budget = equity * risk_pct
        quantity = risk_budget / distance
        exit_index = len(candles) - 1
        exit_raw = float(candles[-1].close)
        reason = "end_of_data"
        for j in range(entry_index, len(candles)):
            bar = candles[j]
            o, h, l = float(bar.open), float(bar.high), float(bar.low)
            if direction == "buy":
                if o <= stop:
                    exit_index, exit_raw, reason = j, o, "stop_gap"
                    break
                if o >= target:
                    exit_index, exit_raw, reason = j, o, "target_gap"
                    break
                hit_stop, hit_target = l <= stop, h >= target
            else:
                if o >= stop:
                    exit_index, exit_raw, reason = j, o, "stop_gap"
                    break
                if o <= target:
                    exit_index, exit_raw, reason = j, o, "target_gap"
                    break
                hit_stop, hit_target = h >= stop, l <= target
            if hit_stop:
                exit_index, exit_raw, reason = j, stop, "stop"
                break
            if hit_target:
                exit_index, exit_raw, reason = j, target, "target"
                break
        exit_bar = candles[exit_index]
        exit_exec = exit_raw * (1 - side_cost_rate) if direction == "buy" else exit_raw * (1 + side_cost_rate)
        pnl = ((exit_exec - entry) if direction == "buy" else (entry - exit_exec)) * quantity
        equity += pnl
        exit_day = exit_bar.timestamp.date().isoformat()
        daily_pnl[exit_day] = daily_pnl.get(exit_day, 0.0) + pnl
        if daily_pnl[exit_day] <= -(initial_equity * 0.015):
            halted_days.add(exit_day)
        peak = max(peak, equity)
        if peak > 0:
            max_drawdown_pct = max(max_drawdown_pct, (peak - equity) / peak * 100)
        trades.append({
            "side": direction, "entry_time": entry_bar.timestamp.isoformat(),
            "exit_time": exit_bar.timestamp.isoformat(), "entry_price": round(entry, 8),
            "exit_price": round(exit_exec, 8), "quantity": round(quantity, 8),
            "pnl": round(pnl, 2), "return_on_equity_pct": round(pnl / max(equity - pnl, 1e-12) * 100, 4),
            "exit_reason": reason,
        })
        equity_curve.append(equity)
        i = exit_index + 1
    wins = [t["pnl"] for t in trades if t["pnl"] > 0]
    losses = [-t["pnl"] for t in trades if t["pnl"] < 0]
    gross_profit, gross_loss = sum(wins), sum(losses)
    net_profit = equity - initial_equity
    return {
        "mode": "backtest_only", "execution_enabled": False, "orders_submitted": 0,
        "market": candles[-1].market.value, "symbol": candles[-1].symbol,
        "period_start": candles[0].timestamp.isoformat(), "period_end": candles[-1].timestamp.isoformat(),
        "strategy": {"type": "EMA crossover", "fast_period": fast_period, "slow_period": slow_period,
                      "risk_pct": risk_pct, "spread_bps": spread_bps, "slippage_bps": slippage_bps,
                      "fee_bps": fee_bps, "stop_atr_multiplier": stop_atr_multiplier,
                      "reward_risk": reward_risk, "same_bar_stop_first": True,
                      "entry_timing": "next_candle_open"},
        "metrics": {"initial_equity": round(initial_equity, 2), "ending_equity": round(equity, 2),
                    "net_profit": round(net_profit, 2), "net_return_pct": round(net_profit / initial_equity * 100, 4),
                    "trades": len(trades), "win_rate_pct": round(len(wins) / len(trades) * 100, 2) if trades else 0,
                    "profit_factor": round(gross_profit / gross_loss, 4) if gross_loss else (None if not gross_profit else "infinite"),
                    "expectancy_per_trade": round(net_profit / len(trades), 2) if trades else 0,
                    "max_drawdown_pct": round(max_drawdown_pct, 4), "daily_halted_days": len(halted_days)},
        "trades": trades,
        "disclaimer": "Historical simulation only; results are not predictive. Includes configured spread, slippage and fee assumptions; actual fills may differ.",
    }
