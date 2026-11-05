from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)


def test_astra_state_is_public_read_only_simulation():
    response = client.get("/api/astra/state")
    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "simulation" and body["paper_only"] is True
    assert [a["name"] for a in body["agents"]] == ["NOVA", "ORIN", "VESK", "LUMA", "KIRA", "DASH"]
    assert response.headers["cache-control"] == "no-store"


def test_astra_state_does_not_touch_the_paper_order_engine(monkeypatch):
    monkeypatch.setenv("TRADING_API_KEY", "test-only-api-key-32-chars-minimum")
    before = client.get("/risk/status", headers={"X-API-Key": "test-only-api-key-32-chars-minimum"}).json()
    client.get("/api/astra/state")
    after = client.get("/risk/status", headers={"X-API-Key": "test-only-api-key-32-chars-minimum"}).json()
    assert before == after


def test_astra_dashboard_page_is_served():
    response = client.get("/astra")
    assert response.status_code == 200
    assert "ASTRA" in response.text and "SIMULATION" in response.text
    assert response.headers["x-frame-options"] == "DENY"
