"""ASTRA simulation: six agents on a SIMULATED market, gated by the real RiskManager.

Everything here is synthetic. Prices come from a seeded random-walk with
regime switching, not from any exchange, so results say nothing about live
performance. Guarantees enforced in code (and covered by tests):

* every entry passes ``RiskManager.approve`` (same limits as the paper API);
* signals are computed on a CLOSED candle and filled at the NEXT candle open
  (no look-ahead), with slippage and fees on both sides;
* when stop and target are both touched inside one candle, the stop wins;
* this module never imports a broker/exchange connector and never touches the
  real paper engine's risk state (it owns a separate RiskManager).
"""
from __future__ import annotations

import random
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import RLock

from app.astra_agents import BaseAgent, Proposal, atr, build_agents, confidence_from
from app.models import MarketType, Side, Signal
from app.risk import RiskConfig, RiskManager, RiskRejected

SYMBOL = "BTC/USD"
FEE_RATE = 0.0004      # per side, on notional
SLIPPAGE = 0.0002      # adverse, on entries and stop exits
COOLDOWN_BARS = 3
RISK_FRACTION = 0.25   # simulation sizes trades at 25% of the per-trade limit so six agents fit in the daily loss limit
BAR_MINUTES = 15
SIM_DAY_BARS = 96      # one simulated trading day = 96 fifteen-minute candles
REGIMES = {
    "trend_up": (0.0014, 0.0050),
    "trend_down": (-0.0014, 0.0050),
    "range": (0.0, 0.0038),
    "spike": (0.0, 0.0085),
}
DISCLAIMER = ("SIMULATION: synthetic prices, paper only. Not a trade recommendation and not evidence "
              "of live performance.")


@dataclass
class Position:
    agent: str
    side: Side
    entry: float
    stop: float
    target: float | None
    qty: float
    risk_amount: float
    proposal: Proposal
    signal_tick: int
    fill_tick: int
    best_close: float
    bars: int = 0


class AstraSimulation:
    def __init__(self, seed: int = 20261105, start_price: float = 62_000.0,
                 initial_equity: float = 50_000.0, interval: float = 1.0, warmup: int = 60):
        self._lock = RLock()
        self._rng = random.Random(seed)
        self.interval = interval
        self.initial_equity = float(initial_equity)
        self.risk = RiskManager(RiskConfig(initial_equity=self.initial_equity))
        self.agents: list[BaseAgent] = build_agents()
        self.tick = 0
        self._t0 = datetime(2026, 11, 5, 0, 0, tzinfo=timezone.utc)
        self._last_close = float(start_price)
        self._regime = "range"
        self._regime_left = 0
        self._anchor = float(start_price)
        self.candles: list[dict] = []
        self.positions: dict[str, Position] = {}
        self.pending: dict[str, tuple[Proposal, int, float, Signal]] = {}
        self.cooldown: dict[str, int] = {a.spec.key: 0 for a in self.agents}
        self.last_block_tick: dict[str, int] = {a.spec.key: -99 for a in self.agents}
        self.trades: deque[dict] = deque(maxlen=500)
        self.stream: deque[dict] = deque(maxlen=40)
        self.realized = 0.0
        self.closed = 0
        self.wins = 0
        self.rejected = 0
        self.sim_days = 0
        self.halted_ticks = 0
        self._was_halted = False
        self.evaluated = 0
        self.stats = {a.spec.key: {"pnl": 0.0, "trades": 0, "wins": 0, "hist": deque(maxlen=60)}
                      for a in self.agents}
        self.equity_curve: deque[float] = deque(maxlen=120)
        self.hesitation: deque[float] = deque(maxlen=120)
        self.latest_strength: dict[str, float] = {a.spec.key: 0.0 for a in self.agents}
        self.latest_dir: dict[str, int] = {a.spec.key: 0 for a in self.agents}
        self.last_actor = self.agents[0].spec.key
        self._last_advance = time.monotonic()
        for _ in range(warmup):
            self.step()

    # ---------- market ----------
    def _next_candle(self) -> dict:
        rng = self._rng
        if self._regime_left <= 0:
            self._regime = rng.choices(list(REGIMES), weights=[3, 3, 3, 1])[0]
            self._regime_left = rng.randint(25, 70)
            self._anchor = self._last_close
        self._regime_left -= 1
        drift, vol = REGIMES[self._regime]
        if self._regime == "range":
            drift = -0.04 * (self._last_close - self._anchor) / self._anchor
        o = self._last_close
        r = drift + vol * rng.gauss(0, 1)
        c = o * (1 + r)
        wick = abs(rng.gauss(0, vol * 0.6))
        h = max(o, c) * (1 + wick * rng.random())
        l = min(o, c) * (1 - wick * rng.random())
        o, c, h, l = (round(x, 2) for x in (o, c, h, l))
        h, l = max(h, o, c), min(l, o, c)
        volume = round(5 + 40 * (abs(r) / vol) * rng.uniform(0.5, 1.5), 3)
        self.tick += 1
        self._last_close = c
        return {"tick": self.tick, "ts": (self._t0 + timedelta(minutes=BAR_MINUTES * self.tick)).isoformat(),
                "open": o, "high": h, "low": l, "close": c, "volume": volume}

    # ---------- bookkeeping ----------
    def _log(self, agent: str, kind: str, pnl: float | None = None, note: str = "") -> None:
        self.last_actor = agent
        self.stream.appendleft({"tick": self.tick, "agent": agent.upper(), "kind": kind,
                                "pnl": None if pnl is None else round(pnl, 2), "note": note})

    def _close(self, pos: Position, price: float, reason: str) -> None:
        direction = 1 if pos.side == Side.buy else -1
        gross = (price - pos.entry) * direction * pos.qty
        fees = FEE_RATE * pos.qty * (pos.entry + price)
        pnl = gross - fees
        self.risk.release_open_risk(pos.risk_amount)
        self.risk.record_realized_pnl(pnl)
        self.realized += pnl
        self.closed += 1
        s = self.stats[pos.agent]
        s["pnl"] += pnl
        s["trades"] += 1
        if pnl > 0:
            self.wins += 1
            s["wins"] += 1
        self.cooldown[pos.agent] = COOLDOWN_BARS
        self.trades.append({"agent": pos.agent, "side": pos.side.value, "signal_tick": pos.signal_tick,
                            "fill_tick": pos.fill_tick, "exit_tick": self.tick,
                            "entry": round(pos.entry, 2), "exit": round(price, 2),
                            "pnl": round(pnl, 2), "reason": reason})
        self._log(pos.agent, reason, pnl)
        del self.positions[pos.agent]

    def _fill_pending(self, c: dict) -> None:
        for key, (proposal, signal_tick, risk_amount, signal) in list(self.pending.items()):
            buy = proposal.side == Side.buy
            fill = c["open"] * (1 + SLIPPAGE) if buy else c["open"] * (1 - SLIPPAGE)
            sign = 1 if buy else -1
            stop = fill - sign * proposal.stop_distance
            target = None if proposal.target_distance is None else fill + sign * proposal.target_distance
            qty = risk_amount / proposal.stop_distance
            self.positions[key] = Position(key, proposal.side, fill, stop, target, qty, risk_amount,
                                           proposal, signal_tick, self.tick, fill)
            self._log(key, "ENTRY " + proposal.side.value.upper(), None, proposal.rationale)
            del self.pending[key]

    def _manage(self, c: dict, a: float) -> None:
        for key, pos in list(self.positions.items()):
            long = pos.side == Side.buy
            p = pos.proposal
            pos.bars += 1 if pos.fill_tick < self.tick else 0
            stop_hit = c["low"] <= pos.stop if long else c["high"] >= pos.stop
            target_hit = pos.target is not None and (c["high"] >= pos.target if long else c["low"] <= pos.target)
            if stop_hit:  # conservative: stop wins if both are touched in one candle
                gap = min(pos.stop, c["open"]) if long else max(pos.stop, c["open"])
                price = gap * (1 - SLIPPAGE) if long else gap * (1 + SLIPPAGE)
                self._close(pos, price, "TRAIL STOP" if p.trailing_atr and (
                    (pos.stop > pos.entry) if long else (pos.stop < pos.entry)) else "STOP LOSS")
                continue
            if target_hit:
                self._close(pos, pos.target, "TAKE PROFIT")
                continue
            r_dist = p.stop_distance
            unreal_r = ((c["close"] - pos.entry) if long else (pos.entry - c["close"])) / r_dist
            if p.cut_r is not None and unreal_r <= -p.cut_r:
                self._close(pos, c["close"], "CUT EARLY")
                continue
            if p.max_bars is not None and pos.bars >= p.max_bars:
                self._close(pos, c["close"], "TIME EXIT")
                continue
            if p.trailing_atr and a > 0:
                pos.best_close = max(pos.best_close, c["close"]) if long else min(pos.best_close, c["close"])
                trail = pos.best_close - p.trailing_atr * a if long else pos.best_close + p.trailing_atr * a
                pos.stop = max(pos.stop, trail) if long else min(pos.stop, trail)

    def _signals(self, c: dict) -> None:
        cfg = self.risk.config
        limit = cfg.initial_equity * cfg.risk_per_trade_pct
        halted = bool(self.risk.status["halted"])
        if halted and not self._was_halted:
            self._log(self.last_actor, "TRADING HALTED", None, "Daily loss limit reached")
        self._was_halted = halted
        for agent in self.agents:
            key = agent.spec.key
            self.evaluated += 1
            proposal = agent.propose(self.candles)
            self.latest_strength[key] = proposal.strength if proposal else 0.0
            self.latest_dir[key] = 0 if proposal is None else (1 if proposal.side == Side.buy else -1)
            if halted:
                self.halted_ticks += 1 if key == self.agents[0].spec.key else 0
                continue
            if self.cooldown[key] > 0:
                self.cooldown[key] -= 1
                continue
            if proposal is None or key in self.positions or key in self.pending:
                continue
            side_sign = 1 if proposal.side == Side.buy else -1
            entry = c["close"]
            signal = Signal(strategy_id=f"astra-{key}", market=MarketType.crypto, symbol=SYMBOL,
                            side=proposal.side, entry=entry,
                            stop_loss=entry - side_sign * proposal.stop_distance,
                            take_profit=None if proposal.target_distance is None
                            else entry + side_sign * proposal.target_distance,
                            confidence=confidence_from(proposal.strength), rationale=proposal.rationale[:500])
            scale = min(1.0, max(0.5, 0.5 + 0.5 * (proposal.strength - 0.6) / 0.3))
            risk_amount = round(limit * RISK_FRACTION * scale, 2)
            try:
                self.risk.approve(signal, risk_amount, f"astra-{key}-{self.tick}")
            except RiskRejected as exc:
                self.rejected += 1
                if self.tick - self.last_block_tick[key] >= 10:
                    self.last_block_tick[key] = self.tick
                    self._log(key, "RISK BLOCKED", None, str(exc))
                continue
            self.pending[key] = (proposal, self.tick, risk_amount, signal)

    # ---------- public ----------
    def step(self) -> None:
        with self._lock:
            c = self._next_candle()
            self.candles.append(c)
            if len(self.candles) > 400:
                del self.candles[:-400]
            a = atr(self.candles)
            if self.tick % SIM_DAY_BARS == 0:
                self._new_sim_day()
            self._fill_pending(c)
            self._manage(c, a)
            self._signals(c)
            self.hesitation.append(round(1 - max(self.latest_strength.values()), 4))
            self.equity_curve.append(round(self._equity(), 2))
            for agent in self.agents:
                k = agent.spec.key
                self.stats[k]["hist"].append(round(self.stats[k]["pnl"], 2))

    def _new_sim_day(self) -> None:
        """Reset the daily loss counter/halt at a simulated day boundary (open risk is preserved)."""
        open_risk = sum(p.risk_amount for p in self.positions.values()) + sum(v[2] for v in self.pending.values())
        self.risk.restore_persisted_state(0.0, open_risk, False, datetime.now(timezone.utc).date())
        self.sim_days += 1
        self._log(self.last_actor, "NEW SIM DAY", None, "Daily loss counter reset")

    def advance(self, now: float | None = None, max_steps: int = 30) -> int:
        """Advance by wall-clock time (one candle per ``interval`` seconds)."""
        with self._lock:
            now = time.monotonic() if now is None else now
            n = int((now - self._last_advance) / self.interval)
            if n <= 0:
                return 0
            n = min(n, max_steps)
            self._last_advance = now if n == max_steps else self._last_advance + n * self.interval
            for _ in range(n):
                self.step()
            return n

    def _open_pnl(self) -> float:
        if not self.candles:
            return 0.0
        px = self.candles[-1]["close"]
        total = 0.0
        for pos in self.positions.values():
            d = 1 if pos.side == Side.buy else -1
            total += (px - pos.entry) * d * pos.qty
        return total

    def _equity(self) -> float:
        return self.initial_equity + self.realized + self._open_pnl()

    def state(self) -> dict:
        with self._lock:
            last = self.candles[-1]
            prev = self.candles[-2]["close"] if len(self.candles) > 1 else last["open"]
            equity = self._equity()
            closes = [c["close"] for c in self.candles[-21:]]
            rets = [(b - a_) / a_ for a_, b in zip(closes, closes[1:])]
            vol = (sum(r * r for r in rets) / len(rets)) ** 0.5 if rets else 0.0
            vorticity = min(1.0, vol / 0.003)
            weights = sum(self.latest_strength.values())
            coherence = (abs(sum(self.latest_dir[k] * self.latest_strength[k] for k in self.latest_dir)) / weights
                         if weights > 0 else 0.0)
            agents = []
            for ag in self.agents:
                k = ag.spec.key
                s = self.stats[k]
                pos = self.positions.get(k)
                agents.append({
                    "key": k, "name": ag.spec.name, "role": ag.spec.role, "color": ag.spec.color,
                    "pnl": round(s["pnl"], 2), "trades": s["trades"],
                    "win_rate": round(100 * s["wins"] / s["trades"], 1) if s["trades"] else None,
                    "position": pos.side.value if pos else ("pending" if k in self.pending else None),
                    "history": list(s["hist"]),
                })
            return {
                "mode": "simulation", "paper_only": True, "disclaimer": DISCLAIMER,
                "symbol": SYMBOL, "tick": self.tick, "regime": self._regime,
                "price": last["close"], "change_pct": round(100 * (last["close"] - prev) / prev, 3),
                "initial_equity": self.initial_equity,
                "balance": round(equity, 2), "pnl": round(equity - self.initial_equity, 2),
                "pnl_pct": round(100 * (equity - self.initial_equity) / self.initial_equity, 2),
                "closed_trades": self.closed,
                "win_rate": round(100 * self.wins / self.closed, 1) if self.closed else None,
                "signals_evaluated": self.evaluated, "risk_blocked": self.rejected,
                "halted_ticks": self.halted_ticks, "sim_days": self.sim_days,
                "active_agents": len(self.positions) + len(self.pending),
                "fees_and_slippage": {"fee_per_side": FEE_RATE, "slippage": SLIPPAGE},
                "risk": self.risk.status,
                "candles": [{"t": c["tick"], "o": c["open"], "h": c["high"], "l": c["low"],
                             "c": c["close"], "v": c["volume"]} for c in self.candles[-60:]],
                "equity_curve": list(self.equity_curve), "hesitation": list(self.hesitation),
                "vortex": {"coherence": round(coherence, 3), "vorticity": round(vorticity, 3),
                           "angular_velocity": round(1.5 + 2.0 * vorticity, 2),
                           "axial_stretch": round(1.0 + 0.6 * coherence, 2),
                           "note": "Visual indicators derived from volatility and agent agreement; not trading signals."},
                "agents": agents, "stream": list(self.stream)[:12], "active_route": self.last_actor,
            }
