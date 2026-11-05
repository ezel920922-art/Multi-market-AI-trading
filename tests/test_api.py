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

def test_paper_order_returns_success_if_post_commit_snapshot_read_fails(monkeypatch):
    from app import main
    from app.engine import TradingEngine

    class CommittedStore:
        def has_order_key(self, key):
            return False

        def persist_order(self, request, result, risk_config):
            self.persisted_order_id = result.order_id

        def load_risk_state(self):
            raise RuntimeError("temporary read failure after commit")

    monkeypatch.setenv("TRADING_API_KEY", TEST_API_KEY)
    monkeypatch.setattr(main, "engine", TradingEngine())
    store = CommittedStore()
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main, "database_ready", True)

    response = client.post("/paper/orders", headers=auth_headers(), json={
        "signal": {
            "strategy_id": "post-commit-refresh-test",
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
        "idempotency_key": "post-commit-refresh-001",
    })

    assert response.status_code == 200
    assert store.persisted_order_id == response.json()["order_id"]
    assert main.engine.risk.status["open_risk"] == 25
    assert "post-commit-refresh-001" in main.engine.risk._seen_keys


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


def test_concurrent_paper_orders_serialize_persistence(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Lock
    from time import sleep
    from app import main
    from app.engine import TradingEngine

    class TrackingStore:
        def __init__(self):
            self.guard = Lock()
            self.active = 0
            self.max_active = 0

        def has_order_key(self, key):
            return False

        def persist_order(self, request, result, risk_status):
            with self.guard:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            sleep(0.03)
            with self.guard:
                self.active -= 1

    monkeypatch.setenv("TRADING_API_KEY", TEST_API_KEY)
    monkeypatch.setattr(main, "engine", TradingEngine())
    store = TrackingStore()
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main, "database_ready", True)

    def submit(index):
        return client.post("/paper/orders", headers=auth_headers(), json={
            "signal": {
                "strategy_id": "concurrency-test",
                "market": "equities",
                "symbol": f"TEST{index}",
                "side": "buy",
                "entry": 100,
                "stop_loss": 99,
                "take_profit": 102,
                "confidence": 0.9,
            },
            "quantity": 1,
            "risk_amount": 25,
            "idempotency_key": f"concurrency-test-key-{index:03d}",
        })

    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(submit, range(4)))

    assert all(response.status_code == 200 for response in responses)
    assert store.max_active == 1
    assert main.engine.risk.status["open_risk"] == 100


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


def test_news_endpoint_requires_api_key(monkeypatch):
    monkeypatch.setenv("TRADING_API_KEY", TEST_API_KEY)
    response = client.get("/api/news")
    assert response.status_code == 401


def test_news_endpoint_requires_server_side_key(monkeypatch):
    monkeypatch.setenv("TRADING_API_KEY", TEST_API_KEY)
    monkeypatch.delenv("NEWS_API_KEY", raising=False)
    response = client.get("/api/news", headers=auth_headers())
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

def test_security_headers_are_applied(monkeypatch):
    monkeypatch.setenv("TRADING_API_KEY", TEST_API_KEY)
    response = client.get("/health")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["permissions-policy"] == "camera=(), microphone=(), geolocation=()"

def test_api_documentation_is_disabled():
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_health_degrades_when_persistence_is_required_but_missing(monkeypatch):
    from app import main

    monkeypatch.setenv("REQUIRE_PERSISTENCE", "true")
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.setattr(main, "store", None)
    monkeypatch.setattr(main, "database_ready", False)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "degraded"
    assert response.json()["persistence_required"] is True


def test_paper_order_fails_closed_when_persistence_is_required_but_missing(monkeypatch):
    from app import main

    monkeypatch.setenv("TRADING_API_KEY", TEST_API_KEY)
    monkeypatch.setenv("REQUIRE_PERSISTENCE", "true")
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.setattr(main, "store", None)
    monkeypatch.setattr(main, "database_ready", False)
    response = client.post("/paper/orders", headers=auth_headers(), json={
        "signal": {
            "strategy_id": "persistence-required-test",
            "market": "equities",
            "symbol": "TEST",
            "side": "buy",
            "entry": 100,
            "stop_loss": 99,
            "take_profit": 102,
            "confidence": 0.9
        },
        "quantity": 1,
        "risk_amount": 25,
        "idempotency_key": "persistence-required-001"
    })
    assert response.status_code == 503
    assert "Durable database persistence is required" in response.json()["detail"]


def test_ai_swarm_desk_preview_is_available_without_replacing_dashboard():
    response = client.get('/desk')
    assert response.status_code == 200
    assert 'AI SWARM DESK' in response.text
    assert 'PAPER ONLY' in response.text
    assert 'NO LIVE FEED' in response.text


def test_existing_dashboard_remains_available():
    response = client.get('/')
    assert response.status_code == 200
    assert 'MULTI-MARKET' in response.text


def test_market_provider_status_does_not_expose_api_key(monkeypatch):
    monkeypatch.setenv("TWELVEDATA_API_KEY", "private-market-secret")
    response = client.get("/api/market/status")
    assert response.status_code == 200
    assert response.json()["configured"] is True
    assert response.json()["order_routing"] is False
    assert "private-market-secret" not in response.text


def test_market_candles_requires_api_key(monkeypatch):
    monkeypatch.setenv("TRADING_API_KEY", TEST_API_KEY)
    response = client.get("/api/market/candles?market=forex&symbol=EUR/USD")
    assert response.status_code == 401


def test_market_candles_returns_provider_data_without_execution(monkeypatch):
    from app import main
    from datetime import datetime, timedelta, timezone
    from app.market_data import Candle
    from app.models import MarketType
    now = datetime.now(timezone.utc)
    candles = [Candle(market=MarketType.commodities, symbol="XAUUSD",
        timestamp=now-timedelta(minutes=34-i), open=2000+i, high=2001+i,
        low=1999+i, close=2000.5+i, volume=1000+i) for i in range(35)]
    monkeypatch.setenv("TRADING_API_KEY", TEST_API_KEY)
    monkeypatch.setattr(main, "fetch_candles", lambda *args: candles)
    response = client.get("/api/market/candles?market=commodities&symbol=XAUUSD", headers=auth_headers())
    assert response.status_code == 200
    body = response.json()
    assert body["provider"] == "Twelve Data"
    assert body["mode"] == "read_only"
    assert body["count"] == 35
    assert body["candles"][0]["symbol"] == "XAUUSD"


def test_market_candles_fail_closed_without_provider_key(monkeypatch):
    from app import main
    from app.market_provider import MarketProviderError
    monkeypatch.setenv("TRADING_API_KEY", TEST_API_KEY)
    monkeypatch.setattr(main, "fetch_candles", lambda *args: (_ for _ in ()).throw(MarketProviderError("TWELVEDATA_API_KEY is not configured")))
    response = client.get("/api/market/candles?market=forex&symbol=EUR/USD", headers=auth_headers())
    assert response.status_code == 503
    assert "not configured" in response.json()["detail"]


def test_kelly_sizing_caps_risk_and_never_places_order(monkeypatch):
    monkeypatch.setenv("TRADING_API_KEY", TEST_API_KEY)
    response = client.post("/api/risk/kelly-size", headers=auth_headers(), json={
        "symbol": "xauusd", "side": "buy", "entry": 2000, "stop_loss": 1990,
        "equity": 10000, "win_rate": 0.65, "average_win_loss_ratio": 2.0,
        "kelly_fraction": 0.5
    })
    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "analysis_only"
    assert body["order_placed"] is False
    assert body["execution_enabled"] is False
    assert body["risk_pct"] <= 0.5
    assert body["risk_budget"] <= 50
    assert body["suggested_quantity"] >= 0


def test_kelly_sizing_returns_zero_when_estimated_edge_is_negative(monkeypatch):
    monkeypatch.setenv("TRADING_API_KEY", TEST_API_KEY)
    response = client.post("/api/risk/kelly-size", headers=auth_headers(), json={
        "symbol": "EURUSD", "side": "buy", "entry": 1.1, "stop_loss": 1.099,
        "equity": 10000, "win_rate": 0.3, "average_win_loss_ratio": 1.0
    })
    assert response.status_code == 200
    assert response.json()["trade_allowed_by_kelly"] is False
    assert response.json()["suggested_quantity"] == 0


def test_kelly_sizing_rejects_wrong_stop_direction(monkeypatch):
    monkeypatch.setenv("TRADING_API_KEY", TEST_API_KEY)
    response = client.post("/api/risk/kelly-size", headers=auth_headers(), json={
        "symbol": "XAUUSD", "side": "buy", "entry": 2000, "stop_loss": 2001,
        "equity": 10000, "win_rate": 0.6, "average_win_loss_ratio": 2.0
    })
    assert response.status_code == 422


def test_ema_backtest_endpoint_is_authenticated_and_paper_only(monkeypatch):
    from datetime import datetime, timedelta, timezone
    from math import sin
    from app.market_data import Candle
    from app.models import MarketType
    monkeypatch.setenv("TRADING_API_KEY", TEST_API_KEY)
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    rows = []
    previous = 100.0
    for i in range(100):
        close = 100 + 0.025*i + 3.2*sin(i/7) + 0.7*sin(i/2.2)
        rows.append(Candle(market=MarketType.equities, symbol="TEST",
            timestamp=now-timedelta(minutes=99-i), open=previous,
            high=max(previous,close)+0.6, low=min(previous,close)-0.6,
            close=close, volume=1000+i))
        previous=close
    response = client.post('/api/backtest/ema', headers=auth_headers(), json={
        'candles':[c.model_dump(mode='json') for c in rows]
    })
    assert response.status_code == 200
    body=response.json()
    assert body['mode']=='backtest_only'
    assert body['execution_enabled'] is False
    assert body['orders_submitted']==0
    assert body['strategy']['entry_timing']=='next_candle_open'


def test_ema_backtest_rejects_risk_above_cap(monkeypatch):
    monkeypatch.setenv("TRADING_API_KEY", TEST_API_KEY)
    response = client.post('/api/backtest/ema', headers=auth_headers(), json={
        'candles':[], 'risk_pct':0.01
    })
    assert response.status_code == 422


def test_agent_spectrogram_replays_history_and_is_authenticated(monkeypatch):
    from datetime import datetime, timedelta, timezone
    from app.market_data import Candle
    from app.models import MarketType
    monkeypatch.setenv("TRADING_API_KEY", TEST_API_KEY)
    now = datetime.now(timezone.utc)
    candles = [Candle(market=MarketType.commodities, symbol="XAUUSD",
        timestamp=now-timedelta(minutes=34-i), open=2000+i*.5,
        high=2001+i*.5, low=1999+i*.5, close=2000.5+i*.5, volume=1000+i) for i in range(35)]
    response=client.post('/api/agents/spectrogram',headers=auth_headers(),json={
        'candles':[c.model_dump(mode='json') for c in candles]
    })
    assert response.status_code==200
    body=response.json()
    assert body['predictive'] is False
    assert body['windows']>=1
    assert len(body['rows'][0]['agents'])==6


def test_agent_spectrogram_rejects_missing_api_key(monkeypatch):
    monkeypatch.setenv("TRADING_API_KEY", TEST_API_KEY)
    response=client.post('/api/agents/spectrogram',json={'candles':[]})
    assert response.status_code==401


def test_paper_trade_history_endpoint_uses_durable_store(monkeypatch):
    from app import main
    class FakeStore:
        def list_orders(self, status=None, limit=100):
            return [{"id": 7, "symbol": "EURUSD", "status": "filled"}]
    monkeypatch.setenv("TRADING_API_KEY", TEST_API_KEY)
    monkeypatch.setattr(main, "store", FakeStore())
    monkeypatch.setattr(main, "database_ready", True)
    response = client.get("/paper/orders?status=open", headers=auth_headers())
    assert response.status_code == 200
    assert response.json()["mode"] == "paper_only"
    assert response.json()["orders"][0]["status"] == "filled"


def test_paper_trade_close_endpoint_updates_risk_state(monkeypatch):
    from datetime import date
    from app import main
    from app.engine import TradingEngine
    class FakeStore:
        def close_order(self, order_id, exit_price, risk_config):
            return {"order_id": order_id, "status": "closed", "exit_price": exit_price,
                    "realized_pnl": 12.5, "current_equity": 10012.5,
                    "risk_state": {"daily_realized_pnl": 12.5, "open_risk_pct": 0.0,
                                   "trading_halted": False, "trading_day": date.today()}}
    monkeypatch.setenv("TRADING_API_KEY", TEST_API_KEY)
    monkeypatch.setattr(main, "store", FakeStore())
    monkeypatch.setattr(main, "database_ready", True)
    monkeypatch.setattr(main, "engine", TradingEngine())
    response = client.post("/paper/orders/PAPER-abc/close", headers=auth_headers(), json={"exit_price": 101.25})
    assert response.status_code == 200
    assert response.json()["realized_pnl"] == 12.5
    assert response.json()["status"] == "closed"
    assert main.engine.risk.status["realized_pnl"] == 12.5


def test_close_returns_success_if_post_commit_risk_restore_fails(monkeypatch):
    from datetime import date
    from app import main
    from app.engine import TradingEngine

    class CommittedCloseStore:
        closed = False

        def close_order(self, order_id, exit_price, risk_config):
            self.closed = True
            return {
                "order_id": order_id, "status": "closed", "exit_price": exit_price,
                "realized_pnl": 12.5, "current_equity": 10012.5,
                "risk_state": {
                    "daily_realized_pnl": 12.5, "open_risk_pct": 0.0,
                    "trading_halted": False, "trading_day": date.today(),
                },
            }

    monkeypatch.setenv("TRADING_API_KEY", TEST_API_KEY)
    store = CommittedCloseStore()
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main, "database_ready", True)
    engine = TradingEngine()
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(engine.risk, "restore_persisted_state",
                        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("refresh failed")))

    response = client.post(
        "/paper/orders/PAPER-committed-close/close",
        headers=auth_headers(),
        json={"exit_price": 101.25},
    )

    assert response.status_code == 200
    assert store.closed is True
    assert response.json()["status"] == "closed"
    assert response.json()["realized_pnl"] == 12.5


def test_xauusd_new_position_blocked_when_one_is_already_open(monkeypatch):
    from app import main
    from app.engine import TradingEngine
    class FakeStore:
        def has_order_key(self, key): return False
        def has_open_xauusd(self, side): return True
    monkeypatch.setenv("TRADING_API_KEY", TEST_API_KEY)
    monkeypatch.setattr(main, "store", FakeStore())
    monkeypatch.setattr(main, "database_ready", True)
    monkeypatch.setattr(main, "engine", TradingEngine())
    response = client.post("/paper/orders", headers=auth_headers(), json={
        "signal": {"strategy_id":"gold-test", "market":"commodities", "symbol":"XAUUSD",
                   "side":"sell", "entry":2000, "stop_loss":2010, "take_profit":1980,
                   "confidence":0.95},
        "quantity":1, "risk_amount":25, "idempotency_key":"xauusd-open-guard-001"
    })
    assert response.status_code == 409
    assert "XAUUSD" in response.json()["detail"]



def test_xauusd_punctuation_alias_is_blocked_when_position_is_open(monkeypatch):
    from app import main
    from app.engine import TradingEngine

    class FakeStore:
        def has_order_key(self, key): return False
        def has_open_xauusd(self, side): return True

    monkeypatch.setenv("TRADING_API_KEY", TEST_API_KEY)
    monkeypatch.setattr(main, "store", FakeStore())
    monkeypatch.setattr(main, "database_ready", True)
    monkeypatch.setattr(main, "engine", TradingEngine())
    response = client.post("/paper/orders", headers=auth_headers(), json={
        "signal": {"strategy_id": "gold-alias-test", "market": "commodities",
                   "symbol": "XAU/USD", "side": "sell", "entry": 2000,
                   "stop_loss": 2010, "take_profit": 1980, "confidence": 0.95},
        "quantity": 1, "risk_amount": 25, "idempotency_key": "xauusd-alias-guard-001"
    })
    assert response.status_code == 409
    assert "XAUUSD" in response.json()["detail"]


def test_pwa_manifest_and_service_worker_are_served():
    manifest = client.get("/manifest.webmanifest")
    assert manifest.status_code == 200
    assert manifest.headers["content-type"].startswith("application/manifest+json")
    data = manifest.json()
    assert data["start_url"] == "/desk"
    assert data["display"] == "standalone"
    worker = client.get("/service-worker.js")
    assert worker.status_code == 200
    assert "Service-Worker-Allowed" in worker.headers
    assert "u.pathname.startsWith('/api/')" in worker.text


def test_pwa_icons_are_served_from_allowlist():
    icon = client.get("/static/icons/icon-192.svg")
    assert icon.status_code == 200
    assert icon.headers["content-type"].startswith("image/svg+xml")
    assert "<svg" in icon.text
    assert client.get("/static/icons/not-an-icon.svg").status_code == 404


def test_swarm_desk_has_mobile_navigation_and_paper_only_controls():
    response = client.get("/desk")
    assert response.status_code == 200
    for anchor in ('id="overview"', 'id="swarm"', 'id="market"', 'id="paperTrading"'):
        assert anchor in response.text
    assert 'class="mobile-nav"' in response.text
    assert "PAPER ONLY" in response.text
    assert "EXECUTION" in response.text
