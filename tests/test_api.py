from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)
TEST_API_KEY = "test-only-api-key-32-chars-minimum"

def auth_headers():
    return {"X-API-Key": TEST_API_KEY}

def test_health_is_paper_only():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["mode"] == "paper_only"
    assert response.json()["real_execution_enabled"] is False

def test_risk_status_endpoint(monkeypatch):
    monkeypatch.setenv("TRADING_API_KEY", TEST_API_KEY)
    response = client.get("/risk/status", headers=auth_headers())
    assert response.status_code == 200
    body = response.json()
    assert body["paper_only"] is True
    assert "daily_loss_limit" in body

def test_protected_endpoint_rejects_missing_api_key(monkeypatch):
    monkeypatch.setenv("TRADING_API_KEY", TEST_API_KEY)
    response = client.get("/risk/status")
    assert response.status_code == 401

def test_protected_endpoint_fails_closed_when_api_key_is_unconfigured(monkeypatch):
    monkeypatch.delenv("TRADING_API_KEY", raising=False)
    response = client.get("/risk/status")
    assert response.status_code == 503

def test_paper_order_endpoint(monkeypatch):
    monkeypatch.setenv("TRADING_API_KEY", TEST_API_KEY)
    response = client.post("/paper/orders", headers=auth_headers(), json={
        "signal": {
            "strategy_id": "api-test",
            "market": "equities",
            "symbol": "TEST",
            "side": "buy",
            "entry": 100,
            "stop_loss": 99,
            "take_profit": 102,
            "confidence": 0.9,
            "rationale": "automated API smoke test"
        },
        "quantity": 1,
        "risk_amount": 25,
        "idempotency_key": "api-test-key-001"
    })
    assert response.status_code == 200
    body = response.json()
    assert body["paper"] is True
    assert body["status"] == "paper_filled"
    assert body["order_id"].startswith("PAPER-")

def test_paper_order_releases_reserved_risk_when_persistence_fails(monkeypatch):
    from app import main
    from app.engine import TradingEngine

    class FailingStore:
        def has_order_key(self, key):
            return False

        def persist_order(self, request, result, risk_status):
            raise RuntimeError("simulated database outage")

    monkeypatch.setenv("TRADING_API_KEY", TEST_API_KEY)
    monkeypatch.setattr(main, "engine", TradingEngine())
    monkeypatch.setattr(main, "store", FailingStore())
    monkeypatch.setattr(main, "database_ready", True)
    response = client.post("/paper/orders", headers=auth_headers(), json={
        "signal": {
            "strategy_id": "persistence-failure-test",
            "market": "equities",
            "symbol": "TEST",
            "side": "buy",
            "entry": 100,
            "stop_loss": 99,
            "take_profit": 102,
            "confidence": 0.9,
        },
        "quantity": 1,
        "risk_amount": 25,
        "idempotency_key": "persist-failure-001",
    })

    assert response.status_code == 503
    assert main.engine.risk.status["open_risk"] == 0
    assert "persist-failure-001" not in main.engine.risk._seen_keys


def test_paper_order_rejects_infinite_quantity(monkeypatch):
    monkeypatch.setenv("TRADING_API_KEY", TEST_API_KEY)
    response = client.post("/paper/orders", headers=auth_headers(), json={
        "signal": {
            "strategy_id": "api-test",
            "market": "equities",
            "symbol": "TEST",
            "side": "buy",
            "entry": 100,
            "stop_loss": 99,
            "take_profit": 102,
            "confidence": 0.9
        },
        "quantity": "Infinity",
        "risk_amount": 25,
        "idempotency_key": "api-test-key-002"
    })
    assert response.status_code == 422


def test_dashboard_has_professional_sections():
    response = client.get("/")
    assert response.status_code == 200
    for section in ("brokers", "verification", "news", "strategies", "lab", "toolkit"):
        assert f'id="{section}"' in response.text
    assert "BrokerCheck" in response.text
    assert "TradingView" in response.text
    assert "Paper trading only" in response.text or "التداول الورقي فقط" in response.text


def test_integration_status_does_not_expose_secrets(monkeypatch):
    monkeypatch.setenv("NEWS_API_KEY", "test-secret-never-return-this")
    response = client.get("/api/integrations")
    assert response.status_code == 200
    assert response.json()["newsapi"]["configured"] is True
    assert "test-secret-never-return-this" not in response.text
    assert response.json()["real_execution_enabled"] is False


def test_news_endpoint_requires_server_side_key(monkeypatch):
    monkeypatch.delenv("NEWS_API_KEY", raising=False)
    response = client.get("/api/news")
    assert response.status_code == 503
    assert "not configured" in response.json()["detail"]


def test_paper_order_rejects_buy_with_stop_loss_above_entry(monkeypatch):
    monkeypatch.setenv("TRADING_API_KEY", TEST_API_KEY)
    response = client.post("/paper/orders", headers=auth_headers(), json={
        "signal": {
            "strategy_id": "safety-test",
            "market": "equities",
            "symbol": "TEST",
            "side": "buy",
            "entry": 100,
            "stop_loss": 101,
            "take_profit": 103,
            "confidence": 0.9,
            "rationale": "invalid stop direction must be rejected"
        },
        "quantity": 1,
        "risk_amount": 25,
        "idempotency_key": "safety-buy-sl-001"
    })
    assert response.status_code == 422
    assert "stop-loss must be below entry" in response.json()["detail"]


def test_paper_order_accepts_sell_with_take_profit_below_entry(monkeypatch):
    monkeypatch.setenv("TRADING_API_KEY", TEST_API_KEY)
    response = client.post("/paper/orders", headers=auth_headers(), json={
        "signal": {
            "strategy_id": "safety-test",
            "market": "equities",
            "symbol": "TEST",
            "side": "sell",
            "entry": 100,
            "stop_loss": 101,
            "take_profit": 99.5,
            "confidence": 0.9,
            "rationale": "valid short-side target"
        },
        "quantity": 1,
        "risk_amount": 25,
        "idempotency_key": "safety-sell-tp-001"
    })
    # The short target is below entry, so this order should pass directional checks.
    assert response.status_code == 200


def test_paper_order_rejects_risk_above_per_trade_limit(monkeypatch):
    monkeypatch.setenv("TRADING_API_KEY", TEST_API_KEY)
    response = client.post("/paper/orders", headers=auth_headers(), json={
        "signal": {
            "strategy_id": "safety-test",
            "market": "equities",
            "symbol": "TEST",
            "side": "buy",
            "entry": 100,
            "stop_loss": 99,
            "take_profit": 102,
            "confidence": 0.9,
            "rationale": "risk limit must be enforced"
        },
        "quantity": 1,
        "risk_amount": 100,
        "idempotency_key": "safety-risk-cap-001"
    })
    assert response.status_code == 422
    assert "exceeds configured limit" in response.json()["detail"]


def test_paper_order_rejects_sell_with_take_profit_above_entry(monkeypatch):
    monkeypatch.setenv("TRADING_API_KEY", TEST_API_KEY)
    response = client.post("/paper/orders", headers=auth_headers(), json={
        "signal": {
            "strategy_id": "safety-test",
            "market": "equities",
            "symbol": "TEST",
            "side": "sell",
            "entry": 100,
            "stop_loss": 101,
            "take_profit": 102,
            "confidence": 0.9,
            "rationale": "short target above entry is invalid"
        },
        "quantity": 1,
        "risk_amount": 25,
        "idempotency_key": "safety-sell-tp-invalid-001"
    })
    assert response.status_code == 422
    assert "take-profit must be below entry" in response.json()["detail"]


def test_setup_analysis_calculates_risk_without_placing_order(monkeypatch):
    monkeypatch.setenv("TRADING_API_KEY", TEST_API_KEY)
    response = client.post("/api/analysis/setup", headers=auth_headers(), json={
        "market": "equities",
        "symbol": "test",
        "side": "buy",
        "entry": 100,
        "stop_loss": 99,
        "take_profit": 102,
        "equity": 10000,
        "risk_pct": 0.005
    })
    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "analysis_only"
    assert body["order_placed"] is False
    assert body["symbol"] == "TEST"
    assert body["risk_budget"] == 50
    assert body["reward_risk_ratio"] == 2
    assert body["suggested_quantity"] == 50


def test_setup_analysis_rejects_invalid_buy_direction(monkeypatch):
    monkeypatch.setenv("TRADING_API_KEY", TEST_API_KEY)
    response = client.post("/api/analysis/setup", headers=auth_headers(), json={
        "market": "forex",
        "symbol": "EURUSD",
        "side": "buy",
        "entry": 1.1,
        "stop_loss": 1.11,
        "take_profit": 1.12
    })
    assert response.status_code == 422
    assert "stop_loss < entry < take_profit" in response.json()["detail"]


def test_setup_analysis_caps_risk_percentage_at_half_percent(monkeypatch):
    monkeypatch.setenv("TRADING_API_KEY", TEST_API_KEY)
    response = client.post("/api/analysis/setup", headers=auth_headers(), json={
        "market": "equities",
        "symbol": "TEST",
        "side": "buy",
        "entry": 100,
        "stop_loss": 99,
        "take_profit": 102,
        "risk_pct": 0.01
    })
    assert response.status_code == 422