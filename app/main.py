import hmac
import os
import json
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
from pathlib import Path
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from app.models import Signal, OrderRequest, MarketType, Side
from app.engine import TradingEngine
from app.risk import RiskRejected
from app.storage import DuplicateOrderKey, PostgresStore

app = FastAPI(title="Multi-Market Trading Agency", version="0.2.0",
              description="Paper-only API. No real broker execution is available.")
engine = TradingEngine()
store = PostgresStore.from_env()
database_ready = False

class SubmitSignalRequest(BaseModel):
    signal: Signal
    quantity: float = Field(gt=0, allow_inf_nan=False)
    risk_amount: float = Field(gt=0, allow_inf_nan=False)
    idempotency_key: str = Field(min_length=8, max_length=120)

class SetupAnalysisRequest(BaseModel):
    market: MarketType
    symbol: str = Field(min_length=1, max_length=40)
    side: Side
    entry: float = Field(gt=0, allow_inf_nan=False)
    stop_loss: float = Field(gt=0, allow_inf_nan=False)
    take_profit: float = Field(gt=0, allow_inf_nan=False)
    equity: float = Field(default=10_000, gt=0, allow_inf_nan=False)
    risk_pct: float = Field(default=0.005, gt=0, le=0.005, allow_inf_nan=False)

def require_api_key(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> None:
    expected = os.getenv("TRADING_API_KEY", "")
    if len(expected) < 32:
        raise HTTPException(status_code=503, detail="API authentication is not configured securely")
    if not x_api_key or not hmac.compare_digest(x_api_key, expected):
        raise HTTPException(status_code=401, detail="Invalid or missing API key",
                            headers={"WWW-Authenticate": "ApiKey"})

@app.on_event("startup")
def startup():
    global database_ready
    if store is not None:
        try:
            store.initialize(engine.risk.config.initial_equity)
            state = store.load_risk_state()
            if state:
                engine.risk.restore_persisted_state(
                    realized_pnl=float(state["daily_realized_pnl"]),
                    open_risk=float(state["open_risk_pct"]) * engine.risk.config.initial_equity,
                    halted=bool(state["trading_halted"]),
                    trading_day=state["trading_day"],
                )
            database_ready = True
        except Exception as exc:
            database_ready = False
            raise RuntimeError("DATABASE_URL is configured but database initialization failed") from exc

@app.get("/health")
def health():
    return {"status": "ok" if store is None or database_ready else "degraded",
            "mode": "paper_only", "real_execution_enabled": False,
            "persistence": "postgresql" if store is not None and database_ready else "memory_only"}

@app.get("/risk/status", dependencies=[Depends(require_api_key)])
def risk_status():
    if store is not None and not database_ready:
        raise HTTPException(status_code=503, detail="Database persistence is unavailable")
    return engine.risk.status

@app.post("/paper/orders", dependencies=[Depends(require_api_key)])
def submit_paper_order(payload: SubmitSignalRequest):
    if store is not None:
        if not database_ready:
            raise HTTPException(status_code=503, detail="Database persistence is unavailable")
        try:
            if store.has_order_key(payload.idempotency_key):
                raise HTTPException(status_code=409, detail="Idempotency key has already been used")
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=503, detail="Could not verify order idempotency") from exc
    try:
        result = engine.submit_signal(payload.signal, payload.quantity, payload.risk_amount,
                                      payload.idempotency_key)
        if store is not None:
            request = OrderRequest(signal=payload.signal, quantity=payload.quantity,
                                   risk_amount=payload.risk_amount,
                                   idempotency_key=payload.idempotency_key)
            try:
                store.persist_order(request, result, engine.risk.status)
            except DuplicateOrderKey as exc:
                # The paper engine reserves risk before persistence. Undo that reservation
                # if the database rejects a duplicate key (including a concurrent race).
                engine.risk.release_open_risk(payload.risk_amount)
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except Exception as exc:
                # Fail closed: never leave phantom exposure in memory after a failed write.
                engine.risk.release_open_risk(payload.risk_amount)
                raise HTTPException(status_code=503, detail="Paper order was not durably recorded") from exc
        return result.model_dump(mode="json")
    except RiskRejected as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/analysis/setup", dependencies=[Depends(require_api_key)])
def analyze_setup(payload: SetupAnalysisRequest):
    """Calculate a risk-budgeted paper setup; this endpoint never places an order."""
    entry = float(payload.entry)
    stop = float(payload.stop_loss)
    target = float(payload.take_profit)
    if payload.side == Side.buy and not (stop < entry < target):
        raise HTTPException(status_code=422, detail="For a buy setup, stop_loss < entry < take_profit is required")
    if payload.side == Side.sell and not (target < entry < stop):
        raise HTTPException(status_code=422, detail="For a sell setup, take_profit < entry < stop_loss is required")
    stop_distance_pct = abs(entry - stop) / entry
    config = engine.risk.config
    if not config.min_stop_distance_pct <= stop_distance_pct <= config.max_stop_distance_pct:
        raise HTTPException(status_code=422, detail="Stop-loss distance is outside the configured risk limits")
    risk_per_unit = abs(entry - stop)
    reward_per_unit = abs(target - entry)
    risk_budget = float(payload.equity) * float(payload.risk_pct)
    return {
        "mode": "analysis_only",
        "order_placed": False,
        "market": payload.market.value,
        "symbol": payload.symbol.strip().upper(),
        "side": payload.side.value,
        "entry": entry,
        "stop_loss": stop,
        "take_profit": target,
        "stop_distance_pct": round(stop_distance_pct * 100, 4),
        "risk_budget": round(risk_budget, 2),
        "risk_per_unit": round(risk_per_unit, 8),
        "reward_per_unit": round(reward_per_unit, 8),
        "reward_risk_ratio": round(reward_per_unit / risk_per_unit, 4),
        "suggested_quantity": round(risk_budget / risk_per_unit, 8),
        "assumptions": ["Before fees, slippage, funding, and contract-size multipliers", "Not a trade recommendation"],
    }
@app.get("/api/integrations")
def integration_status():
    return {
        "mode": "paper_only",
        "real_execution_enabled": False,
        "newsapi": {"configured": bool(os.getenv("NEWS_API_KEY", "").strip())},
        "brokers": {
            "metatrader5": {"configured": False, "mode": "local_terminal_required"},
            "interactive_brokers": {"configured": False, "mode": "credentials_and_gateway_required"},
            "alpaca": {"configured": False, "mode": "paper_account_and_api_keys_required"},
            "tradingview": {"configured": False, "mode": "webhook_setup_required"},
        },
    }


@app.get("/api/news")
def market_news():
    api_key = os.getenv("NEWS_API_KEY", "").strip()
    if not api_key:
        raise HTTPException(status_code=503, detail="NEWS_API_KEY is not configured; use the official source links in the dashboard")
    query = urlencode({"country": "us", "category": "business", "pageSize": 10})
    request = Request(
        "https://newsapi.org/v2/top-headlines?" + query,
        headers={"Accept": "application/json", "User-Agent": "MultiMarketAI/0.3", "X-Api-Key": api_key},
    )
    try:
        with urlopen(request, timeout=8) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        if exc.code in (401, 403):
            raise HTTPException(status_code=502, detail="News provider rejected its API credentials") from exc
        raise HTTPException(status_code=502, detail="News provider returned an HTTP error") from exc
    except (URLError, TimeoutError, ValueError):
        raise HTTPException(status_code=502, detail="News provider is temporarily unavailable") from None
    if payload.get("status") != "ok":
        raise HTTPException(status_code=502, detail="News provider did not return a successful response")
    articles = []
    for item in payload.get("articles", [])[:10]:
        articles.append({
            "title": item.get("title"),
            "source": (item.get("source") or {}).get("name"),
            "published_at": item.get("publishedAt"),
            "url": item.get("url"),
            "description": item.get("description"),
        })
    return {"provider": "NewsAPI", "count": len(articles), "articles": articles}

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def dashboard():
    dashboard_file = Path(__file__).with_name("dashboard.html")
    return HTMLResponse(content=dashboard_file.read_text(encoding="utf-8"))
