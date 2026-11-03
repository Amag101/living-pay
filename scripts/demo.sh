#!/bin/bash
# Starts Living Pay against the fake PayPal sandbox, so it runs with no PayPal account.
# Set ANTHROPIC_API_KEY first to have Claude negotiate; without it, keyword rules stand in.
# Ctrl+C stops both servers.
set -e
cd "$(dirname "$0")/.."

python3 fake_paypal.py --port 8791 --webhook http://localhost:8790/webhooks/paypal &
FAKE=$!
trap 'kill $FAKE 2>/dev/null' EXIT

export PAYPAL_API_BASE=http://localhost:8791 PAYPAL_CLIENT_ID=fake PAYPAL_CLIENT_SECRET=fake
export PAYPAL_WEBHOOK_ID=WH-FAKE-1 LIVING_DB=demo.db

echo "Open:         http://localhost:8790"
echo "Money ledger: http://localhost:8790/ledger"
python3 server.py
