# RESEARCH.md — What the Evidence Actually Says About Crypto Trading

Synthesis of five research angles (academic literature, real bot performance, technical signals, sentiment/LLM signals, risk management) for a **spot-only, long-only Coinbase agent** (no shorting, no leverage, ~0.6% taker fee, hours-to-weeks horizon) that cross-checks LLM-analyst rise/fall predictions against its own OHLCV-derived technical signals.

---

## 1. The Ground Truth: Fees and the Null Hypothesis

Two facts dominate everything else in this document:

1. **A round trip at 0.6% taker costs ~1.2% before spread/slippage (realistically 1.3–1.5%).** Any trade whose expected gross edge is below ~1.5% is negative-EV before it starts. At one round trip per day this is 300%+ annualized fee drag on turnover. Fee minimization (maker orders at 0.4%, low turnover, liquid majors only) is the most reliable "alpha" available at retail.
2. **Buy-and-hold / DCA is the null hypothesis.** No independent, audited evidence shows any retail bot platform's user base beating it net of fees over a full cycle. BTC itself had a weekly Sharpe of ~0.23 (2011–2018) — better than equities. BIS estimates ~73–81% of retail Bitcoin app users lost money (Bulletin 69). The burden of proof is on the agent.

---

## 2. Strategy Classes: What Works, What Doesn't

### Works (with strong caveats)
- **Time-series momentum / trend-following at 1–4 week horizons, on large liquid coins.** Liu & Tsyvinski (RFS 2021): one-SD weekly return increase predicted +3.2–3.7% at 1–3 week horizons (pre-2018 sample). Momentum is the only characteristic (with size) robust across ~20,000 research designs (Fieberg et al., IRFA 2024), and it is concentrated in **large** coins (4.2%/week above-median size vs insignificant below-median).
- **Crucial horizon structure: continuation holds only ~2–4 weeks, then flips to significant reversal beyond ~1 month** (Dobrynskaya; Tzouvanas; Grobys & Sapkota). Crypto momentum runs ~10x faster than equity momentum. Never hold a momentum position to a monthly horizon.
- **Daily trading-range (Donchian-style) breakout** is the single-asset daily rule with the best peer-reviewed support (Gerritsen et al., FRL 2020 — the only one of seven trend indicators to consistently beat buy-and-hold on bootstrapped Sharpe). Price-to-MA ratios (5–100 day) also forecast BTC returns in and out of sample (Detzel et al., Financial Management 2021).
- **Indicator aggregation beats single indicators.** The JFQA 2025 CTREND factor (28 indicators, weekly, cross-sectional) survives 30–60bp per-leg costs (breakeven 1.41%), a 1-day lag, and 55k design variations. Single or paired indicators do not beat buy-and-hold at scale (Jin et al. 2024).
- **Risk-managed (vol-scaled) momentum** is the variant that survives the 26–53% post-cost alpha haircut.

### Doesn't work / can't be used here
- **Short-term (daily) mean reversion**: real cross-sectional pattern, but it is liquidity-provision compensation concentrated in small illiquid coins — a taker-fee implementation hands the profits back in costs.
- **Contrarian RSI/oscillator timing**: the rigorous evidence says the informative direction of RSI in crypto is *momentum* (high RSI → higher future returns; RSI H-L 3.52%/week, t=5.41, JFQA 2025). Classic overbought/oversold usage is unsupported; RSI divergences ineffective.
- **Calendar seasonality**: the best-powered study (Baur et al. 2019, 15M+ obs) finds no persistent day-of-week/time-of-day return patterns; the early Monday effect decayed post-publication.
- **Intraday TA**: 1-minute MA evidence is entirely pre-cost; intraday breakout buy signals were value-*destroying* (Corbet et al. 2019). Ruled out at 0.6% taker regardless.
- **Carry / arbitrage / market-making**: the measured edges (kimchi premium >$1B, funding carry ~7% p.a.) sit behind barriers retail can't cross (capital controls, cross-margining, fee tiers, latency). Not accessible spot-only anyway.
- **Long-short academic alphas**: 2.5–4%/week headline numbers are gross, in-sample, 2014–2018, and draw much of their alpha from short legs we cannot take. A long-only, taker-fee implementation captures a fraction at best.

### The sharpest negative results
- Hudson & Urquhart (Annals of OR): ~15,000 rules — significant in-sample, but **BTC showed no predictability out-of-sample**; the durable benefit of TA was drawdown reduction, not higher returns. The most liquid coin lost its edge first.
- Anghel (FRL 2021): across 861 coins, with a proper White reality check, neither classic TA nor ML rules generally earn abnormal returns.
- Post-publication decay is real: the crypto size effect is already gone out-of-sample; anomaly alpha is bull-market-concentrated and fades.

---

## 3. Bots: Measured Track Record

- **No commercial bot platform publishes audited performance.** Platform "bot profit" metrics exclude unrealized inventory losses and systematically overstate results.
- **Grid bots** underperform holding in uptrends (vendor Pionex admits this; independent test: +12.25% vs +39.9% HODL in 29 days) and hold uncapped depreciating bags in downtrends. Only viable in confirmed ranges with hard break exits.
- **Martingale/DCA safety-order bots**: many small wins, then catastrophic loss in sustained downtrends (LUNA wipeouts documented). Win rates are uninformative about EV.
- **Freqtrade's own maintainers** document a systematic backtest-to-live gap: +3% backtests "frequently produce negative results live" (lookahead bias, repainting, unmodeled slippage).
- **LLM trading agents, live**: Alpha Arena S1 (real money, Oct–Nov 2025) — 4 of 6 frontier models lost 31–63% in two weeks; win rates 25–30%; **fees dominated PnL** (13–17% of capital consumed in fees). Academic backtests agree: FINSABER and StockBench show reported LLM alpha evaporates over longer/broader evaluation; CryptoTrade (EMNLP 2024) beat time-series ML baselines but **not simple traditional signals**.
- **Operational risk is first-order**: the largest measured retail bot losses came from the 3Commas API-key breach ($14.8–22M) and cloud-bot outages during volatility — not from strategy. Use withdrawal-disabled API keys and IP allowlists.

**Implication for this agent:** the LLM analyst's rise/fall calls must be treated as a *text-interpretation input to be gated by technical and risk layers*, never as an autonomous numeric forecaster. That is exactly the failure mode measured live.

---

## 4. Technical Signals With Evidence (for the composite score)

| Signal | Evidence | Use |
|---|---|---|
| Price vs MA (20d fast anchor, 50–200d slow filter) | Strong (Detzel et al.; Grobys 20d VMA) | Primary trend gate |
| Donchian breakout (daily, ~20-day channel) | Moderate-strong (Gerritsen; practitioner 15–20d optimum) | Entry trigger |
| 1–4 week momentum (ROC) | Strong (RFS/JF; robust across 20k designs) | Directional score; exit by ~4 weeks |
| RSI(14) as *momentum-direction* feature | Strong cross-sectionally (JFQA 2025) | Score in momentum direction; never contrarian standalone |
| Volume confirmation | Conditional (Balcilar: predictive only in mid-quantile "normal" regimes) | Confirmation/veto only; downweight in extreme regimes |
| Realized-vol regime (MSGARCH or vol-percentile) | Strong (Ardia et al.) | Sizing/regime layer, not direction |
| MACD, Bollinger, CCI standalone | Weak standalone; valuable only in aggregation | Minor ensemble features |

Wider/adaptive entry bands (well above the textbook 0–1%) materially improve net-of-cost MA performance by cutting whipsaw churn — important at our fee level.

---

## 5. Sentiment and LLM Signals: Worth Exactly This Much

- **Attention/volume metrics (tweet counts, Google Trends, Reddit activity) predict volatility and volume, generally NOT returns.** Use them as regime/vol forecasters and bubble detectors, not directional signals.
- **Sentiment has short-horizon (~1–2 day), coin-specific return predictability** (BTC/BCH/LTC; Kraaijeveld & De Smedt), and crypto-Twitter sentiment stays positive through crashes — tweet *volume* often beats sentiment directionally.
- **Every strong published result combines sentiment with market confirmation** (Garcia & Schweitzer: polarization + exchange volume). Sentiment without a concurrent price/volume response is noise or manipulation (1–14% bot content is a lower bound; manipulation-as-a-service is SEC-documented; the fake SEC ETF tweet is the canonical spoof).
- **Event signals decay in minutes-to-hours** (Musk tweets: +3.6% in 2 minutes, peak ~60 min). By the time an hours-cadence agent sees a viral event, the alpha is gone — do not chase.
- **Fear & Greed Index**: best long-sample evidence says causality runs from returns → index. Contrarian context at extremes (<25/>75) only, with confirmation; extremes persist for weeks in strong trends.
- **LLMs are mediocre numeric forecasters** (tokenization/calibration issues; memorization leakage makes published LLM-strategy backtests suspect). Their measured value is as **text processors**: news classification, event typing, fact-vs-opinion separation.

---

## 6. Risk Management: The Strongest Evidence in This Document

The sizing and exit layers have better evidence than any entry signal.

- **Volatility targeting** reliably cuts tail risk and drawdowns (Harvey et al., 60+ assets, 1926–2017) and specifically helps momentum strategies (~doubles momentum Sharpe). Caveat: real-time versions often fail to *raise* returns (Cederburg et al. JFE 2020) — justify it as risk control, treat Sharpe gains as a bonus.
- **Fractional Kelly (1/4–1/2) as an exposure cap**, never full Kelly (full Kelly: ~1/2 probability of ever halving capital; half-Kelly keeps ~75% of growth at ~half the drawdown).
- **Fixed-fraction per-trade risk (0.5–1% of equity) with ATR-scaled unit sizing** is the robust practitioner standard.
- **Stops follow the Kaminski–Lo rule**: under a random walk stops lower expected return; they add value only under momentum/regime-switching. So: stops belong on momentum/trend entries — where they measurably tame crashes (equity momentum worst month −49.8% → −11.4%, Sharpe doubled; crypto: 10–30% stops turned momentum from crash-prone to +9.1%/month, Sadaqat & Butt 2023). Make them **wide and volatility-scaled (2–3× ATR)**; tight fixed stops destroy value. For non-trend trades, prefer **time-based exits** — one of the few exit mechanisms that consistently adds value in large-scale exit testing.
- **Drawdown circuit breakers**: no evidence they enhance returns; clear logic that they bound ruin from regime change, strategy breakage, or bugs. Two-stage (halve at −10%, flat + cool-off at −15–20%) with an explicit restart rule.
- **Overtrading is the best-documented retail killer** (Barber & Odean; Taiwan: <1% of day traders durably profitable). Trade frequency is a cost.

---

## 7. Sources

**Strong (peer-reviewed, top journals, or measured live data)**
- Liu & Tsyvinski (2021), RFS 34(6) — BTC time-series momentum, attention. NBER WP 24877.
- Liu, Tsyvinski & Wu (2022), Journal of Finance 77(2) — crypto 3-factor model. NBER WP 25882.
- Fieberg et al. (2025), JFQA 60(7) — CTREND trend factor, doi:10.1017/S0022109024000747.
- Fieberg et al. (2024), IRFA 92 — non-standard errors, 20,736 designs.
- Detzel et al. (2021), Financial Management — MA predictability for BTC.
- Hudson & Urquhart (2019/2021), Annals of Operations Research — 15k rules, BTC OOS failure.
- Anghel (2021), FRL 39 — reality check on 861 coins.
- Makarov & Schoar (2020), JFE — crypto arbitrage.
- Schmeling, Schrimpf & Todorov, BIS WP 1087 — crypto carry.
- Bianchi, Babiak & Dickerson (2022), JBF — reversal as liquidity provision; 30/40bp cost convention.
- Kaminski & Lo (2014), J. Financial Markets — when stops stop losses.
- Han, Zhou & Zhu (SSRN 2407199) — momentum stop-loss.
- Harvey et al. (2018), JPM — vol targeting; Cederburg et al. (2020), JFE — its OOS limits.
- Barber & Odean (2000), JF; Barber, Lee, Liu & Odean (Taiwan) — retail overtrading.
- BIS Bulletin 69 — retail crypto losses.
- Kraaijeveld & De Smedt (2020), JIFMIM; Shen, Urquhart & Wang (2019), Econ. Letters; Garcia & Schweitzer (2015), RSOS; Ante (2023), TFSC — sentiment.
- Alpha Arena S1 (nof1, live real-money LLM test); FINSABER (arXiv:2505.07078, KDD 2026); StockBench (arXiv:2510.02209); CryptoTrade (EMNLP 2024).
- Ardia, Bluteau & Ruede (2019), FRL — MSGARCH regimes.
- 3Commas breach coverage (BleepingComputer, CoinDesk, Blockworks).

**Moderate (field journals, single studies, mixed replication)**
- Gerritsen et al. (2020), FRL — Donchian breakout. Grobys et al. (2020), FRL — 20d VMA. Grobys & Sapkota (2019), Econ. Letters — no monthly momentum. Dobrynskaya (SSRN 3913263) — momentum→reversal. Sadaqat & Butt (2023), JBEF — crypto stops. Risk-managed momentum (FRL 2025). Transaction-costs-and-bubbles (JIFMIM 2022). Balcilar et al. (2017) — volume quantiles. Cavalheiro (2024) vs 2026 VAR study — F&G contested. Pionex admission + user grid tests. Freqtrade maintainer testimony.

**Weak (practitioner, vendor, anecdotal — engineering guidance only)**
- Donchian/ADX parameterizations (QuantifiedStrategies, LuxAlgo); KJ Trading exit study; sentiment-decay composites; Cryptohopper Trustpilot reviews; Jia (2022) grid backtest (overfit); Hummingbot vendor simulations; Wilinski RSI study (MDPI Sensors — non-finance venue).

**Honesty note:** most academic samples end 2018–2022, are dominated by the 2017/2021 bulls, and report gross long-short returns. Expect live, long-only, post-cost performance to be a small fraction of headline numbers — and expect edges to decay fastest on BTC.
---

## Appendix: How this maps to the implementation

| Research finding | Where it lives in the code |
|---|---|
| Fee drag is the binding constraint (~1.2–1.5% round trip) | `submit_prediction` requires `expected_move_pct`; RISE predictions under `FEE_GATE_PCT` (default 1.5%) are recorded but never traded |
| LLM-alone trading loses money live (Alpha Arena, FINSABER) | The analyst's prediction is a gated input: technical cross-check + risk engine + mandatory analyst double-check before execution |
| Trend/MA filters are the best-evidenced signal family | Trend gate is the largest component (0.40) of the composite technical score |
| Momentum robust at 1–4 weeks, reverses beyond ~1 month | Momentum component blends ~3d/7d lookbacks (0.30); `run_maintenance` force-exits any position held > 28 days |
| RSI is a momentum signal in crypto, not contrarian | RSI(14) scored in the momentum direction (0.15); no overbought veto; RSI ≥ 85 emits a blow-off warning only |
| Volume predicts only in normal regimes | Volume confirmation (0.15) is disabled entirely in extreme-volatility regimes |
| Vol targeting reduces tail risk (not necessarily returns) | High-volatility regime halves position size; sizing scales with confidence but is capped by mode + hard dollar cap |
| Tight stops destroy value; wide vol-aware stops help momentum | Stops are 10/15/20% by mode (floor from the research), enforced by `run_maintenance` |
| Overtrading is the best-documented retail killer | Per-coin cooldowns, daily trade caps, and the fee gate |
| Drawdown breakers bound ruin (not returns) | Daily-loss circuit breaker disables trading until manual review |
| Operational risk is first-order (3Commas breach) | Trade-only API key enforced at startup; no fund-movement code paths; whitelist + hard caps re-checked at order time |

The composite-score weights and thresholds (0.40 trend / 0.30 momentum / 0.15 RSI / 0.15 volume; 0.35 agreement threshold) follow the synthesis's recommended spec, adapted from daily to hourly candles for an hours-to-weeks agent. Numbers marked provisional in the sources above should be re-estimated against the agent's own logged hit rates.
