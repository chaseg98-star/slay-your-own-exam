"""Exchange access.

The ``Exchange`` protocol is deliberately narrow: balances, market data, and
spot BUY/SELL market orders. It has **no** withdraw / send / transfer /
deposit operations — that entire capability class is absent from the agent by
construction (see guardrails.py for the other two defense layers).

Two implementations:

* :class:`CoinbaseExchange` — Coinbase Advanced Trade, via the official
  ``coinbase-advanced-py`` SDK (imported lazily so paper mode and tests work
  without it).
* :class:`PaperExchange` — simulated fills against real market prices (public
  Coinbase market-data endpoint, no API key needed), used for dry runs.
"""

from __future__ import annotations

import json
import time
import urllib.request
import uuid
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal
from typing import Callable, Protocol

from .models import Fill


class ExchangeError(RuntimeError):
    pass


@dataclass
class ProductInfo:
    product_id: str
    price: float
    base_increment: str
    quote_increment: str
    price_percentage_change_24h: float | None = None
    volume_24h: float | None = None


@dataclass
class Candle:
    start: int  # unix seconds
    open: float
    high: float
    low: float
    close: float
    volume: float


GRANULARITIES = (
    "ONE_MINUTE",
    "FIVE_MINUTE",
    "FIFTEEN_MINUTE",
    "THIRTY_MINUTE",
    "ONE_HOUR",
    "TWO_HOUR",
    "SIX_HOUR",
    "ONE_DAY",
)

_GRANULARITY_SECONDS = {
    "ONE_MINUTE": 60,
    "FIVE_MINUTE": 300,
    "FIFTEEN_MINUTE": 900,
    "THIRTY_MINUTE": 1800,
    "ONE_HOUR": 3600,
    "TWO_HOUR": 7200,
    "SIX_HOUR": 21600,
    "ONE_DAY": 86400,
}


class Exchange(Protocol):
    """Trade-only surface. Intentionally has no fund-movement operations."""

    def get_key_permissions(self) -> dict: ...

    def get_balances(self) -> dict[str, float]: ...

    def get_product(self, product_id: str) -> ProductInfo: ...

    def get_candles(self, product_id: str, granularity: str, limit: int) -> list[Candle]: ...

    def market_buy(self, product_id: str, quote_size: float) -> Fill: ...

    def market_sell(self, product_id: str, base_size: float) -> Fill: ...


def quantize_down(value: float, increment: str) -> str:
    """Round ``value`` down to a multiple of ``increment``, as a string."""
    inc = Decimal(increment)
    quantized = (Decimal(str(value)) / inc).to_integral_value(rounding=ROUND_DOWN) * inc
    return format(quantized.normalize() if quantized == quantized.to_integral_value() else quantized, "f")


def _field(obj, name: str, default=None):
    """Read ``name`` from an SDK response object or a plain dict."""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _wrap_sdk_errors(fn):
    """SDK/HTTP failures become ExchangeError so callers have one error type."""

    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ExchangeError:
            raise
        except Exception as exc:
            raise ExchangeError(f"Coinbase API call failed: {exc}") from exc

    return wrapper


class CoinbaseExchange:
    """Coinbase Advanced Trade via the official SDK (auth'd with a CDP API key)."""

    def __init__(self, api_key_name: str, api_private_key: str):
        try:
            from coinbase.rest import RESTClient
        except ImportError as exc:  # pragma: no cover
            raise ExchangeError(
                "coinbase-advanced-py is not installed; run `pip install coinbase-advanced-py`"
            ) from exc
        # \\n-escaped PEM from .env files needs unescaping before signing works.
        self._client = RESTClient(api_key=api_key_name, api_secret=api_private_key.replace("\\n", "\n"))

    @_wrap_sdk_errors
    def get_key_permissions(self) -> dict:
        perms = self._client.get_api_key_permissions()
        return {
            "can_view": bool(_field(perms, "can_view")),
            "can_trade": bool(_field(perms, "can_trade")),
            "can_transfer": bool(_field(perms, "can_transfer")),
            "portfolio_type": _field(perms, "portfolio_type"),
        }

    @_wrap_sdk_errors
    def get_balances(self) -> dict[str, float]:
        balances: dict[str, float] = {}
        cursor = None
        while True:
            resp = self._client.get_accounts(limit=250, cursor=cursor)
            for account in _field(resp, "accounts", []) or []:
                available = _field(account, "available_balance", {}) or {}
                currency = _field(account, "currency") or _field(available, "currency")
                value = float(_field(available, "value", 0) or 0)
                if currency:
                    balances[currency] = balances.get(currency, 0.0) + value
            if not _field(resp, "has_next"):
                break
            cursor = _field(resp, "cursor")
        return balances

    @_wrap_sdk_errors
    def get_product(self, product_id: str) -> ProductInfo:
        product = self._client.get_product(product_id=product_id)
        price = _field(product, "price")
        if price in (None, ""):
            raise ExchangeError(f"No price available for {product_id}")
        change = _field(product, "price_percentage_change_24h")
        volume = _field(product, "volume_24h")
        return ProductInfo(
            product_id=product_id,
            price=float(price),
            base_increment=str(_field(product, "base_increment", "0.00000001")),
            quote_increment=str(_field(product, "quote_increment", "0.01")),
            price_percentage_change_24h=float(change) if change not in (None, "") else None,
            volume_24h=float(volume) if volume not in (None, "") else None,
        )

    @_wrap_sdk_errors
    def get_candles(self, product_id: str, granularity: str, limit: int) -> list[Candle]:
        if granularity not in GRANULARITIES:
            raise ExchangeError(f"granularity must be one of {GRANULARITIES}")
        limit = max(1, min(int(limit), 300))
        end = int(time.time())
        start = end - _GRANULARITY_SECONDS[granularity] * limit
        resp = self._client.get_candles(
            product_id=product_id, start=str(start), end=str(end), granularity=granularity
        )
        candles = []
        for c in _field(resp, "candles", []) or []:
            candles.append(
                Candle(
                    start=int(_field(c, "start", 0)),
                    open=float(_field(c, "open", 0)),
                    high=float(_field(c, "high", 0)),
                    low=float(_field(c, "low", 0)),
                    close=float(_field(c, "close", 0)),
                    volume=float(_field(c, "volume", 0)),
                )
            )
        candles.sort(key=lambda c: c.start)
        return candles

    @_wrap_sdk_errors
    def market_buy(self, product_id: str, quote_size: float) -> Fill:
        product = self.get_product(product_id)
        size = quantize_down(quote_size, product.quote_increment)
        resp = self._client.market_order_buy(
            client_order_id=uuid.uuid4().hex, product_id=product_id, quote_size=size
        )
        return self._resolve_fill(resp, product, side="BUY", requested=float(size))

    @_wrap_sdk_errors
    def market_sell(self, product_id: str, base_size: float) -> Fill:
        product = self.get_product(product_id)
        size = quantize_down(base_size, product.base_increment)
        if Decimal(size) <= 0:
            raise ExchangeError(f"Sell size {base_size} rounds to zero at increment {product.base_increment}")
        resp = self._client.market_order_sell(
            client_order_id=uuid.uuid4().hex, product_id=product_id, base_size=size
        )
        return self._resolve_fill(resp, product, side="SELL", requested=float(size))

    def _resolve_fill(self, resp, product: ProductInfo, side: str, requested: float) -> Fill:
        if not _field(resp, "success", True):
            error = _field(resp, "error_response", {}) or {}
            raise ExchangeError(
                f"Order rejected by Coinbase: {_field(error, 'error', 'unknown')} — "
                f"{_field(error, 'message', '') or _field(error, 'error_details', '')}"
            )
        success = _field(resp, "success_response", {}) or {}
        order_id = _field(success, "order_id") or _field(resp, "order_id") or ""

        # Market orders normally fill immediately; poll briefly for the real fill.
        avg_price, filled_base, filled_quote, fee = None, None, None, 0.0
        for _ in range(10):
            try:
                order_resp = self._client.get_order(order_id=order_id)
            except Exception:
                break
            order = _field(order_resp, "order", order_resp)
            status = _field(order, "status")
            raw_price = _field(order, "average_filled_price")
            raw_base = _field(order, "filled_size")
            raw_quote = _field(order, "filled_value")
            raw_fee = _field(order, "total_fees")
            if raw_price not in (None, "", "0") and raw_base not in (None, "", "0"):
                avg_price = float(raw_price)
                filled_base = float(raw_base)
                filled_quote = float(raw_quote) if raw_quote not in (None, "") else avg_price * filled_base
                fee = float(raw_fee) if raw_fee not in (None, "") else 0.0
            if status in ("FILLED", "CANCELLED", "EXPIRED", "FAILED"):
                break
            time.sleep(0.5)

        if filled_base:
            return Fill(
                order_id=order_id,
                product_id=product.product_id,
                side=side,
                base_size=filled_base,
                # Buys: cost including fee. Sells: net proceeds after fee.
                quote_size=(filled_quote or 0.0) + fee if side == "BUY" else (filled_quote or 0.0) - fee,
                price=avg_price or product.price,
                fee=fee,
            )

        # The order WAS placed but the fill could not be confirmed. Raising here
        # would lose the accounting entirely (and invite a duplicate retry), so
        # return a conservative estimate flagged for verification instead.
        est_fee_rate = 0.006
        if side == "BUY":
            est_base = requested * (1 - est_fee_rate) / product.price
            est_quote, est_fee = requested, requested * est_fee_rate
        else:
            est_base = requested
            gross = requested * product.price
            est_fee = gross * est_fee_rate
            est_quote = gross - est_fee
        return Fill(
            order_id=order_id,
            product_id=product.product_id,
            side=side,
            base_size=est_base,
            quote_size=est_quote,
            price=product.price,
            fee=est_fee,
            estimated=True,
        )


PUBLIC_MARKET_BASE = "https://api.coinbase.com/api/v3/brokerage/market"

# Offline fallback prices so paper mode still works without network access.
FALLBACK_PRICES = {
    "BTC-USD": 65000.0, "ETH-USD": 3400.0, "SOL-USD": 150.0, "XRP-USD": 0.6,
    "ADA-USD": 0.45, "DOGE-USD": 0.15, "AVAX-USD": 30.0, "LINK-USD": 15.0,
    "LTC-USD": 80.0, "DOT-USD": 6.0,
}


def fetch_public_candles(product_id: str, granularity: str, limit: int) -> list[Candle]:
    """OHLCV candles from Coinbase's public (unauthenticated) market data.

    Also used by exchanges that don't serve candle history (e.g. Robinhood):
    prices for liquid majors are essentially identical across venues, so the
    technical engine reads Coinbase public data regardless of where orders go.
    Returns an empty list when offline.
    """
    if granularity not in GRANULARITIES:
        raise ExchangeError(f"granularity must be one of {GRANULARITIES}")
    limit = max(1, min(int(limit), 300))
    end = int(time.time())
    start = end - _GRANULARITY_SECONDS[granularity] * limit
    url = (
        f"{PUBLIC_MARKET_BASE}/products/{product_id}/candles"
        f"?start={start}&end={end}&granularity={granularity}"
    )
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read())
    except Exception:
        return []
    candles = [
        Candle(
            start=int(c.get("start", 0)),
            open=float(c.get("open", 0)),
            high=float(c.get("high", 0)),
            low=float(c.get("low", 0)),
            close=float(c.get("close", 0)),
            volume=float(c.get("volume", 0)),
        )
        for c in data.get("candles", [])
    ]
    candles.sort(key=lambda c: c.start)
    return candles


def public_price_source(product_id: str) -> float:
    """Spot price from Coinbase's public (unauthenticated) market-data endpoint."""
    url = f"{PUBLIC_MARKET_BASE}/products/{product_id}"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read())
        price = data.get("price")
        if price in (None, ""):
            raise ExchangeError(f"No public price for {product_id}")
        return float(price)
    except ExchangeError:
        raise
    except Exception:
        fallback = FALLBACK_PRICES.get(product_id) or FALLBACK_PRICES.get(
            product_id.replace("-USDC", "-USD")
        )
        if fallback is None:
            raise ExchangeError(f"Cannot price {product_id}: public endpoint unreachable and no fallback")
        return fallback


class PaperExchange:
    """Simulated exchange: real prices where reachable, instant fills, taker fee."""

    def __init__(
        self,
        quote_currency: str = "USD",
        starting_quote: float = 10_000.0,
        fee_rate: float = 0.006,
        price_source: Callable[[str], float] | None = None,
    ):
        self.quote_currency = quote_currency
        self.fee_rate = fee_rate
        self.balances: dict[str, float] = {quote_currency: starting_quote}
        self._price_source = price_source or public_price_source

    def get_key_permissions(self) -> dict:
        return {"can_view": True, "can_trade": True, "can_transfer": False, "paper": True}

    def get_balances(self) -> dict[str, float]:
        return {k: v for k, v in self.balances.items() if v > 0}

    def get_product(self, product_id: str) -> ProductInfo:
        return ProductInfo(
            product_id=product_id,
            price=self._price_source(product_id),
            base_increment="0.00000001",
            quote_increment="0.01",
        )

    def get_candles(self, product_id: str, granularity: str, limit: int) -> list[Candle]:
        """Real candles from the public endpoint; empty list when offline."""
        return fetch_public_candles(product_id, granularity, limit)

    def market_buy(self, product_id: str, quote_size: float) -> Fill:
        quote_size = float(quantize_down(quote_size, "0.01"))
        available = self.balances.get(self.quote_currency, 0.0)
        if quote_size > available + 1e-9:
            raise ExchangeError(
                f"Insufficient {self.quote_currency}: need {quote_size:.2f}, have {available:.2f}"
            )
        price = self._price_source(product_id)
        fee = quote_size * self.fee_rate
        base = (quote_size - fee) / price
        base_currency = product_id.split("-")[0]
        self.balances[self.quote_currency] = available - quote_size
        self.balances[base_currency] = self.balances.get(base_currency, 0.0) + base
        return Fill(
            order_id=f"paper-{uuid.uuid4().hex[:12]}",
            product_id=product_id,
            side="BUY",
            base_size=base,
            quote_size=quote_size,
            price=price,
            fee=fee,
        )

    def market_sell(self, product_id: str, base_size: float) -> Fill:
        base_size = float(quantize_down(base_size, "0.00000001"))
        base_currency = product_id.split("-")[0]
        held = self.balances.get(base_currency, 0.0)
        if base_size > held + 1e-12:
            raise ExchangeError(f"Insufficient {base_currency}: need {base_size}, have {held}")
        if base_size <= 0:
            raise ExchangeError("Sell size rounds to zero")
        price = self._price_source(product_id)
        proceeds = base_size * price
        fee = proceeds * self.fee_rate
        self.balances[base_currency] = held - base_size
        self.balances[self.quote_currency] = self.balances.get(self.quote_currency, 0.0) + proceeds - fee
        return Fill(
            order_id=f"paper-{uuid.uuid4().hex[:12]}",
            product_id=product_id,
            side="SELL",
            base_size=base_size,
            quote_size=proceeds - fee,
            price=price,
            fee=fee,
        )
