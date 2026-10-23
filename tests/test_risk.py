import pytest
from app.models import Signal, MarketType, Side
from app.risk import RiskManager, RiskConfig, RiskRejected
from app.engine import TradingEngine

def signal(side=Side.buy, confidence=0.9, entry=100.0, stop=99.0):
    return Signal(strategy_id="test-strategy", market=MarketType.equities, symbol="TEST",
                  side=side, entry=entry, stop_loss=stop,
                  take_profit=102.0 if side == Side.buy else 98.0,
                  confidence=confidence, rationale="unit test")

def test_valid_signal_only_creates_paper_order():
    result = TradingEngine().submit_signal(signal(), 10, 50, "unique-key-001")
    assert result.status == "paper_filled"
    assert result.paper is True
    assert result.order_id.startswith("PAPER-")

def test_rejects_risk_above_limit():
    with pytest.raises(RiskRejected, match="exceeds"):
        TradingEngine().submit_signal(signal(), 10, 51, "unique-key-002")

def test_rejects_low_confidence():
    with pytest.raises(RiskRejected, match="confidence"):
        TradingEngine().submit_signal(signal(confidence=0.2), 1, 10, "unique-key-003")

def test_buy_stop_must_be_below_entry():
    with pytest.raises(RiskRejected, match="below entry"):
        TradingEngine().submit_signal(signal(stop=101), 1, 10, "unique-key-004")

def test_daily_loss_halts_new_signals():
    risk = RiskManager(RiskConfig())
    risk.record_realized_pnl(-150)
    with pytest.raises(RiskRejected, match="halted|limit"):
        risk.approve(signal(), 10, "unique-key-005")

def test_idempotency_key_cannot_be_reused_for_new_risk():
    engine = TradingEngine()
    engine.submit_signal(signal(), 1, 10, "unique-key-006")
    with pytest.raises(RiskRejected, match="Duplicate"):
        engine.submit_signal(signal(), 1, 10, "unique-key-006")

def test_rejects_sell_stop_on_wrong_side():
    with pytest.raises(RiskRejected, match="above entry"):
        TradingEngine().submit_signal(signal(side=Side.sell, stop=99), 1, 10, "unique-key-007")
