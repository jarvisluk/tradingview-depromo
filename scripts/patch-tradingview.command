#!/bin/bash
# Double-click wrapper for the TradingView de-promo patcher.
# On macOS, double-clicking a .command file runs it in Terminal
# (a plain .sh usually just opens in a text editor instead).
#
#   double-click            -> in-place patch of /Applications/TradingView.app
#                              (quits the app first, relaunches after)
#   drag a .app onto it     -> patch that copy instead (--src)
#
# NOTE: no `set -u` here — macOS ships bash 3.2, where expanding an empty
# array under -u is a fatal "unbound variable" error.
set -eo pipefail
cd "$(dirname "$0")/.."

if [[ $# -gt 0 ]]; then
  exec python3 scripts/patch-tradingview.py --src "$1"
fi
exec python3 scripts/patch-tradingview.py
