#!/bin/bash
# Stop just the morning scanner (signal-only, no position/order risk).
# Usage: bash deploy/stop_morning.sh
ssh -i deploy/ssh-key-2026-06-30.key ubuntu@92.4.94.88 "sudo systemctl stop morning-scanner && sudo systemctl is-active morning-scanner || true"
