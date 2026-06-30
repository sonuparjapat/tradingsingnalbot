#!/bin/bash
# Show the last 50 lines of error log + current service status.
# Usage: bash deploy/errors.sh
ssh -i deploy/ssh-key-2026-06-30.key ubuntu@92.4.94.88 "echo '=== Service status ==='; sudo systemctl status nifty-bot --no-pager; echo; echo '=== Last 50 error lines ==='; tail -50 ~/tradingsignalbot/bot_error.log; echo; echo '=== Restart count ==='; systemctl show nifty-bot -p NRestarts"
