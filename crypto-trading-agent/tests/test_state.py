from coinbase_trading_agent.models import Decision, Direction, Fill, Prediction, RiskMode
from coinbase_trading_agent.state import _utc_midnight_ts

NOW = 1_750_000_000.0


def fill(order_id="o1", side="BUY", base=0.01, quote=500.0, price=50_000.0, fee=3.0):
    return Fill(order_id=order_id, product_id="BTC-USD", side=side,
                base_size=base, quote_size=quote, price=price, fee=fee)


def test_meta_roundtrip(store):
    assert store.trading_enabled()
    store.set_trading_enabled(False, "testing")
    assert not store.trading_enabled()
    assert store.disabled_reason() == "testing"
    store.set_trading_enabled(True)
    assert store.trading_enabled() and store.disabled_reason() is None

    assert store.get_risk_mode(RiskMode.CONSERVATIVE) is RiskMode.CONSERVATIVE
    store.set_risk_mode(RiskMode.AGGRESSIVE)
    assert store.get_risk_mode(RiskMode.CONSERVATIVE) is RiskMode.AGGRESSIVE


def test_trade_counters_respect_utc_day_and_cap_flag(store):
    yesterday = _utc_midnight_ts(NOW) - 3600
    store.record_trade(fill("old"), None, None, now=yesterday)
    store.record_trade(fill("today1"), None, None, now=NOW - 60)
    store.record_trade(fill("uncapped"), None, None, counts_toward_cap=False, now=NOW - 30)
    assert store.trades_today(NOW) == 1
    assert store.trades_today(NOW, capped_only=False) == 2


def test_realized_pnl_today_ignores_untracked(store):
    store.record_trade(fill("s1", side="SELL"), None, realized_pnl=-120.0, now=NOW - 60)
    store.record_trade(fill("s2", side="SELL"), None, realized_pnl=None, now=NOW - 30)
    store.record_trade(fill("s3", side="SELL"), None, realized_pnl=20.0, now=NOW - 10)
    assert store.realized_pnl_today(NOW) == -100.0


def test_cost_basis_buy_sell_roundtrip(store):
    store.apply_buy("BTC-USD", 0.02, 1000.0)  # avg cost 50k
    store.apply_buy("BTC-USD", 0.02, 1200.0)  # avg now 55k
    qty, cost = store.get_position("BTC-USD")
    assert abs(qty - 0.04) < 1e-12 and abs(cost - 2200.0) < 1e-9

    realized = store.apply_sell("BTC-USD", 0.02, proceeds=1300.0)  # sold at 65k
    assert abs(realized - (1300.0 - 1100.0)) < 1e-6
    qty, cost = store.get_position("BTC-USD")
    assert abs(qty - 0.02) < 1e-12 and abs(cost - 1100.0) < 1e-6


def test_sell_untracked_returns_none(store):
    assert store.apply_sell("BTC-USD", 0.01, 500.0) is None


def test_sell_more_than_tracked_realizes_only_tracked_part(store):
    store.apply_buy("BTC-USD", 0.01, 500.0)
    realized = store.apply_sell("BTC-USD", 0.02, proceeds=1200.0)
    # tracked half: proceeds/base*tracked - avg*tracked = 600 - 500 = 100
    assert abs(realized - 100.0) < 1e-6
    qty, _ = store.get_position("BTC-USD")
    assert qty == 0.0


def test_adopt_and_reconcile(store):
    store.adopt_position("ETH-USD", 2.0, 2500.0)
    qty, cost = store.get_position("ETH-USD")
    assert qty == 2.0 and cost == 5000.0
    store.reconcile_down("ETH-USD", 1.0)
    qty, cost = store.get_position("ETH-USD")
    assert qty == 1.0 and abs(cost - 2500.0) < 1e-9


def test_proposal_lifecycle(store):
    pred = Prediction("BTC-USD", Direction.RISE, 0.9, 24, "thesis long enough here", created_at=NOW)
    store.record_prediction(pred, Decision("buy", ["r"], quote_size=100))
    p = store.create_proposal(
        prediction_id=pred.id, product_id="BTC-USD", action="buy",
        quote_size=100.0, base_size=0.0, ttl_minutes=15, tech={"score": 0.4}, now=NOW,
    )
    assert p["status"] == "proposed" and p["tech"]["score"] == 0.4
    assert len(store.pending_proposals(NOW)) == 1

    assert store.expire_stale_proposals(NOW + 16 * 60) == 1
    assert store.get_proposal(p["id"])["status"] == "expired"
    assert store.pending_proposals(NOW + 16 * 60) == []


def test_prediction_log_roundtrip(store):
    pred = Prediction("ETH-USD", Direction.FALL, 0.7, 12, "thesis long enough here", created_at=NOW)
    store.record_prediction(pred, Decision("no_action", ["why not"]))
    rows = store.recent_predictions()
    assert rows[0]["product_id"] == "ETH-USD"
    assert rows[0]["decision"]["action"] == "no_action"


def test_apply_sell_residue_closes_position(store):
    store.apply_buy("BTC-USD", 0.01, 500.0, now=NOW)
    # increment rounding sells a hair less than the full position
    store.apply_sell("BTC-USD", 0.01 - 1e-11, proceeds=520.0)
    qty, cost = store.get_position("BTC-USD")
    assert qty == 0.0 and cost == 0.0
    assert store.position_opened_at("BTC-USD") is None


def test_opened_at_set_and_kept(store):
    store.apply_buy("BTC-USD", 0.01, 500.0, now=NOW)
    assert store.position_opened_at("BTC-USD") == NOW
    store.apply_buy("BTC-USD", 0.01, 500.0, now=NOW + 1000)
    assert store.position_opened_at("BTC-USD") == NOW  # adding keeps the clock


def test_close_dust_zeroes_residue(store):
    store.apply_buy("BTC-USD", 0.005, 250.0, now=NOW)
    # quantized exit leaves one increment (1e-8) of residue — larger than
    # apply_sell's sliver threshold for this position size
    store.apply_sell("BTC-USD", 0.005 - 1e-8, proceeds=249.0)
    qty, _ = store.get_position("BTC-USD")
    assert qty > 0  # residue survived apply_sell
    store.close_dust("BTC-USD", price=50_000.0, dust_usd=0.50)
    assert store.get_position("BTC-USD") == (0.0, 0.0)
    assert store.position_opened_at("BTC-USD") is None
    # a real position is never dust-closed
    store.apply_buy("BTC-USD", 0.005, 250.0, now=NOW)
    store.close_dust("BTC-USD", price=50_000.0, dust_usd=0.50)
    assert store.get_position("BTC-USD")[0] == 0.005


def test_breaker_latch_day_boundary(store):
    assert store.breaker_tripped_today(NOW) is False
    store.record_breaker_trip(NOW)
    assert store.breaker_tripped_today(NOW) is True
    assert store.breaker_tripped_today(NOW + 24 * 3600) is False
