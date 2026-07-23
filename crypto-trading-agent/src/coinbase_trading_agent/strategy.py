"""Technical strategy engine.

Computes the agent's own view from raw OHLCV candles so every analyst
prediction is cross-checked against price data. Signal design follows the
evidence surveyed in RESEARCH.md:

* trend (price vs EMA50, EMA20 vs EMA50) is the primary gate — the
  best-documented signal family in crypto (weight 0.40);
* 1-4 week momentum is the robust horizon; longer lookbacks flip to
  reversal (weight 0.30);
* RSI(14) is scored in the MOMENTUM direction — in crypto, high RSI
  predicts higher returns (JFQA 2025); classic contrarian
  overbought/oversold usage is unsupported (weight 0.15);
* volume confirms only in normal regimes (Balcilar et al.) — the
  component is disabled entirely when volatility is extreme (weight 0.15);
* realized-volatility regime scales SIZE only, never direction.

Crucially, technical disagreement can only shrink or block a trade — it
never sizes one up.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field

from .exchange import Candle
from .models import Direction

MIN_CANDLES = 60


@dataclass
class TechView:
    score: float  # -1 (strong bearish) .. +1 (strong bullish)
    regime: str  # "uptrend" | "downtrend" | "ranging" | "high_volatility" | "unknown"
    indicators: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "score": round(self.score, 3),
            "regime": self.regime,
            "indicators": self.indicators,
            "notes": self.notes,
        }


def ema(values: list[float], period: int) -> float:
    if not values:
        return 0.0
    k = 2.0 / (period + 1.0)
    out = values[0]
    for v in values[1:]:
        out = v * k + out * (1.0 - k)
    return out


def rsi(closes: list[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for prev, cur in zip(closes[-period - 1 : -1], closes[-period:]):
        change = cur - prev
        gains.append(max(0.0, change))
        losses.append(max(0.0, -change))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def _log_returns(closes: list[float]) -> list[float]:
    return [
        math.log(b / a)
        for a, b in zip(closes[:-1], closes[1:])
        if a > 0 and b > 0
    ]


def technical_view(candles: list[Candle]) -> TechView:
    """Composite technical score from hourly (or any fixed-interval) candles."""
    if len(candles) < MIN_CANDLES:
        return TechView(
            score=0.0,
            regime="unknown",
            notes=[
                f"only {len(candles)} candles available (<{MIN_CANDLES}); "
                "no technical adjustment applied"
            ],
        )

    closes = [c.close for c in candles]
    volumes = [c.volume for c in candles]
    price = closes[-1]
    ema20 = ema(closes, 20)
    ema50 = ema(closes, 50)
    rsi14 = rsi(closes, 14)

    notes: list[str] = []
    score = 0.0

    # Volatility regime first (it gates the volume component below).
    returns = _log_returns(closes)
    recent_sigma = statistics.pstdev(returns[-24:]) if len(returns) >= 24 else 0.0
    full_sigma = statistics.pstdev(returns) if len(returns) >= 2 else 0.0
    high_vol = full_sigma > 0 and recent_sigma > 1.5 * full_sigma

    # Trend gate (±0.40): the best-evidenced signal family in crypto.
    trend = 0.0
    if price > ema50 and ema20 > ema50:
        trend = 0.4
        notes.append("trend bullish: price above EMA50 and EMA20 above EMA50")
    elif price < ema50 and ema20 < ema50:
        trend = -0.4
        notes.append("trend bearish: price below EMA50 and EMA20 below EMA50")
    else:
        notes.append("trend mixed: EMAs disagree")
    score += trend

    # Momentum (±0.30): blended short/medium lookback ROC. The robust
    # continuation horizon in crypto is 1-4 weeks; never longer.
    lookbacks = [lb for lb in (72, 168) if lb < len(closes)] or [max(24, len(closes) // 3)]
    rocs = [price / closes[-lb] - 1.0 for lb in lookbacks if closes[-lb] > 0]
    momentum_return = sum(rocs) / len(rocs) if rocs else 0.0
    momentum = max(-0.3, min(0.3, momentum_return * 3.0))
    score += momentum
    notes.append(
        f"momentum: {momentum_return:+.1%} blended over {lookbacks} candle lookbacks"
    )

    # RSI(14) (±0.15), scored in the MOMENTUM direction: in crypto, high RSI
    # predicts continued strength (JFQA 2025); contrarian use is unsupported.
    rsi_component = max(-1.0, min(1.0, (rsi14 - 50.0) / 25.0)) * 0.15
    score += rsi_component
    notes.append(f"RSI {rsi14:.0f} scored as momentum: {rsi_component:+.2f}")
    if rsi14 >= 85:
        notes.append("RSI >= 85 blow-off warning: consider tightening exits, not adding")

    # Volume confirmation (±0.15) — only in normal-volatility regimes;
    # volume has no documented predictive power in extreme regimes.
    recent_vol = sum(volumes[-10:]) / 10.0
    baseline = sum(volumes[:-10]) / max(1, len(volumes) - 10)
    vol_ratio = recent_vol / baseline if baseline > 0 else 1.0
    if high_vol:
        notes.append("volume component disabled: extreme-volatility regime")
    elif abs(trend) > 0:
        if vol_ratio >= 1.25:
            score += 0.15 if trend > 0 else -0.15
            notes.append(f"volume {vol_ratio:.1f}x baseline confirms the trend")
        elif vol_ratio <= 0.75:
            score += -0.075 if trend > 0 else 0.075
            notes.append(f"volume contracting ({vol_ratio:.1f}x): trend unconfirmed")

    if high_vol:
        regime = "high_volatility"
        notes.append(
            f"volatility elevated: recent sigma {recent_sigma:.4f} > 1.5x window sigma {full_sigma:.4f}"
        )
    elif trend > 0:
        regime = "uptrend"
    elif trend < 0:
        regime = "downtrend"
    else:
        regime = "ranging"

    return TechView(
        score=max(-1.0, min(1.0, score)),
        regime=regime,
        indicators={
            "price": price,
            "ema20": round(ema20, 8),
            "ema50": round(ema50, 8),
            "rsi14": round(rsi14, 1),
            "momentum_return": round(momentum_return, 4),
            "volume_ratio": round(vol_ratio, 2),
            "recent_sigma": round(recent_sigma, 6),
            "window_sigma": round(full_sigma, 6),
        },
        notes=notes,
    )


def adjust_for_technicals(
    direction: Direction, confidence: float, view: TechView
) -> tuple[float, float, list[str]]:
    """Cross-check a prediction against the technical view.

    Returns (adjusted_confidence, size_multiplier, notes). Adjustments only
    ever reduce risk: confidence and size are never increased.
    """
    notes: list[str] = []
    adjusted = confidence
    multiplier = 1.0

    if view.regime == "unknown":
        return confidence, 1.0, ["technical data unavailable; relying on analyst confidence alone"]

    alignment = view.score if direction is Direction.RISE else -view.score

    # Agreement threshold 0.35 per RESEARCH.md: below it a prediction is at
    # best unconfirmed, and unconfirmed trades get cut, never full-sized.
    if alignment >= 0.35:
        notes.append(f"technicals agree (alignment {alignment:+.2f}); no adjustment")
    elif alignment > -0.2:
        multiplier = 0.75
        notes.append(f"technicals neutral (alignment {alignment:+.2f}); size reduced to 75%")
    elif alignment > -0.5:
        adjusted = max(0.0, confidence - 0.10)
        multiplier = 0.5
        notes.append(
            f"technicals disagree (alignment {alignment:+.2f}); "
            "confidence -0.10 and size reduced to 50%"
        )
    else:
        adjusted = max(0.0, confidence - 0.15)
        multiplier = 0.25
        notes.append(
            f"technicals strongly disagree (alignment {alignment:+.2f}); "
            "confidence -0.15 and size reduced to 25%"
        )

    if view.regime == "high_volatility":
        multiplier *= 0.5
        notes.append("high-volatility regime: size halved (volatility-targeted sizing)")

    return adjusted, multiplier, notes
