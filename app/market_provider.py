"""Read-only Twelve Data OHLCV adapter. No order-routing capabilities."""
from __future__ import annotations

import os
import time
from threading import RLock
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from datetime import datetime, timezone

from app.market_data import Candle, MarketDataQualityError, validate_candle_series
from app.models import MarketType

BASE_URL = "https://api.twelvedata.com/time_series"
ALLOWED_INTERVALS = {"1min", "5min", "15min", "30min", "1h", "4h", "1day"}
_CACHE: dict[tuple, tuple[float, list[Candle]]] = {}
_LOCK = RLock()

class MarketProviderError(RuntimeError):
    pass

def provider_status() -> dict:
    configured = bool(os.getenv("TWELVEDATA_API_KEY", "").strip())
    return {"provider": "Twelve Data", "configured": configured,
            "mode": "read_only_ohlcv" if configured else "not_configured",
            "order_routing": False,
            "supported_intervals": sorted(ALLOWED_INTERVALS)}

def fetch_candles(market: MarketType, symbol: str, interval: str = "1min", outputsize: int = 100) -> list[Candle]:
    api_key = os.getenv("TWELVEDATA_API_KEY", "").strip()
    if not api_key:
        raise MarketProviderError("TWELVEDATA_API_KEY is not configured")
    symbol = symbol.strip().upper()
    if not symbol or len(symbol) > 40:
        raise MarketProviderError("Invalid symbol")
    if interval not in ALLOWED_INTERVALS:
        raise MarketProviderError("Unsupported interval")
    if not 30 <= outputsize <= 500:
        raise MarketProviderError("outputsize must be between 30 and 500")
    cache_key = (market.value, symbol, interval, outputsize)
    now_mono = time.monotonic()
    with _LOCK:
        cached = _CACHE.get(cache_key)
        if cached and now_mono - cached[0] < 15:
            return list(cached[1])
    query = urlencode({"symbol": symbol, "interval": interval, "outputsize": outputsize,
                       "timezone": "UTC", "format": "JSON", "apikey": api_key})
    request = Request(BASE_URL + "?" + query, headers={"Accept": "application/json", "User-Agent": "MultiMarketAI/0.4"})
    try:
        with urlopen(request, timeout=8) as response:
            import json
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise MarketProviderError("Market data provider returned an HTTP error") from exc
    except (URLError, TimeoutError, ValueError):
        raise MarketProviderError("Market data provider is temporarily unavailable") from None
    if payload.get("status") == "error" or not isinstance(payload.get("values"), list):
        # Do not echo provider payloads: they may include request details.
        raise MarketProviderError("Market data provider rejected the symbol, interval, or API plan")
    candles = []
    try:
        for row in reversed(payload["values"]):
            timestamp = datetime.fromisoformat(row["datetime"].replace("Z", "+00:00"))
            # Provider request explicitly sets timezone=UTC; its JSON timestamps are often naive.
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            candles.append(Candle(market=market, symbol=symbol, timestamp=timestamp,
                open=float(row["open"]), high=float(row["high"]), low=float(row["low"]),
                close=float(row["close"]), volume=float(row.get("volume") or 0)))
    except (KeyError, TypeError, ValueError) as exc:
        raise MarketProviderError("Market data provider returned malformed candle data") from exc
    freshness_limits = {"1min": 300, "5min": 900, "15min": 1800,
                        "30min": 3600, "1h": 7200, "4h": 18000, "1day": 90000}
    try:
        validate_candle_series(candles, max_staleness_seconds=freshness_limits[interval])
    except MarketDataQualityError as exc:
        raise MarketProviderError(str(exc)) from exc
    with _LOCK:
        _CACHE[cache_key] = (now_mono, list(candles))
    return candles
