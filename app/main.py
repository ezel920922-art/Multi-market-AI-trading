import hmac
import os
import json
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
from pathlib import Path
from threading import RLock
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Query
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field
from app.models import Signal, OrderRequest, MarketType, Side
from app.engine import TradingEngine
from dataclasses import replace
from app.risk import RiskRejected
from app.storage import DuplicateOrderKey, OpenXauusdPosition, DurableRiskRejected, PostgresStore

app = FastAPI(title="Multi-Market Trading Agency", version="0.2.0",
              description="Paper-only API. No real broker execution is available.",
              docs_url=None, redoc_url=None, openapi_url=None)
engine = TradingEngine()
store = PostgresStore.from_env()
database_ready = False
# Serialize the full in-process risk-reserve + durable-write sequence.
# Horizontal scaling requires a database-level atomic risk reservation instead.
order_submission_lock = RLock()

@app.middleware("http")
async def security_headers(request: Request, call_next):
    """Apply baseline browser security headers to every response."""
    response: Response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
    # API responses may contain account/risk state and should not be stored by shared caches.
    if request.url.path.startswith("/api/") or request.url.path.startswith("/risk/") or request.url.path.startswith("/paper/"):
        response.headers.setdefault("Cache-Control", "no-store")
    if request.url.scheme == "https":
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response

from app.market_data import Candle
from app.agents import AGENTS, analyze_market, analyze_historical_spectrogram
from app.market_provider import fetch_candles, provider_status, MarketProviderError
from app.backtest import run_ema_backtest

class AgentAnalysisRequest(BaseModel):
    candles: list[Candle] = Field(min_length=1, max_length=500)

class SubmitSignalRequest(BaseModel):
    signal: Signal
    quantity: float = Field(gt=0, allow_inf_nan=False)
    risk_amount: float = Field(gt=0, allow_inf_nan=False)
    idempotency_key: str = Field(min_length=8, max_length=120)

class ClosePaperTradeRequest(BaseModel):
    exit_price: float = Field(gt=0, allow_inf_nan=False)


class SetupAnalysisRequest(BaseModel):
    market: MarketType
    symbol: str = Field(min_length=1, max_length=40)
    side: Side
    entry: float = Field(gt=0, allow_inf_nan=False)
    stop_loss: float = Field(gt=0, allow_inf_nan=False)
    take_profit: float = Field(gt=0, allow_inf_nan=False)
    equity: float = Field(default=10_000, gt=0, allow_inf_nan=False)
    risk_pct: float = Field(default=0.005, gt=0, le=0.005, allow_inf_nan=False)


class KellySizingRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=40)
    side: Side
    entry: float = Field(gt=0, allow_inf_nan=False)
    stop_loss: float = Field(gt=0, allow_inf_nan=False)
    equity: float = Field(gt=0, allow_inf_nan=False)
    win_rate: float = Field(ge=0, le=1, allow_inf_nan=False)
    average_win_loss_ratio: float = Field(gt=0, le=20, allow_inf_nan=False)
    kelly_fraction: float = Field(default=0.25, gt=0, le=0.5, allow_inf_nan=False)


class BacktestRequest(BaseModel):
    candles: list[Candle] = Field(min_length=30, max_length=500)
    fast_period: int = Field(default=8, ge=2, le=99)
    slow_period: int = Field(default=21, ge=3, le=100)
    initial_equity: float = Field(default=10_000, gt=0, allow_inf_nan=False)
    risk_pct: float = Field(default=0.005, gt=0, le=0.005, allow_inf_nan=False)
    spread_bps: float = Field(default=2, ge=0, le=100, allow_inf_nan=False)
    slippage_bps: float = Field(default=1, ge=0, le=100, allow_inf_nan=False)
    fee_bps: float = Field(default=1, ge=0, le=100, allow_inf_nan=False)
    stop_atr_multiplier: float = Field(default=1.5, ge=0.5, le=10, allow_inf_nan=False)
    reward_risk: float = Field(default=2, ge=0.5, le=10, allow_inf_nan=False)

def persistence_required() -> bool:
    """Require durable order/risk state on hosted or explicitly production deployments."""
    return (os.getenv("REQUIRE_PERSISTENCE", "").strip().lower() == "true"
            or os.getenv("APP_ENV", "").strip().lower() == "production"
            or os.getenv("RENDER", "").strip().lower() == "true")


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
                # Durable account equity is canonical; avoid restoring percentages against a process default.
                stored_equity = float(state["starting_equity"])
                if stored_equity <= 0:
                    raise RuntimeError("Stored starting equity is invalid")
                engine.risk.config = replace(engine.risk.config, initial_equity=stored_equity)
                engine.risk.restore_persisted_state(
                    realized_pnl=float(state["daily_realized_pnl"]),
                    open_risk=float(state["open_risk_pct"]) * stored_equity,
                    halted=bool(state["trading_halted"]),
                    trading_day=state["trading_day"],
                )
            database_ready = True
        except Exception as exc:
            database_ready = False
            raise RuntimeError("DATABASE_URL is configured but database initialization failed") from exc

@app.get("/health")
def health():
    ready = (store is not None and database_ready) or (store is None and not persistence_required())
    return {"status": "ok" if ready else "degraded",
            "mode": "paper_only", "real_execution_enabled": False,
            "persistence": "postgresql" if store is not None and database_ready else "memory_only",
            "persistence_required": persistence_required()}

@app.get("/risk/status", dependencies=[Depends(require_api_key)])
def risk_status():
    if persistence_required() and store is None:
        raise HTTPException(status_code=503, detail="Durable database persistence is required in production")
    if store is not None and not database_ready:
        raise HTTPException(status_code=503, detail="Database persistence is unavailable")
    return engine.risk.status

@app.post("/paper/orders", dependencies=[Depends(require_api_key)])
def submit_paper_order(payload: SubmitSignalRequest):
    with order_submission_lock:
        if persistence_required() and store is None:
            raise HTTPException(status_code=503, detail="Durable database persistence is required before accepting paper orders")
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
        if store is not None and payload.signal.symbol.strip().upper() == "XAUUSD":
            try:
                if store.has_open_xauusd(payload.signal.side.value):
                    raise HTTPException(status_code=409, detail="An XAUUSD paper position is already open; opposite, grid, and averaging entries are blocked")
            except HTTPException:
                raise
            except Exception as exc:
                raise HTTPException(status_code=503, detail="Could not verify existing XAUUSD exposure") from exc
        try:
            result = engine.submit_signal(payload.signal, payload.quantity, payload.risk_amount,
                                          payload.idempotency_key)
            if store is not None:
                request = OrderRequest(signal=payload.signal, quantity=payload.quantity,
                                       risk_amount=payload.risk_amount,
                                       idempotency_key=payload.idempotency_key)
                try:
                    store.persist_order(request, result, engine.risk.status, engine.risk.config)
                except (DuplicateOrderKey, OpenXauusdPosition, DurableRiskRejected) as exc:
                    # Undo the in-memory reservation if durable validation rejects this order.
                    engine.risk.release_reservation(payload.risk_amount, payload.idempotency_key)
                    # Reconcile this worker after a durable rejection.
                    try:
                        state = store.load_risk_state()
                        if state:
                            engine.risk.restore_persisted_state(
                                realized_pnl=float(state["daily_realized_pnl"]),
                                open_risk=float(state["open_risk_pct"]) * engine.risk.config.initial_equity,
                                halted=bool(state["trading_halted"]), trading_day=state["trading_day"],
                            )
                    except Exception:
                        pass
                    raise HTTPException(status_code=409, detail=str(exc)) from exc
                except Exception as exc:
                    # Fail closed: never leave phantom exposure in memory after a failed write.
                    engine.risk.release_reservation(payload.risk_amount, payload.idempotency_key)
                    raise HTTPException(status_code=503, detail="Paper order was not durably recorded") from exc
                # Reconcile local risk state with the committed database snapshot.
                state = store.load_risk_state()
                if state:
                    engine.risk.restore_persisted_state(
                        realized_pnl=float(state["daily_realized_pnl"]),
                        open_risk=float(state["open_risk_pct"]) * engine.risk.config.initial_equity,
                        halted=bool(state["trading_halted"]), trading_day=state["trading_day"],
                    )
            return result.model_dump(mode="json")
        except RiskRejected as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/paper/orders", dependencies=[Depends(require_api_key)])
def list_paper_orders(status: str | None = Query(default=None), limit: int = Query(default=100, ge=1, le=250)):
    if store is None or not database_ready:
        raise HTTPException(status_code=503, detail="Durable database persistence is required for trade history")
    try:
        rows = store.list_orders(status=status, limit=limit)
        return {"mode": "paper_only", "count": len(rows), "orders": rows}
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Could not load paper trade history") from exc


@app.post("/paper/orders/{order_id}/close", dependencies=[Depends(require_api_key)])
def close_paper_order(order_id: str, payload: ClosePaperTradeRequest):
    if store is None or not database_ready:
        raise HTTPException(status_code=503, detail="Durable database persistence is required to close paper trades")
    with order_submission_lock:
        try:
            result = store.close_order(order_id, payload.exit_price)
            if result is None:
                raise HTTPException(status_code=404, detail="Paper trade not found")
            state = result["risk_state"]
            engine.risk.restore_persisted_state(
                realized_pnl=float(state["daily_realized_pnl"]),
                open_risk=float(state["open_risk_pct"]) * engine.risk.config.initial_equity,
                halted=bool(state["trading_halted"]), trading_day=state["trading_day"],
            )
            return {key: value for key, value in result.items() if key != "risk_state"}
        except HTTPException:
            raise
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=503, detail="Could not close paper trade safely") from exc


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
@app.post("/api/backtest/ema", dependencies=[Depends(require_api_key)])
def backtest_ema(payload: BacktestRequest):
    """Run a conservative next-bar EMA crossover simulation; never submits orders."""
    if payload.fast_period >= payload.slow_period:
        raise HTTPException(status_code=422, detail="fast_period must be lower than slow_period")
    try:
        return run_ema_backtest(
            payload.candles, fast_period=payload.fast_period, slow_period=payload.slow_period,
            initial_equity=payload.initial_equity, risk_pct=payload.risk_pct,
            spread_bps=payload.spread_bps, slippage_bps=payload.slippage_bps,
            fee_bps=payload.fee_bps, stop_atr_multiplier=payload.stop_atr_multiplier,
            reward_risk=payload.reward_risk,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/risk/kelly-size", dependencies=[Depends(require_api_key)])
def kelly_position_size(payload: KellySizingRequest):
    """Calculate fractional-Kelly paper sizing; never creates or submits an order."""
    if payload.side == Side.buy and payload.stop_loss >= payload.entry:
        raise HTTPException(status_code=422, detail="Buy stop-loss must be below entry")
    if payload.side == Side.sell and payload.stop_loss <= payload.entry:
        raise HTTPException(status_code=422, detail="Sell stop-loss must be above entry")
    stop_distance = abs(payload.entry - payload.stop_loss)
    stop_distance_pct = stop_distance / payload.entry
    if not engine.risk.config.min_stop_distance_pct <= stop_distance_pct <= engine.risk.config.max_stop_distance_pct:
        raise HTTPException(status_code=422, detail="Stop-loss distance is outside configured limits")
    p = float(payload.win_rate)
    b = float(payload.average_win_loss_ratio)
    full_kelly = p - ((1.0 - p) / b)
    proposed_fraction = max(0.0, full_kelly * float(payload.kelly_fraction))
    risk_cap_pct = min(engine.risk.config.risk_per_trade_pct, 0.005)
    applied_risk_pct = min(proposed_fraction, risk_cap_pct)
    risk_budget = float(payload.equity) * applied_risk_pct
    quantity = risk_budget / stop_distance if risk_budget > 0 else 0.0
    return {
        "mode": "analysis_only", "order_placed": False, "execution_enabled": False,
        "symbol": payload.symbol.strip().upper(), "side": payload.side.value,
        "full_kelly_fraction": round(full_kelly, 6),
        "kelly_fraction_applied": float(payload.kelly_fraction),
        "risk_pct": round(applied_risk_pct * 100, 4),
        "risk_budget": round(risk_budget, 2),
        "stop_distance": round(stop_distance, 8),
        "suggested_quantity": round(quantity, 8),
        "trade_allowed_by_kelly": applied_risk_pct > 0,
        "risk_cap_pct": round(risk_cap_pct * 100, 4),
        "disclaimer": "Fractional Kelly estimate from user-supplied historical win rate/payoff; not a validated forecast or trade recommendation."
    }


@app.get("/api/agents/status")
def agent_status():
    """Static roster; agents remain idle until verified candles are supplied."""
    return {"mode":"paper_only", "execution_enabled":False, "agent_count":len(AGENTS),
            "agents":[{**agent, "status":"idle_waiting_for_verified_data"} for agent in AGENTS],
            "shared_pool":{"status":"no_live_balance_connected","live_pnl_available":False},
            "data_source":"user_supplied_ohlcv_only"}

@app.post("/api/agents/analyze", dependencies=[Depends(require_api_key)])
def agents_analyze(payload: AgentAnalysisRequest):
    """Run six independent rule-based votes over real supplied candles. Never submits orders."""
    try:
        return analyze_market(payload.candles)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/agents/spectrogram", dependencies=[Depends(require_api_key)])
def agents_spectrogram(payload: AgentAnalysisRequest):
    """Replay historical OHLCV windows to render agent signals; no forward prediction."""
    try:
        return analyze_historical_spectrogram(payload.candles)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/market/status")
def market_provider_status():
    """Expose provider configuration without revealing credentials."""
    return provider_status()


@app.get("/api/market/candles", dependencies=[Depends(require_api_key)])
def market_candles(
    market: MarketType = Query(...),
    symbol: str = Query(min_length=1, max_length=40),
    interval: str = Query(default="1min", max_length=8),
    outputsize: int = Query(default=100, ge=30, le=500),
):
    """Fetch read-only OHLCV from the configured provider; never submits orders."""
    try:
        candles = fetch_candles(market, symbol, interval, outputsize)
        return {
            "provider": "Twelve Data",
            "mode": "read_only",
            "market": market.value,
            "symbol": symbol.strip().upper(),
            "interval": interval,
            "count": len(candles),
            "candles": [c.model_dump(mode="json") for c in candles],
        }
    except MarketProviderError as exc:
        message = str(exc)
        status_code = 503 if "not configured" in message or "unavailable" in message else 422
        raise HTTPException(status_code=status_code, detail=message) from exc


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


@app.get("/api/news", dependencies=[Depends(require_api_key)])
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

@app.get("/desk", response_class=HTMLResponse, include_in_schema=False)
def swarm_desk():
    """Render the AI Swarm Desk preview without replacing the existing dashboard."""
    desk_file = Path(__file__).with_name("swarm_desk.html")
    return HTMLResponse(content=desk_file.read_text(encoding="utf-8"))
