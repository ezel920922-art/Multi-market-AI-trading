import pytest
from app.astra_agents import build_agents
from app.astra_sim import AstraSimulation, FEE_RATE, SIM_DAY_BARS


def run(seed=7, steps=1500):
    sim = AstraSimulation(seed=seed)
    for _ in range(steps):
        sim.step()
    return sim


def test_six_agents_with_unique_roles():
    agents = build_agents()
    assert [a.spec.name for a in agents] == ["NOVA", "ORIN", "VESK", "LUMA", "KIRA", "DASH"]
    assert len({a.spec.role for a in agents}) == 6


def test_simulation_is_deterministic_for_a_seed():
    a, b = run(seed=11, steps=400), run(seed=11, steps=400)
    assert a.state()["candles"] == b.state()["candles"]
    assert list(a.trades) == list(b.trades)
    assert a.state()["balance"] == b.state()["balance"]


def test_state_is_labelled_simulation_and_paper_only():
    state = run(steps=100).state()
    assert state["mode"] == "simulation" and state["paper_only"] is True
    assert "SIMULATION" in state["disclaimer"]
    assert len(state["agents"]) == 6
    assert state["risk"]["paper_only"] is True


def test_no_lookahead_fills_happen_on_the_next_candle():
    sim = run(seed=3, steps=1500)
    assert len(sim.trades) > 20
    assert all(t["fill_tick"] == t["signal_tick"] + 1 for t in sim.trades)
    assert all(t["exit_tick"] >= t["fill_tick"] for t in sim.trades)


def test_risk_gate_limits_hold_throughout():
    sim = AstraSimulation(seed=5)
    limits = sim.risk.status
    for _ in range(1500):
        sim.step()
        status = sim.risk.status
        assert status["open_risk"] <= limits["max_open_risk_limit"] + 1e-6
        assert len(sim.positions) + len(sim.pending) <= 6
        for pos in sim.positions.values():
            assert pos.risk_amount <= limits["risk_per_trade_limit"] + 1e-9


def test_open_risk_is_released_when_everything_is_closed():
    sim = run(seed=9, steps=600)
    expected = sum(p.risk_amount for p in sim.positions.values()) + sum(v[2] for v in sim.pending.values())
    assert sim.risk.status["open_risk"] == pytest.approx(expected, abs=0.02)


def test_agent_pnl_sums_to_realized_and_includes_costs():
    sim = run(seed=2, steps=1200)
    assert sum(s["pnl"] for s in sim.stats.values()) == pytest.approx(sim.realized, abs=1e-6)
    if len(sim.trades) < sim.trades.maxlen:
        assert sum(t["pnl"] for t in sim.trades) == pytest.approx(sim.realized, abs=0.01 * max(1, len(sim.trades)))
    assert FEE_RATE > 0


def test_daily_loss_halt_blocks_entries_and_clears_on_new_sim_day():
    sim = AstraSimulation(seed=1, warmup=40)
    sim.risk.record_realized_pnl(-10_000)
    assert sim.risk.status["halted"] is True
    before = len(sim.positions) + len(sim.pending)
    for _ in range(5):
        if sim.tick % SIM_DAY_BARS == SIM_DAY_BARS - 1:
            break
        sim.step()
    assert len(sim.positions) + len(sim.pending) <= before
    while sim.tick % SIM_DAY_BARS != 0:
        sim.step()
    assert sim.risk.status["halted"] is False


def test_candles_are_valid_ohlc():
    for c in run(steps=300).state()["candles"]:
        assert c["h"] >= max(c["o"], c["c"]) and c["l"] <= min(c["o"], c["c"]) and c["v"] >= 0


def test_advance_is_time_based_and_capped():
    sim = AstraSimulation(seed=4, interval=1.0)
    t0 = sim.tick
    assert sim.advance(now=sim._last_advance + 0.2) == 0
    assert sim.advance(now=sim._last_advance + 3.1) == 3
    assert sim.tick == t0 + 3
    assert sim.advance(now=sim._last_advance + 500, max_steps=30) == 30
