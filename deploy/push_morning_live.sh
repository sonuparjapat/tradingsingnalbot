#!/bin/bash
# Run this whenever you change nifty_morning_scanner.py and want the server updated.
# Usage: bash deploy/push_morning_live.sh

set -e
KEY="deploy/ssh-key-2026-06-30.key"
SERVER="ubuntu@92.4.94.88"
REMOTE_DIR="~/tradingsignalbot"

echo "=== Uploading updated morning scanner + backtest ==="
scp -i "$KEY" nifty_morning_scanner.py nifty_morning_backtest.py "$SERVER:$REMOTE_DIR/"

echo "=== Restarting morning-scanner service ==="
ssh -i "$KEY" "$SERVER" "sudo systemctl restart morning-scanner"

echo "=== Waiting for startup ==="
sleep 8

echo "=== Recent log output ==="
ssh -i "$KEY" "$SERVER" "tail -20 ~/tradingsignalbot/morning_scanner.log"

echo ""
echo "=== Done. Check Telegram for the startup message to confirm. ==="
