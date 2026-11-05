"""Opt-in integration coverage against the isolated Neon PR test branch.

Set TRADING_TEST_DATABASE_URL to the disposable test branch only. The guard
row prevents accidentally running this suite against the primary database.
"""
import os
import uuid
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest

from app.models import MarketType, OrderRequest, OrderResult, Side, Signal
from app.risk import RiskConfig
from app.storage import DuplicateOrderKey, OpenXauusdPosition, DurableRiskRejected, PostgresStore

DATABASE_URL = os.getenv("TRADING_TEST_DATABASE_URL", "").strip()
pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="TRADING_TEST_DATABASE_URL is not configured",
)

EXPECTED_TEST_BRANCH = "br-weathered-river-ba84ti98"


def _assert_isolated_test_branch():
    with psycopg.connect(DATABASE_URL) as conn:
        database = conn.execute("SELECT current_database()").fetchone()[0]
        marker = conn.execute(
            "SELECT branch_id FROM public.integration_test_guard WHERE id = 1"
        ).fetchone()
    assert database == "trading_agency", f"Unexpected test database: {database}"
    assert marker and marker[0] == EXPECTED_TEST_BRANCH, (
        "Refusing to run PostgreSQL integration tests outside the dedicated PR test branch"
    )


def _new_order(prefix="pgtest-"):
    suffix = uuid.uuid4().hex
    signal = Signal(
        strategy_id="postgres-integration-test",
        market=MarketType.commodities,
        symbol="XAUUSD",
        side=Side.buy,
        entry=2000.0,
        stop_loss=1990.0,
        take_profit=2020.0,
        confidence=0.95,
        rationale="Isolated PostgreSQL integration test",
    )
    request = OrderRequest(
        signal=signal,
        quantity=0.1,
        risk_amount=25.0,
        idempotency_key=f"{prefix}{suffix}",
    )
    result = OrderResult(
        order_id=f"{prefix}{suffix}",
        status="filled",
        market=MarketType.commodities,
        symbol="XAUUSD",
        side=Side.buy,
        quantity=0.1,
        paper=True,
        message="Integration test only",
    )
    return request, result


def _assert_insert_failure_rolls_back(store, config):
    """Force the final event insert to fail after order/risk writes have run."""
    request, result = _new_order(prefix="pgtest-rollback-")
    suffix = uuid.uuid4().hex[:12]
    trigger_name = f"pgtest_force_event_failure_{suffix}"
    function_name = f"pgtest_raise_on_rollback_order_{suffix}"

    # The trigger is confined to this disposable, branch-guarded test database
    # and only rejects the unique order id generated for this rollback case.
    with psycopg.connect(DATABASE_URL) as conn:
        before = conn.execute(
            "SELECT open_risk_pct, current_equity, daily_realized_pnl, version "
            "FROM public.risk_state WHERE id = 1"
        ).fetchone()
        conn.execute(
            f"""
            CREATE OR REPLACE FUNCTION public.{function_name}()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF NEW.metadata->>'order_id' LIKE 'pgtest-rollback-%' THEN
                    RAISE EXCEPTION 'intentional integration-test rollback';
                END IF;
                RETURN NEW;
            END;
            $$
            """
        )
        conn.execute(
            f"""
            CREATE TRIGGER {trigger_name}
            BEFORE INSERT ON public.risk_events
            FOR EACH ROW EXECUTE FUNCTION public.{function_name}()
            """
        )

    try:
        with pytest.raises(psycopg.errors.RaiseException, match="intentional integration-test rollback"):
            store.persist_order(request, result, config)

        with psycopg.connect(DATABASE_URL) as conn:
            after = conn.execute(
                "SELECT open_risk_pct, current_equity, daily_realized_pnl, version "
                "FROM public.risk_state WHERE id = 1"
            ).fetchone()
            order_count = conn.execute(
                "SELECT count(*) FROM public.paper_orders WHERE client_order_id = %s",
                (request.idempotency_key,),
            ).fetchone()[0]
            event_count = conn.execute(
                "SELECT count(*) FROM public.risk_events WHERE metadata->>'order_id' = %s",
                (result.order_id,),
            ).fetchone()[0]

        assert order_count == 0, "Order insert survived a failed transaction"
        assert event_count == 0, "Risk event unexpectedly survived a failed transaction"
        assert tuple(after) == tuple(before), (
            f"Risk state changed despite transaction rollback: before={before}, after={after}"
        )
    finally:
        with psycopg.connect(DATABASE_URL) as conn:
            conn.execute(f"DROP TRIGGER IF EXISTS {trigger_name} ON public.risk_events")
            conn.execute(f"DROP FUNCTION IF EXISTS public.{function_name}()")


def test_postgres_atomic_risk_persistence_concurrency_close_and_rollback():
    _assert_isolated_test_branch()
    config = RiskConfig()
    store = PostgresStore(DATABASE_URL)

    with psycopg.connect(DATABASE_URL) as conn:
        open_xau = conn.execute(
            "SELECT count(*) FROM paper_orders "
            "WHERE upper(symbol) = 'XAUUSD' AND status = 'filled'"
        ).fetchone()[0]
        baseline = conn.execute(
            "SELECT open_risk_pct, current_equity, daily_realized_pnl "
            "FROM risk_state WHERE id = 1"
        ).fetchone()
        assert open_xau == 0, "Test branch must start without an open XAUUSD position"
        assert float(baseline[0]) == 0.0, "Test branch must start with zero open risk"

    pairs = [_new_order(), _new_order()]

    def submit(pair):
        request, result = pair
        try:
            PostgresStore(DATABASE_URL).persist_order(request, result, config)
            return "accepted", request, result
        except Exception as exc:
            return type(exc), request, result

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(submit, pairs))

    accepted = [item for item in outcomes if item[0] == "accepted"]
    rejected = [item for item in outcomes if item[0] is OpenXauusdPosition]
    assert len(accepted) == 1 and len(rejected) == 1, (
        f"Expected one XAUUSD fill and one rejection; got {[item[0] for item in outcomes]}"
    )

    request, result = accepted[0][1], accepted[0][2]
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            persisted = conn.execute(
                "SELECT status FROM paper_orders WHERE client_order_id = %s",
                (request.idempotency_key,),
            ).fetchone()
            reserved_risk = conn.execute(
                "SELECT open_risk_pct FROM risk_state WHERE id = 1"
            ).fetchone()[0]
        assert persisted and persisted[0] == "filled"
        assert abs(float(reserved_risk) - 0.0025) < 1e-9

        closed = store.close_order(result.order_id, 2010.0, config)
        assert closed and closed["status"] == "closed"

        # Retry the same idempotency key after close so the durable unique
        # constraint is reached; the failed transaction must not alter risk.
        with pytest.raises(DuplicateOrderKey):
            store.persist_order(request, result, config)

        with psycopg.connect(DATABASE_URL) as conn:
            state = conn.execute(
                "SELECT open_risk_pct, current_equity, daily_realized_pnl "
                "FROM risk_state WHERE id = 1"
            ).fetchone()
            order = conn.execute(
                "SELECT status, result_json->>'realized_pnl' "
                "FROM paper_orders WHERE client_order_id = %s",
                (request.idempotency_key,),
            ).fetchone()
            order_count = conn.execute(
                "SELECT count(*) FROM paper_orders WHERE client_order_id = %s",
                (request.idempotency_key,),
            ).fetchone()[0]
            event_count = conn.execute(
                "SELECT count(*) FROM risk_events WHERE metadata->>'order_id' = %s",
                (result.order_id,),
            ).fetchone()[0]

        assert order and order[0] == "closed"
        assert abs(float(state[0])) < 1e-9
        assert abs(float(state[1]) - (float(baseline[1]) + 1.0)) < 1e-6
        assert abs(float(state[2]) - (float(baseline[2]) + 1.0)) < 1e-6
        assert order_count == 1
        assert event_count == 2

        # Verify the durable gate reserves the remaining daily-loss budget.
        # Restore the exact prior daily snapshot afterward so this disposable
        # branch remains reusable by subsequent CI runs.
        with psycopg.connect(DATABASE_URL) as conn:
            prior_day = conn.execute(
                "SELECT trading_day FROM public.risk_state WHERE id = 1"
            ).fetchone()[0]
            conn.execute(
                "UPDATE public.risk_state SET trading_day=CURRENT_DATE, daily_realized_pnl=-140, "
                "trading_halted=false, open_risk_pct=0 WHERE id=1"
            )
        capacity_request, capacity_result = _new_order(prefix="pgtest-daily-capacity-")
        capacity_request.risk_amount = 20.0
        try:
            with pytest.raises(DurableRiskRejected, match="remaining daily loss capacity"):
                store.persist_order(capacity_request, capacity_result, config)
        finally:
            with psycopg.connect(DATABASE_URL) as conn:
                conn.execute(
                    "UPDATE public.risk_state SET trading_day=%s, daily_realized_pnl=%s, "
                    "trading_halted=false, open_risk_pct=0 WHERE id=1",
                    (prior_day, float(baseline[2])),
                )

        _assert_insert_failure_rolls_back(store, config)
    finally:
        # Never leave an open test position behind if an assertion fails.
        with psycopg.connect(DATABASE_URL) as conn:
            pending = conn.execute(
                "SELECT result_json->>'order_id', entry_price FROM paper_orders "
                "WHERE client_order_id = %s AND status = 'filled'",
                (request.idempotency_key,),
            ).fetchone()
        if pending:
            store.close_order(pending[0], float(pending[1]), config)
