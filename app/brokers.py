from abc import ABC, abstractmethod
from uuid import uuid4
from app.models import OrderRequest, OrderResult

class BrokerAdapter(ABC):
    @abstractmethod
    def submit(self, order: OrderRequest) -> OrderResult:
        raise NotImplementedError

class PaperBroker(BrokerAdapter):
    """Simulated broker only. Does not connect to an exchange or broker."""
    def __init__(self):
        self.orders: dict[str, OrderResult] = {}
        self.idempotency: dict[str, str] = {}

    def submit(self, order: OrderRequest) -> OrderResult:
        prior_id = self.idempotency.get(order.idempotency_key)
        if prior_id:
            return self.orders[prior_id]
        result = OrderResult(
            order_id=f"PAPER-{uuid4().hex[:12]}",
            status="paper_filled",
            market=order.signal.market,
            symbol=order.signal.symbol,
            side=order.signal.side,
            quantity=order.quantity,
            paper=True,
            message="Simulated fill only; no real order was sent.",
        )
        self.orders[result.order_id] = result
        self.idempotency[order.idempotency_key] = result.order_id
        return result
