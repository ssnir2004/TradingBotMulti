#!/usr/bin/env bash
# Launches ONE user's live-mode IB Gateway headlessly via IBC + Xvfb
# (Gateway needs a display even when nobody's watching it). Called by the
# instantiated deploy/ibgateway-live@.service, $1=user_id (%i in the unit
# file). Live-only (unlike TradingBot's own version of this script) - see
# docs/architecture.md: this system never runs paper trading.
#
# Each user is their own IB Gateway process - needs its own settings dir
# (so login sessions never clobber each other), its own log dir, and its
# own copy of IBC's gatewaystart.sh (IBC's gatewaystart.sh does not read
# environment variables - it has a block of `VAR=value` assignments at the
# top meant to be edited directly; instances patching the SAME physical
# file at once would race each other, so each gets its own copy).
set -euo pipefail

USER_ID="${1:-}"
if [ -z "$USER_ID" ]; then
    echo "Usage: $0 <user_id>" >&2
    exit 1
fi

IBC_PATH="/opt/ibc"
TWS_PATH="/opt/ibgateway"
TWS_MAJOR_VRSN="1045"          # from `IB Gateway 10.45`
IBC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

SUFFIX="${USER_ID}-live"
IBC_INI="${IBC_DIR}/config-${SUFFIX}.ini"
TWS_SETTINGS_PATH="/home/tradingbotmulti/ibgateway-settings-${SUFFIX}"
LOG_PATH="/home/tradingbotmulti/ibc-logs-${SUFFIX}"
GATEWAYSTART_TEMPLATE="$IBC_PATH/gatewaystart.sh"
GATEWAYSTART="$IBC_PATH/gatewaystart-${SUFFIX}.sh"

if [ ! -f "$IBC_INI" ]; then
    echo "Missing $IBC_INI - this user hasn't saved IBKR credentials and clicked Connect from the dashboard yet." >&2
    exit 1
fi

if [ ! -f "$GATEWAYSTART_TEMPLATE" ]; then
    echo "$GATEWAYSTART_TEMPLATE not found - check your IBC install path (IBC_PATH=$IBC_PATH)." >&2
    exit 1
fi

mkdir -p "$TWS_SETTINGS_PATH" "$LOG_PATH"
cp "$GATEWAYSTART_TEMPLATE" "$GATEWAYSTART"

sed -i \
    -e "s|^TWS_MAJOR_VRSN=.*|TWS_MAJOR_VRSN=${TWS_MAJOR_VRSN}|" \
    -e "s|^IBC_INI=.*|IBC_INI=${IBC_INI}|" \
    -e "s|^TRADING_MODE=.*|TRADING_MODE=live|" \
    -e "s|^IBC_PATH=.*|IBC_PATH=${IBC_PATH}|" \
    -e "s|^TWS_PATH=.*|TWS_PATH=${TWS_PATH}|" \
    -e "s|^TWS_SETTINGS_PATH=.*|TWS_SETTINGS_PATH=${TWS_SETTINGS_PATH}|" \
    -e "s|^LOG_PATH=.*|LOG_PATH=${LOG_PATH}|" \
    "$GATEWAYSTART"

exec xvfb-run --auto-servernum --server-args="-screen 0 1024x768x24" \
    "$GATEWAYSTART" -inline
