import os
from app.models import Signal, OrderRequest, OrderResult
from app.risk import RiskManager
from app.brokers import PaperBroker


class TradingEngine:
    def __init__(self, risk: RiskManager | None = None, broker: PaperBroker | None = None, store=None):
        # A configured DATABASE_URL enables persistence. Startup fails closed if the
        # database driver or connection is broken instead of silently using memory.
        if store is None and os.getenv("DATABASE_URL", "").strip():
            from app.persistence import PostgresStore
            store = PostgresStore()
        self.store = store
        self.risk = risk or RiskManager(store=store)
        self.broker = broker or PaperBroker(store=store)

    def submit_signal(self, signal: Signal, quantity: float, risk_amount: float, idempotency_key: str):
        # Return the original result on retries, rather than charging risk twice or
        # turning a successful request retry into a duplicate-key error.
        if self.store is not None:
            saved = self.store.get_order(idempotency_key)
            if saved is not None:
                return OrderResult.model_validate(saved)
        prior_id = self.broker.idempotency.get(idempotency_key)
        if prior_id is not None:
            return self.broker.orders[prior_id]

        self.risk.approve(signal, risk_amount, idempotency_key)
        request = OrderRequest(signal=signal, quantity=quantity, risk_amount=risk_amount, idempotency_key=idempotency_key)
        return self.broker.submit(request)
