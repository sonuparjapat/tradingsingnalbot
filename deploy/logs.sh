#!/bin/bash
# Live-stream the bot's logs. Ctrl+C to stop watching (does NOT stop the bot).
# Usage: bash deploy/logs.sh
ssh -i deploy/ssh-key-2026-06-30.key ubuntu@92.4.94.88 "tail -f ~/tradingsignalbot/bot.log"
