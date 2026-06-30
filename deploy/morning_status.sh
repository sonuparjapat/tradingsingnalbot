#!/bin/bash
# Quick health check for the morning scanner specifically.
# Usage: bash deploy/morning_status.sh
ssh -i deploy/ssh-key-2026-06-30.key ubuntu@92.4.94.88 "sudo systemctl status morning-scanner --no-pager; echo; echo '=== Last 15 log lines ==='; tail -15 ~/tradingsignalbot/morning_scanner.log"
