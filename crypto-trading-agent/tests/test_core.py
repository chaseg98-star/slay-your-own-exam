import pytest

from coinbase_trading_agent.guardrails import GuardrailViolation
from conftest import make_config, make_core

THESIS = "Strong on-chain accumulation plus ETF inflow headlines; expecting continuation."


def test_startup_refuses_transfer_key(store, clock):
    core, exchange, _ = make_core(store, clock)
    exchange.get_key_permissions = lambda: {"can_view": True, "can_trade": True, "can_transfer": True}
    with pytest.raises(GuardrailViolation):
        core.startup_checks()


def test_paper_startup_ok(store, clock):
    core, _, _ = make_core(store, clock)
    out = core.startup_checks()
    assert out["key_permissions"]["can_transfer"] is False


def test_immediate_execution_when_confirmation_off(store, clock):
    core, exchange, _ = make_core(store, clock)
    out = core.submit_prediction("BTC-USD", "rise", 0.9, 24, THESIS, 5.0)
    assert out["executed"] is True
    assert out["fill"]["side"] == "BUY"
    assert exchange.balances["BTC"] > 0
    # conservative mode (default): 5% of 10k * scalar(0.9→0.75) = 375 → capped at 250
    assert out["fill"]["quote_size"] <= 250.0


def test_two_phase_confirm_flow(store, clock):
    config = make_config(REQUIRE_CONFIRMATION="1")
    core, exchange, _ = make_core(store, clock, config=config)
    out = core.submit_prediction("BTC-USD", "rise", 0.9, 24, THESIS, 5.0)
    assert out["executed"] is False and "proposal" in out
    assert exchange.balances.get("BTC", 0.0) == 0.0  # nothing bought yet

    confirmed = core.confirm_decision(out["proposal"]["id"])
    assert confirmed["executed"] is True
    assert exchange.balances["BTC"] > 0

    with pytest.raises(ValueError, match="already"):
        core.confirm_decision(out["proposal"]["id"])


def test_two_phase_reject_flow(store, clock):
    config = make_config(REQUIRE_CONFIRMATION="1")
    core, exchange, _ = make_core(store, clock, config=config)
    out = core.submit_prediction("BTC-USD", "rise", 0.9, 24, THESIS, 5.0)
    rejected = core.reject_decision(out["proposal"]["id"], "technicals contradict the thesis")
    assert rejected["status"] == "rejected"
    assert exchange.balances.get("BTC", 0.0) == 0.0


def test_expired_proposal_cannot_execute(store, clock):
    config = make_config(REQUIRE_CONFIRMATION="1")
    core, exchange, _ = make_core(store, clock, config=config)
    out = core.submit_prediction("BTC-USD", "rise", 0.9, 24, THESIS, 5.0)
    clock.advance(16 * 60)
    with pytest.raises(ValueError, match="expired"):
        core.confirm_decision(out["proposal"]["id"])
    assert exchange.balances.get("BTC", 0.0) == 0.0


def test_low_confidence_records_but_never_proposes(store, clock):
    core, _, _ = make_core(store, clock)
    out = core.submit_prediction("BTC-USD", "rise", 0.5, 24, THESIS, 5.0)
    assert out["executed"] is False and "proposal" not in out
    assert core.get_predictions(5)[0]["decision"]["action"] == "no_action"


def test_fall_sells_held_position(store, clock):
    core, exchange, _ = make_core(store, clock)
    core.submit_prediction("BTC-USD", "rise", 0.95, 24, THESIS, 5.0)
    clock.advance(7 * 3600)  # past conservative cooldown (6h)
    out = core.submit_prediction("BTC-USD", "fall", 0.95, 24, THESIS, 5.0)
    assert out["executed"] is True
    assert out["fill"]["side"] == "SELL"


def test_kill_switch_blocks_and_reenables(store, clock):
    core, _, _ = make_core(store, clock)
    core.set_trading_enabled(False, "user hit the kill switch")
    out = core.submit_prediction("BTC-USD", "rise", 0.95, 24, THESIS, 5.0)
    assert out["executed"] is False
    with pytest.raises(ValueError):
        core.close_position("BTC-USD", 1.0, "should fail: nothing held")
    core.set_trading_enabled(True)
    assert core.submit_prediction("ETH-USD", "rise", 0.95, 24, THESIS, 5.0)["executed"] is True


def test_close_position_bypasses_cooldown_but_not_kill_switch(store, clock):
    core, exchange, _ = make_core(store, clock)
    core.submit_prediction("BTC-USD", "rise", 0.95, 24, THESIS, 5.0)
    # within cooldown, close_position still works (risk-reducing)
    out = core.close_position("BTC-USD", 1.0, "thesis invalidated")
    assert out["executed"] is True
    assert exchange.balances.get("BTC", 0.0) == 0.0


def test_challenge_unwinds_buy(store, clock):
    core, exchange, _ = make_core(store, clock)
    buy = core.submit_prediction("BTC-USD", "rise", 0.95, 24, THESIS, 5.0)
    out = core.challenge_trade(buy["fill"]["order_id"], "The catalyst was already priced in; I was wrong.")
    assert out["unwound"] is True
    assert exchange.balances.get("BTC", 0.0) == 0.0


def test_challenge_sell_refused(store, clock):
    core, _, _ = make_core(store, clock)
    core.submit_prediction("BTC-USD", "rise", 0.95, 24, THESIS, 5.0)
    exit_out = core.close_position("BTC-USD", 1.0, "exit")
    out = core.challenge_trade(exit_out["fill"]["order_id"], "Actually that sell was premature I think.")
    assert out["unwound"] is False


def test_stop_loss_maintenance(store, clock):
    core, exchange, prices = make_core(store, clock)
    core.submit_prediction("BTC-USD", "rise", 0.95, 24, THESIS, 5.0)
    prices["BTC-USD"] = 50_000.0 * 0.85  # -15%, past conservative stop of 10%
    report = core.run_maintenance()
    assert any(a["action"] == "stop_loss_exit" for a in report["actions"])
    assert exchange.balances.get("BTC", 0.0) == 0.0


def test_stop_loss_not_triggered_within_band(store, clock):
    core, exchange, prices = make_core(store, clock)
    core.submit_prediction("BTC-USD", "rise", 0.95, 24, THESIS, 5.0)
    prices["BTC-USD"] = 50_000.0 * 0.95  # -5%: inside the 10% conservative stop
    report = core.run_maintenance()
    assert report["actions"] == []
    assert exchange.balances.get("BTC", 0.0) > 0


def test_max_hold_time_exit(store, clock):
    core, exchange, prices = make_core(store, clock)
    core.submit_prediction("BTC-USD", "rise", 0.95, 24, THESIS, 5.0)
    prices["BTC-USD"] = 50_000.0 * 1.05  # profitable — time exit fires regardless
    clock.advance(29 * 86400)
    report = core.run_maintenance()
    assert any(a["action"] == "max_hold_time_exit" for a in report["actions"])
    assert exchange.balances.get("BTC", 0.0) == 0.0


def test_fee_gate_blocks_small_expected_moves(store, clock):
    core, exchange, _ = make_core(store, clock)
    out = core.submit_prediction("BTC-USD", "rise", 0.95, 24, THESIS, 0.8)
    assert out["executed"] is False and "proposal" not in out
    assert any("fee gate" in r for r in out["decision"]["reasons"])
    assert exchange.balances.get("BTC", 0.0) == 0.0
    # fall predictions are exits (risk-reducing) and are exempt from the gate
    core.submit_prediction("BTC-USD", "rise", 0.95, 24, THESIS, 5.0)
    clock.advance(7 * 3600)
    out = core.submit_prediction("BTC-USD", "fall", 0.95, 24, THESIS, 0.8)
    assert out["executed"] is True and out["fill"]["side"] == "SELL"


def test_daily_loss_breaker_disables_trading(store, clock):
    core, exchange, prices = make_core(store, clock)
    core.submit_prediction("BTC-USD", "rise", 0.95, 24, THESIS, 5.0)
    # near-total wipeout: realized loss (~$250) exceeds 2% of the portfolio (~$195)
    prices["BTC-USD"] = 50_000.0 * 0.01
    core.run_maintenance()  # stop-loss sells at a big realized loss
    status = core.get_status()
    assert status["trading_enabled"] is False
    assert "circuit breaker" in status["disabled_reason"]
    # and predictions no longer trade
    out = core.submit_prediction("ETH-USD", "rise", 0.95, 24, THESIS, 5.0)
    assert out["executed"] is False


def test_input_validation(store, clock):
    core, _, _ = make_core(store, clock)
    with pytest.raises(ValueError):
        core.submit_prediction("btc_usd!!", "rise", 0.9, 24, THESIS, 5.0)
    with pytest.raises(ValueError):
        core.submit_prediction("BTC-USD", "moon", 0.9, 24, THESIS, 5.0)
    with pytest.raises(ValueError):
        core.submit_prediction("BTC-USD", "rise", 1.5, 24, THESIS, 5.0)
    with pytest.raises(ValueError):
        core.submit_prediction("BTC-USD", "rise", 0.9, 0.1, THESIS, 5.0)
    with pytest.raises(ValueError):
        core.submit_prediction("BTC-USD", "rise", 0.9, 24, "gm", 5.0)


def test_portfolio_reporting(store, clock):
    core, _, _ = make_core(store, clock)
    core.submit_prediction("BTC-USD", "rise", 0.95, 24, THESIS, 5.0)
    portfolio = core.get_portfolio()
    assert portfolio["total_value"] == pytest.approx(10_000, rel=0.02)
    btc = next(p for p in portfolio["positions"] if p["product_id"] == "BTC-USD")
    assert btc["avg_cost"] is not None and btc["unrealized_pnl"] is not None


def test_status_shape(store, clock):
    core, _, _ = make_core(store, clock)
    status = core.get_status()
    assert status["risk_mode"] == "conservative"
    assert status["trading_mode"] == "paper"
    assert "withdraw funds" in status["capabilities_excluded"][0]


def test_set_risk_mode(store, clock):
    core, _, _ = make_core(store, clock)
    out = core.set_risk_mode("aggressive")
    assert out["risk_params"]["min_confidence"] == 0.55
    with pytest.raises(ValueError):
        core.set_risk_mode("yolo")
