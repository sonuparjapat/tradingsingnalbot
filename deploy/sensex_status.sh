#!/bin/bash
# Quick health check for the SENSEX bot specifically.
# Usage: bash deploy/sensex_status.sh
ssh -i deploy/ssh-key-2026-06-30.key ubuntu@92.4.94.88 "sudo systemctl status sensex-bot --no-pager; echo; echo '=== Last 15 log lines ==='; tail -15 ~/tradingsignalbot/sensex_bot.log"
