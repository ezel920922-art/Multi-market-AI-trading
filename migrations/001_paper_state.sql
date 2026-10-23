-- PostgreSQL persistence for paper-trading state.
-- Safe to apply repeatedly; no existing rows or tables are deleted.
CREATE TABLE IF NOT EXISTS risk_state (
    singleton_id SMALLINT PRIMARY KEY DEFAULT 1 CHECK (singleton_id = 1),
    utc_day DATE NOT NULL,
    realized_pnl DOUBLE PRECISION NOT NULL DEFAULT 0,
    open_risk DOUBLE PRECISION NOT NULL DEFAULT 0 CHECK (open_risk >= 0),
    halted BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

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
);
