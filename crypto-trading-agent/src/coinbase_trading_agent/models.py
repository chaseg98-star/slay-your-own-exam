"""Typed domain objects shared across the agent."""

from __future__ import annotations

import enum
import time
import uuid
from dataclasses import dataclass, field


class Direction(str, enum.Enum):
    RISE = "rise"
    FALL = "fall"


class RiskMode(str, enum.Enum):
    CONSERVATIVE = "conservative"
    MODERATE = "moderate"
    AGGRESSIVE = "aggressive"


@dataclass(frozen=True)
class RiskParams:
    # Predictions below this confidence are recorded but never traded.
    min_confidence: float
    # Largest single buy, as a fraction of total portfolio value.
    max_trade_pct: float
    # Ceiling on any one asset's share of total portfolio value.
    max_asset_exposure_pct: float
    # Fraction of a position sold on a maximum-confidence FALL prediction.
    sell_fraction: float
    # Prediction-driven orders allowed per UTC day.
    daily_trade_cap: int
    # Realized loss (fraction of portfolio) that trips the circuit breaker.
    daily_loss_limit_pct: float
    # Minimum minutes between prediction-driven orders on the same product.
    cooldown_minutes: int
    # Unrealized drawdown from avg cost at which run_maintenance() exits a
    # position. Deliberately wide: tight stops measurably destroy value in
    # crypto; the evidence supports 10%+ volatility-scaled stops (RESEARCH.md).
    stop_loss_pct: float


RISK_PROFILES: dict[RiskMode, RiskParams] = {
    RiskMode.CONSERVATIVE: RiskParams(
        min_confidence=0.80,
        max_trade_pct=0.05,
        max_asset_exposure_pct=0.15,
        sell_fraction=0.33,
        daily_trade_cap=4,
        daily_loss_limit_pct=0.02,
        cooldown_minutes=360,
        stop_loss_pct=0.10,
    ),
    RiskMode.MODERATE: RiskParams(
        min_confidence=0.65,
        max_trade_pct=0.10,
        max_asset_exposure_pct=0.30,
        sell_fraction=0.50,
        daily_trade_cap=8,
        daily_loss_limit_pct=0.05,
        cooldown_minutes=120,
        stop_loss_pct=0.15,
    ),
    RiskMode.AGGRESSIVE: RiskParams(
        min_confidence=0.55,
        max_trade_pct=0.20,
        max_asset_exposure_pct=0.50,
        sell_fraction=1.00,
        daily_trade_cap=16,
        daily_loss_limit_pct=0.10,
        cooldown_minutes=30,
        stop_loss_pct=0.20,
    ),
}

# Crypto momentum continuation flips to reversal beyond ~1 month (RESEARCH.md);
# run_maintenance() force-exits any position held longer than this, all modes.
MAX_HOLD_DAYS = 28


@dataclass
class Prediction:
    product_id: str
    direction: Direction
    confidence: float
    horizon_hours: float
    thesis: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: float = field(default_factory=time.time)


@dataclass
class Decision:
    action: str  # "buy" | "sell" | "no_action"
    reasons: list[str] = field(default_factory=list)
    quote_size: float = 0.0  # notional in quote currency, for buys
    base_size: float = 0.0  # asset quantity, for sells
    trip_breaker: bool = False

    def as_dict(self) -> dict:
        return {
            "action": self.action,
            "reasons": self.reasons,
            "quote_size": round(self.quote_size, 2),
            "base_size": self.base_size,
        }


@dataclass
class Fill:
    order_id: str
    product_id: str
    side: str  # "BUY" | "SELL"
    base_size: float
    quote_size: float
    price: float
    fee: float


@dataclass
class PortfolioSnapshot:
    quote_currency: str
    quote_balance: float
    # product_id -> (base quantity, current price)
    positions: dict[str, tuple[float, float]] = field(default_factory=dict)

    @property
    def total_value(self) -> float:
        return self.quote_balance + sum(qty * price for qty, price in self.positions.values())

    def exposure_value(self, product_id: str) -> float:
        qty, price = self.positions.get(product_id, (0.0, 0.0))
        return qty * price
