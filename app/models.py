from datetime import datetime, timezone
from enum import Enum
from pydantic import BaseModel, Field, field_validator
import re

class MarketType(str, Enum):
    forex = "forex"
    equities = "equities"
    crypto = "crypto"
    commodities = "commodities"
    indices = "indices"
    futures = "futures"
    bonds = "bonds"

class Side(str, Enum):
    buy = "buy"
    sell = "sell"

class Signal(BaseModel):
    strategy_id: str = Field(min_length=1, max_length=80)
    market: MarketType
    symbol: str = Field(min_length=1, max_length=40)
    side: Side
    entry: float = Field(gt=0, allow_inf_nan=False)
    stop_loss: float = Field(gt=0, allow_inf_nan=False)
    take_profit: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    rationale: str = Field(default="", max_length=500)

    @field_validator("symbol")
    @classmethod
    def safe_symbol(cls, v: str) -> str:
        v = v.strip().upper()
        if not re.fullmatch(r"[A-Z0-9._:/\\-]{1,40}", v):
            raise ValueError("Invalid symbol format")
        return v

class OrderRequest(BaseModel):
    signal: Signal
    quantity: float = Field(gt=0, allow_inf_nan=False)
    risk_amount: float = Field(gt=0, allow_inf_nan=False)
    idempotency_key: str = Field(min_length=8, max_length=120)

class OrderResult(BaseModel):
    order_id: str
    status: str
    market: MarketType
    symbol: str
    side: Side
    quantity: float
    paper: bool = True
    message: str
