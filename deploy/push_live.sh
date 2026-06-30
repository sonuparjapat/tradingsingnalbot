#!/bin/bash
# Run this from your project folder (Git Bash / Bash tool) whenever you change
# nifty_zerodha_bot.py or nifty_zerodha_backtest.py and want the server updated.
#
# Usage: bash deploy/push_live.sh

set -e
KEY="deploy/ssh-key-2026-06-30.key"
SERVER="ubuntu@92.4.94.88"
REMOTE_DIR="~/tradingsignalbot"

echo "=== Uploading updated files ==="
scp -i "$KEY" nifty_zerodha_bot.py nifty_zerodha_backtest.py "$SERVER:$REMOTE_DIR/"

echo "=== Restarting bot service ==="
ssh -i "$KEY" "$SERVER" "sudo systemctl restart nifty-bot"

echo "=== Waiting for startup ==="
sleep 8

echo "=== Recent log output ==="
ssh -i "$KEY" "$SERVER" "tail -20 ~/tradingsignalbot/bot.log"

echo ""
echo "=== Done. Check Telegram for the startup message to confirm. ==="
