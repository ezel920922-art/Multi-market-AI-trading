"""ASTRA six-agent signal layer (simulation only).

Each agent has ONE role and a deterministic, rule-based entry. Agents only
*propose*; the simulation sends every proposal through the central RiskManager
and fills it at the NEXT candle open (no look-ahead). Nothing here talks to a
broker, exchange or LLM, and nothing here is a trade recommendation.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import log
from statistics import pstdev

from app.models import Side


@dataclass(frozen=True)
class AgentSpec:
    key: str
    name: str
    role: str
    color: str


@dataclass(frozen=True)
class Proposal:
    side: Side
    stop_distance: float            # absolute price distance from fill
    target_distance: float | None   # None = no fixed target (runner)
    strength: float                 # 0..1 raw signal strength
    rationale: str
    trailing_atr: float | None = None   # trailing stop in ATR units
    max_bars: int | None = None         # time stop
    cut_r: float | None = None          # close early if unrealized <= -cut_r * R


def atr(candles: list[dict], n: int = 14) -> float:
    window = candles[-(n + 1):]
    if len(window) < 2:
        return 0.0
    trs = []
    for prev, cur in zip(window, window[1:]):
        trs.append(max(cur["high"] - cur["low"], abs(cur["high"] - prev["close"]), abs(cur["low"] - prev["close"])))
    return sum(trs) / len(trs)


def ema(values: list[float], n: int) -> float:
    k = 2 / (n + 1)
    out = values[0]
    for v in values[1:]:
        out = v * k + out * (1 - k)
    return out


def confidence_from(strength: float) -> float:
    """Map raw strength to a Signal confidence; the risk gate requires >= 0.60."""
    return round(min(0.90, max(0.0, strength)), 4)


class BaseAgent:
    spec: AgentSpec
    min_bars = 35

    def propose(self, candles: list[dict]) -> Proposal | None:
        raise NotImplementedError

    def ready(self, candles: list[dict]) -> bool:
        return len(candles) >= self.min_bars


class Nova(BaseAgent):
    spec = AgentSpec("nova", "NOVA", "Finds the ceiling", "#1aa37a")

    def propose(self, candles):
        if not self.ready(candles):
            return None
        a = atr(candles)
        last = candles[-1]
        ceiling = max(c["high"] for c in candles[-31:-1])
        if a <= 0 or last["high"] < ceiling * 0.9995 or last["close"] >= last["open"]:
            return None
        rejection = (ceiling - last["close"]) / a
        if rejection < 0.3:
            return None
        return Proposal(Side.sell, (ceiling - last["close"]) + 0.5 * a, 1.5 * a,
                        min(0.90, 0.62 + 0.12 * rejection),
                        "Rejection at 30-bar ceiling", max_bars=20)


class Orin(BaseAgent):
    spec = AgentSpec("orin", "ORIN", "Prices the tail", "#2a9fd6")

    def propose(self, candles):
        if not self.ready(candles):
            return None
        closes = [c["close"] for c in candles[-42:]]
        rets = [log(b / a_) for a_, b in zip(closes, closes[1:])]
        sigma = pstdev(rets[:-1]) if len(rets) > 5 else 0.0
        if sigma <= 0:
            return None
        z = rets[-1] / sigma
        if abs(z) < 2.2:
            return None
        a = atr(candles)
        side = Side.sell if z > 0 else Side.buy
        return Proposal(side, 1.5 * a, 1.0 * a, min(0.90, 0.62 + 0.08 * (abs(z) - 2.2)),
                        f"Fade {abs(z):.1f}-sigma tail move", max_bars=10)


class Vesk(BaseAgent):
    spec = AgentSpec("vesk", "VESK", "Holds the runners", "#7a4be0")

    def propose(self, candles):
        if not self.ready(candles):
            return None
        closes = [c["close"] for c in candles]
        fast, slow = ema(closes[-30:], 8), ema(closes[-30:], 21)
        prior = closes[-21:-1]
        a = atr(candles)
        last = closes[-1]
        if fast > slow and last >= max(prior):
            side = Side.buy
        elif fast < slow and last <= min(prior):
            side = Side.sell
        else:
            return None
        return Proposal(side, 2.5 * a, None, 0.72, "Trend breakout, trailing stop",
                        trailing_atr=2.5, max_bars=60)


class Luma(BaseAgent):
    spec = AgentSpec("luma", "LUMA", "Reads the flow", "#e0508a")

    def propose(self, candles):
        if not self.ready(candles):
            return None
        recent = candles[-10:]
        total = sum(c["volume"] for c in recent)
        if total <= 0:
            return None
        imbalance = sum(c["volume"] * (1 if c["close"] > c["open"] else -1 if c["close"] < c["open"] else 0)
                        for c in recent) / total
        drift = candles[-1]["close"] - candles[-6]["close"]
        if abs(imbalance) < 0.35 or imbalance * drift <= 0:
            return None
        a = atr(candles)
        side = Side.buy if imbalance > 0 else Side.sell
        return Proposal(side, 1.2 * a, 2.0 * a, min(0.90, 0.55 + 0.5 * abs(imbalance)),
                        f"Volume-flow imbalance {imbalance:+.2f}", max_bars=24)


class Kira(BaseAgent):
    spec = AgentSpec("kira", "KIRA", "Cuts losers early", "#e08a1e")

    def propose(self, candles):
        if not self.ready(candles):
            return None
        closes = [c["close"] for c in candles]
        fast, slow = ema(closes[-30:], 8), ema(closes[-30:], 21)
        last = candles[-1]
        a = atr(candles)
        if fast > slow and slow < last["close"] < fast and last["close"] > last["open"]:
            side = Side.buy
        elif fast < slow and fast < last["close"] < slow and last["close"] < last["open"]:
            side = Side.sell
        else:
            return None
        return Proposal(side, 0.8 * a, 1.6 * a, 0.68, "Pullback resumption; cuts at -0.5R",
                        max_bars=12, cut_r=0.5)


class Dash(BaseAgent):
    spec = AgentSpec("dash", "DASH", "Scalps the open", "#b8b800")

    def propose(self, candles):
        if not self.ready(candles):
            return None
        a = atr(candles)
        c3 = candles[-3:]
        bodies = [c["close"] - c["open"] for c in c3]
        if a <= 0:
            return None
        if all(b > 0.3 * a for b in bodies):
            side = Side.buy
        elif all(b < -0.3 * a for b in bodies):
            side = Side.sell
        else:
            return None
        return Proposal(side, 0.6 * a, 0.9 * a, 0.64, "3-bar momentum scalp", max_bars=6)


def build_agents() -> list[BaseAgent]:
    return [Nova(), Orin(), Vesk(), Luma(), Kira(), Dash()]
