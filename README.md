# Living Pay

**The invoice that negotiates for you.**

Freelancers and small businesses lose hours chasing invoices, and a client who can't pay all at once usually just pays late. Living Pay sends a real PayPal invoice with an AI assistant attached. The client can ask it questions and arrange a payment plan, more time or a discount, and then pay. The assistant agrees to what the owner's rules allow, asks the owner about anything else, and declines the rest.

Built for the [PayPal AI Hackathon](https://paypalaihackathon.devpost.com/).

## How it works

1. **The owner sends an invoice:** who it's for, what it's for and how much. It goes out as a PayPal invoice with its own page.
2. **The client talks to the invoice.** "Can I split this into 3 payments?" gets an answer straight away, and the invoice changes in PayPal.
3. **Bigger requests go to the owner,** who can approve, decline or counter with one message.
4. **The client pays** with PayPal or a card, on the invoice page or on PayPal's own page.
   On a payment plan, the same link becomes their plan page, with progress, the schedule, email reminders and an Add to calendar button.
5. **Everyone gets paid.** When an invoice is paid in full, a subcontractor's share can go out through PayPal Payouts.

## Why the AI can be trusted with money

- **The rules are code, not a prompt.** Claude can only propose an offer. `policy.py` then allows it, sends it to the owner, or declines it, so nothing a client types can talk it past the rules.
- **Autonomy is earned.** After the owner approves the same kind of request three times, the assistant asks to handle those itself. An undo narrows the rule back.
- **Everything can be undone,** from the assistant or the ledger, and every action is logged with the reason.

## What it uses

| | |
|---|---|
| **PayPal** | Invoicing v2, Orders v2 and the JavaScript SDK, Payouts, Webhooks (sandbox) |
| **AI** | Claude (`claude-opus-5-5`): the negotiator's tool calls, and reading rules written in plain English |
| **Ledger** | AG Grid Community |
| **App** | Python standard library and SQLite, with no framework or build step |

## Run it

You need Python 3.9 or newer.

**Try it without a PayPal account,** using a built-in stand-in for PayPal:

```bash
./scripts/demo.sh
```

**Run it on the PayPal sandbox:**

```bash
pip3 install -r requirements.txt
cp .env.example .env    # then add your sandbox app's Client ID and Secret, and your Claude API key
python3 server.py
```

Then open http://localhost:8790. To pay an invoice, log in with a sandbox Personal account from the PayPal developer dashboard. Create the app on a US sandbox business account, because PayPal denies payouts from some other countries.

`python3 scripts/paypal_check.py` checks every PayPal call the app relies on against your sandbox.

Without a Claude key, simple keyword rules stand in for the assistant.

## Tests

```bash
for t in tests/test_*.py; do python3 "$t"; done
```

The tests run against `fake_paypal.py`, a stand-in that behaves the way the real sandbox was observed to behave. They need no network or accounts.

## Project layout

| Path | What it is |
|---|---|
| `server.py` | The app: invoices, the assistant, approvals and the web server |
| `policy.py` | The rules that decide what the assistant may agree to |
| `paypal_api.py` | The PayPal REST client |
| `fake_paypal.py` | A stand-in for PayPal, for tests and the demo |
| `google_api.py`, `vault.py` | Optional Gmail sending, with encrypted tokens |
| `app/` | The web pages |

## License

[MIT](LICENSE)
