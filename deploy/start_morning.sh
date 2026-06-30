#!/bin/bash
# Start (or restart if already running) just the morning scanner.
# Usage: bash deploy/start_morning.sh
ssh -i deploy/ssh-key-2026-06-30.key ubuntu@92.4.94.88 "sudo systemctl start morning-scanner && sleep 5 && sudo systemctl status morning-scanner --no-pager"
