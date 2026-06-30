#!/bin/bash
# Live-stream the morning scanner's logs. Ctrl+C to stop watching (does NOT stop it).
# Usage: bash deploy/morning_logs.sh
ssh -i deploy/ssh-key-2026-06-30.key ubuntu@92.4.94.88 "tail -f ~/tradingsignalbot/morning_scanner.log"
