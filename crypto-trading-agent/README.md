# Crypto Trading Agent (MCP) — Coinbase or Robinhood

An MCP server that turns a Claude analyst's crypto research into **risk-managed
spot trades on your Coinbase or Robinhood account** — and nothing else. The analyst
researches news, social sentiment, on-chain data, and markets; this agent takes
its rise/fall predictions, cross-checks them against its own technical read of
raw OHLCV data, sizes them under the active risk mode, and executes only after
the analyst double-checks the proposal.

**Trade-only by construction.** The agent cannot withdraw funds, send funds to
any wallet, or deposit funds:

1. **The code has no fund-movement capability.** The exchange interface exposes
   only balances, market data, and spot BUY/SELL. There is no withdraw, send,
   transfer, or deposit code path anywhere in the package (tests assert this).
2. **The API key can't do it either.** You create the Coinbase key with only
   *View* + *Trade* permissions. At startup the agent queries the key's actual
   permissions and **refuses to run** if the key can transfer funds.
3. **Every order re-passes a final guardrail** (side ∈ {BUY, SELL}, whitelisted
   product, hard dollar cap, kill switch honored) immediately before the
   exchange call, independent of what the risk engine decided.

The API key permission check (#2) is the load-bearing guarantee: even a bug in
this code could not move money off the account, because Coinbase rejects what
the key is not permitted to do.

## How it works

```
Claude analyst (research: news, X/Twitter, Reddit, on-chain, markets)
      │
      │  submit_prediction("SOL-USD", "rise", confidence=0.8, horizon_hours=48, thesis=...)
      ▼
┌─────────────────────────── this MCP server ────────────────────────────┐
│ 1. Technical cross-check: trend, momentum, RSI, volume, volatility     │
│    regime from raw candles. Disagreement shrinks or blocks the trade;  │
│    agreement never upsizes it.                                         │
│ 2. Risk engine: confidence floor, per-trade & per-asset caps, daily    │
│    trade cap, per-coin cooldown, daily-loss circuit breaker.           │
│ 3. Proposal: a sized trade the analyst must confirm_decision() or      │
│    reject_decision() after double-checking (15 min TTL).               │
│ 4. Guardrails + market order on Coinbase Advanced Trade (or paper).    │
│ 5. Journal: every prediction, decision, refusal-with-reasons, fill,    │
│    and P&L is recorded in SQLite.                                      │
└────────────────────────────────────────────────────────────────────────┘
```

Post-trade oversight (no human needed): `run_maintenance` enforces stop-losses
and the 28-day maximum hold every time it's called, `challenge_trade` lets the
analyst unwind a BUY it concludes was wrong, and the daily-loss circuit breaker
halts trading on a bad day. The breaker is **latched**: for the rest of that
UTC day nothing can re-enable trading or switch to a riskier mode (tightening
is allowed) — the limit is per-day, and a same-day reset would defeat it. From
the next UTC day, the analyst can re-enable with a written review, unless you
set `LOCK_RISK_CONTROLS=1`, which reserves mode changes and re-enabling for
you. Note: the kill switch is absolute — while trading is disabled, even
protective stop-loss sells are skipped, so flatten positions before a long
shutdown if you don't want market exposure.

## Risk modes

| | conservative | moderate | aggressive |
|---|---|---|---|
| Min confidence to trade | 0.80 | 0.65 | 0.55 |
| Max single buy (of portfolio) | 5% | 10% | 20% |
| Max per-asset exposure | 15% | 30% | 50% |
| Sell fraction on FALL (scaled by confidence) | up to 33% | up to 50% | up to 100% |
| Daily trade cap | 4 | 8 | 16 |
| Daily realized-loss breaker | 2% | 5% | 10% |
| Per-coin cooldown | 6 h | 2 h | 30 min |
| Stop-loss | 10% | 15% | 20% |

Stops are deliberately wide — tight stops measurably destroy value in crypto
(see RESEARCH.md). On top of every mode: a hard `MAX_TRADE_USD` per-buy cap
(default $250), a product whitelist (default: 10 large-cap coins), a $5
minimum trade, a **fee gate** (RISE predictions expecting a move under 1.5%
are never traded — fees would eat the edge), and a **28-day maximum hold**
(crypto momentum reverses beyond ~1 month; `run_maintenance` time-exits).

## Setup

### 1. Install

```bash
cd crypto-trading-agent
pip install -e .
```

### 2. Try it in paper mode first (default — no API key needed)

Paper mode simulates fills against real Coinbase market prices with a 0.6%
taker fee. Run the agent this way until you trust it.

### 3a. Live on Coinbase (EXCHANGE=coinbase)

1. In Coinbase, create a **dedicated portfolio** and move only the money you
   are prepared to lose into it. The agent can only touch that portfolio.
2. Go to the [CDP API key portal](https://portal.cdp.coinbase.com/access/api)
   and create a key **scoped to that portfolio** with **View and Trade
   permissions only. Do not grant Transfer.** The agent refuses to start if the
   key can transfer.
3. Put the key in your environment (see `.env.example`).

### 3b. Live on Robinhood (EXCHANGE=robinhood)

Uses the official [Robinhood Crypto Trading API](https://docs.robinhood.com)
(crypto only — Robinhood has no public stock API).

1. Generate a keypair on your machine (after `pip install -e .`):
   ```bash
   coinbase-trading-agent --generate-robinhood-keys
   ```
   It prints a PUBLIC key (safe to share — Robinhood gets this) and a PRIVATE
   key (goes only into your local config; never share it or paste it into any
   chat or website).
2. In the Robinhood app/web: **Account → Settings → Crypto → API trading**.
   Register the **public** key there and save the API key Robinhood issues.
2. Set `EXCHANGE=robinhood`, `ROBINHOOD_API_KEY`, and `ROBINHOOD_PRIVATE_KEY`
   in the env (see `.env.example`).
3. Two structural differences from Coinbase to know:
   - The Robinhood Crypto API has **no withdrawal/transfer/deposit endpoints at
     all**, so trade-only is guaranteed by the API surface itself.
   - Robinhood has **no sub-portfolios**: the key sees your entire crypto
     buying power. The agent's caps (`MAX_TRADE_USD`, exposure limits) and
     `PORTFOLIO_FLOOR_USD` are the sizing limits, so set them deliberately.
   - OHLCV candles for the technical engine come from Coinbase's free public
     market data (Robinhood serves none); execution stays on Robinhood.

### 3c. Standing safety rails (any venue)

- **Portfolio floor** (`PORTFOLIO_FLOOR_USD`): if total account value ever
  falls to the floor, the agent liquidates every position immediately and
  halts. There is deliberately no "it might bounce back" override — a crash
  that will recover is indistinguishable from one that won't while it's
  happening. If the analyst still believes in the coin afterward, it re-enters
  through a fresh, risk-checked prediction.
- **Shock review alerts** (`SHOCK_DROP_PCT`, default 8): any held coin dropping
  ≥8% in an hour (or ≥16% in a day) raises a `REVIEW_REQUIRED` alert on the
  next scan, instructing the analyst to re-research the thesis immediately and
  close the position if it no longer holds.
- **The 30-minute scan**: `run_maintenance` is the heartbeat — floor, stops,
  max-hold, shock alerts. Run it two ways at once:
  1. The analyst calls it at the start of every session and every ~30 minutes
     while monitoring (see `analyst-prompt.md`).
  2. A cron/launchd job runs it headlessly so the rails hold with no Claude
     session open:
     ```
     */30 * * * * /path/to/.venv/bin/coinbase-trading-agent --monitor >> ~/trading-monitor.log 2>&1
     ```
     (same env vars as the server; it prints the sweep report as JSON).

### 4. Connect Claude

Claude Desktop (`claude_desktop_config.json`) or Claude Code (`.mcp.json`):

```json
{
  "mcpServers": {
    "coinbase-trader": {
      "command": "/path/to/your/venv/bin/coinbase-trading-agent",
      "env": {
        "TRADING_MODE": "paper",
        "DEFAULT_RISK_MODE": "conservative",
        "MAX_TRADE_USD": "250"
      }
    }
  }
}
```

Use the absolute path to the console script (GUI apps like Claude Desktop
don't inherit your shell's PATH); `which coinbase-trading-agent` prints it.

For live trading set `TRADING_MODE=live` plus `COINBASE_API_KEY_NAME` and
`COINBASE_API_PRIVATE_KEY`. All knobs are documented in `.env.example`.

### 5. Give the analyst its instructions

Paste `analyst-prompt.md` into the analyst Claude's project/system prompt. It
defines the research → predict → double-check → confirm loop and the
maintenance cadence.

## Tools

| Tool | Purpose |
|---|---|
| `get_status` | Mode, limits, kill switch, today's counters, pending proposals |
| `get_portfolio` | Balances, positions, avg cost, unrealized P&L |
| `get_market_data` | Price, 24h change, volume |
| `get_technical_analysis` | The agent's own OHLCV read: score, regime, EMA/RSI/momentum/volume |
| `submit_prediction` | rise/fall + confidence + horizon + expected move % + thesis → refusal or sized proposal |
| `confirm_decision` / `reject_decision` | The analyst's mandatory double-check |
| `close_position` | Risk-reducing exit (bypasses cap/cooldown, honors kill switch) |
| `challenge_trade` | Unwind a recent BUY the analyst concludes was wrong |
| `run_maintenance` | Stop-loss sweep, proposal expiry, breaker re-check |
| `set_risk_mode` | conservative / moderate / aggressive |
| `set_trading_enabled` | Kill switch |
| `get_trade_log` / `get_predictions` | Full journal, including refusals and why |

## What the research says (see RESEARCH.md)

The strategy layer was built from a literature-and-field survey (RESEARCH.md,
with sources): trend/momentum at 1–4 week horizons is the best-documented
signal class in crypto; RSI works in the *momentum* direction there, not the
classic contrarian reading; volume confirms only in normal regimes; sentiment
predicts volatility more than returns, decays in hours, and needs price/volume
confirmation; fees are the binding constraint at retail; the only live
real-money LLM trading test lost heavily with fees dominating P&L; and most
retail bots fail to beat buy-and-hold. Hence every one of this agent's layers:
fee gate, technical cross-check that only ever de-risks, volatility-aware
sizing, wide stops, a 28-day max hold, cooldowns against overtrading, a
daily-loss breaker, and the analyst double-check.

## Honest disclaimers

- **You can lose the money in the account.** No prediction system — human,
  LLM, or technical — reliably beats crypto markets. Sentiment-driven trading
  is high-risk; most bots underperform simply holding.
- Fees are real: at ~0.6% taker per side, round-trips cost ~1.2%. Overtrading
  bleeds accounts, which is why cooldowns and daily caps exist.
- This is not financial advice, and past performance of any strategy is no
  guarantee of anything. Start in paper mode, then small, conservative, and
  with money you can afford to lose.
- Taxes: every sell is a taxable event in most jurisdictions; the SQLite
  journal (`~/.coinbase-trading-agent/`) is your record.
