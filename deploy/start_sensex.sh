#!/bin/bash
# Start (or restart if already running) just the SENSEX bot.
# Usage: bash deploy/start_sensex.sh
ssh -i deploy/ssh-key-2026-06-30.key ubuntu@92.4.94.88 "sudo systemctl start sensex-bot && sleep 5 && sudo systemctl status sensex-bot --no-pager"
