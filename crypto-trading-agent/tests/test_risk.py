import pytest

from coinbase_trading_agent import risk
from coinbase_trading_agent.models import (
    RISK_PROFILES,
    Direction,
    PortfolioSnapshot,
    Prediction,
    RiskMode,
)

PARAMS = RISK_PROFILES[RiskMode.MODERATE]
NOW = 1_750_000_000.0


def snap(quote=10_000.0, positions=None):
    return PortfolioSnapshot(quote_currency="USD", quote_balance=quote, positions=positions or {})


def pred(direction=Direction.RISE, confidence=0.8, product="BTC-USD"):
    return Prediction(
        product_id=product,
        direction=direction,
        confidence=confidence,
        horizon_hours=24,
        thesis="test thesis long enough",
        created_at=NOW,
    )


def evaluate(p, snapshot, **overrides):
    kwargs = dict(
        trades_today=0,
        realized_pnl_today=0.0,
        last_trade_ts=None,
        now=NOW,
        whitelist=("BTC-USD", "ETH-USD", "SOL-USD"),
        max_trade_usd=250.0,
        min_trade_usd=5.0,
        trading_enabled=True,
    )
    kwargs.update(overrides)
    return risk.evaluate(p, PARAMS, snapshot, **kwargs)


def test_kill_switch_blocks_everything():
    d = evaluate(pred(), snap(), trading_enabled=False)
    assert d.action == "no_action"
    assert "disabled" in d.reasons[0]


def test_low_confidence_recorded_not_traded():
    d = evaluate(pred(confidence=0.5), snap())
    assert d.action == "no_action"
    assert "below" in " ".join(d.reasons)


def test_buy_size_scales_with_confidence_and_caps():
    # moderate: max 10% of 10k = 1000, but hard cap 250 applies
    d = evaluate(pred(confidence=1.0), snap())
    assert d.action == "buy"
    assert d.quote_size == 250.0
    assert any("hard per-trade cap" in r for r in d.reasons)


def test_buy_clamped_by_exposure_cap():
    # BTC at 4000 of a 14000 portfolio; moderate caps exposure at 30% (4200)
    s = snap(quote=10_000.0, positions={"BTC-USD": (0.08, 50_000.0)})
    d = evaluate(pred(confidence=1.0), s)
    assert d.action == "buy"
    assert d.quote_size == pytest.approx(0.30 * 14_000.0 - 4_000.0)  # 200


def test_buy_blocked_when_exposure_room_below_minimum():
    # BTC at 4285 of a 14285 portfolio; room = 4285.5 - 4285 = 0.5 < $5 minimum
    s = snap(quote=10_000.0, positions={"BTC-USD": (0.0857, 50_000.0)})
    d = evaluate(pred(confidence=1.0), s)
    assert d.action == "no_action"


def test_buy_blocked_at_full_exposure():
    # BTC already 40% of portfolio, over the 30% cap
    s = snap(quote=9_000.0, positions={"BTC-USD": (0.12, 50_000.0)})
    d = evaluate(pred(confidence=1.0), s)
    assert d.action == "no_action"
    assert any("already at" in r for r in d.reasons)


def test_whitelist_rejected():
    d = evaluate(pred(product="PEPE-USD"), snap(), whitelist=("BTC-USD",))
    assert d.action == "no_action"
    assert "whitelist" in " ".join(d.reasons)


def test_daily_cap():
    d = evaluate(pred(), snap(), trades_today=PARAMS.daily_trade_cap)
    assert d.action == "no_action"
    assert "cap" in " ".join(d.reasons)


def test_cooldown():
    d = evaluate(pred(), snap(), last_trade_ts=NOW - 60 * 30)  # 30 min ago, cooldown 120
    assert d.action == "no_action"
    assert "cooldown" in " ".join(d.reasons)


def test_cooldown_elapsed_allows_trade():
    d = evaluate(pred(), snap(), last_trade_ts=NOW - 60 * 121)
    assert d.action == "buy"


def test_circuit_breaker_trips():
    d = evaluate(pred(), snap(), realized_pnl_today=-600.0)  # 6% of 10k > 5% limit
    assert d.action == "no_action"
    assert d.trip_breaker


def test_fall_without_position_is_no_action():
    d = evaluate(pred(direction=Direction.FALL, confidence=0.9), snap())
    assert d.action == "no_action"
    assert "short" in " ".join(d.reasons)


def test_fall_with_position_sells_fraction():
    positions = {"BTC-USD": (0.04, 50_000.0)}  # $2000 position
    d = evaluate(pred(direction=Direction.FALL, confidence=1.0), snap(positions=positions))
    assert d.action == "sell"
    # moderate sell_fraction 0.5 * scalar 1.0 = half the position
    assert abs(d.base_size - 0.02) < 1e-9


def test_fall_small_position_sells_everything():
    positions = {"BTC-USD": (0.00015, 50_000.0)}  # $7.50 position < 2*min
    d = evaluate(pred(direction=Direction.FALL, confidence=1.0), snap(positions=positions))
    assert d.action == "sell"
    assert abs(d.base_size - 0.00015) < 1e-12


def test_buy_clamped_to_quote_balance():
    # wealth is parked in ETH so the BTC exposure cap leaves plenty of room,
    # but only $100 of quote remains to spend
    d = evaluate(pred(confidence=1.0), snap(quote=100.0, positions={"ETH-USD": (3.8, 2_500.0)}))
    assert d.action == "buy"
    assert d.quote_size == pytest.approx(99.0)  # 1% fee headroom


def test_confidence_scalar_bounds():
    assert risk._confidence_scalar(PARAMS.min_confidence, PARAMS.min_confidence) == 0.5
    assert risk._confidence_scalar(1.0, PARAMS.min_confidence) == 1.0
