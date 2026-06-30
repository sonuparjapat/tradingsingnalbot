#!/bin/bash
# One-time + repeatable: install the sensex-bot and morning-scanner systemd
# services (if not already installed) and start/restart ALL THREE bots
# together in one go: nifty-bot, sensex-bot, morning-scanner.
#
# Safe to re-run any time — installing a service is idempotent, and restarting
# nifty-bot just applies whatever is already deployed (no code changes pushed
# here — use push_live.sh / push_sensex_live.sh / push_morning_live.sh for that).
#
# Usage: bash deploy/start_all.sh

set -e
KEY="deploy/ssh-key-2026-06-30.key"
SERVER="ubuntu@92.4.94.88"
REMOTE_DIR="~/tradingsignalbot"

echo "=== Uploading all bot files + service files (in case not present) ==="
scp -i "$KEY" nifty_zerodha_bot.py nifty_zerodha_backtest.py \
               sensex_zerodha_bot.py sensex_zerodha_backtest.py \
               nifty_morning_scanner.py "$SERVER:$REMOTE_DIR/"
ssh -i "$KEY" "$SERVER" "mkdir -p $REMOTE_DIR/deploy"
scp -i "$KEY" deploy/sensex-bot.service deploy/morning-scanner.service "$SERVER:$REMOTE_DIR/deploy/"

echo "=== Installing sensex-bot + morning-scanner systemd services ==="
ssh -i "$KEY" "$SERVER" "
  sudo cp $REMOTE_DIR/deploy/sensex-bot.service /etc/systemd/system/sensex-bot.service
  sudo cp $REMOTE_DIR/deploy/morning-scanner.service /etc/systemd/system/morning-scanner.service
  sudo sed -i 's|/usr/bin/python3|/home/ubuntu/tradingsignalbot/venv/bin/python3|' /etc/systemd/system/sensex-bot.service
  sudo sed -i 's|/usr/bin/python3|/home/ubuntu/tradingsignalbot/venv/bin/python3|' /etc/systemd/system/morning-scanner.service
  sudo systemctl daemon-reload
  sudo systemctl enable sensex-bot.service
  sudo systemctl enable morning-scanner.service
"

echo "=== Starting ALL THREE bots together ==="
ssh -i "$KEY" "$SERVER" "
  sudo systemctl restart nifty-bot
  sudo systemctl restart sensex-bot
  sudo systemctl restart morning-scanner
"

echo "=== Waiting for startup ==="
sleep 8

echo "=== NIFTY bot — recent log ==="
ssh -i "$KEY" "$SERVER" "tail -15 ~/tradingsignalbot/bot.log"
echo ""
echo "=== SENSEX bot — recent log ==="
ssh -i "$KEY" "$SERVER" "tail -15 ~/tradingsignalbot/sensex_bot.log"
echo ""
echo "=== Morning scanner — recent log ==="
ssh -i "$KEY" "$SERVER" "tail -15 ~/tradingsignalbot/morning_scanner.log"

echo ""
echo "=== Done. Check Telegram for all three startup messages to confirm. ==="
