"""Configuration, loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .models import RiskMode

DEFAULT_WHITELIST = "BTC,ETH,SOL,XRP,ADA,DOGE,AVAX,LINK,LTC,DOT"


class ConfigError(RuntimeError):
    pass


def _normalize_whitelist(raw: str, quote_currency: str) -> tuple[str, ...]:
    products = []
    for entry in raw.split(","):
        entry = entry.strip().upper()
        if not entry:
            continue
        if "-" not in entry:
            entry = f"{entry}-{quote_currency}"
        elif entry.split("-", 1)[1] != quote_currency:
            # One quote currency per agent: a second quote (e.g. BTC-USDC next
            # to BTC-USD) would double-count the shared base balance.
            raise ConfigError(
                f"whitelist entry {entry!r} does not use the configured quote currency "
                f"{quote_currency}; set QUOTE_CURRENCY instead of mixing quotes"
            )
        products.append(entry)
    if not products:
        raise ConfigError("PRODUCT_WHITELIST is empty; the agent needs at least one tradable product")
    return tuple(dict.fromkeys(products))


@dataclass(frozen=True)
class Config:
    trading_mode: str  # "paper" | "live"
    exchange: str  # "coinbase" | "robinhood" (live mode execution venue)
    quote_currency: str
    product_whitelist: tuple[str, ...]
    max_trade_usd: float
    min_trade_usd: float
    paper_starting_usd: float
    paper_fee_rate: float
    data_dir: Path
    api_key_name: str | None
    api_private_key: str | None
    robinhood_api_key: str | None
    robinhood_private_key: str | None
    default_risk_mode: RiskMode
    # Hard capital floor: if total portfolio value falls to this, everything is
    # liquidated and trading halts. 0 disables the floor.
    portfolio_floor_usd: float
    # A held coin dropping this % in 1h (or 2x this in 24h) raises a
    # REVIEW REQUIRED alert for the analyst on every maintenance scan.
    shock_drop_pct: float
    # Two-phase execution: proposals must be confirmed by the analyst.
    require_confirmation: bool
    proposal_ttl_minutes: float
    challenge_window_hours: float
    # Fee gate: minimum expected move (%) for a trade to be worth its fees.
    # A taker round trip costs ~1.2-1.5% all-in, so the default demands 1.5%.
    fee_gate_pct: float
    # When true, the analyst cannot change risk mode or re-enable trading —
    # only the operator can, via env/restart. Off by default (the analyst is
    # designed to supervise autonomously); turn on for extra hardening.
    lock_risk_controls: bool

    @classmethod
    def from_env(cls, env: dict | None = None) -> "Config":
        env = dict(os.environ if env is None else env)

        trading_mode = env.get("TRADING_MODE", "paper").strip().lower()
        if trading_mode not in ("paper", "live"):
            raise ConfigError(f"TRADING_MODE must be 'paper' or 'live', got {trading_mode!r}")

        quote_currency = env.get("QUOTE_CURRENCY", "USD").strip().upper()
        if quote_currency not in ("USD", "USDC"):
            raise ConfigError(f"QUOTE_CURRENCY must be USD or USDC, got {quote_currency!r}")

        exchange = env.get("EXCHANGE", "coinbase").strip().lower()
        if exchange not in ("coinbase", "robinhood"):
            raise ConfigError(f"EXCHANGE must be 'coinbase' or 'robinhood', got {exchange!r}")
        if exchange == "robinhood" and quote_currency != "USD":
            raise ConfigError("Robinhood trades in USD buying power; set QUOTE_CURRENCY=USD")

        whitelist = _normalize_whitelist(env.get("PRODUCT_WHITELIST", DEFAULT_WHITELIST), quote_currency)

        api_key_name = env.get("COINBASE_API_KEY_NAME") or None
        api_private_key = env.get("COINBASE_API_PRIVATE_KEY") or None
        robinhood_api_key = env.get("ROBINHOOD_API_KEY") or None
        robinhood_private_key = env.get("ROBINHOOD_PRIVATE_KEY") or None
        if trading_mode == "live":
            if exchange == "coinbase" and not (api_key_name and api_private_key):
                raise ConfigError(
                    "TRADING_MODE=live with EXCHANGE=coinbase requires COINBASE_API_KEY_NAME and "
                    "COINBASE_API_PRIVATE_KEY. Create the key with View + Trade ONLY (never Transfer)."
                )
            if exchange == "robinhood" and not (robinhood_api_key and robinhood_private_key):
                raise ConfigError(
                    "TRADING_MODE=live with EXCHANGE=robinhood requires ROBINHOOD_API_KEY and "
                    "ROBINHOOD_PRIVATE_KEY (base64 Ed25519 seed) from the Robinhood API portal."
                )

        mode_raw = env.get("DEFAULT_RISK_MODE", "conservative").strip().lower()
        try:
            default_risk_mode = RiskMode(mode_raw)
        except ValueError:
            raise ConfigError(
                f"DEFAULT_RISK_MODE must be one of conservative/moderate/aggressive, got {mode_raw!r}"
            ) from None

        def _positive_float(name: str, default: str) -> float:
            try:
                value = float(env.get(name, default))
            except ValueError:
                raise ConfigError(f"{name} must be a number, got {env.get(name)!r}") from None
            if value <= 0:
                raise ConfigError(f"{name} must be positive, got {value}")
            return value

        max_trade_usd = _positive_float("MAX_TRADE_USD", "250")
        min_trade_usd = _positive_float("MIN_TRADE_USD", "5")
        if min_trade_usd > max_trade_usd:
            raise ConfigError("MIN_TRADE_USD cannot exceed MAX_TRADE_USD")

        paper_fee_rate = float(env.get("PAPER_FEE_RATE", "0.006"))
        if not 0 <= paper_fee_rate < 0.05:
            raise ConfigError(f"PAPER_FEE_RATE must be in [0, 0.05), got {paper_fee_rate}")

        data_dir = Path(env.get("AGENT_DATA_DIR", Path.home() / ".coinbase-trading-agent")).expanduser()

        def _flag(name: str, default: str) -> bool:
            return env.get(name, default).strip().lower() not in ("0", "false", "no")

        require_confirmation = _flag("REQUIRE_CONFIRMATION", "1")

        portfolio_floor_usd = float(env.get("PORTFOLIO_FLOOR_USD", "0"))
        if portfolio_floor_usd < 0:
            raise ConfigError("PORTFOLIO_FLOOR_USD cannot be negative")

        return cls(
            trading_mode=trading_mode,
            exchange=exchange,
            quote_currency=quote_currency,
            product_whitelist=whitelist,
            max_trade_usd=max_trade_usd,
            min_trade_usd=min_trade_usd,
            paper_starting_usd=_positive_float("PAPER_STARTING_USD", "10000"),
            paper_fee_rate=paper_fee_rate,
            data_dir=data_dir,
            api_key_name=api_key_name,
            api_private_key=api_private_key,
            robinhood_api_key=robinhood_api_key,
            robinhood_private_key=robinhood_private_key,
            default_risk_mode=default_risk_mode,
            portfolio_floor_usd=portfolio_floor_usd,
            shock_drop_pct=_positive_float("SHOCK_DROP_PCT", "8"),
            require_confirmation=require_confirmation,
            proposal_ttl_minutes=_positive_float("PROPOSAL_TTL_MINUTES", "15"),
            challenge_window_hours=_positive_float("CHALLENGE_WINDOW_HOURS", "24"),
            fee_gate_pct=_positive_float("FEE_GATE_PCT", "1.5"),
            lock_risk_controls=_flag("LOCK_RISK_CONTROLS", "0"),
        )
