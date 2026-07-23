import pytest

from coinbase_trading_agent import guardrails
from coinbase_trading_agent.guardrails import GuardrailViolation


def test_transfer_capable_key_refused():
    with pytest.raises(GuardrailViolation, match="TRANSFER"):
        guardrails.assert_trade_only_key({"can_view": True, "can_trade": True, "can_transfer": True})


def test_trade_only_key_accepted():
    guardrails.assert_trade_only_key({"can_view": True, "can_trade": True, "can_transfer": False})


def test_view_only_key_refused():
    with pytest.raises(GuardrailViolation, match="TRADE"):
        guardrails.assert_trade_only_key({"can_view": True, "can_trade": False, "can_transfer": False})


def _order(**overrides):
    kwargs = dict(
        side="BUY",
        product_id="BTC-USD",
        notional_usd=100.0,
        whitelist=("BTC-USD",),
        max_trade_usd=250.0,
        trading_enabled=True,
    )
    kwargs.update(overrides)
    return kwargs


def test_valid_order_passes():
    guardrails.validate_order(**_order())


def test_non_spot_side_refused():
    for side in ("SHORT", "WITHDRAW", "TRANSFER", "buy"):
        with pytest.raises(GuardrailViolation):
            guardrails.validate_order(**_order(side=side))


def test_non_whitelisted_product_refused():
    with pytest.raises(GuardrailViolation, match="whitelist"):
        guardrails.validate_order(**_order(product_id="PEPE-USD"))


def test_disabled_trading_refused():
    with pytest.raises(GuardrailViolation, match="disabled"):
        guardrails.validate_order(**_order(trading_enabled=False))


def test_buy_over_cap_refused():
    with pytest.raises(GuardrailViolation, match="hard cap"):
        guardrails.validate_order(**_order(notional_usd=251.0))


def test_sell_allowed_above_buy_cap():
    guardrails.validate_order(**_order(side="SELL", notional_usd=5000.0))


def test_nonpositive_notional_refused():
    with pytest.raises(GuardrailViolation):
        guardrails.validate_order(**_order(notional_usd=0.0))


def test_exchange_interface_has_no_fund_movement_methods():
    """The entire withdraw/send/deposit capability class must be absent."""
    from coinbase_trading_agent.exchange import CoinbaseExchange, PaperExchange

    for cls in (CoinbaseExchange, PaperExchange):
        names = " ".join(dir(cls)).lower()
        for forbidden in ("withdraw", "deposit", "transfer", "send_to", "wallet"):
            assert forbidden not in names, f"{cls.__name__} exposes forbidden capability {forbidden!r}"


def test_sell_has_no_dollar_cap():
    """Sells are exits; capping them would trap large positions."""
    guardrails.validate_order(**_order(side="SELL", notional_usd=1_000_000.0))
