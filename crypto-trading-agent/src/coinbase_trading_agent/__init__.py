"""Coinbase trading MCP agent.

An MCP server that lets an analyst LLM submit rise/fall predictions and
executes risk-managed spot trades on Coinbase Advanced Trade. Trade-only by
construction: no code path can withdraw, send, or deposit funds.
"""

__version__ = "0.1.0"
