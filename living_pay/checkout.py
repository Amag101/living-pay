"""Taking payment on the invoice page, and what happens after a client pays."""

from datetime import timezone, date, timedelta, datetime
import json
import re
import secrets
import urllib.request

from living_pay import paypal_api as paypal
from living_pay.paypal_api import PayPalError
from living_pay.config import CURRENCY_CODE, LOCK
from living_pay.store import ex, get_setting, iso, log, money, now, post, q, q1, who
from living_pay.invoices import cash, due_now, invoice_balance, invoice_link, order_tag, plan_of, plan_rows, refund_sources, refundable
from living_pay.connectors import connector, paypal_client, paypal_mode
from living_pay.automations import enqueue, payment_received, process_queue
from living_pay.negotiator import chat_add, living, living_state


def checkout_amount(inv, which):
    if inv["status"] != "sent":
        raise ValueError("This invoice is %s" % ("already paid" if inv["status"] == "paid" else "no longer active"))
    amount = invoice_balance(inv) if which == "balance" else due_now(inv)
    if amount <= 0:
        raise ValueError("Nothing is due on this invoice")
    return amount


def create_checkout(token, which, funding=None):
    """Creates a PayPal order for the amount the server says is due. The browser never names a price."""
    with LOCK:
        inv = living(token)
        if not inv:
            raise KeyError("invoice")
        amount = checkout_amount(inv, which)
    order = paypal_client().create_order(amount, CURRENCY_CODE, None, order_tag(inv),
                                         "%s %s: %s" % (get_setting("business"), inv["number"], inv.get("title") or ""),
                                         request_id="lp-order-%d-%s" % (inv["id"], secrets.token_hex(6)),
                                         brand_name=get_setting("business"))
    with LOCK:
        ex("INSERT INTO checkout_orders(id,invoice_id,amount,status,at,funding) VALUES(?,?,?,?,?,?)",
           (order["id"], inv["id"], amount, order.get("status", "CREATED"), iso(now()),
            funding if funding in ("paylater", "paypal", "card") else None))
    return {"order_id": order["id"], "amount": amount}


def thank_client(inv, amount):
    chat_add(inv["id"], "agent", "Thank you! %s received. %s" % (cash(amount), (
        "This invoice is now fully paid." if inv["status"] == "paid"
        else "%s is left, and %s is due next." % (cash(invoice_balance(inv)), cash(due_now(inv))))))


def record_refund(inv, amount, reference, kind, payment_ref=None, actor="paypal", note=None):
    """Money going back to the client. A refund is the owner's choice, so what's still owed stays as it was.
    A reversal is taken back against the owner's will, so that money is owed again."""
    if q1("SELECT id FROM refunds WHERE reference=?", (reference,)):
        return False
    net = round((inv["amount_paid"] or 0) - (inv["refunded"] or 0), 2)
    amount = round(min(float(amount), net), 2)
    if amount <= 0:
        return False
    ex("INSERT INTO refunds(invoice_id,amount,kind,reference,at,payment_ref) VALUES(?,?,?,?,?,?)",
       (inv["id"], amount, kind, reference, iso(now()), payment_ref))
    c = q1("SELECT * FROM contacts WHERE id=?", (inv["contact_id"],))
    if kind == "reversal":
        ex("UPDATE invoices SET amount_paid=?, status='sent', paid_at=NULL WHERE id=?",
           (round((inv["amount_paid"] or 0) - amount, 2), inv["id"]))
        inv = q1("SELECT * FROM invoices WHERE id=?", (inv["id"],))
        advance_plan(inv)
        text = "**Payment reversed:** PayPal took %s back from you on %s, from %s. %s is now owed again." % (
            money(amount), inv["number"], who(c), money(invoice_balance(inv)))
    else:
        refunded = round((inv["refunded"] or 0) + amount, 2)
        whole = refunded >= (inv["amount_paid"] or 0) - 0.005 and inv["status"] == "paid"
        ex("UPDATE invoices SET refunded=?, status=? WHERE id=?",
           (refunded, "refunded" if whole else inv["status"], inv["id"]))
        chat_add(inv["id"], "agent", "%s has refunded %s to you. It should reach you through PayPal.%s" % (
            get_setting("business"), cash(amount), " Their note: %s" % note if note else ""))
        text = "**Refund:** %s went back to %s on %s." % (money(amount), who(c), inv["number"])
        if inv["status"] == "sent":
            text += " The %s still unpaid stays open, so reminders continue unless you cancel it in PayPal." % (
                money(invoice_balance(inv)))
    paid_out = q("SELECT p.amount, s.name FROM payouts p JOIN subcontractors s ON s.id=p.sub_id "
                 "WHERE p.invoice_id=? AND p.status NOT IN ('cancelled','failed')", (inv["id"],))
    for p in paid_out:
        text += " You already paid %s %s for this job, and this doesn't change that." % (p["name"], money(p["amount"]))
    log("refund_" + kind, "%s of %s on %s went back to %s" % (kind.capitalize(), money(amount), inv["number"], who(c)),
        actor=actor, data={"invoice_id": inv["id"], "amount": amount, "reference": reference})
    post("bot", text)
    inv = q1("SELECT * FROM invoices WHERE id=?", (inv["id"],))
    if not refundable(inv):  # nothing left to give back, so an open request has nothing to decide
        ex("UPDATE proposals SET status='cancelled', decided_at=? WHERE kind='refund_request' AND status='pending' "
           "AND dedupe LIKE ?", (iso(now()), "refundreq:%d:%%" % inv["id"]))
    return True


def refund_invoice(invoice_id, amount, note="", key="", request_id=None):
    """A signed-in person sends money back through PayPal. The assistant and clients have no way to start this."""
    if not re.fullmatch(r"[A-Za-z0-9-]{8,40}", str(key)):
        key = secrets.token_hex(8)  # with a key from the browser, a double click refunds only once
    note = str(note or "").strip()[:255]
    with LOCK:
        inv = q1("SELECT * FROM invoices WHERE id=?", (invoice_id,))
        if not inv:
            raise ValueError("That invoice doesn't exist")
        most = refundable(inv)
        try:
            amount = round(float(amount), 2)
        except (TypeError, ValueError):
            raise ValueError("The refund amount must be a number")
        if amount <= 0:
            raise ValueError("The refund must be more than zero")
        if amount > most + 0.005:
            raise ValueError("You can refund up to %s on this invoice" % money(most))
        steps, left = [], amount
        for source in refund_sources(inv):
            take = round(min(left, source["left"]), 2)
            if take > 0:
                steps.append((source["reference"], take))
                left = round(left - take, 2)
    # PayPal is called outside the lock. If its webhook arrives first, record_refund sees the same reference.
    for i, (ref, take) in enumerate(steps):
        if ref.startswith("capture:"):
            out = paypal_client().refund_capture(ref.split(":", 1)[1], take, CURRENCY_CODE, note or None,
                                                 request_id="lp-refund-%s-%d" % (key, i))
            reference = "refund:%s" % out["id"]
        else:
            reference = "refund:%s-%d" % (key, i)
        with LOCK:
            record_refund(q1("SELECT * FROM invoices WHERE id=?", (invoice_id,)), take, reference, "refund",
                          payment_ref=ref, actor="owner", note=note)
    with LOCK:
        if request_id:
            ex("UPDATE proposals SET status='executed', decided_at=? WHERE id=? AND kind='refund_request'",
               (iso(now()), request_id))
        process_queue()


def decline_refund_request(request_id):
    with LOCK:
        p = q1("SELECT * FROM proposals WHERE id=? AND kind='refund_request' AND status='pending'", (request_id,))
        if not p:
            raise ValueError("That request has already been answered")
        ex("UPDATE proposals SET status='skipped', decided_at=? WHERE id=?", (iso(now()), p["id"]))
        draft = json.loads(p["draft"])
        log("skipped", p["summary"], p["id"], None, "owner")
        chat_add(draft["invoice_id"], "agent", "%s has decided not to refund this. Please contact them directly "
                 "if you'd like to talk about it." % get_setting("business"))


def advance_plan(inv):
    """After a payment: the next installment becomes the minimum, and PayPal's note shows what's left."""
    if inv["status"] == "sent" and plan_of(inv):
        connector("accounting").execute("update_terms", {"invoice_id": inv["id"], "min_due": due_now(inv)})


def settle_payment(inv, amount, reference, method, actor):
    """Records a payment, moves a plan on to its next installment and tells both sides."""
    acc = connector("accounting")
    result = acc.execute("record_payment", {"invoice_id": inv["id"], "amount": amount, "method": method,
                                            "reference": reference, "note": "Paid on Living Pay"})
    if result.get("duplicate"):
        return result
    payment_received(inv, result, actor)
    inv = q1("SELECT * FROM invoices WHERE id=?", (inv["id"],))
    try:
        advance_plan(inv)
    except PayPalError as e:  # the money is in, so a failed update to PayPal's note mustn't undo that
        print("Couldn't move the plan on for %s: %s" % (inv["number"], e))
    thank_client(inv, amount)
    return result


def capture_checkout(token, order_id):
    with LOCK:
        inv = living(token)
        row = q1("SELECT * FROM checkout_orders WHERE id=?", (order_id,))
        if not inv or not row or row["invoice_id"] != inv["id"]:
            raise KeyError("order")
        if row["status"] == "COMPLETED":
            return living_state(token)
        # The discount, plan or another tab may have changed what's owed since this order was made.
        other = q1("SELECT COALESCE(SUM(amount),0) n FROM checkout_orders WHERE invoice_id=? AND status='CAPTURING' "
                   "AND id!=?", (inv["id"], order_id))["n"]
        if inv["status"] != "sent" or row["amount"] + other > invoice_balance(inv) + 0.005:
            ex("UPDATE checkout_orders SET status='REFUSED' WHERE id=?", (order_id,))
            raise ValueError("The amount due has changed, so nothing was charged. Please check the amount and pay again.")
        ex("UPDATE checkout_orders SET status='CAPTURING' WHERE id=?", (order_id,))
    try:
        captured = paypal_client().capture_order(order_id, request_id="lp-capture-" + order_id)
    except PayPalError:
        with LOCK:
            ex("UPDATE checkout_orders SET status='CREATED' WHERE id=?", (order_id,))
        raise
    cap_id, amount, status = paypal.capture_of(captured)
    with LOCK:
        ex("UPDATE checkout_orders SET status=? WHERE id=?", (captured.get("status"), order_id))
        if status != "COMPLETED":
            raise ValueError("PayPal is still processing this payment. Please don't pay again: it will show here "
                             "once it clears.")
        try:
            settle_payment(living(token), amount, "capture:%s" % cap_id, "PAYPAL", "client")
        except (PayPalError, ValueError, TypeError, KeyError) as e:
            # PayPal has taken the money, so the client must never be told otherwise.
            print("Captured %s but couldn't record it: %r" % (cap_id, e))
            log("failed", "PayPal took %s for %s, but it couldn't be recorded here" % (money(amount), inv["number"]),
                actor="paypal", data={"capture": cap_id})
            post("bot", "**Please check this payment:** PayPal took %s for %s, but I couldn't record it here. "
                        "Check it in your PayPal account before asking the client again." % (money(amount), inv["number"]))
            if living(token):
                chat_add(inv["id"], "agent", "Your payment of %s went through. Please don't pay again."
                         "%s will confirm it." % (cash(amount), " " + get_setting("business")))
            process_queue()
            return living_state(token)
        if row["funding"] == "paylater":
            ex("UPDATE payments SET method='PAY_LATER' WHERE reference=?", ("capture:%s" % cap_id,))
            post("bot", "**Pay Later:** the client used PayPal Pay Later for %s, so you have that money today and "
                        "they pay PayPal over time." % money(amount))
        process_queue()
    return living_state(token)


def fake_approve(token, order_id):
    """Fake mode only: stands in for the buyer approving the order in PayPal's popup."""
    if paypal_mode() != "fake":
        raise KeyError("fake approval")
    with LOCK:
        inv = living(token)
        row = q1("SELECT * FROM checkout_orders WHERE id=?", (order_id,))
        if not inv or not row or row["invoice_id"] != inv["id"]:
            raise KeyError("order")
    req = urllib.request.Request(paypal.base_url() + "/__fake/orders/%s/approve" % order_id,
                                 data=b"{}", method="POST", headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=10).read()
    return {"approved": order_id}


def demo_pay(token, which):
    """Offline mode only (no PayPal configured): records a simulated payment."""
    if paypal_mode() != "offline":
        raise KeyError("demo payment")
    with LOCK:
        inv = living(token)
        if not inv:
            raise KeyError("invoice")
        settle_payment(inv, checkout_amount(inv, which), "demo:%s" % secrets.token_hex(6), "OTHER", "client")
        process_queue()
    return living_state(token)


def set_reminders(token, on):
    with LOCK:
        inv = living(token)
        if not inv:
            raise KeyError("invoice")
        if inv["status"] != "sent" or not plan_of(inv):
            raise ValueError("Reminders are for invoices with a payment plan still to pay")
        ex("UPDATE invoices SET remind_email=? WHERE id=?", (1 if on else 0, inv["id"]))
        log("reminders_changed", "%s reminders %s for %s" % ("Email", "on" if on else "off", inv["number"]),
            actor="client", data={"invoice_id": inv["id"]})
        if on:  # a payment may already be close
            enqueue("clock.tick", {"at": iso(now())})
            process_queue()
    return living_state(token)


def calendar_file(token):
    """An .ics of the payments still to make, with an alert the day before each one."""
    with LOCK:
        inv = living(token)
        if not inv:
            raise KeyError("invoice")
        rows, biz, link = [r for r in plan_rows(inv) if r["owed"] > 0], get_setting("business"), invoice_link(inv)
        def text(s):
            return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")
        lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Living Pay//EN", "CALSCALE:GREGORIAN"]
        for r in rows:
            start = date.fromisoformat(r["due"])
            lines += ["BEGIN:VEVENT", "UID:%s-%s@livingpay" % (inv["token"], r["due"]),
                      "DTSTAMP:%s" % datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
                      "DTSTART;VALUE=DATE:%s" % start.strftime("%Y%m%d"),
                      "DTEND;VALUE=DATE:%s" % (start + timedelta(days=1)).strftime("%Y%m%d"),
                      "SUMMARY:%s" % text("Pay %s to %s" % (cash(r["owed"]), biz)),
                      "DESCRIPTION:%s" % text("Invoice %s. Pay here: %s" % (inv["number"], link)),
                      "URL:%s" % link,
                      "BEGIN:VALARM", "ACTION:DISPLAY", "TRIGGER:-P1D",
                      "DESCRIPTION:%s" % text("Payment to %s due tomorrow" % biz), "END:VALARM", "END:VEVENT"]
        lines.append("END:VCALENDAR")
        return ("\r\n".join(lines) + "\r\n").encode(), inv["number"]
