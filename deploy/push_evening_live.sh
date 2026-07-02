#!/bin/bash
# Run this whenever you change nifty_evening_scanner.py and want the server updated.
# Usage: bash deploy/push_evening_live.sh

set -e
KEY="deploy/ssh-key-2026-06-30.key"
SERVER="ubuntu@92.4.94.88"
REMOTE_DIR="~/tradingsignalbot"

echo "=== Uploading evening scanner + backtest ==="
scp -i "$KEY" nifty_evening_scanner.py nifty_evening_backtest.py "$SERVER:$REMOTE_DIR/"

echo "=== Restarting evening-scanner service ==="
ssh -i "$KEY" "$SERVER" "sudo systemctl restart evening-scanner"

echo "=== Waiting for startup ==="
sleep 8

echo "=== Recent log output ==="
ssh -i "$KEY" "$SERVER" "tail -20 ~/tradingsignalbot/evening_scanner.log"

echo ""
echo "=== Done. Check Telegram for the startup message to confirm. ==="
