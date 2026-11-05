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
    assert result["agents"][-1]["name"] == "KELLY"
    assert [agent["name"] for agent in result["agents"]] == ["PRIOR", "EDGE", "SPOTTER", "TAKER", "CLOSER", "KELLY"]
    assert result["symbol"] == "XAUUSD"


def test_six_agent_analysis_requires_at_least_thirty_candles():
    with pytest.raises(ValueError, match="At least 30"):
        analyze_market(sample_candles(12))


def test_historical_spectrogram_replays_windows_without_forward_prediction():
    from app.agents import analyze_historical_spectrogram
    result = analyze_historical_spectrogram(sample_candles())
    assert result['mode'] == 'historical_replay'
    assert result['predictive'] is False
    assert result['windows'] >= 1
    assert result['agents'] == ['PRIOR', 'EDGE', 'SPOTTER', 'TAKER', 'CLOSER', 'KELLY']
    assert all(len(row['agents']) == 6 for row in result['rows'])



def test_agent_freshness_uses_selected_daily_interval():
    from app.agents import analyze_market
    candles = sample_candles()
    candles = [
        Candle(
            market=c.market, symbol=c.symbol,
            timestamp=c.timestamp - timedelta(hours=2),
            open=c.open, high=c.high, low=c.low, close=c.close, volume=c.volume,
        )
        for c in candles
    ]
    with pytest.raises(ValueError, match="stale"):
        analyze_market(candles, interval="1min")
    result = analyze_market(candles, interval="1day")
    assert result["data_quality"]["quality_ok"] is True


def test_agent_rejects_unsupported_interval():
    with pytest.raises(ValueError, match="Unsupported candle interval"):
        analyze_market(sample_candles(), interval="2h")
