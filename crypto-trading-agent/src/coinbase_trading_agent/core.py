"""AgentCore: the trading agent's brain, independent of the MCP transport.

Every MCP tool maps to one method here. Flow for a prediction:

    analyst  ──submit_prediction──▶  risk engine + technical cross-check
                                      │
                     no_action ◀──────┤ (recorded, explained)
                                      ▼
                              proposal (two-phase)
                                      │
    analyst double-checks ──confirm_decision / reject_decision──▶ execute / drop

Guardrails run immediately before every exchange order, independent of what
the risk engine decided. There is no code path that can move funds off the
account (see guardrails.py).
"""

from __future__ import annotations

import re
import time

from . import __version__, guardrails, risk, strategy
from .config import Config
from .exchange import Exchange, ExchangeError
from .models import (
    MAX_HOLD_DAYS,
    RISK_PROFILES,
    Decision,
    Direction,
    Fill,
    PortfolioSnapshot,
    Prediction,
    RiskMode,
)
from .state import Store

_PRODUCT_RE = re.compile(r"^[A-Z0-9]{2,12}-[A-Z]{3,5}$")

DUST_USD = 0.50  # balances below this are ignored as position dust


class AgentCore:
    def __init__(self, config: Config, store: Store, exchange: Exchange, now=time.time):
        self.config = config
        self.store = store
        self.exchange = exchange
        self._now = now

    # -- startup ----------------------------------------------------------

    def startup_checks(self) -> dict:
        perms = self.exchange.get_key_permissions()
        guardrails.assert_trade_only_key(perms)
        return {"key_permissions": perms, "trading_mode": self.config.trading_mode}

    # -- helpers ----------------------------------------------------------

    def _validate_product(self, product_id: str) -> str:
        product_id = (product_id or "").strip().upper()
        if not _PRODUCT_RE.match(product_id):
            raise ValueError(
                f"invalid product id {product_id!r}; expected e.g. 'BTC-{self.config.quote_currency}'"
            )
        return product_id

    def _params(self):
        return RISK_PROFILES[self.store.get_risk_mode(self.config.default_risk_mode)]

    def _snapshot(self) -> PortfolioSnapshot:
        balances = self.exchange.get_balances()
        positions: dict[str, tuple[float, float]] = {}
        for product_id in self.config.product_whitelist:
            base_currency = product_id.split("-")[0]
            qty = balances.get(base_currency, 0.0)
            if qty <= 0:
                self.store.reconcile_down(product_id, 0.0)
                continue
            price = self.exchange.get_product(product_id).price
            if qty * price < DUST_USD:
                # Not a position, but keep the tracked basis honest so stale
                # cost can't poison the next buy's P&L.
                self.store.reconcile_down(product_id, qty)
                continue
            positions[product_id] = (qty, price)
            tracked_qty, _ = self.store.get_position(product_id)
            if qty > tracked_qty * (1 + 1e-9) + 1e-12:
                # Coins bought outside the agent: adopt at today's price.
                self.store.adopt_position(product_id, qty - tracked_qty, price, now=self._now())
            elif qty < tracked_qty * (1 - 1e-9):
                self.store.reconcile_down(product_id, qty)
        return PortfolioSnapshot(
            quote_currency=self.config.quote_currency,
            quote_balance=balances.get(self.config.quote_currency, 0.0),
            positions=positions,
        )

    def _tech_view(self, product_id: str) -> strategy.TechView:
        try:
            candles = self.exchange.get_candles(product_id, "ONE_HOUR", 200)
        except ExchangeError:
            candles = []
        return strategy.technical_view(candles)

    # -- reads ------------------------------------------------------------

    def get_status(self) -> dict:
        now = self._now()
        self.store.expire_stale_proposals(now)
        mode = self.store.get_risk_mode(self.config.default_risk_mode)
        params = RISK_PROFILES[mode]
        return {
            "version": __version__,
            "trading_mode": self.config.trading_mode,
            "risk_mode": mode.value,
            "risk_params": params.__dict__,
            "trading_enabled": self.store.trading_enabled(),
            "disabled_reason": self.store.disabled_reason(),
            "require_confirmation": self.config.require_confirmation,
            "trades_today": self.store.trades_today(now),
            "daily_trade_cap": params.daily_trade_cap,
            "realized_pnl_today": round(self.store.realized_pnl_today(now), 2),
            "pending_proposals": self.store.pending_proposals(now),
            "product_whitelist": list(self.config.product_whitelist),
            "max_trade_usd": self.config.max_trade_usd,
            "capabilities_excluded": list(guardrails.FORBIDDEN_CAPABILITIES),
        }

    def get_portfolio(self) -> dict:
        snapshot = self._snapshot()
        positions = []
        for product_id, (qty, price) in sorted(snapshot.positions.items()):
            tracked_qty, cost = self.store.get_position(product_id)
            avg_cost = cost / tracked_qty if tracked_qty > 0 else None
            positions.append(
                {
                    "product_id": product_id,
                    "base_qty": qty,
                    "price": price,
                    "value": round(qty * price, 2),
                    "avg_cost": round(avg_cost, 8) if avg_cost else None,
                    "unrealized_pnl": round((price - avg_cost) * qty, 2) if avg_cost else None,
                }
            )
        return {
            "quote_currency": snapshot.quote_currency,
            "quote_balance": round(snapshot.quote_balance, 2),
            "positions": positions,
            "total_value": round(snapshot.total_value, 2),
        }

    def get_market_data(self, product_ids: list[str]) -> dict:
        if not product_ids:
            product_ids = list(self.config.product_whitelist)
        results, errors = [], []
        for raw in product_ids[:15]:
            try:
                product_id = self._validate_product(raw)
                info = self.exchange.get_product(product_id)
                results.append(
                    {
                        "product_id": product_id,
                        "price": info.price,
                        "change_24h_pct": info.price_percentage_change_24h,
                        "volume_24h": info.volume_24h,
                        "tradable": product_id in self.config.product_whitelist,
                    }
                )
            except (ValueError, ExchangeError) as exc:
                errors.append({"product_id": raw, "error": str(exc)})
        return {"markets": results, "errors": errors}

    def get_technical_analysis(self, product_id: str, granularity: str = "ONE_HOUR", limit: int = 200) -> dict:
        product_id = self._validate_product(product_id)
        candles = self.exchange.get_candles(product_id, granularity, limit)
        view = strategy.technical_view(candles)
        recent = [
            {"start": c.start, "open": c.open, "high": c.high, "low": c.low, "close": c.close, "volume": c.volume}
            for c in candles[-24:]
        ]
        return {"product_id": product_id, "technical_view": view.as_dict(), "recent_candles": recent}

    def get_trade_log(self, limit: int = 20) -> list[dict]:
        return self.store.recent_trades(limit)

    def get_predictions(self, limit: int = 20) -> list[dict]:
        return self.store.recent_predictions(limit)

    # -- controls ---------------------------------------------------------

    _MODE_RISK_ORDER = [RiskMode.CONSERVATIVE, RiskMode.MODERATE, RiskMode.AGGRESSIVE]

    def set_risk_mode(self, mode: str) -> dict:
        if self.config.lock_risk_controls:
            raise guardrails.GuardrailViolation(
                "risk controls are locked (LOCK_RISK_CONTROLS=1); only the operator can "
                "change the risk mode via the environment"
            )
        try:
            risk_mode = RiskMode((mode or "").strip().lower())
        except ValueError:
            raise ValueError(f"mode must be one of {[m.value for m in RiskMode]}, got {mode!r}") from None
        # While the breaker latch is active, limits may be tightened but never
        # loosened — otherwise a mode switch would defeat the daily-loss halt.
        current = self.store.get_risk_mode(self.config.default_risk_mode)
        if (
            self.store.breaker_tripped_today(self._now())
            and self._MODE_RISK_ORDER.index(risk_mode) > self._MODE_RISK_ORDER.index(current)
        ):
            raise guardrails.GuardrailViolation(
                "the daily-loss circuit breaker tripped today; switching to a riskier mode "
                "is blocked until the next UTC day (tightening is allowed)"
            )
        self.store.set_risk_mode(risk_mode)
        return {"risk_mode": risk_mode.value, "risk_params": RISK_PROFILES[risk_mode].__dict__}

    def set_trading_enabled(self, enabled: bool, reason: str = "") -> dict:
        reason = (reason or "").strip()
        if enabled:
            # Disabling is always allowed; re-enabling is gated three ways:
            # optional operator lock, the breaker latch (the loss limit is
            # per-day — same-day re-enable would defeat it), and a written
            # diagnosis once the latch has expired.
            if self.config.lock_risk_controls:
                raise guardrails.GuardrailViolation(
                    "risk controls are locked (LOCK_RISK_CONTROLS=1); only the operator can "
                    "re-enable trading"
                )
            if self.store.breaker_tripped_today(self._now()):
                raise guardrails.GuardrailViolation(
                    "breaker latch: the daily-loss circuit breaker tripped today and trading "
                    "stays halted until the next UTC day. Review the journal now; re-enable "
                    "with your written findings after UTC midnight."
                )
            current_reason = self.store.disabled_reason() or ""
            if "circuit breaker" in current_reason and len(reason) < 20:
                raise ValueError(
                    "re-enabling after a circuit-breaker trip requires a reason explaining "
                    "what was reviewed (>= 20 characters)"
                )
        self.store.set_trading_enabled(enabled, reason or ("manually disabled" if not enabled else ""))
        return {
            "trading_enabled": self.store.trading_enabled(),
            "disabled_reason": self.store.disabled_reason(),
        }

    # -- trading ----------------------------------------------------------

    def submit_prediction(
        self,
        product_id: str,
        direction: str,
        confidence: float,
        horizon_hours: float,
        thesis: str,
        expected_move_pct: float,
    ) -> dict:
        product_id = self._validate_product(product_id)
        try:
            parsed_direction = Direction((direction or "").strip().lower())
        except ValueError:
            raise ValueError(f"direction must be 'rise' or 'fall', got {direction!r}") from None
        confidence = float(confidence)
        if not 0.0 < confidence <= 1.0:
            raise ValueError(f"confidence must be in (0, 1], got {confidence}")
        horizon_hours = float(horizon_hours)
        if not 0.5 <= horizon_hours <= 24 * 30:
            raise ValueError("horizon_hours must be between 0.5 and 720")
        thesis = (thesis or "").strip()
        if len(thesis) < 20:
            raise ValueError("thesis must explain the reasoning (>= 20 characters)")
        expected_move_pct = abs(float(expected_move_pct))
        if expected_move_pct > 100.0:
            raise ValueError("expected_move_pct is a percentage, e.g. 3.0 for a 3% move")

        now = self._now()
        self.store.expire_stale_proposals(now)
        pred = Prediction(
            product_id=product_id,
            direction=parsed_direction,
            confidence=confidence,
            horizon_hours=horizon_hours,
            thesis=thesis,
            created_at=now,
        )

        # Fee gate (RESEARCH.md rule #1): a taker round trip costs ~1.2-1.5%,
        # so a predicted move below the gate is negative-EV before it starts.
        # Applies to buys; risk-reducing FALL exits are exempt.
        if parsed_direction is Direction.RISE and expected_move_pct < self.config.fee_gate_pct:
            decision = Decision(
                "no_action",
                [
                    f"fee gate: expected move {expected_move_pct:.2f}% is below "
                    f"{self.config.fee_gate_pct:.2f}%; fees (~1.2-1.5% round trip) would consume "
                    "the edge. Prediction recorded only."
                ],
            )
            self.store.record_prediction(pred, decision)
            return {
                "prediction_id": pred.id,
                "decision": decision.as_dict(),
                "executed": False,
            }

        snapshot = self._snapshot()
        view = self._tech_view(product_id)
        adj_confidence, size_multiplier, tech_notes = strategy.adjust_for_technicals(
            parsed_direction, confidence, view
        )

        params = self._params()
        adjusted_pred = Prediction(
            product_id=product_id,
            direction=parsed_direction,
            confidence=adj_confidence,
            horizon_hours=horizon_hours,
            thesis=thesis,
            id=pred.id,
            created_at=now,
        )
        decision = risk.evaluate(
            adjusted_pred,
            params,
            snapshot,
            trades_today=self.store.trades_today(now),
            realized_pnl_today=self.store.realized_pnl_today(now),
            last_trade_ts=self.store.last_trade_ts(product_id),
            now=now,
            whitelist=self.config.product_whitelist,
            max_trade_usd=self.config.max_trade_usd,
            min_trade_usd=self.config.min_trade_usd,
            trading_enabled=self.store.trading_enabled(),
            disabled_reason=self.store.disabled_reason(),
        )
        decision.reasons = tech_notes + decision.reasons

        # The technical size multiplier applies to BUYS ONLY: shrinking a sell
        # would shrink a risk-reducing exit, which increases risk. (Sells are
        # still gated by the adjusted confidence above.)
        if decision.action == "buy" and size_multiplier < 1.0:
            decision.quote_size *= size_multiplier
            if decision.quote_size < self.config.min_trade_usd:
                decision = Decision(
                    "no_action",
                    decision.reasons
                    + ["size after technical reduction fell below the minimum; not trading"],
                )

        if decision.trip_breaker:
            self.store.record_breaker_trip(now)
            self.store.set_trading_enabled(False, decision.reasons[-1])

        self.store.record_prediction(pred, decision)

        if decision.action == "no_action":
            return {
                "prediction_id": pred.id,
                "decision": decision.as_dict(),
                "technical_view": view.as_dict(),
                "executed": False,
            }

        if self.config.require_confirmation:
            proposal = self.store.create_proposal(
                prediction_id=pred.id,
                product_id=product_id,
                action=decision.action,
                quote_size=decision.quote_size,
                base_size=decision.base_size,
                ttl_minutes=self.config.proposal_ttl_minutes,
                tech=view.as_dict(),
                now=now,
            )
            return {
                "prediction_id": pred.id,
                "decision": decision.as_dict(),
                "technical_view": view.as_dict(),
                "executed": False,
                "proposal": proposal,
                "next_step": (
                    "Double-check this against the technical view and your own research, then call "
                    f"confirm_decision('{proposal['id']}') to execute or "
                    f"reject_decision('{proposal['id']}', reason) to drop it. "
                    f"Expires in {self.config.proposal_ttl_minutes:.0f} minutes."
                ),
            }

        fill, realized = self._execute(product_id, decision, prediction_id=pred.id)
        return {
            "prediction_id": pred.id,
            "decision": decision.as_dict(),
            "technical_view": view.as_dict(),
            "executed": True,
            "fill": fill.__dict__,
            "realized_pnl": realized,
        }

    def confirm_decision(self, proposal_id: str) -> dict:
        now = self._now()
        proposal = self.store.get_proposal((proposal_id or "").strip())
        if not proposal:
            raise ValueError(f"no proposal {proposal_id!r}")
        if proposal["status"] != "proposed":
            raise ValueError(f"proposal {proposal_id} is already {proposal['status']}")
        if now > proposal["expires_at"]:
            self.store.resolve_proposal(proposal["id"], "expired", "TTL elapsed before confirmation")
            raise ValueError(
                f"proposal {proposal_id} expired; submit a fresh prediction if the thesis still holds"
            )

        # Consume the proposal BEFORE the order call: if the order outcome ends
        # up unknown, a retry must not be able to execute it twice.
        self.store.resolve_proposal(proposal["id"], "confirmed", "executing")

        # Re-run the risk checks against CURRENT state. Proposals are sized at
        # submit time; without this, stacking several proposals and confirming
        # them all would bypass the daily cap, cooldown, and exposure caps.
        recheck = self._recheck_proposal(proposal, now)
        if recheck.action != proposal["action"]:
            self.store.resolve_proposal(
                proposal["id"], "rejected", "risk re-check at confirmation refused it: "
                + "; ".join(recheck.reasons)
            )
            return {
                "proposal_id": proposal["id"],
                "executed": False,
                "status": "rejected",
                "reasons": recheck.reasons,
            }

        decision = Decision(
            action=proposal["action"],
            reasons=[f"confirmed proposal {proposal['id']}"] + recheck.reasons,
            # Never execute larger than proposed OR larger than the re-check allows.
            quote_size=min(proposal["quote_size"], recheck.quote_size) if recheck.quote_size else proposal["quote_size"],
            base_size=min(proposal["base_size"], recheck.base_size) if recheck.base_size else proposal["base_size"],
        )
        fill, realized = self._execute(
            proposal["product_id"], decision, prediction_id=proposal["prediction_id"]
        )
        self.store.resolve_proposal(proposal["id"], "confirmed", "analyst confirmed", fill.order_id)
        return {"proposal_id": proposal["id"], "executed": True, "fill": fill.__dict__, "realized_pnl": realized}

    def _recheck_proposal(self, proposal: dict, now: float) -> Decision:
        stored = self.store.get_prediction(proposal["prediction_id"])
        if not stored:
            return Decision("no_action", ["original prediction not found"])
        pred = Prediction(
            product_id=stored["product_id"],
            direction=Direction(stored["direction"]),
            confidence=stored["confidence"],
            horizon_hours=stored["horizon_hours"],
            thesis=stored["thesis"],
            id=stored["id"],
            created_at=stored["created_at"],
        )
        snapshot = self._snapshot()
        view = self._tech_view(pred.product_id)
        adj_confidence, _, _ = strategy.adjust_for_technicals(pred.direction, pred.confidence, view)
        adjusted = Prediction(
            product_id=pred.product_id, direction=pred.direction, confidence=adj_confidence,
            horizon_hours=pred.horizon_hours, thesis=pred.thesis, id=pred.id,
            created_at=pred.created_at,
        )
        return risk.evaluate(
            adjusted,
            self._params(),
            snapshot,
            trades_today=self.store.trades_today(now),
            realized_pnl_today=self.store.realized_pnl_today(now),
            last_trade_ts=self.store.last_trade_ts(pred.product_id),
            now=now,
            whitelist=self.config.product_whitelist,
            max_trade_usd=self.config.max_trade_usd,
            min_trade_usd=self.config.min_trade_usd,
            trading_enabled=self.store.trading_enabled(),
            disabled_reason=self.store.disabled_reason(),
        )

    def reject_decision(self, proposal_id: str, reason: str) -> dict:
        proposal = self.store.get_proposal((proposal_id or "").strip())
        if not proposal:
            raise ValueError(f"no proposal {proposal_id!r}")
        if proposal["status"] != "proposed":
            raise ValueError(f"proposal {proposal_id} is already {proposal['status']}")
        reason = (reason or "").strip() or "rejected by analyst double-check"
        self.store.resolve_proposal(proposal["id"], "rejected", reason)
        return {"proposal_id": proposal["id"], "status": "rejected", "reason": reason}

    def close_position(self, product_id: str, fraction: float = 1.0, reason: str = "") -> dict:
        product_id = self._validate_product(product_id)
        fraction = float(fraction)
        if not 0.0 < fraction <= 1.0:
            raise ValueError(f"fraction must be in (0, 1], got {fraction}")
        snapshot = self._snapshot()
        qty, price = snapshot.positions.get(product_id, (0.0, 0.0))
        if qty <= 0:
            raise ValueError(f"no {product_id} position to close")
        base = qty * fraction
        if (qty - base) * price < self.config.min_trade_usd:
            base = qty  # avoid leaving dust
        decision = Decision(
            "sell",
            [f"analyst-initiated exit ({reason or 'no reason given'})"],
            base_size=base,
        )
        fill, realized = self._execute(
            product_id, decision, prediction_id=None, counts_toward_cap=False,
            note=f"close_position: {reason or 'analyst exit'}",
        )
        return {"executed": True, "fill": fill.__dict__, "realized_pnl": realized}

    def challenge_trade(self, order_id: str, reasoning: str) -> dict:
        """Analyst believes a recent trade was wrong; unwind it if possible.

        Only BUY trades can be unwound directly (by selling what was bought) —
        re-entering after a challenged SELL is a risk-increasing act and must go
        through submit_prediction like any other buy.
        """
        reasoning = (reasoning or "").strip()
        if len(reasoning) < 20:
            raise ValueError("challenge reasoning must explain the error (>= 20 characters)")
        trade = self.store.get_trade((order_id or "").strip())
        if not trade:
            raise ValueError(f"no trade with order_id {order_id!r}")
        now = self._now()
        age_hours = (now - trade["created_at"]) / 3600.0
        if age_hours > self.config.challenge_window_hours:
            raise ValueError(
                f"trade is {age_hours:.1f}h old; challenges are limited to "
                f"{self.config.challenge_window_hours:.0f}h"
            )
        if trade["side"] != "BUY":
            return {
                "unwound": False,
                "message": "Only BUY trades can be unwound by challenge. If you want to re-enter "
                "after a sell you now believe was wrong, submit a fresh RISE prediction.",
            }
        balances = self.exchange.get_balances()
        base_currency = trade["product_id"].split("-")[0]
        base = min(trade["base_size"], balances.get(base_currency, 0.0))
        if base <= 0:
            return {"unwound": False, "message": "nothing left of that position to unwind"}
        decision = Decision("sell", [f"challenge of {order_id}: {reasoning}"], base_size=base)
        fill, realized = self._execute(
            trade["product_id"], decision, prediction_id=trade["prediction_id"],
            counts_toward_cap=False, note=f"challenge_trade({order_id}): {reasoning}",
        )
        return {"unwound": True, "fill": fill.__dict__, "realized_pnl": realized}

    def run_maintenance(self) -> dict:
        """Stop-loss sweep + housekeeping. The analyst should call this regularly."""
        now = self._now()
        expired = self.store.expire_stale_proposals(now)
        actions: list[dict] = []
        if not self.store.trading_enabled():
            return {
                "stop_loss_checks": "skipped: trading disabled "
                f"({self.store.disabled_reason() or 'kill switch'})",
                "proposals_expired": expired,
                "actions": actions,
            }
        params = self._params()
        snapshot = self._snapshot()
        for product_id, (qty, price) in snapshot.positions.items():
            tracked_qty, cost = self.store.get_position(product_id)
            if tracked_qty <= 0 or cost <= 0:
                continue
            avg_cost = cost / tracked_qty
            drawdown = price / avg_cost - 1.0
            opened_at = self.store.position_opened_at(product_id)
            held_days = (now - opened_at) / 86400.0 if opened_at else 0.0

            exit_reason = None
            if drawdown <= -params.stop_loss_pct:
                exit_reason = (
                    "stop_loss_exit",
                    f"stop-loss: {product_id} at {drawdown:+.1%} vs avg cost "
                    f"{avg_cost:.2f}; mode limit is -{params.stop_loss_pct:.0%}",
                )
            elif held_days > MAX_HOLD_DAYS:
                # Crypto momentum flips to reversal beyond ~1 month; time-exit
                # regardless of P&L (RESEARCH.md).
                exit_reason = (
                    "max_hold_time_exit",
                    f"time exit: {product_id} held {held_days:.0f} days, "
                    f"max is {MAX_HOLD_DAYS}; momentum beyond ~1 month reverses",
                )
            if exit_reason:
                action_name, why = exit_reason
                decision = Decision("sell", [why], base_size=qty)
                # One failing exit must not abort the protective sweep for the
                # remaining positions; the breaker is evaluated once at the end
                # so a mid-sweep trip can't block the other stop-losses either.
                try:
                    fill, realized = self._execute(
                        product_id, decision, prediction_id=None, counts_toward_cap=False,
                        note=why, check_breaker=False,
                    )
                    actions.append(
                        {"action": action_name, "product_id": product_id,
                         "drawdown": round(drawdown, 4), "held_days": round(held_days, 1),
                         "fill": fill.__dict__, "realized_pnl": realized}
                    )
                except (ExchangeError, guardrails.GuardrailViolation) as exc:
                    actions.append(
                        {"action": f"{action_name}_FAILED", "product_id": product_id,
                         "drawdown": round(drawdown, 4), "held_days": round(held_days, 1),
                         "error": str(exc)}
                    )
        # Post-sweep breaker check: stop-losses may have pushed us past the daily limit.
        self._maybe_trip_breaker(now)
        return {"proposals_expired": expired, "actions": actions,
                "trading_enabled": self.store.trading_enabled(),
                "disabled_reason": self.store.disabled_reason()}

    # -- execution --------------------------------------------------------

    def _maybe_trip_breaker(self, now: float) -> None:
        params = self._params()
        snapshot_value = self._snapshot().total_value
        if snapshot_value <= 0:
            return
        pnl = self.store.realized_pnl_today(now)
        if pnl <= -params.daily_loss_limit_pct * snapshot_value and self.store.trading_enabled():
            self.store.record_breaker_trip(now)
            self.store.set_trading_enabled(
                False,
                f"daily-loss circuit breaker: realized {pnl:+.2f} today exceeds "
                f"{params.daily_loss_limit_pct:.0%} of portfolio; latched until the next "
                "UTC day, then re-enable with set_trading_enabled(true) and a written review",
            )

    def _execute(
        self,
        product_id: str,
        decision: Decision,
        prediction_id: str | None,
        counts_toward_cap: bool = True,
        note: str = "",
        check_breaker: bool = True,
    ) -> tuple[Fill, float | None]:
        now = self._now()
        if decision.action == "buy":
            side, notional = "BUY", decision.quote_size
        elif decision.action == "sell":
            price = self.exchange.get_product(product_id).price
            side, notional = "SELL", decision.base_size * price
        else:
            raise ValueError(f"cannot execute decision with action {decision.action!r}")

        guardrails.validate_order(
            side=side,
            product_id=product_id,
            notional_usd=notional,
            whitelist=self.config.product_whitelist,
            max_trade_usd=self.config.max_trade_usd,
            trading_enabled=self.store.trading_enabled(),
        )

        if side == "BUY":
            fill = self.exchange.market_buy(product_id, decision.quote_size)
            self.store.apply_buy(product_id, fill.base_size, fill.quote_size, now=now)
            realized = None
        else:
            balances = self.exchange.get_balances()
            base_currency = product_id.split("-")[0]
            base = min(decision.base_size, balances.get(base_currency, 0.0))
            if base <= 0:
                raise ExchangeError(f"no {base_currency} available to sell")
            fill = self.exchange.market_sell(product_id, base)
            realized = self.store.apply_sell(product_id, fill.base_size, fill.quote_size)
            # Quantized exits can leave sub-increment residue; close it out so
            # the max-hold clock resets for the next position.
            self.store.close_dust(product_id, fill.price, DUST_USD)

        if fill.estimated:
            note = (note + " | " if note else "") + (
                "FILL ESTIMATED: order placed but fill unconfirmed; verify on Coinbase"
            )
        self.store.record_trade(
            fill, prediction_id, realized, counts_toward_cap=counts_toward_cap, note=note, now=now
        )
        if check_breaker:
            self._maybe_trip_breaker(now)
        return fill, round(realized, 2) if realized is not None else None
