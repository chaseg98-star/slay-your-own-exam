"""Hard, last-line-of-defense checks.

The agent is trade-only by construction, on three independent layers:

1. The ``Exchange`` interface exposes no withdraw / send / transfer / deposit
   operations, so no code path in this package can move funds in or out of
   the Coinbase account.
2. On startup against the live API, :func:`assert_trade_only_key` verifies the
   API key itself cannot transfer funds and refuses to run otherwise — even a
   bug or prompt-injected instruction cannot do what the key cannot do.
3. Every order passes through :func:`validate_order` immediately before the
   exchange call, independently of whatever the risk engine decided.
"""

from __future__ import annotations

FORBIDDEN_CAPABILITIES = (
    "withdraw funds",
    "send funds to an external wallet",
    "deposit funds",
    "transfer between accounts",
)


class GuardrailViolation(RuntimeError):
    """A hard safety rule would have been broken; the operation was refused."""


def assert_trade_only_key(permissions: dict) -> None:
    """Refuse to operate with an API key that can move funds.

    ``permissions`` is the Coinbase Advanced Trade key-permissions payload
    (``can_view`` / ``can_trade`` / ``can_transfer``).
    """
    if permissions.get("can_transfer"):
        raise GuardrailViolation(
            "The configured Coinbase API key has TRANSFER permission. This agent refuses "
            "to run with a key that can move funds. Delete the key and create a new one "
            "with only 'View' and 'Trade' permissions."
        )
    if not permissions.get("can_trade"):
        raise GuardrailViolation(
            "The configured Coinbase API key lacks TRADE permission, so it cannot place "
            "orders. Create a key with 'View' and 'Trade' permissions (and nothing else)."
        )


def validate_order(
    *,
    side: str,
    product_id: str,
    notional_usd: float,
    whitelist: tuple[str, ...],
    max_trade_usd: float,
    trading_enabled: bool,
) -> None:
    """Final pre-flight check run immediately before any exchange order call."""
    if side not in ("BUY", "SELL"):
        raise GuardrailViolation(f"Refusing order with side {side!r}; only spot BUY/SELL is permitted")
    if product_id not in whitelist:
        raise GuardrailViolation(
            f"Refusing order for {product_id}: not in the configured product whitelist {list(whitelist)}"
        )
    if not trading_enabled:
        raise GuardrailViolation("Refusing order: trading is disabled (kill switch / circuit breaker)")
    if notional_usd <= 0:
        raise GuardrailViolation(f"Refusing order with non-positive notional {notional_usd}")
    # Only buys get a dollar cap. Sells are risk-reducing exits of assets the
    # account already holds (size is clamped to the actual balance upstream);
    # capping them would leave the agent unable to exit a large position.
    if side == "BUY" and notional_usd > max_trade_usd:
        raise GuardrailViolation(
            f"Refusing BUY of ${notional_usd:,.2f} on {product_id}: exceeds the hard cap "
            f"${max_trade_usd:,.2f}"
        )
