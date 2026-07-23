"""Risk engine: turns an analyst prediction into a sized order or a refusal.

Pure functions over snapshots — no I/O — so every rule is unit-testable.
"""

from __future__ import annotations

from .models import Decision, Direction, PortfolioSnapshot, Prediction, RiskParams


def _confidence_scalar(confidence: float, min_confidence: float) -> float:
    """Map [min_confidence, 1.0] onto [0.5, 1.0] of the mode's max size."""
    if min_confidence >= 1.0:
        return 1.0
    span = (confidence - min_confidence) / (1.0 - min_confidence)
    return 0.5 + 0.5 * max(0.0, min(1.0, span))


def evaluate(
    pred: Prediction,
    params: RiskParams,
    snapshot: PortfolioSnapshot,
    *,
    trades_today: int,
    realized_pnl_today: float,
    last_trade_ts: float | None,
    now: float,
    whitelist: tuple[str, ...],
    max_trade_usd: float,
    min_trade_usd: float,
    trading_enabled: bool,
    disabled_reason: str | None = None,
) -> Decision:
    reasons: list[str] = []

    if not trading_enabled:
        return Decision(
            "no_action",
            [f"trading is disabled ({disabled_reason or 'kill switch engaged'}); prediction recorded only"],
        )

    total = snapshot.total_value
    if total <= 0:
        return Decision("no_action", ["portfolio has no value; nothing to trade with"])

    # Daily-loss circuit breaker: realized losses today past the mode's limit
    # halt all prediction-driven trading until a human re-enables it.
    loss_limit = params.daily_loss_limit_pct * total
    if realized_pnl_today <= -loss_limit:
        return Decision(
            "no_action",
            [
                f"daily-loss circuit breaker tripped: realized {realized_pnl_today:+.2f} today "
                f"exceeds the {params.daily_loss_limit_pct:.0%} limit (-{loss_limit:.2f}); "
                "trading disabled until manually re-enabled"
            ],
            trip_breaker=True,
        )

    if pred.product_id not in whitelist:
        return Decision(
            "no_action",
            [f"{pred.product_id} is not in the product whitelist; ask the operator to add it"],
        )

    if pred.confidence < params.min_confidence:
        return Decision(
            "no_action",
            [
                f"confidence {pred.confidence:.2f} is below this mode's minimum "
                f"{params.min_confidence:.2f}; prediction recorded only"
            ],
        )

    if trades_today >= params.daily_trade_cap:
        return Decision(
            "no_action",
            [f"daily trade cap reached ({trades_today}/{params.daily_trade_cap}); prediction recorded only"],
        )

    if last_trade_ts is not None:
        elapsed_min = (now - last_trade_ts) / 60.0
        if elapsed_min < params.cooldown_minutes:
            return Decision(
                "no_action",
                [
                    f"{pred.product_id} traded {elapsed_min:.0f} min ago; cooldown is "
                    f"{params.cooldown_minutes} min in this mode"
                ],
            )

    scalar = _confidence_scalar(pred.confidence, params.min_confidence)

    if pred.direction is Direction.RISE:
        target = total * params.max_trade_pct * scalar
        reasons.append(f"target size {target:.2f} = {params.max_trade_pct:.0%} of {total:.2f} × {scalar:.2f} confidence scalar")

        exposure = snapshot.exposure_value(pred.product_id)
        room = params.max_asset_exposure_pct * total - exposure
        if room <= 0:
            return Decision(
                "no_action",
                reasons
                + [
                    f"{pred.product_id} already at {exposure / total:.0%} of portfolio; "
                    f"mode caps any one asset at {params.max_asset_exposure_pct:.0%}"
                ],
            )
        if target > room:
            reasons.append(f"clamped to {room:.2f} by the {params.max_asset_exposure_pct:.0%} per-asset exposure cap")
            target = room
        if target > max_trade_usd:
            reasons.append(f"clamped to the hard per-trade cap ${max_trade_usd:.2f}")
            target = max_trade_usd
        # Keep ~1% headroom so fees never overdraw the quote balance.
        spendable = snapshot.quote_balance * 0.99
        if target > spendable:
            reasons.append(f"clamped to available {snapshot.quote_currency} balance ({spendable:.2f} after fee headroom)")
            target = spendable
        if target < min_trade_usd:
            return Decision(
                "no_action",
                reasons + [f"final size {target:.2f} is below the {min_trade_usd:.2f} minimum; not worth the fees"],
            )
        return Decision("buy", reasons + [f"BUY {pred.product_id} for {target:.2f} {snapshot.quote_currency}"], quote_size=target)

    # FALL → reduce or exit the position. Spot account: no shorting.
    qty, price = snapshot.positions.get(pred.product_id, (0.0, 0.0))
    if qty <= 0 or price <= 0:
        return Decision(
            "no_action",
            ["no position to reduce; spot accounts cannot short — prediction recorded only"],
        )
    fraction = min(1.0, params.sell_fraction * scalar)
    base = qty * fraction
    notional = base * price
    reasons.append(f"sell fraction {fraction:.2f} = {params.sell_fraction:.2f} × {scalar:.2f} confidence scalar")
    if (qty * price) < 2 * min_trade_usd or (qty - base) * price < min_trade_usd:
        reasons.append("remaining position would be dust; selling the full position instead")
        base, notional = qty, qty * price
    elif notional < min_trade_usd:
        # Fee-wasteful micro-sell: bump to the minimum notional instead.
        base = min(qty, min_trade_usd / price)
        notional = base * price
        reasons.append(f"sell bumped to the {min_trade_usd:.2f} minimum notional")
    return Decision(
        "sell",
        reasons + [f"SELL {base:.8f} {pred.product_id.split('-')[0]} (~{notional:.2f})"],
        base_size=base,
    )
