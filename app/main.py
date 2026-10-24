import hmac
import os
from pathlib import Path
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from app.models import Signal, OrderRequest
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
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except Exception as exc:
                raise HTTPException(status_code=503, detail="Paper order was not durably recorded") from exc
        return result.model_dump(mode="json")
    except RiskRejected as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def dashboard():
    dashboard_file = Path(__file__).with_name("dashboard.html")
    return HTMLResponse(content=dashboard_file.read_text(encoding="utf-8"))
