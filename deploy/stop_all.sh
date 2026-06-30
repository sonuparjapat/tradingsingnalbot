#!/bin/bash
# Stop ALL THREE bots together. Services stay installed (enabled) — they just
# won't auto-start again until you run start_all.sh or start them individually.
# Usage: bash deploy/stop_all.sh
ssh -i deploy/ssh-key-2026-06-30.key ubuntu@92.4.94.88 "
  sudo systemctl stop nifty-bot
  sudo systemctl stop sensex-bot
  sudo systemctl stop morning-scanner
  echo '=== Status after stop ==='
  sudo systemctl is-active nifty-bot sensex-bot morning-scanner || true
"
