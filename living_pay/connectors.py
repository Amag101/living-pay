"""The services the executor acts through. Each write has an inverse, which is how undo works.

PayPal is the real connector. The plain Accounting, Email and Payouts classes are stand-ins for
the tests and for trying the app with no PayPal account.
"""

from datetime import timedelta
import hashlib
import json
import os
import secrets

from living_pay import paypal_api as paypal
from living_pay.paypal_api import PayPalError
from living_pay.config import CURRENCY_CODE, PUBLIC_URL
from living_pay.store import ex, get_setting, iso, money, nice_date, now, q1, set_setting
from living_pay.invoices import TERMS, invoice_balance, invoice_link, invoice_total, paypal_note, resplit_plan


class Connector:
    name = ""
    label = ""

    def execute(self, op, params):
        raise NotImplementedError

    def inverse(self, op, params, result):
        """Return the write that reverses this one, or None if it can't be reversed."""
        return None

    def health(self):
        return "practice mode"


class Accounting(Connector):
    """The local invoice book. PayPalAccounting mirrors every write to PayPal."""
    name, label = "accounting", "Accounting (practice)"

    def next_number(self):
        n = int(get_setting("next_invoice"))
        set_setting("next_invoice", n + 1)
        return "INV-%d" % n

    def insert(self, number, params, issued, due, token=None, **paypal_fields):
        token = token or secrets.token_urlsafe(16)
        cols = dict(number=number, contact_id=params["contact_id"],
                    amount=params["amount"], status="sent", issued_at=iso(issued), due_at=iso(due),
                    title=params.get("title"), token=token, sub_id=params.get("sub_id"),
                    sub_share=params.get("sub_share") or 0, **paypal_fields)
        inv_id = ex("INSERT INTO invoices(%s) VALUES(%s)" % (",".join(cols), ",".join("?" * len(cols))),
                    tuple(cols.values()))
        return {"invoice_id": inv_id, "number": number, "due": nice_date(due),
                "link": "%s/i/%s" % (PUBLIC_URL, token)}

    def execute(self, op, params):
        if op == "create_invoice":
            issued = now()
            return self.insert(self.next_number(), params, issued, issued + timedelta(days=params["due_days"]))
        inv = q1("SELECT * FROM invoices WHERE id=?", (params["invoice_id"],))
        if op == "void_invoice":
            ex("UPDATE invoices SET status='void' WHERE id=?", (inv["id"],))
            return {"invoice_id": inv["id"]}
        if op == "update_terms":
            return self.set_terms(inv, resplit_plan(inv, params))
        if op == "record_payment":
            return self.add_payment(inv, params)
        if op == "remind":
            return {}  # nowhere to send it without PayPal
        raise ValueError("unknown accounting op " + op)

    def set_terms(self, inv, params):
        if inv["status"] != "sent":
            raise ValueError("%s is %s, so its terms can't change" % (inv["number"], inv["status"]))
        after = {k: params[k] for k in TERMS if params.get(k) is not None}
        # An undo restores only what this change touched, so it can't wipe out a later change.
        before = {k: (inv[k] or "") if k == "plan" else inv[k] for k in after}  # "" clears a plan
        if after:
            ex("UPDATE invoices SET %s WHERE id=?" % ",".join("%s=?" % k for k in after),
               tuple(after.values()) + (inv["id"],))
        return {"invoice_id": inv["id"], "number": inv["number"], "before": before}

    def add_payment(self, inv, params):
        """Idempotent on `reference` (a PayPal capture id, or the owner's manual entry)."""
        if q1("SELECT id FROM payments WHERE reference=?", (params["reference"],)):
            inv = q1("SELECT * FROM invoices WHERE id=?", (inv["id"],))
            return {"invoice_id": inv["id"], "duplicate": True, "fully_paid": inv["status"] == "paid"}
        if inv["status"] != "sent":
            raise ValueError("%s is %s, so it can't take a payment" % (inv["number"], inv["status"]))
        amount = round(float(params["amount"]), 2)
        if amount <= 0 or amount > invoice_balance(inv) + 0.005:
            raise ValueError("a payment of %.2f doesn't fit the %.2f still due" % (amount, invoice_balance(inv)))
        ex("INSERT INTO payments(invoice_id,amount,method,reference,external_id,at) VALUES(?,?,?,?,?,?)",
           (inv["id"], amount, params.get("method", "OTHER"), params["reference"], params.get("external_id"),
            iso(now())))
        paid = round((inv["amount_paid"] or 0) + amount, 2)
        full = paid >= invoice_total(inv) - 0.005
        ex("UPDATE invoices SET amount_paid=?, status=?, paid_at=? WHERE id=?",
           (paid, "paid" if full else "sent", iso(now()) if full else None, inv["id"]))
        return {"invoice_id": inv["id"], "amount": amount, "fully_paid": full,
                "balance": round(invoice_total(inv) - paid, 2)}

    def inverse(self, op, params, result):
        if op == "create_invoice":
            return {"connector": "accounting", "op": "void_invoice",
                    "params": {"invoice_id": result["invoice_id"]},
                    "label": "Void %s" % result["number"]}
        if op == "update_terms":
            return {"connector": "accounting", "op": "update_terms",
                    "params": dict(result["before"], invoice_id=result["invoice_id"]),
                    "label": "Restore the earlier terms on %s" % result["number"]}
        return None


class PayPalAccounting(Accounting):
    """Invoices live in PayPal (Invoicing v2). The local row mirrors them so the
    automations keep working, and webhooks or polling bring PayPal's changes back."""
    label = "PayPal Invoicing"

    def health(self):
        return "connected (%s)" % ("PayPal sandbox" if paypal.base_url() == paypal.SANDBOX_BASE
                                   else "PayPal at " + paypal.base_url())

    def body(self, inv, contact):
        return paypal.build_invoice(
            "%s-%s" % (inv["number"], get_setting("paypal_suffix")),
            {"name": contact["name"], "email": contact["email"]},
            [{"name": inv.get("title") or "Services", "amount": inv["amount"]}], CURRENCY_CODE,
            inv["due_at"][:10], note="Questions, or need a payment plan? Ask the invoice: %s" % invoice_link(inv),
            invoicer_name=get_setting("business"))

    def execute(self, op, params):
        client = paypal_client()
        # Same call, same id: a retry is idempotent. An undo replays the same op for the same
        # proposal with other params, so the id covers the params as well as the proposal.
        rid = "li-%s-%s-%s" % (get_setting("paypal_suffix"), op, hashlib.sha256(json.dumps(
            [params.get("proposal_id") or secrets.token_hex(8), params], sort_keys=True, default=str)
            .encode()).hexdigest()[:24])
        if op == "create_invoice":
            issued = now()
            due = issued + timedelta(days=params["due_days"])
            number = self.next_number()
            contact = q1("SELECT * FROM contacts WHERE id=?", (params["contact_id"],))
            draft = {"number": number, "amount": params["amount"], "title": params.get("title"),
                     "due_at": iso(due), "token": secrets.token_urlsafe(16)}
            created = client.create_invoice(self.body(draft, contact), request_id=rid)
            sent = client.send_invoice(created["id"], request_id=rid + "-send")
            result = self.insert(number, params, issued, due, token=draft["token"], paypal_id=created["id"],
                                 paypal_status="SENT", payer_url=paypal.payer_link(sent))
            result["paypal_id"] = created["id"]
            return result
        inv = q1("SELECT * FROM invoices WHERE id=?", (params["invoice_id"],))
        if not inv["paypal_id"]:  # invoices from before PayPal was connected stay local
            return super().execute(op, params)
        if op == "void_invoice":
            client.cancel_invoice(inv["paypal_id"], "Invoice %s cancelled" % inv["number"],
                                  "This invoice was sent in error. Please disregard it.", request_id=rid)
            ex("UPDATE invoices SET paypal_status='CANCELLED' WHERE id=?", (inv["id"],))
            return super().execute(op, params)
        if op == "update_terms":
            if inv["status"] != "sent":
                raise ValueError("%s is %s, so its terms can't change" % (inv["number"], inv["status"]))
            params = resplit_plan(inv, params)
            current = client.get_invoice(inv["paypal_id"])
            due = params.get("due_at")
            terms = dict(inv, **{k: params[k] for k in TERMS if params.get(k) is not None})
            updated = client.update_invoice(paypal.apply_terms(
                current, discount_pct=params.get("discount_pct"), min_due=params.get("min_due"),
                due_date=due[:10] if due else None, note=paypal_note(terms)), request_id=rid)
            ex("UPDATE invoices SET snapshot=? WHERE id=?", (json.dumps(updated), inv["id"]))
            return self.set_terms(inv, params)
        if op == "remind":
            client.remind(inv["paypal_id"], params["subject"], params["note"], request_id=rid)
            return {}
        if op == "record_payment":
            if q1("SELECT id FROM payments WHERE reference=?", (params["reference"],)):
                return self.add_payment(inv, params)
            if params.get("method") != "PAYPAL_WEBHOOK":  # PayPal already knows about payments it reports
                out = client.record_payment(inv["paypal_id"], params["amount"], CURRENCY_CODE,
                                            now().date().isoformat(), method=params.get("method", "OTHER"),
                                            note=params.get("note"), request_id=rid)
                params = dict(params, external_id=out.get("payment_id"))
            return self.add_payment(inv, params)
        raise ValueError("unknown accounting op " + op)


class Messaging(Connector):
    """A message can't be unsent, so the inverse is a correction message when the automation supplies one."""

    def record(self, contact, to_addr, params, subject=None, external_id=None, note=None):
        mid = ex("INSERT INTO outbox(channel,contact_id,to_addr,subject,body,sent_at,proposal_id,external_id,note) "
                 "VALUES(?,?,?,?,?,?,?,?,?)",
                 (self.name, contact["id"], to_addr, subject if subject is not None else params.get("subject", ""),
                  params["body"], iso(now()), params.get("proposal_id"), external_id, note))
        ex("UPDATE contacts SET contacted=1 WHERE id=?", (contact["id"],))
        return mid

    def execute(self, op, params):
        contact = q1("SELECT * FROM contacts WHERE id=?", (params["contact_id"],))
        to_addr = contact["email"]
        mid = self.record(contact, to_addr, params)
        return {"message_id": mid, "to": to_addr}

    def inverse(self, op, params, result):
        if params.get("correction_body"):
            return {"connector": self.name, "op": "send",
                    "params": {"contact_id": params["contact_id"],
                               "subject": params.get("correction_subject", ""),
                               "body": params["correction_body"]},
                    "label": "Send a correction"}
        return None


class Email(Messaging):
    name, label = "email", "Email (practice: nothing is really sent)"


class RetryLater(ValueError):
    """An undo that can't happen yet but may work shortly; the action stays undoable."""


class Payouts(Connector):
    """Money out to subcontractors. PayPalPayouts sends it with PayPal Payouts."""
    name, label = "payouts", "Payouts (practice)"

    def check(self, params):
        if q1("SELECT id FROM payouts WHERE invoice_id=? AND sub_id=? AND status NOT IN ('cancelled','failed')",
              (params["invoice_id"], params["sub_id"])):
            raise ValueError("this job's subcontractor has already been paid")
        return q1("SELECT * FROM subcontractors WHERE id=?", (params["sub_id"],))

    def record(self, params, sub, status, batch_id=None, item_id=None):
        pid = ex("INSERT INTO payouts(invoice_id,sub_id,amount,status,batch_id,item_id,proposal_id,at,updated_at) "
                 "VALUES(?,?,?,?,?,?,?,?,?)",
                 (params["invoice_id"], sub["id"], params["amount"], status, batch_id, item_id,
                  params.get("proposal_id"), iso(now()), iso(now())))
        return {"payout_id": pid, "name": sub["name"], "amount": params["amount"], "status": status}

    def execute(self, op, params):
        if op == "send_payout":
            return self.record(params, self.check(params), "sent")
        if op == "cancel_payout":
            row = q1("SELECT * FROM payouts WHERE id=?", (params["payout_id"],))
            if row["status"] not in ("sent", "unclaimed", "pending"):
                raise ValueError("the payout is already %s" % row["status"])
            ex("UPDATE payouts SET status='cancelled', updated_at=? WHERE id=?", (iso(now()), row["id"]))
            return {"payout_id": row["id"]}
        raise ValueError("unknown payouts op " + op)

    def inverse(self, op, params, result):
        if op == "send_payout":
            return {"connector": "payouts", "op": "cancel_payout", "params": {"payout_id": result["payout_id"]},
                    "label": "Cancel the %s payout to %s (only while it's unclaimed)"
                             % (money(result["amount"]), result["name"])}
        return None


class PayPalPayouts(Payouts):
    label = "PayPal Payouts"

    def health(self):
        return PAYPAL_ACCOUNTING.health()

    def execute(self, op, params):
        client = paypal_client()
        if op == "send_payout":
            sub = self.check(params)
            inv = q1("SELECT * FROM invoices WHERE id=?", (params["invoice_id"],))
            # One batch per proposal: PayPal refuses a reused sender_batch_id, which makes retries safe.
            batch = "li-%s-po-%s" % (get_setting("paypal_suffix"), params.get("proposal_id") or secrets.token_hex(6))
            out = client.create_payout(batch, sub["email"], params["amount"], CURRENCY_CODE, params["note"],
                                       "Payment from %s for %s" % (get_setting("business"), inv["number"]))
            bid = out["batch_header"]["payout_batch_id"]
            items = client.get_payout_batch(bid).get("items", [])
            item = items[0] if items else {}
            return self.record(params, sub, (item.get("transaction_status") or "pending").lower(),
                               bid, item.get("payout_item_id"))
        if op == "cancel_payout":
            row = q1("SELECT * FROM payouts WHERE id=?", (params["payout_id"],))
            if row["item_id"]:
                item = client.get_payout_item(row["item_id"])
                status = item.get("transaction_status", "")
                batch = client.get_payout_batch(row["batch_id"]).get("batch_header", {}).get("batch_status", "")
                # PayPal cancels only unclaimed items whose whole batch has finished processing.
                if status == "PENDING" or batch in ("PENDING", "PROCESSING"):
                    raise RetryLater("PayPal is still processing this payout, so it can't be cancelled yet. "
                                     "Try undo again in a minute")
                try:
                    client.cancel_payout_item(row["item_id"])  # PayPal refuses once the money is claimed
                except PayPalError as e:
                    if e.status < 500:
                        raise
                    raise RetryLater("PayPal couldn't cancel it just now (a PayPal-side error). Try undo again "
                                     "later. If nobody claims it, PayPal returns unclaimed payouts after 30 days")
            ex("UPDATE payouts SET status='cancelled', updated_at=? WHERE id=?", (iso(now()), row["id"]))
            return {"payout_id": row["id"]}
        raise ValueError("unknown payouts op " + op)


class InvoiceChat(Connector):
    """Tells the client something on their invoice page."""
    name, label = "invoice_chat", "The invoice page"

    def execute(self, op, params):
        ex("INSERT INTO invoice_chat(invoice_id,role,body,at) VALUES(?,'agent',?,?)",
           (params["invoice_id"], params["body"], iso(now())))
        return {}

    def inverse(self, op, params, result):
        if not params.get("undo_body"):
            return None
        return {"connector": self.name, "op": "say", "label": "Tell the client on their invoice page",
                "params": {"invoice_id": params["invoice_id"], "body": params["undo_body"]}}


CONNECTORS = {c.name: c for c in [Accounting(), Email(), Payouts(), InvoiceChat()]}
PAYPAL_ACCOUNTING = PayPalAccounting()
_paypal = None


def paypal_client():
    """A PayPal REST client when PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET are set."""
    global _paypal
    creds = paypal.load_client()
    if not creds:
        return None
    if _paypal is None or (_paypal.client_id, _paypal.base) != (creds["client_id"], paypal.base_url()):
        _paypal = paypal.Client(creds["client_id"], creds["client_secret"])
    return _paypal


PAYPAL_PAYOUTS = PayPalPayouts()


def connector(name):
    if name == "accounting" and paypal_client():
        return PAYPAL_ACCOUNTING
    if name == "payouts" and paypal_client():
        return PAYPAL_PAYOUTS
    return CONNECTORS[name]


# ---- Checkout ------------------------------------------------------------

def paypal_mode():
    if not paypal_client():
        return "offline"
    return "fake" if os.environ.get("PAYPAL_API_BASE") else "paypal"
