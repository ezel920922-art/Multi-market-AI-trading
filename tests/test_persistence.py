from datetime import datetime, timezone
from app.brokers import PaperBroker
from app.engine import TradingEngine
from app.models import MarketType, Side, Signal


class FakeStore:
    def __init__(self):
        self.state = {
            "day": datetime.now(timezone.utc).date(),
            "realized_pnl": 0.0,
            "open_risk": 0.0,
            "halted": False,
        }
        self.orders = {}

    def load_risk_state(self):
        return dict(self.state)

    def save_risk_state(self, *, day, realized_pnl, open_risk, halted):
        self.state = {
            "day": day,
            "realized_pnl": realized_pnl,
            "open_risk": open_risk,
            "halted": halted,
        }

    def has_idempotency_key(self, key):
        return key in self.orders

    def save_order(self, result, order):
        self.orders[order.idempotency_key] = result.model_dump(mode="json")

    def get_order(self, key):
        return self.orders.get(key)


def sample_signal():
    return Signal(
        strategy_id="persistence-test",
        market=MarketType.equities,
        symbol="TEST",
        side=Side.buy,
        entry=100,
        stop_loss=99,
        take_profit=102,
        confidence=0.9,
    )


def test_risk_state_is_loaded_and_saved():
    store = FakeStore()
    store.state["realized_pnl"] = -25.0
    engine = TradingEngine(store=store)
    assert engine.risk.status["realized_pnl"] == -25.0
    engine.risk.record_realized_pnl(-10.0)
    assert store.state["realized_pnl"] == -35.0


def test_paper_order_is_persisted():
    store = FakeStore()
    engine = TradingEngine(store=store)
    result = engine.submit_signal(sample_signal(), 1, 25, "persist-key-001")
    assert result.paper is True
    assert store.get_order("persist-key-001")["order_id"] == result.order_id


def test_persisted_idempotency_key_is_rejected_before_second_order():
    store = FakeStore()
    engine = TradingEngine(store=store)
    engine.submit_signal(sample_signal(), 1, 25, "persist-key-002")
    from app.risk import RiskRejected
    import pytest
    with pytest.raises(RiskRejected, match="Duplicate"):
        engine.submit_signal(sample_signal(), 1, 25, "persist-key-002")
