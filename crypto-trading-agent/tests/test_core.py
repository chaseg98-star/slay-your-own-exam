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


def test_proposal_stacking_cannot_bypass_caps(store, clock):
    """Stacked proposals re-check risk at confirmation: the daily cap, cooldown,
    and exposure limits apply at execute time, not just at submit time."""
    config = make_config(REQUIRE_CONFIRMATION="1")
    core, exchange, _ = make_core(store, clock, config=config)
    first = core.submit_prediction("BTC-USD", "rise", 0.9, 24, THESIS, 5.0)
    second = core.submit_prediction("BTC-USD", "rise", 0.9, 24, THESIS, 5.0)
    assert "proposal" in first and "proposal" in second

    assert core.confirm_decision(first["proposal"]["id"])["executed"] is True
    # second confirm: same product traded seconds ago -> cooldown refuses it
    out = core.confirm_decision(second["proposal"]["id"])
    assert out["executed"] is False and out["status"] == "rejected"
    assert any("cooldown" in r for r in out["reasons"])
    # and the proposal is consumed — cannot be retried
    with pytest.raises(ValueError, match="already"):
        core.confirm_decision(second["proposal"]["id"])


def test_maintenance_sweep_survives_one_failing_exit(store, clock):
    from coinbase_trading_agent.exchange import ExchangeError

    core, exchange, prices = make_core(store, clock)
    core.submit_prediction("BTC-USD", "rise", 0.95, 24, THESIS, 5.0)
    core.submit_prediction("ETH-USD", "rise", 0.95, 24, THESIS, 5.0)
    prices["BTC-USD"] *= 0.80
    prices["ETH-USD"] *= 0.80

    original_sell = exchange.market_sell

    def failing_btc_sell(product_id, base_size):
        if product_id == "BTC-USD":
            raise ExchangeError("simulated outage")
        return original_sell(product_id, base_size)

    exchange.market_sell = failing_btc_sell
    report = core.run_maintenance()
    actions = {a["product_id"]: a["action"] for a in report["actions"]}
    assert actions["BTC-USD"] == "stop_loss_exit_FAILED"
    assert actions["ETH-USD"] == "stop_loss_exit"  # sweep continued past the failure


def test_breaker_latch_blocks_same_day_reenable_and_loosening(store, clock):
    core, exchange, prices = make_core(store, clock)
    core.submit_prediction("BTC-USD", "rise", 0.95, 24, THESIS, 5.0)
    prices["BTC-USD"] = 50_000.0 * 0.01
    core.run_maintenance()
    assert core.get_status()["trading_enabled"] is False

    # Same UTC day: no re-enable, even with a long written reason, and no
    # loosening of the risk mode. Tightening stays allowed.
    with pytest.raises(GuardrailViolation, match="latch"):
        core.set_trading_enabled(True, "Reviewed the journal: flash crash stop-out, process was sound.")
    with pytest.raises(GuardrailViolation, match="riskier"):
        core.set_risk_mode("aggressive")
    core.set_risk_mode("conservative")

    # Next UTC day: a short reason is still refused, a written review passes.
    clock.advance(24 * 3600)
    with pytest.raises(ValueError, match="reason"):
        core.set_trading_enabled(True, "ok")
    core.set_trading_enabled(True, "Reviewed the journal: BTC flash-crash stop-out, thesis process was sound.")
    assert core.get_status()["trading_enabled"] is True


def test_lock_risk_controls(store, clock):
    config = make_config(LOCK_RISK_CONTROLS="1")
    core, _, _ = make_core(store, clock, config=config)
    with pytest.raises(GuardrailViolation):
        core.set_risk_mode("aggressive")
    core.set_trading_enabled(False, "halting is always allowed")
    with pytest.raises(GuardrailViolation):
        core.set_trading_enabled(True)


def test_full_exit_resets_position_clock(store, clock):
    """A quantized 'full' exit must not leave residue that carries the old
    opened_at onto the next position (which would trigger spurious time exits)."""
    core, exchange, prices = make_core(store, clock)
    core.submit_prediction("BTC-USD", "rise", 0.95, 24, THESIS, 5.0)
    core.close_position("BTC-USD", 1.0, "flat")
    assert store.get_position("BTC-USD") == (0.0, 0.0)
    assert store.position_opened_at("BTC-USD") is None

    clock.advance(40 * 86400)  # long after the original open
    core.submit_prediction("BTC-USD", "rise", 0.95, 24, THESIS, 5.0)
    report = core.run_maintenance()
    assert not any(a["action"] == "max_hold_time_exit" for a in report["actions"])


def test_config_rejects_mixed_quotes():
    from coinbase_trading_agent.config import ConfigError

    with pytest.raises(ConfigError, match="quote"):
        make_config(PRODUCT_WHITELIST="BTC-USD,BTC-USDC")


def test_config_expands_tilde(tmp_path, monkeypatch):
    cfg = make_config(AGENT_DATA_DIR="~/agent-data")
    assert "~" not in str(cfg.data_dir)


def test_portfolio_floor_liquidates_and_halts(store, clock):
    """Chase's rule: start ~$49, hard floor at $30 — everything sells, no override."""
    config = make_config(
        PAPER_STARTING_USD="49", PORTFOLIO_FLOOR_USD="30",
        MIN_TRADE_USD="2", MAX_TRADE_USD="15", DEFAULT_RISK_MODE="aggressive",
    )
    core, exchange, prices = make_core(store, clock, config=config)
    exchange.balances["USD"] = 49.0
    core.submit_prediction("BTC-USD", "rise", 0.95, 24, THESIS, 5.0)
    assert exchange.balances.get("BTC", 0.0) > 0

    # simulate prior losses: cash down to $25, position down to ~$4 → total ~$29 ≤ $30
    exchange.balances["USD"] = 25.0
    prices["BTC-USD"] = 21_750.0
    report = core.run_maintenance()
    assert any(a["action"] == "floor_liquidation" for a in report["actions"])
    assert report["trading_enabled"] is False
    assert "portfolio floor" in report["disabled_reason"]
    assert exchange.balances.get("BTC", 0.0) == 0.0

    # re-enable requires a written review, then buys are still blocked below the floor
    with pytest.raises(ValueError, match="reason"):
        core.set_trading_enabled(True, "ok")
    core.set_trading_enabled(True, "Reviewed with operator: floor did its job, restarting small.")
    out = core.submit_prediction("ETH-USD", "rise", 0.95, 24, THESIS, 5.0)
    assert out["executed"] is False
    assert any("portfolio floor" in r for r in out["decision"]["reasons"])


def test_shock_alert_on_sharp_drop(store, clock):
    from coinbase_trading_agent.exchange import Candle

    core, exchange, prices = make_core(store, clock)
    core.submit_prediction("BTC-USD", "rise", 0.95, 24, THESIS, 5.0)

    # 25 hourly candles flat at 50k; live price -9% vs last close: past the 8%
    # shock threshold but inside the 10% stop-loss (avg cost ≈ 50,300 w/ fee),
    # so the position survives the sweep and the alert fires
    exchange.get_candles = lambda pid, g, n: [
        Candle(start=i * 3600, open=50_000, high=50_500, low=49_500, close=50_000, volume=10)
        for i in range(25)
    ]
    prices["BTC-USD"] = 45_500.0
    report = core.run_maintenance()
    alerts = report["alerts"]
    assert len(alerts) == 1
    assert alerts[0]["type"] == "REVIEW_REQUIRED"
    assert alerts[0]["product_id"] == "BTC-USD"
    assert "Re-research" in alerts[0]["instruction"]


def test_no_shock_alert_when_calm(store, clock):
    from coinbase_trading_agent.exchange import Candle

    core, exchange, prices = make_core(store, clock)
    core.submit_prediction("BTC-USD", "rise", 0.95, 24, THESIS, 5.0)
    exchange.get_candles = lambda pid, g, n: [
        Candle(start=i * 3600, open=50_000, high=50_500, low=49_500, close=50_000, volume=10)
        for i in range(25)
    ]
    prices["BTC-USD"] = 49_500.0  # -1%: no alert
    assert core.run_maintenance()["alerts"] == []


def test_config_exchange_validation():
    from coinbase_trading_agent.config import ConfigError

    cfg = make_config(EXCHANGE="robinhood")
    assert cfg.exchange == "robinhood"
    with pytest.raises(ConfigError, match="EXCHANGE"):
        make_config(EXCHANGE="kraken")
    with pytest.raises(ConfigError, match="USD"):
        make_config(EXCHANGE="robinhood", QUOTE_CURRENCY="USDC")
    with pytest.raises(ConfigError, match="ROBINHOOD_API_KEY"):
        make_config(EXCHANGE="robinhood", TRADING_MODE="live")
