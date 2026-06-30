#!/bin/bash
# Show the last 50 lines of the SENSEX bot's error log + current service status.
# Usage: bash deploy/sensex_errors.sh
ssh -i deploy/ssh-key-2026-06-30.key ubuntu@92.4.94.88 "echo '=== Service status ==='; sudo systemctl status sensex-bot --no-pager; echo; echo '=== Last 50 error lines ==='; tail -50 ~/tradingsignalbot/sensex_bot_error.log; echo; echo '=== Restart count ==='; systemctl show sensex-bot -p NRestarts"
