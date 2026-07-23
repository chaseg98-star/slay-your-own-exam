import math

from coinbase_trading_agent import strategy
from coinbase_trading_agent.exchange import Candle
from coinbase_trading_agent.models import Direction


def make_candles(closes, volume=100.0):
    return [
        Candle(start=i * 3600, open=c, high=c * 1.01, low=c * 0.99, close=c, volume=volume)
        for i, c in enumerate(closes)
    ]


def rising_closes(n=200, start=100.0, step=0.4):
    return [start + i * step for i in range(n)]


def falling_closes(n=200, start=200.0, step=0.4):
    return [start - i * step for i in range(n)]


def test_insufficient_data_is_neutral():
    view = strategy.technical_view(make_candles([100.0] * 10))
    assert view.score == 0.0 and view.regime == "unknown"


def test_uptrend_scores_bullish():
    view = strategy.technical_view(make_candles(rising_closes()))
    assert view.score > 0.3
    assert view.regime == "uptrend"


def test_downtrend_scores_bearish():
    view = strategy.technical_view(make_candles(falling_closes()))
    assert view.score < -0.3
    assert view.regime == "downtrend"


def test_high_volatility_regime():
    closes = [100.0 + 0.05 * i for i in range(176)]
    # violent whipsaw at the end
    closes += [108.8, 99.0, 110.0, 98.0, 111.0, 97.0, 112.0, 96.0,
               113.0, 95.0, 114.0, 94.0, 115.0, 93.0, 116.0, 92.0,
               117.0, 91.0, 118.0, 90.0, 119.0, 89.0, 120.0, 88.0]
    view = strategy.technical_view(make_candles(closes))
    assert view.regime == "high_volatility"


def test_rsi_bounds():
    assert strategy.rsi([100.0] * 20) >= 0
    up = strategy.rsi(rising_closes(30))
    down = strategy.rsi(falling_closes(30))
    assert up > 70 and down < 30


def test_ema_converges_to_constant():
    assert math.isclose(strategy.ema([50.0] * 100, 20), 50.0)


def test_agreement_never_upsizes():
    view = strategy.technical_view(make_candles(rising_closes()))
    conf, mult, _ = strategy.adjust_for_technicals(Direction.RISE, 0.8, view)
    assert conf <= 0.8 and mult <= 1.0


def test_disagreement_downsizes_rise_in_downtrend():
    view = strategy.technical_view(make_candles(falling_closes()))
    conf, mult, notes = strategy.adjust_for_technicals(Direction.RISE, 0.8, view)
    assert conf < 0.8
    assert mult <= 0.5
    assert any("disagree" in n for n in notes)


def test_fall_prediction_in_downtrend_agrees():
    view = strategy.technical_view(make_candles(falling_closes()))
    conf, mult, _ = strategy.adjust_for_technicals(Direction.FALL, 0.8, view)
    assert conf == 0.8 and mult == 1.0


def test_unknown_regime_no_adjustment():
    view = strategy.technical_view(make_candles([100.0] * 5))
    conf, mult, notes = strategy.adjust_for_technicals(Direction.RISE, 0.7, view)
    assert conf == 0.7 and mult == 1.0
    assert "unavailable" in notes[0]
