"""Optional PostgreSQL persistence for paper orders and durable risk snapshots."""
import json
import os
from datetime import datetime, timezone
from typing import Any

import psycopg
from psycopg.rows import dict_row


class DuplicateOrderKey(Exception):
    """An idempotency key already exists in durable storage."""


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

    def persist_order(self, request, result, risk_status: dict):
        signal = request.signal
        request_json = request.model_dump(mode="json")
        result_json = result.model_dump(mode="json")
        equity = float(os.getenv("TRADING_INITIAL_EQUITY", "10000"))
        day = datetime.now(timezone.utc).date()
        realized = float(risk_status["realized_pnl"])
        open_risk_pct = float(risk_status["open_risk"]) / equity
        halted = bool(risk_status["halted"])
        with self.connect() as conn:
            try:
                with conn.transaction():
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
                           SET current_equity = GREATEST(0, starting_equity + %s),
                               daily_realized_pnl = %s,
                               trading_day = %s,
                               trading_halted = %s,
                               halt_reason = CASE WHEN %s THEN 'daily_loss_limit' ELSE NULL END,
                               open_risk_pct = %s,
                               version = version + 1,
                               updated_at = now()
                         WHERE id = 1
                        """,
                        (realized, realized, day, halted, halted, open_risk_pct),
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
                            max(0.0, equity + realized), realized, open_risk_pct,
                            json.dumps({"symbol": signal.symbol, "market": signal.market.value,
                                        "side": signal.side.value, "order_id": result.order_id}),
                        ),
                    )
            except psycopg.errors.UniqueViolation as exc:
                raise DuplicateOrderKey("Idempotency key already exists in durable storage") from exc
