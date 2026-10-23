from datetime import datetime, timezone
from math import isfinite
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from app.models import MarketType

class MarketDataQualityError(ValueError):
    """Raised when a candle series is unsafe for downstream decisions."""

class Candle(BaseModel):
    model_config = ConfigDict(frozen=True)

    market: MarketType
    symbol: str = Field(min_length=1, max_length=40)
    timestamp: datetime
    open: float = Field(gt=0, allow_inf_nan=False)
    high: float = Field(gt=0, allow_inf_nan=False)
    low: float = Field(gt=0, allow_inf_nan=False)
    close: float = Field(gt=0, allow_inf_nan=False)
    volume: float = Field(ge=0, allow_inf_nan=False)

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        value = value.strip().upper()
        if not value:
            raise ValueError("symbol cannot be blank")
        return value

    @field_validator("timestamp")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must include a timezone")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def check_ohlc(self):
        if self.high < max(self.open, self.close):
            raise ValueError("high must be >= open and close")
        if self.low > min(self.open, self.close):
            raise ValueError("low must be <= open and close")
        if self.high < self.low:
            raise ValueError("high must be >= low")
        return self

def validate_candle_series(
    candles: list[Candle],
    *,
    now: datetime | None = None,
    max_staleness_seconds: int = 300,
) -> dict:
    """Validate ordering/identity/freshness before a strategy consumes candles.

    This deliberately does not infer exchange calendars or treat every time gap
    as an error; market sessions and holidays differ by instrument/provider.
    """
    if not candles:
        raise MarketDataQualityError("No candles supplied")
    if max_staleness_seconds < 0:
        raise ValueError("max_staleness_seconds must be >= 0")
    expected_market = candles[0].market
    expected_symbol = candles[0].symbol
    previous = None
    gaps = 0
    for candle in candles:
        if candle.market != expected_market or candle.symbol != expected_symbol:
            raise MarketDataQualityError("Candle series mixes markets or symbols")
        if previous is not None:
            delta = (candle.timestamp - previous.timestamp).total_seconds()
            if delta <= 0:
                raise MarketDataQualityError("Candle timestamps must be strictly increasing")
            if delta > 1:
                gaps += 1
        previous = candle
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("now must include a timezone")
    current = current.astimezone(timezone.utc)
    newest = candles[-1].timestamp
    if newest > current:
        raise MarketDataQualityError("Latest candle timestamp is in the future")
    age = (current - newest).total_seconds()
    if age > max_staleness_seconds:
        raise MarketDataQualityError("Market data is stale")
    return {
        "market": expected_market.value,
        "symbol": expected_symbol,
        "count": len(candles),
        "latest_timestamp": newest.isoformat(),
        "age_seconds": round(age, 3),
        "observed_gaps": gaps,
        "quality_ok": True,
    }
