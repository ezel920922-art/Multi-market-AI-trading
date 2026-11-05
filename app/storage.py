"""Optional PostgreSQL persistence for paper orders and durable risk snapshots."""
import json
import os
from datetime import datetime, timezone
from typing import Any

import psycopg
from psycopg.rows import dict_row


class DuplicateOrderKey(Exception):
    """An idempotency key already exists in durable storage."""


class OpenXauusdPosition(Exception):
    """A concurrent or existing XAUUSD paper position blocks a new entry."""


class DurableRiskRejected(Exception):
    """The database-authoritative risk gate rejected a paper order."""


def _is_xauusd_symbol(symbol: str) -> bool:
    """Treat punctuation variants such as XAU/USD and XAU-USD as the same instrument."""
    return "".join(char for char in symbol.upper() if char.isalnum()) == "XAUUSD"


class PostgresStore:
    def __init__(self, database_url: str):
        if not database_url:
            raise ValueError("DATABASE_URL is required")
        self.database_url = database_url

    @classmethod
    def from_env(cls):
        url = os.getenv("DATABASE_URL", "").strip()
        return cls(url) if url else None

    def connect(self):
        return psycopg.connect(self.database_url, row_factory=dict_row, connect_timeout=8)

    def initialize(self, initial_equity: float = 10_000.0):
        today = datetime.now(timezone.utc).date()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO public.risk_state
                    (id, starting_equity, current_equity, daily_start_equity,
                     daily_realized_pnl, trading_day, trading_halted, open_risk_pct)
                VALUES (1, %s, %s, %s, 0, %s, false, 0)
                ON CONFLICT (id) DO NOTHING
                """,
                (initial_equity, initial_equity, initial_equity, today),
            )

    def load_risk_state(self) -> dict[str, Any] | None:
        with self.connect() as conn:
            return conn.execute(
                """SELECT starting_equity, current_equity, daily_start_equity,
                          daily_realized_pnl, trading_day, trading_halted,
                          halt_reason, open_risk_pct
                   FROM public.risk_state WHERE id = 1"""
            ).fetchone()

    def has_order_key(self, key: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM public.paper_orders WHERE client_order_id = %s",
                (key,),
            ).fetchone()
            return row is not None

    def has_open_xauusd(self, side: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM public.paper_orders WHERE regexp_replace(upper(symbol), '[^A-Z0-9]', '', 'g') = 'XAUUSD' AND status = 'filled' LIMIT 1"
            ).fetchone()
            return row is not None

    def list_orders(self, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as conn:
            if status in {"open", "filled"}:
                rows = conn.execute(
                    "SELECT * FROM public.paper_orders WHERE status = 'filled' ORDER BY created_at DESC LIMIT %s",
                    (limit,),
                ).fetchall()
            elif status in {"closed"}:
                rows = conn.execute(
                    "SELECT * FROM public.paper_orders WHERE status = 'closed' ORDER BY updated_at DESC LIMIT %s",
                    (limit,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM public.paper_orders ORDER BY created_at DESC LIMIT %s", (limit,)
                ).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item["request_json"] = item.get("request_json") or {}
            item["result_json"] = item.get("result_json") or {}
            output.append(item)
        return output

    def close_order(self, order_id: str, exit_price: float, risk_config) -> dict[str, Any] | None:
        day = datetime.now(timezone.utc).date()
        with self.connect() as conn:
            with conn.transaction():
                row = conn.execute(
                    "SELECT * FROM public.paper_orders WHERE result_json->>'order_id' = %s FOR UPDATE",
                    (order_id,),
                ).fetchone()
                if not row:
                    return None
                if row["status"] != "filled":
                    raise ValueError("Paper trade is not open")
                entry = float(row["entry_price"])
                qty = float(row["quantity"])
                pnl = (float(exit_price) - entry) * qty
                if row["side"] == "sell":
                    pnl = -pnl
                risk_state = conn.execute(
                    "SELECT * FROM public.risk_state WHERE id = 1 FOR UPDATE"
                ).fetchone()
                if not risk_state:
                    raise RuntimeError("Risk state is not initialized")
                same_day = risk_state["trading_day"] == day
                daily_pnl = (float(risk_state["daily_realized_pnl"]) if same_day else 0.0) + pnl
                current_equity = max(0.0, float(risk_state["current_equity"]) + pnl)
                daily_start = float(risk_state["daily_start_equity"]) if same_day else float(risk_state["current_equity"])
                loss_limit = float(risk_state["starting_equity"]) * float(risk_config.daily_loss_limit_pct)
                halted = daily_pnl <= -loss_limit
                open_risk = max(0.0, float(risk_state["open_risk_pct"]) - float(row["risk_amount"]) / float(risk_state["starting_equity"]))
                result_json = dict(row["result_json"] or {})
                result_json.update({"exit_price": float(exit_price), "realized_pnl": round(pnl, 8),
                                    "closed_at": datetime.now(timezone.utc).isoformat(), "close_reason": "manual_close"})
                conn.execute(
                    "UPDATE public.paper_orders SET status='closed', result_json=%s::jsonb, updated_at=now() WHERE id=%s",
                    (json.dumps(result_json), row["id"]),
                )
                conn.execute(
                    """UPDATE public.risk_state SET current_equity=%s, daily_start_equity=%s,
                       daily_realized_pnl=%s, trading_day=%s, trading_halted=%s,
                       halt_reason=CASE WHEN %s THEN 'daily_loss_limit' ELSE NULL END,
                       open_risk_pct=%s, version=version+1, updated_at=now() WHERE id=1""",
                    (current_equity, daily_start, daily_pnl, day, halted, halted, open_risk),
                )
                conn.execute(
                    """INSERT INTO public.risk_events(event_type,reason,equity,daily_realized_pnl,open_risk_pct,metadata)
                       VALUES ('paper_trade_closed','manual simulated close',%s,%s,%s,%s::jsonb)""",
                    (current_equity, daily_pnl, open_risk,
                     json.dumps({"order_id": order_id, "symbol": row["symbol"], "exit_price": exit_price, "pnl": pnl})),
                )
                state = conn.execute("SELECT daily_realized_pnl, open_risk_pct, trading_halted, trading_day FROM public.risk_state WHERE id=1").fetchone()
        return {"order_id": order_id, "status": "closed", "exit_price": float(exit_price),
                "realized_pnl": round(pnl, 2), "current_equity": round(current_equity, 2),
                "risk_state": dict(state)}

    def persist_order(self, request, result, risk_config):
        signal = request.signal
        request_json = request.model_dump(mode="json")
        result_json = result.model_dump(mode="json")
        day = datetime.now(timezone.utc).date()
        with self.connect() as conn:
            try:
                with conn.transaction():
                    # Serialize durable risk writes across app instances using the singleton state row.
                    risk_state = conn.execute(
                        """SELECT starting_equity, current_equity, daily_start_equity,
                                  daily_realized_pnl, trading_day, trading_halted,
                                  halt_reason, open_risk_pct
                           FROM public.risk_state WHERE id = 1 FOR UPDATE"""
                    ).fetchone()
                    if not risk_state:
                        raise RuntimeError("Risk state is not initialized")
                    equity = float(risk_state["starting_equity"])
                    if equity <= 0:
                        raise RuntimeError("Stored starting equity is invalid")
                    # The database snapshot is authoritative; never overwrite it with a
                    # potentially stale in-process risk snapshot from another worker.
                    same_day = risk_state["trading_day"] == day
                    realized = float(risk_state["daily_realized_pnl"]) if same_day else 0.0
                    halted = bool(risk_state["trading_halted"]) if same_day else False
                    daily_limit = equity * float(risk_config.daily_loss_limit_pct)
                    if halted or realized <= -daily_limit:
                        raise DurableRiskRejected("Trading halted: durable daily loss limit reached")
                    requested_risk_pct = float(request.risk_amount) / equity
                    if requested_risk_pct > float(risk_config.risk_per_trade_pct) + 1e-9:
                        raise DurableRiskRejected("Risk per trade exceeds the durable configured limit")
                    # Open exposure survives UTC day rollover until its position is closed.
                    open_risk_pct = float(risk_state["open_risk_pct"])
                    next_open_risk_pct = open_risk_pct + requested_risk_pct
                    if next_open_risk_pct > float(risk_config.max_open_risk_pct) + 1e-9:
                        raise DurableRiskRejected("Aggregate open risk limit exceeded in durable state")
                    remaining_daily_capacity_pct = max(0.0, daily_limit / equity + realized / equity - open_risk_pct)
                    if requested_risk_pct > remaining_daily_capacity_pct + 1e-9:
                        raise DurableRiskRejected("Risk exceeds remaining daily loss capacity in durable state")
                    # The row lock makes this check + insert atomic across concurrent instances.
                    if _is_xauusd_symbol(signal.symbol):
                        existing = conn.execute(
                            "SELECT 1 FROM public.paper_orders WHERE regexp_replace(upper(symbol), '[^A-Z0-9]', '', 'g') = 'XAUUSD' AND status = 'filled' LIMIT 1"
                        ).fetchone()
                        if existing:
                            raise OpenXauusdPosition("An XAUUSD paper position is already open")
                    conn.execute(
                        """
                        INSERT INTO public.paper_orders
                          (client_order_id, market, symbol, side, quantity,
                           order_type, entry_price, stop_loss, take_profit,
                           confidence, risk_amount, status, request_json, result_json)
                        VALUES (%s, %s, %s, %s, %s, 'market', %s, %s, %s,
                                %s, %s, 'filled', %s::jsonb, %s::jsonb)
                        """,
                        (
                            request.idempotency_key, signal.market.value, signal.symbol,
                            signal.side.value, request.quantity, signal.entry,
                            signal.stop_loss, signal.take_profit, signal.confidence,
                            request.risk_amount, json.dumps(request_json),
                            json.dumps(result_json),
                        ),
                    )
                    conn.execute(
                        """
                        UPDATE public.risk_state
                           SET daily_start_equity = CASE WHEN trading_day <> %s THEN current_equity ELSE daily_start_equity END,
                               daily_realized_pnl = %s,
                               trading_day = %s,
                               trading_halted = %s,
                               halt_reason = CASE WHEN %s THEN 'daily_loss_limit' ELSE NULL END,
                               open_risk_pct = %s,
                               version = version + 1,
                               updated_at = now()
                         WHERE id = 1
                        """,
                        (day, realized, day, halted, halted, next_open_risk_pct),
                    )
                    conn.execute(
                        """
                        INSERT INTO public.risk_events
                          (event_type, reason, equity, daily_realized_pnl,
                           open_risk_pct, metadata)
                        VALUES ('paper_order_filled', 'simulated execution only',
                                %s, %s, %s, %s::jsonb)
                        """,
                        (
                            float(risk_state["current_equity"]), realized, next_open_risk_pct,
                            json.dumps({"symbol": signal.symbol, "market": signal.market.value,
                                        "side": signal.side.value, "order_id": result.order_id}),
                        ),
                    )
            except psycopg.errors.UniqueViolation as exc:
                raise DuplicateOrderKey("Idempotency key already exists in durable storage") from exc
