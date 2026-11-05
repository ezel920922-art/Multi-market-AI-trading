from datetime import datetime, timedelta, timezone
from math import sin
import pytest
from app.backtest import run_ema_backtest
from app.market_data import Candle
from app.models import MarketType


def candles_series(count=180):
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    rows = []
    previous = 100.0
    for i in range(count):
        close = 100 + 0.025 * i + 3.2 * sin(i / 7.0) + 0.7 * sin(i / 2.2)
        open_price = previous
        rows.append(Candle(market=MarketType.equities, symbol="TEST",
            timestamp=now - timedelta(minutes=count-i-1), open=open_price,
            high=max(open_price, close) + 0.6, low=min(open_price, close) - 0.6,
            close=close, volume=1000 + i))
        previous = close
    return rows


def test_ema_backtest_is_research_only_and_accounts_for_costs():
    result = run_ema_backtest(candles_series())
    assert result["mode"] == "backtest_only"
    assert result["execution_enabled"] is False
    assert result["orders_submitted"] == 0
    assert result["strategy"]["entry_timing"] == "next_candle_open"
    assert result["strategy"]["same_bar_stop_first"] is True
    assert result["metrics"]["ending_equity"] > 0
    assert result["metrics"]["max_drawdown_pct"] >= 0
    assert result["metrics"]["trades"] == len(result["trades"])


def test_ema_backtest_rejects_risk_above_half_percent():
    with pytest.raises(ValueError, match="cannot exceed 0.5%"):
        run_ema_backtest(candles_series(), risk_pct=0.01)


def test_ema_backtest_rejects_insufficient_candles():
    with pytest.raises(ValueError, match="Insufficient candles"):
        run_ema_backtest(candles_series(25))


def test_ema_backtest_rejects_mixed_symbols():
    rows = candles_series()
    rows[-1] = rows[-1].model_copy(update={"symbol": "OTHER"})
    with pytest.raises(ValueError, match="mixes markets or symbols"):
        run_ema_backtest(rows)



def test_remaining_daily_risk_budget_caps_next_trade_to_daily_room():
    from app.backtest import _remaining_daily_risk_budget
    daily = {"2026-11-05": -140.0}
    # A $10,000 account has only $10 of the $150 daily loss budget left.
    assert _remaining_daily_risk_budget(10_000, 0.005, daily, "2026-11-05", 10_000) == 10.0


def test_remaining_daily_risk_budget_blocks_after_daily_limit():
    from app.backtest import _remaining_daily_risk_budget
    daily = {"2026-11-05": -150.0}
    assert _remaining_daily_risk_budget(10_000, 0.005, daily, "2026-11-05", 10_000) == 0.0


def test_remaining_daily_risk_budget_does_not_carry_loss_between_days():
    from app.backtest import _remaining_daily_risk_budget
    daily = {"2026-11-04": -150.0}
    assert _remaining_daily_risk_budget(10_000, 0.005, daily, "2026-11-05", 10_000) == 50.0
