"""Robinhood Crypto exchange backend.

Uses the official Robinhood Crypto Trading API (https://docs.robinhood.com),
authenticated with an Ed25519 API keypair. Two properties worth knowing:

* The API surface contains NO withdrawal, transfer, or deposit endpoints —
  trade-only is structural on Robinhood's side, not a key setting.
* Robinhood serves no OHLCV history, so candles for the technical engine come
  from Coinbase's public market data (prices for liquid majors are essentially
  identical across venues).

Unlike Coinbase, Robinhood has no sub-portfolios: the API key sees the whole
crypto account, so the agent's own caps and the portfolio floor are the only
sizing limits. Fees are embedded in the bid/ask spread (no explicit fee).
"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.request
import uuid
from decimal import Decimal

from .exchange import (
    Candle,
    ExchangeError,
    Fill,
    ProductInfo,
    fetch_public_candles,
    quantize_down,
)

BASE_URL = "https://trading.robinhood.com"


class RobinhoodExchange:
    """Official Robinhood Crypto Trading API. Trade-only by API design."""

    def __init__(self, api_key: str, private_key_b64: str, base_url: str = BASE_URL):
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        except ImportError as exc:  # pragma: no cover
            raise ExchangeError("the 'cryptography' package is required for Robinhood signing") from exc
        try:
            seed = base64.b64decode(private_key_b64.strip())
        except Exception as exc:
            raise ExchangeError("ROBINHOOD_PRIVATE_KEY must be the base64 private key seed") from exc
        if len(seed) == 64:  # some generators emit seed||public_key
            seed = seed[:32]
        if len(seed) != 32:
            raise ExchangeError("ROBINHOOD_PRIVATE_KEY must decode to a 32-byte Ed25519 seed")
        self._key = Ed25519PrivateKey.from_private_bytes(seed)
        self._api_key = api_key.strip()
        self._base_url = base_url.rstrip("/")
        self._pair_cache: dict[str, dict] = {}

    # -- transport ---------------------------------------------------------

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        """Signed request. Message = api_key + timestamp + path + method + body."""
        payload = json.dumps(body) if body is not None else ""
        timestamp = str(int(time.time()))
        message = f"{self._api_key}{timestamp}{path}{method}{payload}"
        signature = base64.b64encode(self._key.sign(message.encode("utf-8"))).decode("utf-8")
        req = urllib.request.Request(
            self._base_url + path,
            data=payload.encode("utf-8") if payload else None,
            method=method,
            headers={
                "x-api-key": self._api_key,
                "x-timestamp": timestamp,
                "x-signature": signature,
                "Content-Type": "application/json; charset=utf-8",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:500]
            except Exception:
                pass
            raise ExchangeError(f"Robinhood API {method} {path} failed: HTTP {exc.code} {detail}") from exc
        except Exception as exc:
            raise ExchangeError(f"Robinhood API {method} {path} failed: {exc}") from exc
        try:
            return json.loads(raw) if raw else {}
        except Exception as exc:
            raise ExchangeError(f"Robinhood API returned non-JSON for {path}") from exc

    # -- exchange interface ------------------------------------------------

    def get_key_permissions(self) -> dict:
        # No permissions-introspection endpoint exists; validate the credentials
        # by hitting the account endpoint. The API itself exposes no transfer/
        # withdrawal/deposit operations, so trade-only holds structurally.
        account = self._request("GET", "/api/v1/crypto/trading/accounts/")
        if not account.get("account_number"):
            raise ExchangeError("Robinhood credentials rejected: no account returned")
        return {
            "can_view": True,
            "can_trade": account.get("status") == "active",
            "can_transfer": False,
            "note": "Robinhood Crypto API has no transfer/withdraw/deposit endpoints",
            "account_status": account.get("status"),
        }

    def get_balances(self) -> dict[str, float]:
        balances: dict[str, float] = {}
        account = self._request("GET", "/api/v1/crypto/trading/accounts/")
        currency = (account.get("buying_power_currency") or "USD").upper()
        balances[currency] = float(account.get("buying_power") or 0)

        path = "/api/v1/crypto/trading/holdings/"
        while path:
            page = self._request("GET", path)
            for holding in page.get("results", []):
                code = (holding.get("asset_code") or "").upper()
                qty = float(
                    holding.get("quantity_available_for_trading")
                    or holding.get("total_quantity")
                    or 0
                )
                if code:
                    balances[code] = balances.get(code, 0.0) + qty
            nxt = page.get("next")
            path = nxt.replace(self._base_url, "") if nxt else None
        return balances

    def _pair(self, product_id: str) -> dict:
        if product_id not in self._pair_cache:
            data = self._request(
                "GET", f"/api/v1/crypto/trading/trading_pairs/?symbol={product_id}"
            )
            results = data.get("results") or []
            if not results:
                raise ExchangeError(f"{product_id} is not tradable on Robinhood")
            self._pair_cache[product_id] = results[0]
        return self._pair_cache[product_id]

    def _best_bid_ask(self, product_id: str) -> dict:
        data = self._request(
            "GET", f"/api/v1/crypto/marketdata/best_bid_ask/?symbol={product_id}"
        )
        results = data.get("results") or []
        if not results:
            raise ExchangeError(f"No Robinhood quote for {product_id}")
        return results[0]

    def get_product(self, product_id: str) -> ProductInfo:
        pair = self._pair(product_id)
        quote = self._best_bid_ask(product_id)
        price = quote.get("price")  # midpoint
        if price in (None, ""):
            bid = float(quote.get("bid_inclusive_of_sell_spread") or 0)
            ask = float(quote.get("ask_inclusive_of_buy_spread") or 0)
            if not (bid and ask):
                raise ExchangeError(f"No usable Robinhood price for {product_id}")
            price = (bid + ask) / 2
        return ProductInfo(
            product_id=product_id,
            price=float(price),
            base_increment=str(
                pair.get("asset_increment") or pair.get("quantity_increment") or "0.000001"
            ),
            quote_increment=str(pair.get("quote_increment") or "0.01"),
            price_percentage_change_24h=None,  # not served by this API
            volume_24h=None,
        )

    def get_candles(self, product_id: str, granularity: str, limit: int) -> list[Candle]:
        return fetch_public_candles(product_id, granularity, limit)

    def market_buy(self, product_id: str, quote_size: float) -> Fill:
        pair = self._pair(product_id)
        quote = self._best_bid_ask(product_id)
        ask = float(quote.get("ask_inclusive_of_buy_spread") or quote.get("price") or 0)
        if ask <= 0:
            raise ExchangeError(f"No ask price for {product_id}")
        increment = str(pair.get("asset_increment") or pair.get("quantity_increment") or "0.000001")
        base = quantize_down(quote_size / ask, increment)
        if Decimal(base) <= 0:
            raise ExchangeError(
                f"Buy of {quote_size:.2f} rounds to zero {product_id} at increment {increment}"
            )
        min_size = pair.get("min_order_size")
        if min_size not in (None, "") and Decimal(base) < Decimal(str(min_size)):
            raise ExchangeError(
                f"Buy of {base} {product_id} is below Robinhood's minimum order size {min_size}"
            )
        order = self._place_order(product_id, "buy", base)
        return self._await_fill(order, product_id, "BUY", float(base), ask)

    def market_sell(self, product_id: str, base_size: float) -> Fill:
        pair = self._pair(product_id)
        increment = str(pair.get("asset_increment") or pair.get("quantity_increment") or "0.000001")
        base = quantize_down(base_size, increment)
        if Decimal(base) <= 0:
            raise ExchangeError(f"Sell size {base_size} rounds to zero at increment {increment}")
        quote = self._best_bid_ask(product_id)
        bid = float(quote.get("bid_inclusive_of_sell_spread") or quote.get("price") or 0)
        order = self._place_order(product_id, "sell", base)
        return self._await_fill(order, product_id, "SELL", float(base), bid)

    def _place_order(self, product_id: str, side: str, asset_quantity: str) -> dict:
        return self._request(
            "POST",
            "/api/v1/crypto/trading/orders/",
            {
                "client_order_id": str(uuid.uuid4()),
                "side": side,
                "symbol": product_id,
                "type": "market",
                "market_order_config": {"asset_quantity": asset_quantity},
            },
        )

    def _await_fill(
        self, order: dict, product_id: str, side: str, requested_base: float, ref_price: float
    ) -> Fill:
        order_id = order.get("id") or ""
        if not order_id:
            raise ExchangeError(f"Robinhood order for {product_id} returned no id: {order}")

        filled_base, avg_price, state = 0.0, None, order.get("state")
        for _ in range(10):
            try:
                current = self._request("GET", f"/api/v1/crypto/trading/orders/{order_id}/")
            except ExchangeError:
                break
            state = current.get("state")
            executions = current.get("executions") or []
            if executions:
                filled_base = sum(float(e.get("quantity") or 0) for e in executions)
                notional = sum(
                    float(e.get("quantity") or 0) * float(e.get("effective_price") or 0)
                    for e in executions
                )
                avg_price = notional / filled_base if filled_base else None
            elif current.get("filled_asset_quantity") not in (None, "", "0"):
                filled_base = float(current["filled_asset_quantity"])
                avg_price = float(current.get("average_price") or 0) or None
            if state in ("filled", "canceled", "failed", "rejected"):
                break
            time.sleep(0.5)

        if state in ("canceled", "failed", "rejected") and not filled_base:
            raise ExchangeError(f"Robinhood {side} order {order_id} on {product_id} was {state}")

        if filled_base and avg_price:
            # Fees live inside the spread; there is no separate fee figure.
            return Fill(
                order_id=order_id,
                product_id=product_id,
                side=side,
                base_size=filled_base,
                quote_size=filled_base * avg_price,
                price=avg_price,
                fee=0.0,
            )

        # Order placed but fill unconfirmed: conservative estimate, flagged.
        return Fill(
            order_id=order_id,
            product_id=product_id,
            side=side,
            base_size=requested_base,
            quote_size=requested_base * ref_price,
            price=ref_price,
            fee=0.0,
            estimated=True,
        )
