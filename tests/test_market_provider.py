import json
from datetime import datetime, timedelta, timezone
from io import BytesIO

import pytest

from app.market_provider import MarketProviderError, fetch_candles, provider_status
from app.models import MarketType


class FakeResponse:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def read(self):
        return self.payload


def payload_rows(count=35, interval_minutes=1):
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    rows = []
    for i in range(count):
        stamp = now - timedelta(minutes=(count - i - 1) * interval_minutes)
        close = 2000 + i
        rows.append({"datetime": stamp.strftime("%Y-%m-%d %H:%M:%S"),
                     "open": str(close - .2), "high": str(close + .5),
                     "low": str(close - .5), "close": str(close), "volume": "100"})
    return {"status": "ok", "values": list(reversed(rows))}


def test_provider_status_never_exposes_key(monkeypatch):
    monkeypatch.setenv("TWELVEDATA_API_KEY", "secret-provider-key")
    status = provider_status()
    assert status["configured"] is True
    assert status["order_routing"] is False
    assert "secret-provider-key" not in str(status)


def test_fetch_candles_parses_and_orders_provider_series(monkeypatch):
    import app.market_provider as provider
    monkeypatch.setenv("TWELVEDATA_API_KEY", "test-key")
    monkeypatch.setattr(provider, "urlopen", lambda request, timeout: FakeResponse(payload_rows()))
    candles = fetch_candles(MarketType.commodities, "XAU/USD", "1min", 35)
    assert len(candles) == 35
    assert candles[0].close < candles[-1].close
    assert candles[-1].symbol == "XAU/USD"


def test_fetch_candles_fails_closed_without_key(monkeypatch):
    monkeypatch.delenv("TWELVEDATA_API_KEY", raising=False)
    with pytest.raises(MarketProviderError, match="not configured"):
        fetch_candles(MarketType.forex, "EUR/USD")


def test_fetch_candles_rejects_provider_error_payload(monkeypatch):
    import app.market_provider as provider
    monkeypatch.setenv("TWELVEDATA_API_KEY", "test-key")
    monkeypatch.setattr(provider, "urlopen", lambda request, timeout: FakeResponse({"status": "error", "message": "bad symbol"}))
    with pytest.raises(MarketProviderError, match="rejected the symbol"):
        fetch_candles(MarketType.forex, "NOTREAL", "1min", 35)


def test_fetch_daily_candles_allow_weekend_closure_age(monkeypatch):
    import app.market_provider as provider
    monkeypatch.setenv("TWELVEDATA_API_KEY", "test-key")
    now = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=2)
    rows = []
    for i in range(35):
        stamp = now - timedelta(days=34-i)
        close = 2000 + i
        rows.append({"datetime": stamp.strftime("%Y-%m-%d"), "open": str(close),
                     "high": str(close+1), "low": str(close-1), "close": str(close), "volume": "1"})
    monkeypatch.setattr(provider, "urlopen", lambda request, timeout: FakeResponse({"status": "ok", "values": list(reversed(rows))}))
    candles = fetch_candles(MarketType.commodities, "XAU/USD", "1day", 35)
    assert len(candles) == 35
