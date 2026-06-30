#!/bin/bash
# Live-stream the SENSEX bot's logs. Ctrl+C to stop watching (does NOT stop the bot).
# Usage: bash deploy/sensex_logs.sh
ssh -i deploy/ssh-key-2026-06-30.key ubuntu@92.4.94.88 "tail -f ~/tradingsignalbot/sensex_bot.log"
