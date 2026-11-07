"""Small PostgreSQL persistence layer for paper-trading state.

Only enabled when DATABASE_URL is configured. This is a single-instance prototype:
run one application worker until risk/order updates are made transactionally across workers.
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timezone
from typing import Any


class PostgresStore:
    def __init__(self, dsn: str | None = None):
        self.dsn = (dsn or os.getenv("DATABASE_URL", "")).strip()
        if not self.dsn:
            raise RuntimeError("DATABASE_URL is required for PostgreSQL persistence")
        try:
            import psycopg
        except ImportError as exc:
            raise RuntimeError("Install psycopg[binary] to use PostgreSQL persistence") from exc
        self._psycopg = psycopg
        self.initialize()

    def connect(self):
        return self._psycopg.connect(self.dsn, connect_timeout=5)

    def initialize(self) -> None:
        with self.connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS risk_state (
                    singleton_id SMALLINT PRIMARY KEY DEFAULT 1 CHECK (singleton_id = 1),
                    utc_day DATE NOT NULL,
                    realized_pnl DOUBLE PRECISION NOT NULL DEFAULT 0,
                    open_risk DOUBLE PRECISION NOT NULL DEFAULT 0 CHECK (open_risk >= 0),
                    halted BOOLEAN NOT NULL DEFAULT FALSE,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS paper_orders (
                    order_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    market TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    quantity DOUBLE PRECISION NOT NULL CHECK (quantity > 0),
                    risk_amount DOUBLE PRECISION NOT NULL CHECK (risk_amount > 0),
                    status TEXT NOT NULL DEFAULT 'paper_filled',
                    payload JSONB NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            """)
            conn.execute("""
                INSERT INTO risk_state (singleton_id, utc_day)
                VALUES (1, %s)
                ON CONFLICT (singleton_id) DO NOTHING
            """, (datetime.now(timezone.utc).date(),))

    def load_risk_state(self) -> dict[str, Any]:
        today = datetime.now(timezone.utc).date()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT utc_day, realized_pnl, open_risk, halted FROM risk_state WHERE singleton_id = 1"
            ).fetchone()
            if row is None:
                conn.execute("INSERT INTO risk_state (singleton_id, utc_day) VALUES (1, %s) ON CONFLICT DO NOTHING", (today,))
                return {"day": today, "realized_pnl": 0.0, "open_risk": 0.0, "halted": False}
            day, pnl, open_risk, halted = row
            if day != today:
                # Daily realized PnL and halt reset at UTC day boundary; open exposure remains.
                conn.execute(
                    "UPDATE risk_state SET utc_day=%s, realized_pnl=0, halted=FALSE, updated_at=now() WHERE singleton_id=1",
                    (today,),
                )
                return {"day": today, "realized_pnl": 0.0, "open_risk": float(open_risk), "halted": False}
            return {"day": day, "realized_pnl": float(pnl), "open_risk": float(open_risk), "halted": bool(halted)}

    def save_risk_state(self, *, day: date, realized_pnl: float, open_risk: float, halted: bool) -> None:
        with self.connect() as conn:
            conn.execute("""
                INSERT INTO risk_state (singleton_id, utc_day, realized_pnl, open_risk, halted, updated_at)
                VALUES (1, %s, %s, %s, %s, now())
                ON CONFLICT (singleton_id) DO UPDATE SET
                    utc_day=EXCLUDED.utc_day, realized_pnl=EXCLUDED.realized_pnl,
                    open_risk=EXCLUDED.open_risk, halted=EXCLUDED.halted, updated_at=now()
            """, (day, realized_pnl, open_risk, halted))

    def has_idempotency_key(self, key: str) -> bool:
        with self.connect() as conn:
            return conn.execute(
                "SELECT 1 FROM paper_orders WHERE idempotency_key=%s", (key,)
            ).fetchone() is not None

    def save_order(self, result, order) -> None:
        payload = result.model_dump(mode="json")
        with self.connect() as conn:
            conn.execute("""
                INSERT INTO paper_orders
                    (order_id, idempotency_key, market, symbol, side, quantity, risk_amount, status, payload)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                ON CONFLICT (idempotency_key) DO NOTHING
            """, (
                result.order_id, order.idempotency_key, order.signal.market.value,
                order.signal.symbol, order.signal.side.value, order.quantity,
                order.risk_amount, result.status, json.dumps(payload),
            ))

    def get_order(self, key: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT payload FROM paper_orders WHERE idempotency_key=%s", (key,)
            ).fetchone()
            if row is None:
                return None
            return row[0] if isinstance(row[0], dict) else json.loads(row[0])
