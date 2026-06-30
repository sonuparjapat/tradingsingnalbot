#!/bin/bash
# Manually restart the bot on the server (no code upload, just a restart).
# Usage: bash deploy/restart.sh
ssh -i deploy/ssh-key-2026-06-30.key ubuntu@92.4.94.88 "sudo systemctl restart nifty-bot && sleep 8 && tail -20 ~/tradingsignalbot/bot.log"
