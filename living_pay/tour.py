"""The guided tour for judges: a demo login with limits, a sample invoice and a way to show a missed payment."""
import json
import os
import time
from datetime import date, timedelta

from living_pay.config import LOCK
from living_pay.store import ex, log, now, q, q1, set_setting
from living_pay.invoices import plan_rows, shift_plan
from living_pay.paypal_api import PayPalError
from living_pay.connectors import connector
from living_pay.automations import TEMPLATES, enqueue, process_queue, sweep_stale
from living_pay.rules import GROWTH_LABELS
from living_pay.owner import new_invoice

JUDGE_INVOICES_PER_DAY = 30
JUDGE_CHATS = (60, 600)  # owner messages allowed, per this many seconds
JUDGE_BLOCKED = ("/api/settings/business", "/api/settings/discount_chip", "/api/reset")
_invoice_times, _chat_times = [], []


def within(stamps, limit, seconds):
    """Counts one more use, or says no once the limit is reached."""
    t = time.time()
    stamps[:] = [s for s in stamps if t - s < seconds]
    if len(stamps) >= limit:
        return False
    stamps.append(t)
    return True


def judge_may_invoice():
    if not within(_invoice_times, JUDGE_INVOICES_PER_DAY, 86400):
        raise ValueError("The demo has made enough invoices for today. Please try again tomorrow.")


def judge_may_chat():
    if not within(_chat_times, JUDGE_CHATS[0], JUDGE_CHATS[1]):
        raise ValueError("That's a lot of messages. Please wait a few minutes and try again.")


def buyer():
    return os.environ.get("LIVING_DEMO_BUYER_EMAIL") or "client@example.com"


def latest():
    return q1("SELECT * FROM invoices WHERE tour=1 ORDER BY id DESC")


def tour_state():
    inv = latest()
    return {"token": inv["token"] if inv else None, "number": inv["number"] if inv else None,
            "has_plan": bool(inv and inv["status"] == "sent" and [r for r in plan_rows(inv) if r["owed"] > 0]),
            "buyer_email": buyer(), "buyer_password": os.environ.get("LIVING_DEMO_BUYER_PASSWORD")}


def start_tour():
    """A fresh start for each judge: default rules and automations, and a new sample invoice."""
    judge_may_invoice()
    with LOCK:
        for inv in q("SELECT * FROM invoices WHERE tour=1 AND status='sent'"):
            try:
                connector("accounting").execute("void_invoice", {"invoice_id": inv["id"]})
            except (PayPalError, ValueError) as e:  # a part-paid invoice can't be cancelled on PayPal
                print("tour: couldn't cancel %s: %s" % (inv["number"], e))
        set_setting("mandate", "{}")
        newest = q1("SELECT COALESCE(MAX(id),0) n FROM offers")["n"]
        set_setting("growth_asked", json.dumps({key: newest for key in GROWTH_LABELS}))  # earlier approvals don't count
        for t in TEMPLATES.values():
            ex("UPDATE automations SET params=?, trust=?, enabled=1, streak=0 WHERE template=?",
               (json.dumps(t.default_params), t.default_trust, t.key))
        ex("UPDATE proposals SET status='cancelled' WHERE status='pending' AND kind='mandate'")
        sweep_stale()
        new_invoice({"contact_id": "new", "new_name": "Ruth Adeyemi", "new_email": buyer(),
                     "title": "Website redesign", "amount": 1000, "due_days": 14})
        inv = q1("SELECT * FROM invoices ORDER BY id DESC")
        ex("UPDATE invoices SET tour=1 WHERE id=?", (inv["id"],))
        log("tour_started", "A judge started the tour on %s" % inv["number"], actor="owner")


def miss_payment(days):
    """Real plans take weeks to slip, so this moves the sample invoice's unpaid dates back and lets the real
    chasing run. It only ever touches the tour's sample invoice."""
    days = int(days)
    if days not in (1, 14):
        raise ValueError("Pick 1 or 14 days late")
    with LOCK:
        inv = latest()
        left = [r for r in plan_rows(inv) if r["owed"] > 0] if inv and inv["status"] == "sent" else []
        if not left:
            raise ValueError("Agree a payment plan on the sample invoice first")
        target = now().date() - timedelta(days=days)
        ex("UPDATE invoices SET plan=? WHERE id=?",
           (shift_plan(inv, (target - date.fromisoformat(left[0]["due"])).days), inv["id"]))
        log("tour_late", "The tour made %s's next payment %d day%s late" % (inv["number"], days, "" if days == 1 else "s"),
            actor="owner")
        enqueue("clock.tick", {"at": now().isoformat()})
        process_queue()
