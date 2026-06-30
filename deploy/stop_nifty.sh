#!/bin/bash
# Stop just the NIFTY bot. If a real position is open, it keeps running on
# Zerodha's side via GTT (broker-side) — but bot-side breakeven/trailing/
# weak-exit/hard-exit management stops until you start it again.
# Usage: bash deploy/stop_nifty.sh
ssh -i deploy/ssh-key-2026-06-30.key ubuntu@92.4.94.88 "sudo systemctl stop nifty-bot && sudo systemctl is-active nifty-bot || true"
