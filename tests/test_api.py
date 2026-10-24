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
    for section in ("brokers", "news", "strategies", "toolkit"):
        assert f'id="{section}"' in response.text
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
