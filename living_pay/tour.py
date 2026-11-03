"""The guided tour for judges: a fresh start, a sample invoice and a way to show a missed payment."""
import glob
import json
import os
import sqlite3
import time
from datetime import date, datetime, timedelta

from living_pay.config import CURRENCY_CODE, DB_PATH, LOCK
from living_pay.store import db, ex, get_setting, iso, log, now, q, q1, set_setting
from living_pay.invoices import invoice_balance, plan_of, plan_rows
from living_pay.paypal_api import PayPalError
from living_pay.connectors import connector, paypal_client
from living_pay.automations import enqueue, process_queue
from living_pay.owner import new_invoice
from living_pay.database import reset

JUDGE_INVOICES_PER_DAY = 200
JUDGE_CHATS = (60, 600)  # owner messages allowed, per this many seconds
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


def keep_a_copy():
    """Starting again never loses anything: the old database stays on disk, next to the live one."""
    copy = sqlite3.connect("%s-before-tour-%020d" % (DB_PATH, time.time_ns()))
    db().backup(copy)
    copy.close()
    # Keeps the first copy, which holds anything from before the tour, and the latest few.
    copies = sorted(glob.glob(glob.escape(DB_PATH) + "-before-tour-*"))
    for old in copies[1:-5]:
        os.remove(old)


def earlier_approvals(amount):
    """Earned trust takes three approvals, so the sample starts with two already on file, as the tour says.
    They belong to no invoice, so they count towards trust without appearing in the invoice's history."""
    first = amount * 0.2
    today = now().date()
    terms = {"installments": 2, "gap_days": 14, "min_due": first, "due_at": (today + timedelta(days=14)).isoformat(),
             "schedule": [{"due": today.isoformat(), "amount": first},
                          {"due": (today + timedelta(days=14)).isoformat(), "amount": amount - first}]}
    for _ in range(2):
        ex("INSERT INTO offers(invoice_id,kind,offer,decision,reasons,terms,status,source,at,decided_at) "
           "VALUES(0,'payment_plan',?,'ESCALATE',?,?,'approved','agent',?,?)",
           (json.dumps({"kind": "payment_plan", "installments": 2, "first_amount": first, "gap_days": 14}),
            json.dumps(["the first payment is under 30% of the balance"]), json.dumps(terms), iso(now()), iso(now())))


def close_open_invoices():
    """Leaves nothing open on the PayPal sandbox, so "Who hasn't paid?" starts clean after Start again.
    PayPal won't cancel a part-paid invoice, so those are recorded as settled instead. No money moves."""
    for inv in q("SELECT * FROM invoices WHERE status='sent'"):
        try:
            if inv["paypal_id"] and (inv["amount_paid"] or 0) > 0:
                paypal_client().record_payment(inv["paypal_id"], invoice_balance(inv), CURRENCY_CODE,
                                               now().date().isoformat(), method="OTHER", note="Demo restarted",
                                               request_id="lp-tour-close-%s" % inv["paypal_id"])
            else:
                connector("accounting").execute("void_invoice", {"invoice_id": inv["id"]})
        except (PayPalError, ValueError) as e:
            print("tour: couldn't close %s: %s" % (inv["number"], e))


def start_tour():
    """A fresh start for the shared demo, with full access. Two judges at once would share it, and this resets it for both."""
    judge_may_invoice()
    with LOCK:
        close_open_invoices()
        business = get_setting("business")
        keep_a_copy()
        reset()
        set_setting("business", business)
        earlier_approvals(1000)
        new_invoice({"contact_id": "new", "new_name": "Ruth Adeyemi", "new_email": buyer(),
                     "title": "Website redesign", "amount": 1000, "due_days": 14})
        inv = q1("SELECT * FROM invoices ORDER BY id DESC")
        ex("UPDATE invoices SET tour=1 WHERE id=?", (inv["id"],))
        log("tour_started", "A judge started the tour on %s" % inv["number"], actor="owner")


def move_back(inv, days):
    """Moves the whole sample invoice in time, so its dates still make sense together."""
    plan = plan_of(inv)
    plan["schedule"] = [dict(p, due=(date.fromisoformat(p["due"]) + timedelta(days=days)).isoformat())
                        for p in plan["schedule"]]
    shifted = lambda at: (datetime.fromisoformat(at) + timedelta(days=days)).isoformat(timespec="minutes")
    ex("UPDATE invoices SET plan=?, issued_at=?, due_at=? WHERE id=?",
       (json.dumps(plan), shifted(inv["issued_at"]), shifted(inv["due_at"]), inv["id"]))


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
        move_back(inv, (target - date.fromisoformat(left[0]["due"])).days)
        log("tour_late", "The tour made %s's next payment %d day%s late" % (inv["number"], days, "" if days == 1 else "s"),
            actor="owner")
        enqueue("clock.tick", {"at": now().isoformat()})
        process_queue()
