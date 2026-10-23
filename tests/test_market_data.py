from datetime import datetime, timezone, timedelta
import pytest
from app.market_data import Candle, MarketDataQualityError, validate_candle_series
from app.models import MarketType

def candle(ts, **overrides):
    payload = {
        "market": MarketType.equities,
        "symbol": "test",
        "timestamp": ts,
        "open": 100,
        "high": 103,
        "low": 98,
        "close": 101,
        "volume": 1000,
    }
    payload.update(overrides)
    return Candle(**payload)

def test_candle_normalizes_symbol_and_timestamp_to_utc():
    ts = datetime(2026, 1, 1, 12, tzinfo=timezone(timedelta(hours=3)))
    item = candle(ts)
    assert item.symbol == "TEST"
    assert item.timestamp.hour == 9
    assert item.timestamp.utcoffset() == timedelta(0)

def test_rejects_naive_timestamp():
    with pytest.raises(ValueError, match="timezone"):
        candle(datetime(2026, 1, 1, 12))

def test_rejects_invalid_ohlc_relationship():
    with pytest.raises(ValueError, match="high"):
        candle(datetime(2026, 1, 1, tzinfo=timezone.utc), high=99)

def test_rejects_duplicate_or_out_of_order_timestamps():
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with pytest.raises(MarketDataQualityError, match="strictly increasing"):
        validate_candle_series([candle(t0), candle(t0)], now=t0 + timedelta(seconds=10))

def test_rejects_stale_market_data():
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with pytest.raises(MarketDataQualityError, match="stale"):
        validate_candle_series([candle(t0)], now=t0 + timedelta(seconds=301), max_staleness_seconds=300)

def test_accepts_fresh_ordered_series_and_reports_gaps():
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    report = validate_candle_series(
        [candle(t0), candle(t0 + timedelta(minutes=1))],
        now=t0 + timedelta(minutes=1, seconds=5),
        max_staleness_seconds=10,
    )
    assert report["quality_ok"] is True
    assert report["count"] == 2
    assert report["symbol"] == "TEST"
    assert report["observed_gaps"] == 1
