"""MCP server exposing the trading agent's tools over stdio.

Run with `coinbase-trading-agent` (after `pip install -e .`) or
`python -m coinbase_trading_agent.server`.
"""

from __future__ import annotations

import sys

from mcp.server.fastmcp import FastMCP

from .config import Config
from .core import AgentCore
from .exchange import PaperExchange
from .state import Store

INSTRUCTIONS = """Coinbase spot-trading agent for an analyst LLM.

You research crypto (news, social sentiment, on-chain data, markets) and submit
rise/fall predictions here; this agent risk-checks them against its configured
risk mode AND its own technical read of the market, then proposes a sized trade.
You must double-check every proposal (confirm_decision / reject_decision) —
that second look is part of the design, so take it seriously: re-verify the
thesis against get_technical_analysis and get_market_data before confirming.

This agent is trade-only: it cannot withdraw funds, send funds to any wallet,
or deposit funds, and it refuses to run if its API key has transfer permission.
Do not attempt fund movements; they are impossible by construction.

Suggested cadence: get_status → research → submit_prediction → double-check →
confirm/reject → run_maintenance (every session; it enforces stop-losses).
"""

mcp = FastMCP("coinbase-trading-agent", instructions=INSTRUCTIONS)

_core: AgentCore | None = None


def _get_core() -> AgentCore:
    global _core
    if _core is None:
        config = Config.from_env()
        store = Store(config.data_dir / f"agent-{config.trading_mode}.sqlite3")
        if config.trading_mode == "live":
            from .exchange import CoinbaseExchange

            exchange = CoinbaseExchange(config.api_key_name, config.api_private_key)
        else:
            exchange = PaperExchange(
                quote_currency=config.quote_currency,
                starting_quote=config.paper_starting_usd,
                fee_rate=config.paper_fee_rate,
            )
        core = AgentCore(config, store, exchange)
        core.startup_checks()  # refuses transfer-capable API keys
        _core = core
    return _core


@mcp.tool()
def get_status() -> dict:
    """Agent status: risk mode & params, kill-switch state, today's trade count and
    realized P&L, pending proposals awaiting confirmation, and the product whitelist."""
    return _get_core().get_status()


@mcp.tool()
def get_portfolio() -> dict:
    """Current balances and positions with price, value, average cost, and unrealized P&L."""
    return _get_core().get_portfolio()


@mcp.tool()
def get_market_data(product_ids: list[str] | None = None) -> dict:
    """Spot price, 24h change, and 24h volume for the given products (e.g. ["BTC-USD"]).
    Defaults to the whitelist. Any product can be quoted; only whitelisted ones trade."""
    return _get_core().get_market_data(product_ids or [])


@mcp.tool()
def get_technical_analysis(product_id: str, granularity: str = "ONE_HOUR", limit: int = 200) -> dict:
    """The agent's own technical read from OHLCV candles: composite score (-1..+1),
    regime (uptrend/downtrend/ranging/high_volatility), EMA20/50, RSI14, momentum,
    volume ratio, plus the recent candles. Use this to double-check every proposal."""
    return _get_core().get_technical_analysis(product_id, granularity, limit)


@mcp.tool()
def submit_prediction(
    product_id: str,
    direction: str,
    confidence: float,
    horizon_hours: float,
    thesis: str,
    expected_move_pct: float,
) -> dict:
    """Submit a researched prediction that a coin will 'rise' or 'fall' within
    horizon_hours, with confidence in (0, 1], a thesis explaining the evidence,
    and expected_move_pct — the size of the move you expect (e.g. 3.0 = 3%).
    RISE predictions below the fee gate (default 1.5%) are recorded but never
    traded: fees would consume the edge. The agent cross-checks the prediction
    against its own technicals, applies the active risk mode's rules, and
    returns either a refusal (with reasons) or a sized trade proposal for you
    to confirm_decision / reject_decision."""
    return _get_core().submit_prediction(
        product_id, direction, confidence, horizon_hours, thesis, expected_move_pct
    )


@mcp.tool()
def confirm_decision(proposal_id: str) -> dict:
    """Execute a proposed trade after you have double-checked it. Re-verify the thesis
    against get_technical_analysis and fresh market data before calling this."""
    return _get_core().confirm_decision(proposal_id)


@mcp.tool()
def reject_decision(proposal_id: str, reason: str) -> dict:
    """Drop a proposed trade your double-check found unconvincing. Say why."""
    return _get_core().reject_decision(proposal_id, reason)


@mcp.tool()
def close_position(product_id: str, fraction: float = 1.0, reason: str = "") -> dict:
    """Sell part or all of an existing position (risk-reducing exit; bypasses the
    daily cap and cooldowns but still honors the kill switch)."""
    return _get_core().close_position(product_id, fraction, reason)


@mcp.tool()
def challenge_trade(order_id: str, reasoning: str) -> dict:
    """If you conclude a recent BUY was a mistake, challenge it with your reasoning
    and the agent will unwind it (sell what was bought). Sells cannot be unwound
    here — re-entering is a new risk and needs a fresh submit_prediction."""
    return _get_core().challenge_trade(order_id, reasoning)


@mcp.tool()
def run_maintenance() -> dict:
    """Stop-loss sweep and housekeeping: exits any position past the mode's stop-loss,
    expires stale proposals, re-checks the daily-loss circuit breaker. Call this at
    the start of every session and periodically while monitoring."""
    return _get_core().run_maintenance()


@mcp.tool()
def set_risk_mode(mode: str) -> dict:
    """Switch risk mode: 'conservative', 'moderate', or 'aggressive'. Returns the
    new mode's parameters (confidence floor, size caps, stop-loss, daily limits)."""
    return _get_core().set_risk_mode(mode)


@mcp.tool()
def set_trading_enabled(enabled: bool, reason: str = "") -> dict:
    """Kill switch. false = halt all trading immediately (predictions are still
    recorded); true = re-enable after a manual review, e.g. when the daily-loss
    circuit breaker tripped."""
    return _get_core().set_trading_enabled(enabled, reason)


@mcp.tool()
def get_trade_log(limit: int = 20) -> list[dict]:
    """Recent executed trades: side, size, price, fee, realized P&L, and the note
    (stop-loss / challenge / close reason) if any."""
    return _get_core().get_trade_log(limit)


@mcp.tool()
def get_predictions(limit: int = 20) -> list[dict]:
    """Recent predictions with the decision each one produced — including refusals
    and why. Review these to calibrate your confidence over time."""
    return _get_core().get_predictions(limit)


def main() -> None:
    try:
        _get_core()
    except Exception as exc:
        print(f"coinbase-trading-agent failed to start: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    mcp.run()


if __name__ == "__main__":
    main()
