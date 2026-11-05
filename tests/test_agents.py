from datetime import datetime, timedelta, timezone
import pytest
from app.market_data import Candle
from app.models import MarketType
from app.agents import analyze_market


def sample_candles(count=35):
    now = datetime.now(timezone.utc)
    result = []
    for i in range(count):
        close = 2000.0 + i * 0.8
        result.append(Candle(
            market=MarketType.commodities,
            symbol="XAUUSD",
            timestamp=now - timedelta(minutes=count-i-1),
            open=close - 0.2,
            high=close + 0.4,
            low=close - 0.5,
            close=close,
            volume=1000 + i * 10,
        ))
    return result


def test_six_agents_analyze_verified_candles_without_execution():
    result = analyze_market(sample_candles())
    assert result["mode"] == "analysis_only"
    assert result["order_placed"] is False
    assert result["execution_enabled"] is False
    assert len(result["agents"]) == 6
    assert result["agents"][-1]["name"] == "DASH"
    assert result["symbol"] == "XAUUSD"


def test_six_agent_analysis_requires_at_least_thirty_candles():
    with pytest.raises(ValueError, match="At least 30"):
        analyze_market(sample_candles(12))
