#!/bin/bash
# Run ONCE on the server to install and enable the evening-scanner service.
# After this, use push_evening_live.sh for all future deploys.
#
# Usage: bash deploy/install_evening_service.sh

set -e
KEY="deploy/ssh-key-2026-06-30.key"
SERVER="ubuntu@92.4.94.88"
REMOTE_DIR="~/tradingsignalbot"

echo "=== Uploading files ==="
scp -i "$KEY" nifty_evening_scanner.py nifty_evening_backtest.py "$SERVER:$REMOTE_DIR/"
scp -i "$KEY" deploy/evening-scanner.service "$SERVER:/tmp/"

echo "=== Installing systemd service ==="
ssh -i "$KEY" "$SERVER" "
  sudo cp /tmp/evening-scanner.service /etc/systemd/system/
  sudo systemctl daemon-reload
  sudo systemctl enable evening-scanner
  sudo systemctl start evening-scanner
  sleep 5
  sudo systemctl status evening-scanner --no-pager
"

echo ""
echo "=== Service installed and started. Check Telegram for the startup message. ==="
