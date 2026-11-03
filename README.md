# Living Pay

**The invoice that negotiates for you.**

**Live app:** https://living-pay.onrender.com (PayPal sandbox, no real money)

A freelance designer bills $1,000. The client can manage $200 now, and the rest when their own client pays. A PayPal invoice can hold a minimum payment but not a schedule, so that deal usually happens over email, and the freelancer chases each instalment by hand.

Living Pay attaches an AI assistant to a real PayPal invoice. It offers PayPal Pay Later first, agrees what the freelancer's rules allow, asks them about anything else, and chases the plan if it slips. Claude handles the conversation, plain code enforces the limits, and PayPal moves the money.

Built for the [PayPal AI Hackathon](https://paypalaihackathon.devpost.com/).

## Try it

Open the [live app](https://living-pay.onrender.com) and sign in with the judge password from our Devpost entry. A guided tour on the first page needs no setup: its first 6 steps take about 5 minutes and end with a PayPal payment. The judge login has full access to one shared demo, and "Start again" resets it for everyone using it.

## How it works

1. **The owner sends an invoice:** who it's for, what it's for and how much. It goes out as a PayPal invoice with its own page.
2. **The client talks to the invoice.** A client who can't pay it all is pointed to PayPal Pay Later first, because the business gets paid today. If that doesn't suit them, "$200 now and the rest on the 15th" gets an answer straight away, and the invoice changes in PayPal.
3. **Bigger requests go to the owner,** who can approve, decline or counter with one message.
4. **The client pays** with PayPal or a card, on the invoice page or on PayPal's own page.
   On a payment plan, the same link becomes their plan page, with progress, the schedule, email reminders and an Add to calendar button.
5. **Everyone gets paid.** When an invoice is paid in full, a subcontractor's share can go out through PayPal Payouts.

## Why the AI can be trusted with money

- **The rules are code, not a prompt.** Claude can only propose an offer. `policy.py` then allows it, sends it to the owner, or declines it, so nothing a client types can talk it past the rules.
- **Autonomy is earned.** After the owner approves the same kind of request three times, the assistant asks to handle those itself. An undo narrows the rule back.
- **Changes to an invoice can be undone,** from the assistant or the ledger, and every action is logged with the reason. A payment that has gone through can't be undone here, and a subcontractor payout can only be stopped while PayPal is still holding it.
- **Only a signed-in person can give money back.** A client can ask, and the assistant can pass that on, but it has no way to refund, and chat commands can't approve one.
- **The owner's assistant can only read PayPal.** Ask "who hasn't paid yet?" and Claude looks it up through PayPal's MCP server, limited in code to five read-only tools. It has no way to refund, cancel or send.

## What it uses

**PayPal (sandbox)**

| Product | How it's used |
|---|---|
| Invoicing v2 | Create, send, change a sent invoice, minimum payments as plan instalments, the plan written into the invoice's note, reminders, cancelling, recording payments |
| Orders v2 and the JavaScript SDK | Checkout on the invoice page, with PayPal, cards and Pay Later, a funding eligibility check, and checks that stop a stale or second order being charged |
| Payments v2 | Full and partial refunds across several captures, with a key that stops a double click refunding twice |
| Payouts | A subcontractor's share, with per-item status tracking, and cancelling unclaimed items as a real undo |
| Webhooks | Signature checks through `verify-webhook-signature`, duplicate protection, and capture, refund, reversal, payout and invoice events, with polling as a backup |
| MCP server (`@paypal/mcp`) | The owner's questions, limited in code to five read-only tools |

Writes carry `PayPal-Request-Id`, an expired token is refreshed on a 401, and 429 and 5xx responses are retried once.

**AI:** Claude (`claude-opus-5-5`) with strict tool schemas, structured outputs for reading plain-English rules, PayPal's MCP tools in Claude's tool loop, defences against prompt injection, a daily call budget and server-side model fallback.

**App:** AG Grid Community for the ledger, and Python's standard library with SQLite, with no framework or build step.

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

Then open http://localhost:8790. Questions about your PayPal account also need Node.js, for PayPal's MCP server. To pay an invoice, log in with a sandbox Personal account from the PayPal developer dashboard. Create the app on a US sandbox business account, because PayPal denies payouts from some other countries.

`python3 scripts/paypal_check.py` checks the 12 invoice, order and payout calls that don't need a buyer against your sandbox. Capture, refunds and webhooks need a buyer or a public address, so they were tested by hand on the live sandbox.

For webhooks, subscribe your sandbox app to `Invoicing` events, `Payment capture completed`, `refunded` and `reversed`, and `Payouts item` events, then set `PAYPAL_WEBHOOK_ID`.

To show the judge's tour, set both `LIVING_OWNER_PASSWORD` and `LIVING_JUDGE_PASSWORD`, and sign in with the judge password. Optionally set `LIVING_SESSION_SECRET` to a long random value.

Without a Claude key, or past the daily limit, simple keyword rules stand in for the assistant. They handle common requests like "$200 now and the rest on the 15th" or "Can I pay in 3 parts?", but they aren't a conversation.

**Put it online:** `render.yaml` and the `Dockerfile` set it up on [Render](https://render.com). It won't start on a public address until you set `LIVING_OWNER_PASSWORD`. Only a client's own invoice link and PayPal's webhook are open without that password, and `LIVING_CLAUDE_DAILY_LIMIT` caps Claude calls per day (400 by default).

## What it doesn't do yet

- Disputes are handled in PayPal, not here. When a refund or reversal happens, Living Pay corrects its numbers and tells the owner, but PayPal's own invoice page keeps showing the original payment.
- Only payments taken on the invoice page can be refunded from Living Pay. Refund a payment made on PayPal's own invoice page in PayPal.
- A payment plan is an agreement, not an automatic charge: the client comes back to pay each instalment. Living Pay reminds them when one is missed, and asks the owner whether to end the plan at 14 days late.
- PayPal lists payments taken on the invoice page as recorded by the seller, because they arrive through Orders and are then recorded on the invoice.
- Sandbox only. It hasn't handled real money.

- One business per install: there's no sign-up or Connect PayPal button yet, and PayPal keys are set when the app is deployed.
- Decisions don't send alerts yet: the owner and the client both see them in the app.

## Tests

```bash
for t in tests/test_*.py; do python3 "$t" || exit 1; done
```

219 tests run against `fake_paypal.py`, a stand-in that behaves the way the real sandbox was observed to behave. They need no network or accounts.

## Project layout

| Path | What it is |
|---|---|
| `server.py` | Starts the app |
| `living_pay/policy.py` | The rules that decide what the assistant may agree to |
| `living_pay/negotiator.py` | The assistant on each invoice: Claude's prompt, tools and keyword fallback |
| `living_pay/llm.py` | The connection to Claude, with the daily budget |
| `living_pay/rules.py` | The owner's rules, set in plain English and widened when earned |
| `living_pay/owner.py` | The owner's assistant, approvals and new invoices |
| `living_pay/automations.py` | Automations, and the executor that runs and undoes actions |
| `living_pay/invoices.py` | Totals, balances and payment plan schedules |
| `living_pay/connectors.py` | PayPal, and practice stand-ins |
| `living_pay/checkout.py`, `paypal_events.py` | Taking payment and refunds, and webhooks with polling |
| `living_pay/paypal_api.py`, `paypal_mcp.py` | PayPal's REST API and MCP server |
| `living_pay/tour.py` | The judge's tour |
| `living_pay/store.py`, `database.py`, `config.py` | The database, its setup and settings |
| `living_pay/web.py` | The owner's login, page data and web server |
| `app/` | The web pages |
| `scripts/` | The demo starter and the sandbox check |
| `tests/`, `fake_paypal.py` | The tests, and a stand-in for PayPal |

## License

[MIT](LICENSE)
