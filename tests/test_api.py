from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

def test_health_is_paper_only():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["mode"] == "paper_only"
    assert response.json()["real_execution_enabled"] is False

def test_risk_status_endpoint():
    response = client.get("/risk/status")
    assert response.status_code == 200
    body = response.json()
    assert body["paper_only"] is True
    assert "daily_loss_limit" in body

def test_paper_order_endpoint():
    response = client.post("/paper/orders", json={
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

def test_paper_order_rejects_infinite_quantity():
    response = client.post("/paper/orders", json={
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
