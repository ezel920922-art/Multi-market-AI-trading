from app.models import Signal, OrderRequest
from app.risk import RiskManager
from app.brokers import PaperBroker

class TradingEngine:
    def __init__(self, risk: RiskManager | None = None, broker: PaperBroker | None = None):
        self.risk = risk or RiskManager()
        self.broker = broker or PaperBroker()

    def submit_signal(self, signal: Signal, quantity: float, risk_amount: float, idempotency_key: str):
        self.risk.approve(signal, risk_amount, idempotency_key)
        request = OrderRequest(signal=signal, quantity=quantity, risk_amount=risk_amount, idempotency_key=idempotency_key)
        return self.broker.submit(request)
