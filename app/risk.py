from dataclasses import dataclass
from datetime import datetime, timezone
from threading import RLock
from math import isfinite
from app.models import Signal, Side

@dataclass(frozen=True)
class RiskConfig:
    initial_equity: float = 10_000.0
    risk_per_trade_pct: float = 0.005
    daily_loss_limit_pct: float = 0.015
    max_open_risk_pct: float = 0.02
    min_confidence: float = 0.60
    min_stop_distance_pct: float = 0.0005
    max_stop_distance_pct: float = 0.10

    def __post_init__(self) -> None:
        values = {
            "initial_equity": self.initial_equity,
            "risk_per_trade_pct": self.risk_per_trade_pct,
            "daily_loss_limit_pct": self.daily_loss_limit_pct,
            "max_open_risk_pct": self.max_open_risk_pct,
            "min_confidence": self.min_confidence,
            "min_stop_distance_pct": self.min_stop_distance_pct,
            "max_stop_distance_pct": self.max_stop_distance_pct,
        }
        for name, value in values.items():
            try:
                finite = isfinite(float(value))
            except (TypeError, ValueError):
                finite = False
            if not finite:
                raise ValueError(f"{name} must be finite")
        if self.initial_equity <= 0:
            raise ValueError("initial_equity must be positive")
        for name in ("risk_per_trade_pct", "daily_loss_limit_pct", "max_open_risk_pct"):
            value = getattr(self, name)
            if not 0 < value <= 1:
                raise ValueError(f"{name} must be in (0, 1]")
        if not 0 <= self.min_confidence <= 1:
            raise ValueError("min_confidence must be in [0, 1]")
        if not 0 < self.min_stop_distance_pct <= self.max_stop_distance_pct <= 1:
            raise ValueError("stop distance percentages must satisfy 0 < min <= max <= 1")

class RiskRejected(ValueError):
    pass

class RiskManager:
    """Central fail-closed risk gate. State is in-memory in this prototype."""
    def __init__(self, config: RiskConfig | None = None):
        self.config = config or RiskConfig()
        self._lock = RLock()
        self._day = datetime.now(timezone.utc).date()
        self._realized_pnl = 0.0
        self._open_risk = 0.0
        self._seen_keys: set[str] = set()
        self._halted = False

    def _roll_day(self) -> None:
        """Reset daily PnL/halt at the UTC day boundary; preserve open exposure."""
        current_day = datetime.now(timezone.utc).date()
        if current_day != self._day:
            self._day = current_day
            self._realized_pnl = 0.0
            self._halted = False

    def record_realized_pnl(self, pnl: float):
        if not isfinite(float(pnl)):
            raise ValueError("pnl must be finite")
        with self._lock:
            self._roll_day()
            self._realized_pnl += float(pnl)
            if self._realized_pnl <= -(self.config.initial_equity * self.config.daily_loss_limit_pct):
                self._halted = True

    def release_open_risk(self, amount: float):
        if not isfinite(float(amount)):
            raise ValueError("amount must be finite")
        with self._lock:
            self._roll_day()
            self._open_risk = max(0.0, self._open_risk - max(0.0, float(amount)))

    def approve(self, signal: Signal, risk_amount: float, idempotency_key: str) -> None:
        with self._lock:
            self._roll_day()
            if self._halted:
                raise RiskRejected("Trading halted: daily loss limit reached or safety halt active")
            try:
                risk_amount = float(risk_amount)
            except (TypeError, ValueError):
                raise RiskRejected("Risk amount must be finite and positive")
            if not isfinite(risk_amount) or risk_amount <= 0:
                raise RiskRejected("Risk amount must be finite and positive")
            if not idempotency_key or idempotency_key in self._seen_keys:
                raise RiskRejected("Duplicate or empty idempotency key")
            if signal.confidence < self.config.min_confidence:
                raise RiskRejected("Signal confidence below configured threshold")
            distance = abs(signal.entry - signal.stop_loss) / signal.entry
            if not isfinite(distance) or distance < self.config.min_stop_distance_pct:
                raise RiskRejected("Stop-loss distance is too small or invalid")
            if distance > self.config.max_stop_distance_pct:
                raise RiskRejected("Stop-loss distance is too large")
            if signal.side == Side.buy and signal.stop_loss >= signal.entry:
                raise RiskRejected("Buy stop-loss must be below entry")
            if signal.side == Side.sell and signal.stop_loss <= signal.entry:
                raise RiskRejected("Sell stop-loss must be above entry")
            if signal.take_profit is not None:
                if signal.side == Side.buy and signal.take_profit <= signal.entry:
                    raise RiskRejected("Buy take-profit must be above entry")
                if signal.side == Side.sell and signal.take_profit >= signal.entry:
                    raise RiskRejected("Sell take-profit must be below entry")
            max_trade_risk = self.config.initial_equity * self.config.risk_per_trade_pct
            if risk_amount > max_trade_risk + 1e-9:
                raise RiskRejected("Risk per trade exceeds configured limit")
            max_total_risk = self.config.initial_equity * self.config.max_open_risk_pct
            if self._open_risk + risk_amount > max_total_risk + 1e-9:
                raise RiskRejected("Aggregate open risk limit exceeded")
            daily_limit = self.config.initial_equity * self.config.daily_loss_limit_pct
            if self._realized_pnl <= -daily_limit:
                self._halted = True
                raise RiskRejected("Daily loss limit reached")
            self._seen_keys.add(idempotency_key)
            self._open_risk += risk_amount

    @property
    def status(self) -> dict:
        with self._lock:
            self._roll_day()
            return {
                "halted": self._halted,
                "realized_pnl": round(self._realized_pnl, 2),
                "open_risk": round(self._open_risk, 2),
                "daily_loss_limit": round(self.config.initial_equity * self.config.daily_loss_limit_pct, 2),
                "risk_per_trade_limit": round(self.config.initial_equity * self.config.risk_per_trade_pct, 2),
                "max_open_risk_limit": round(self.config.initial_equity * self.config.max_open_risk_pct, 2),
                "utc_day": self._day.isoformat(),
                "paper_only": True,
            }
