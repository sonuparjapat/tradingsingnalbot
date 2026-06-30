#!/bin/bash
# Run this from your project folder whenever you change sensex_zerodha_bot.py
# or sensex_zerodha_backtest.py and want the server updated.
#
# Usage: bash deploy/push_sensex_live.sh

set -e
KEY="deploy/ssh-key-2026-06-30.key"
SERVER="ubuntu@92.4.94.88"
REMOTE_DIR="~/tradingsignalbot"

echo "=== Uploading updated SENSEX files ==="
scp -i "$KEY" sensex_zerodha_bot.py sensex_zerodha_backtest.py "$SERVER:$REMOTE_DIR/"

echo "=== Restarting sensex-bot service ==="
ssh -i "$KEY" "$SERVER" "sudo systemctl restart sensex-bot"

echo "=== Waiting for startup ==="
sleep 8

echo "=== Recent log output ==="
ssh -i "$KEY" "$SERVER" "tail -20 ~/tradingsignalbot/sensex_bot.log"

echo ""
echo "=== Done. Check Telegram for the startup message to confirm. ==="
