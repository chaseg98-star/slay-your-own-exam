#!/usr/bin/env bash
# One-command setup for the crypto trading agent on macOS.
#
#   bash setup-mac.sh
#
# Does everything automatable: updates the code, builds the Python
# environment, generates your Robinhood API keypair, writes the Claude
# Desktop config, and (optionally) installs the 30-minute background
# safety monitor. The only steps left for you are the ones only you can
# do: registering the public key inside your Robinhood account and
# pasting back the API key Robinhood issues.

set -euo pipefail

AGENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$AGENT_DIR")"
CLAUDE_CONFIG="$HOME/Library/Application Support/Claude/claude_desktop_config.json"

echo "==> Crypto trading agent setup"

# --- 1. Python 3.10+ ---------------------------------------------------------
PY=""
for cand in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$cand" >/dev/null 2>&1 \
     && "$cand" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
    PY="$cand"
    break
  fi
done
if [ -z "$PY" ]; then
  echo ""
  echo "Your Mac only has Apple's outdated Python. Install a current one first:"
  echo "  1. Open https://www.python.org/downloads/  and click the download button"
  echo "  2. Run the installer (all defaults are fine)"
  echo "  3. Close and reopen Terminal, then run this script again"
  exit 1
fi
echo "==> Using $($PY --version 2>&1)"

# --- 2. Latest code ----------------------------------------------------------
if git -C "$REPO_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "==> Updating code"
  git -C "$REPO_DIR" fetch origin >/dev/null 2>&1 || true
  git -C "$REPO_DIR" pull --ff-only >/dev/null 2>&1 || true
fi

# --- 3. Environment ----------------------------------------------------------
cd "$AGENT_DIR"
if [ ! -x .venv/bin/python ] || ! .venv/bin/python -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
  rm -rf .venv
  "$PY" -m venv .venv
fi
echo "==> Installing the agent (takes a minute)"
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -e .
BIN="$AGENT_DIR/.venv/bin/coinbase-trading-agent"
echo "==> Agent installed: $BIN"

# --- 4. Robinhood credentials ------------------------------------------------
echo ""
read -r -p "Set up LIVE Robinhood trading now? (n = paper mode, no keys needed) [y/N] " LIVE
LIVE=$(echo "${LIVE:-n}" | tr '[:upper:]' '[:lower:]')

RH_KEY=""
RH_PRIV=""
if [ "$LIVE" = "y" ]; then
  KEYS=$(.venv/bin/python - <<'PY'
import base64
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
k = Ed25519PrivateKey.generate()
priv = base64.b64encode(k.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())).decode()
pub = base64.b64encode(k.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
print(pub)
print(priv)
PY
)
  RH_PUB=$(printf '%s\n' "$KEYS" | sed -n 1p)
  RH_PRIV=$(printf '%s\n' "$KEYS" | sed -n 2p)

  echo ""
  echo "Your keypair is generated. The PRIVATE key stays in this computer's config"
  echo "only — this script handles it for you, so you never need to copy it."
  echo ""
  echo "Now the one part only you can do:"
  echo "  1. Open Robinhood -> Account -> Settings -> Crypto -> API trading"
  echo "  2. Add an API key and paste in this PUBLIC key:"
  echo ""
  echo "     $RH_PUB"
  echo ""
  echo "  3. Allow read + trading, confirm with your 2FA"
  echo "  4. Robinhood shows you an API key ID — paste it below"
  echo ""
  read -r -p "API key from Robinhood: " RH_KEY
  if [ -z "$RH_KEY" ]; then
    echo "No API key entered; falling back to paper mode. Rerun the script to try again."
    LIVE="n"
  fi
fi

# --- 5. Risk profile ---------------------------------------------------------
read -r -p "Portfolio floor in USD (liquidate everything + halt at this value) [30]: " FLOOR
read -r -p "Max single trade in USD [15]: " MAXT
read -r -p "Risk mode (conservative/moderate/aggressive) [moderate]: " MODE
FLOOR="${FLOOR:-30}"
MAXT="${MAXT:-15}"
MODE=$(echo "${MODE:-moderate}" | tr '[:upper:]' '[:lower:]')

# --- 6. Claude Desktop config ------------------------------------------------
echo "==> Writing Claude Desktop config"
mkdir -p "$(dirname "$CLAUDE_CONFIG")"
SRV_BIN="$BIN" LIVE="$LIVE" RH_KEY="$RH_KEY" RH_PRIV="$RH_PRIV" \
FLOOR="$FLOOR" MAXT="$MAXT" MODE="$MODE" CFG="$CLAUDE_CONFIG" \
.venv/bin/python - <<'PY'
import json, os, shutil

cfg_path = os.environ["CFG"]
config = {}
if os.path.exists(cfg_path):
    shutil.copy(cfg_path, cfg_path + ".backup")
    with open(cfg_path) as f:
        try:
            config = json.load(f)
        except Exception:
            config = {}

live = os.environ["LIVE"] == "y"
env = {
    "TRADING_MODE": "live" if live else "paper",
    "DEFAULT_RISK_MODE": os.environ["MODE"],
    "MIN_TRADE_USD": "2",
    "MAX_TRADE_USD": os.environ["MAXT"],
    "PORTFOLIO_FLOOR_USD": os.environ["FLOOR"],
}
if live:
    env.update({
        "EXCHANGE": "robinhood",
        "ROBINHOOD_API_KEY": os.environ["RH_KEY"],
        "ROBINHOOD_PRIVATE_KEY": os.environ["RH_PRIV"],
    })

config.setdefault("mcpServers", {})["crypto-trader"] = {
    "command": os.environ["SRV_BIN"],
    "env": env,
}
with open(cfg_path, "w") as f:
    json.dump(config, f, indent=2)
print(f"    wrote {cfg_path}" + (" (backup saved)" if os.path.exists(cfg_path + ".backup") else ""))
PY

# --- 7. Background safety monitor -------------------------------------------
if [ "$LIVE" = "y" ] && command -v crontab >/dev/null 2>&1; then
  read -r -p "Install the 30-minute background safety monitor (recommended)? [Y/n] " CRON
  CRON=$(echo "${CRON:-y}" | tr '[:upper:]' '[:lower:]')
  if [ "$CRON" = "y" ]; then
    LINE="*/30 * * * * TRADING_MODE=live EXCHANGE=robinhood ROBINHOOD_API_KEY=\"$RH_KEY\" ROBINHOOD_PRIVATE_KEY=\"$RH_PRIV\" DEFAULT_RISK_MODE=$MODE MIN_TRADE_USD=2 MAX_TRADE_USD=$MAXT PORTFOLIO_FLOOR_USD=$FLOOR \"$BIN\" --monitor >> \"$HOME/crypto-trader-monitor.log\" 2>&1 # crypto-trader-monitor"
    ( crontab -l 2>/dev/null | grep -v 'crypto-trader-monitor' ; echo "$LINE" ) | crontab -
    echo "==> Monitor installed (log: ~/crypto-trader-monitor.log)"
  fi
fi

# --- Done --------------------------------------------------------------------
echo ""
echo "==> Setup complete."
echo "    1. Restart Claude Desktop (quit fully, reopen)"
echo "    2. Paste analyst-prompt.md into your Claude project"
echo "    3. Say: 'Run get_status and run_maintenance and give me a report'"
if [ "$LIVE" != "y" ]; then
  echo ""
  echo "    (Paper mode: simulated money against real prices. Rerun this script"
  echo "     any time to switch to live Robinhood trading.)"
fi
