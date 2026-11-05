"""Six-agent, rule-based market analysis. No broker execution or synthetic prices."""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from statistics import mean
from app.market_data import Candle, validate_candle_series


AGENTS = [
    {"id":"nova","name":"NOVA","role":"Trend & EMA structure","method":"EMA 8 / EMA 21"},
    {"id":"orion","name":"ORION","role":"Momentum confirmation","method":"5-candle price momentum"},
    {"id":"vesk","name":"VESK","role":"Market structure","method":"20-candle breakout"},
    {"id":"luma","name":"LUMA","role":"Volume participation","method":"Recent vs baseline volume"},
    {"id":"kira","name":"KIRA","role":"Volatility & mean reversion","method":"Range / volatility filter"},
    {"id":"dash","name":"DASH","role":"Risk supervisor","method":"Data quality & exposure veto"},
]


def _ema(values: list[float], period: int) -> float:
    alpha = 2.0 / (period + 1.0)
    value = values[0]
    for item in values[1:]:
        value = alpha * item + (1.0 - alpha) * value
    return value


def _vote(direction: str, confidence: float, reason: str) -> dict:
    return {"vote": direction, "confidence": round(max(0.0, min(1.0, confidence)), 3), "reason": reason}


def analyze_market(candles: list[Candle]) -> dict:
    """Analyze a supplied, validated OHLCV series; never invent prices."""
    quality = validate_candle_series(candles, max_staleness_seconds=300)
    if len(candles) < 30:
        raise ValueError("At least 30 recent candles are required for six-agent analysis")
    closes = [float(c.close) for c in candles]
    highs = [float(c.high) for c in candles]
    lows = [float(c.low) for c in candles]
    volumes = [float(c.volume) for c in candles]
    latest = closes[-1]
    if not isfinite(latest) or latest <= 0:
        raise ValueError("Invalid latest close")
    fast, slow = _ema(closes[-30:], 8), _ema(closes[-30:], 21)
    momentum = (latest / closes[-6] - 1.0) * 100.0
    prior_high, prior_low = max(highs[-21:-1]), min(lows[-21:-1])
    vol_base = mean(volumes[-21:-5]) if any(volumes[-21:-5]) else 0.0
    vol_recent = mean(volumes[-5:])
    true_ranges = []
    for i in range(-14, 0):
        prev = closes[i-1]
        true_ranges.append(max(highs[i]-lows[i], abs(highs[i]-prev), abs(lows[i]-prev)))
    atr_pct = (mean(true_ranges) / latest) * 100.0
    range_pct = ((max(highs[-20:]) - min(lows[-20:])) / latest) * 100.0

    votes = []
    trend = "BUY" if fast > slow else "SELL" if fast < slow else "WAIT"
    votes.append(_vote(trend, min(.95, .62 + abs(fast-slow)/latest*30), "EMA 8 أعلى/أدنى من EMA 21" if trend != "WAIT" else "لا يوجد اتجاه واضح"))
    momentum_vote = "BUY" if momentum > .08 else "SELL" if momentum < -.08 else "WAIT"
    votes.append(_vote(momentum_vote, min(.92, .60 + abs(momentum)*.08), f"زخم آخر 5 شموع: {momentum:.3f}%"))
    structure = "BUY" if latest > prior_high else "SELL" if latest < prior_low else "WAIT"
    votes.append(_vote(structure, .82 if structure != "WAIT" else .55, "اختراق قمة/قاع آخر 20 شمعة" if structure != "WAIT" else "السعر داخل نطاق آخر 20 شمعة"))
    volume_vote = momentum_vote if vol_base > 0 and vol_recent > vol_base*1.15 else "WAIT"
    votes.append(_vote(volume_vote, .72 if volume_vote != "WAIT" else .50, f"حجم آخر 5 شموع مقارنة بالمتوسط: {vol_recent/vol_base:.2f}x" if vol_base > 0 else "بيانات الحجم غير كافية"))
    if atr_pct > 2.0 or range_pct > 8.0:
        regime = "HIGH_VOLATILITY"
        regime_vote = "WAIT"
        regime_reason = f"التذبذب مرتفع (ATR {atr_pct:.2f}%)؛ الامتناع أفضل"
    else:
        regime = "NORMAL"
        regime_vote = "BUY" if momentum > .15 and fast > slow else "SELL" if momentum < -.15 and fast < slow else "WAIT"
        regime_reason = f"التذبذب ضمن النطاق (ATR {atr_pct:.2f}%)"
    votes.append(_vote(regime_vote, .70 if regime_vote != "WAIT" else .58, regime_reason))
    # The sixth agent is a hard data/risk gate, not a directional signal.
    risk_ok = quality["quality_ok"] and atr_pct <= 2.0 and latest > 0
    votes.append(_vote("PASS" if risk_ok else "VETO", .99 if risk_ok else .99,
                       "جودة البيانات مقبولة ولا يوجد حظر تذبذب" if risk_ok else "رفض بسبب جودة البيانات أو التذبذب"))

    directional = [v["vote"] for v in votes[:5] if v["vote"] in ("BUY", "SELL")]
    buy_count, sell_count = directional.count("BUY"), directional.count("SELL")
    if not risk_ok:
        consensus = "NO_TRADE"
    elif buy_count >= 3 and buy_count > sell_count:
        consensus = "BUY"
    elif sell_count >= 3 and sell_count > buy_count:
        consensus = "SELL"
    else:
        consensus = "WAIT"
    confidence = round((max(buy_count, sell_count) / 5.0) * mean(v["confidence"] for v in votes[:5]), 3)
    return {
        "mode":"analysis_only", "order_placed":False, "execution_enabled":False,
        "market":candles[-1].market.value, "symbol":candles[-1].symbol,
        "latest_close":latest, "timestamp":quality["latest_timestamp"],
        "data_quality":quality, "regime":regime, "atr_pct":round(atr_pct,4),
        "consensus":consensus, "confidence":confidence,
        "vote_summary":{"buy":buy_count,"sell":sell_count,"wait":5-buy_count-sell_count},
        "agents":[dict(AGENTS[i], **votes[i]) for i in range(6)],
        "disclaimer":"تحليل قواعدي لبيانات OHLCV المقدمة فقط؛ ليس توصية مالية ولا يرسل أوامر.",
    }
