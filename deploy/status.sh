#!/bin/bash
# Quick health check: is the bot running, how long, any recent activity.
# Usage: bash deploy/status.sh
ssh -i deploy/ssh-key-2026-06-30.key ubuntu@92.4.94.88 "sudo systemctl status nifty-bot --no-pager; echo; echo '=== Last 15 log lines ==='; tail -15 ~/tradingsignalbot/bot.log"
