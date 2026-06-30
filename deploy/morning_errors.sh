#!/bin/bash
# Show the last 50 lines of the morning scanner's error log + service status.
# Usage: bash deploy/morning_errors.sh
ssh -i deploy/ssh-key-2026-06-30.key ubuntu@92.4.94.88 "echo '=== Service status ==='; sudo systemctl status morning-scanner --no-pager; echo; echo '=== Last 50 error lines ==='; tail -50 ~/tradingsignalbot/morning_scanner_error.log; echo; echo '=== Restart count ==='; systemctl show morning-scanner -p NRestarts"
