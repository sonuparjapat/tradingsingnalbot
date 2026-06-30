#!/bin/bash
# Start (or restart if already running) just the NIFTY bot.
# Usage: bash deploy/start_nifty.sh
ssh -i deploy/ssh-key-2026-06-30.key ubuntu@92.4.94.88 "sudo systemctl start nifty-bot && sleep 5 && sudo systemctl status nifty-bot --no-pager"
