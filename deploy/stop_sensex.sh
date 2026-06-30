#!/bin/bash
# Stop just the SENSEX bot (signal-only, so this just stops alerts — no
# position/order risk either way).
# Usage: bash deploy/stop_sensex.sh
ssh -i deploy/ssh-key-2026-06-30.key ubuntu@92.4.94.88 "sudo systemctl stop sensex-bot && sudo systemctl is-active sensex-bot || true"
