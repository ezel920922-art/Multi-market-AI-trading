import hmac
import os
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from app.models import Signal
from app.engine import TradingEngine
from app.risk import RiskRejected

app = FastAPI(title="Multi-Market Trading Agency", version="0.1.0",
              description="Paper-only development API. No real broker execution is available.")
engine = TradingEngine()

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
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing API key",
            headers={"WWW-Authenticate": "ApiKey"},
        )

@app.get("/health")
def health():
    return {"status": "ok", "mode": "paper_only", "real_execution_enabled": False}

@app.get("/risk/status", dependencies=[Depends(require_api_key)])
def risk_status():
    return engine.risk.status

@app.post("/paper/orders", dependencies=[Depends(require_api_key)])
def submit_paper_order(payload: SubmitSignalRequest):
    try:
        result = engine.submit_signal(payload.signal, payload.quantity, payload.risk_amount, payload.idempotency_key)
        return result.model_dump(mode="json")
    except RiskRejected as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
