import base64

import pytest

from coinbase_trading_agent.exchange import ExchangeError
from coinbase_trading_agent.robinhood import RobinhoodExchange

# Deterministic throwaway Ed25519 seed for tests (not a real credential).
TEST_SEED_B64 = base64.b64encode(bytes(range(32))).decode()


def make_exchange(responses):
    """RobinhoodExchange with a scripted transport. `responses` maps
    (method, path-prefix) -> payload or list of payloads (consumed in order)."""
    ex = RobinhoodExchange(api_key="test-key", private_key_b64=TEST_SEED_B64)
    calls = []

    def fake_request(method, path, body=None):
        calls.append((method, path, body))
        for (m, prefix), payload in responses.items():
            if m == method and path.startswith(prefix):
                if isinstance(payload, list):
                    return payload.pop(0) if len(payload) > 1 else payload[0]
                return payload
        raise ExchangeError(f"unexpected {method} {path}")

    ex._request = fake_request
    ex.calls = calls
    return ex


PAIR = {
    (
        "GET",
        "/api/v1/crypto/trading/trading_pairs/",
    ): {"results": [{"symbol": "BTC-USD", "asset_increment": "0.000001",
                     "quote_increment": "0.01", "min_order_size": "0.000001",
                     "status": "tradable"}]}
}
QUOTE = {
    (
        "GET",
        "/api/v1/crypto/marketdata/best_bid_ask/",
    ): {"results": [{"symbol": "BTC-USD", "price": "50000",
                     "bid_inclusive_of_sell_spread": "49850",
                     "ask_inclusive_of_buy_spread": "50150"}]}
}


def test_key_validation_and_trade_only():
    ex = make_exchange({
        ("GET", "/api/v1/crypto/trading/accounts/"): {
            "account_number": "A1", "status": "active",
            "buying_power": "49.00", "buying_power_currency": "USD",
        },
    })
    perms = ex.get_key_permissions()
    assert perms["can_transfer"] is False and perms["can_trade"] is True


def test_bad_seed_rejected():
    with pytest.raises(ExchangeError, match="32-byte"):
        RobinhoodExchange(api_key="k", private_key_b64=base64.b64encode(b"short").decode())


def test_signature_verifies():
    """Headers must carry a valid Ed25519 signature over key+ts+path+method+body."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    ex = RobinhoodExchange(api_key="test-key", private_key_b64=TEST_SEED_B64)
    seed = base64.b64decode(TEST_SEED_B64)
    public = Ed25519PrivateKey.from_private_bytes(seed).public_key()

    captured = {}

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"{}"

    def fake_urlopen(req, timeout=0):
        captured["headers"] = {k.lower(): v for k, v in req.header_items()}
        captured["method"] = req.get_method()
        captured["path"] = req.selector
        return FakeResp()

    import coinbase_trading_agent.robinhood as rh

    original = rh.urllib.request.urlopen
    rh.urllib.request.urlopen = fake_urlopen
    try:
        ex._request("GET", "/api/v1/crypto/trading/accounts/")
    finally:
        rh.urllib.request.urlopen = original

    h = captured["headers"]
    message = f"test-key{h['x-timestamp']}/api/v1/crypto/trading/accounts/GET"
    public.verify(base64.b64decode(h["x-signature"]), message.encode())  # raises if invalid


def test_balances_merge_buying_power_and_holdings():
    ex = make_exchange({
        ("GET", "/api/v1/crypto/trading/accounts/"): {
            "account_number": "A1", "status": "active",
            "buying_power": "31.50", "buying_power_currency": "USD",
        },
        ("GET", "/api/v1/crypto/trading/holdings/"): {
            "results": [
                {"asset_code": "BTC", "quantity_available_for_trading": "0.0002"},
                {"asset_code": "DOGE", "quantity_available_for_trading": "50"},
            ],
            "next": None,
        },
    })
    balances = ex.get_balances()
    assert balances == {"USD": 31.5, "BTC": 0.0002, "DOGE": 50.0}


def test_buy_converts_quote_to_quantized_base():
    order_flow = {
        ("POST", "/api/v1/crypto/trading/orders/"): {"id": "ord-1", "state": "open"},
        ("GET", "/api/v1/crypto/trading/orders/ord-1/"): {
            "id": "ord-1", "state": "filled",
            "executions": [{"effective_price": "50150", "quantity": "0.000199"}],
        },
    }
    ex = make_exchange({**PAIR, **QUOTE, **order_flow})
    fill = ex.market_buy("BTC-USD", 10.0)
    # 10 / 50150 = 0.00019940... -> quantized down to 0.000199
    post = next(c for c in ex.calls if c[0] == "POST")
    assert post[2]["market_order_config"]["asset_quantity"] == "0.000199"
    assert post[2]["side"] == "buy" and post[2]["type"] == "market"
    assert fill.side == "BUY" and fill.estimated is False
    assert fill.price == pytest.approx(50150)
    assert fill.quote_size == pytest.approx(0.000199 * 50150)


def test_sell_uses_executions_weighted_average():
    order_flow = {
        ("POST", "/api/v1/crypto/trading/orders/"): {"id": "ord-2", "state": "open"},
        ("GET", "/api/v1/crypto/trading/orders/ord-2/"): {
            "id": "ord-2", "state": "filled",
            "executions": [
                {"effective_price": "49800", "quantity": "0.0001"},
                {"effective_price": "49900", "quantity": "0.0001"},
            ],
        },
    }
    ex = make_exchange({**PAIR, **QUOTE, **order_flow})
    fill = ex.market_sell("BTC-USD", 0.0002)
    assert fill.side == "SELL"
    assert fill.price == pytest.approx(49850)
    assert fill.base_size == pytest.approx(0.0002)


def test_rejected_order_raises():
    order_flow = {
        ("POST", "/api/v1/crypto/trading/orders/"): {"id": "ord-3", "state": "open"},
        ("GET", "/api/v1/crypto/trading/orders/ord-3/"): {
            "id": "ord-3", "state": "rejected", "executions": [],
        },
    }
    ex = make_exchange({**PAIR, **QUOTE, **order_flow})
    with pytest.raises(ExchangeError, match="rejected"):
        ex.market_sell("BTC-USD", 0.0002)


def test_unconfirmed_fill_returns_estimate():
    order_flow = {
        ("POST", "/api/v1/crypto/trading/orders/"): {"id": "ord-4", "state": "open"},
        ("GET", "/api/v1/crypto/trading/orders/ord-4/"): ExchangeError,
    }

    ex = make_exchange({**PAIR, **QUOTE})
    original_request = ex._request

    def request_with_failing_poll(method, path, body=None):
        if method == "POST":
            return {"id": "ord-4", "state": "open"}
        if "orders/ord-4" in path:
            raise ExchangeError("poll failed")
        return original_request(method, path, body)

    ex._request = request_with_failing_poll
    fill = ex.market_buy("BTC-USD", 10.0)
    assert fill.estimated is True
    assert fill.order_id == "ord-4"


def test_no_fund_movement_surface():
    names = " ".join(dir(RobinhoodExchange)).lower()
    for forbidden in ("withdraw", "deposit", "transfer", "send_to", "wallet"):
        assert forbidden not in names
