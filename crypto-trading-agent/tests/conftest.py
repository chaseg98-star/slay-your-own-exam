import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from coinbase_trading_agent.config import Config  # noqa: E402
from coinbase_trading_agent.core import AgentCore  # noqa: E402
from coinbase_trading_agent.exchange import PaperExchange  # noqa: E402
from coinbase_trading_agent.state import Store  # noqa: E402

PRICES = {"BTC-USD": 50000.0, "ETH-USD": 2500.0, "SOL-USD": 100.0}


class FakeClock:
    def __init__(self, start: float = 1_750_000_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_config(**overrides) -> Config:
    env = {
        "TRADING_MODE": "paper",
        "PRODUCT_WHITELIST": "BTC,ETH,SOL",
        "MAX_TRADE_USD": "250",
        "MIN_TRADE_USD": "5",
        "PAPER_STARTING_USD": "10000",
        "REQUIRE_CONFIRMATION": "0",
        "AGENT_DATA_DIR": "/tmp/unused-in-tests",
    }
    env.update(overrides)
    return Config.from_env(env)


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def store():
    s = Store(":memory:")
    yield s
    s.close()


def make_core(store, clock, *, prices=None, config=None, no_candles=True):
    prices = dict(PRICES if prices is None else prices)
    exchange = PaperExchange(
        quote_currency="USD",
        starting_quote=10_000.0,
        fee_rate=0.006,
        price_source=lambda pid: prices[pid],
    )
    if no_candles:
        exchange.get_candles = lambda *a, **k: []
    core = AgentCore(config or make_config(), store, exchange, now=clock)
    return core, exchange, prices
