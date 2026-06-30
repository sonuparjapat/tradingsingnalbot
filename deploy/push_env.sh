#!/bin/bash
# Run this ONLY when you've changed .env locally (e.g. LOT_SIZE, rotated a
# password/secret) and need that change reflected on the server.
#
# Usage: bash deploy/push_env.sh

set -e
KEY="deploy/ssh-key-2026-06-30.key"
SERVER="ubuntu@92.4.94.88"
REMOTE_DIR="~/tradingsignalbot"

echo "=== Uploading .env ==="
scp -i "$KEY" .env "$SERVER:$REMOTE_DIR/.env"

echo "=== Restarting bot to pick up new values ==="
ssh -i "$KEY" "$SERVER" "sudo systemctl restart nifty-bot"

echo "=== Waiting for startup ==="
sleep 8

echo "=== Recent log output ==="
ssh -i "$KEY" "$SERVER" "tail -20 ~/tradingsignalbot/bot.log"

echo ""
echo "=== Done. Check Telegram for the startup message to confirm. ==="
