"""Webhooks and polling bring payments made on PayPal's side back here."""

from datetime import date, timedelta
import json
import re
import time

from living_pay.paypal_api import PayPalError
from living_pay.config import LOCK, PAYPAL_WEBHOOK_ID, SYNC_SECONDS
from living_pay.store import ex, get_setting, iso, log, money, now, q, q1, set_setting
from living_pay.invoices import invoice_balance
from living_pay.connectors import PAYPAL_ACCOUNTING, paypal_client
from living_pay.automations import enqueue, payment_received, process_queue
from living_pay.checkout import advance_plan, settle_payment, thank_client


def tick_real():
    """One pass of the background loop on the real clock."""
    with LOCK:
        if get_setting("clock") != "real":
            return
        today = date.today()
        last = get_setting("last_day")
        d = date.fromisoformat(last) if last else today
        while d < today:
            d += timedelta(days=1)
            enqueue("clock.day", {"date": d.isoformat()}, "day:%s" % d)
        set_setting("last_day", today.isoformat())
        enqueue("clock.tick", {"at": iso(now())})
        process_queue()


def scheduler():
    while True:
        time.sleep(SYNC_SECONDS)
        try:
            tick_real()
        except Exception as e:
            print("scheduler:", repr(e))
        try:
            poll_paypal()
        except Exception as e:
            print("paypal poll:", repr(e))


def reconcile_invoice(pp):
    """Bring one PayPal invoice's payments and status into the local mirror."""
    inv = q1("SELECT * FROM invoices WHERE paypal_id=?", (pp.get("id"),))
    if not inv:
        return
    ex("UPDATE invoices SET paypal_status=? WHERE id=?", (pp.get("status"), inv["id"]))
    known = {r["external_id"] for r in q("SELECT external_id FROM payments WHERE invoice_id=?", (inv["id"],))}
    for t in pp.get("payments", {}).get("transactions", []):
        ref = "paypal:%s" % t["payment_id"]
        if t["payment_id"] in known or q1("SELECT id FROM payments WHERE reference=?", (ref,)):
            continue  # recorded by us, or already reconciled
        inv = q1("SELECT * FROM invoices WHERE id=?", (inv["id"],))
        if inv["status"] != "sent":
            break
        amount = min(float(t["amount"]["value"]), invoice_balance(inv))
        result = PAYPAL_ACCOUNTING.add_payment(inv, {"amount": amount, "reference": ref, "method": "PAYPAL",
                                                     "external_id": t["payment_id"]})
        payment_received(inv, result, "paypal")
        inv = q1("SELECT * FROM invoices WHERE id=?", (inv["id"],))
        advance_plan(inv)
        thank_client(inv, amount)
    inv = q1("SELECT * FROM invoices WHERE id=?", (inv["id"],))
    if pp.get("status") == "CANCELLED" and inv["status"] == "sent":
        ex("UPDATE invoices SET status='void' WHERE id=?", (inv["id"],))
        log("invoice_cancelled", "%s was cancelled in PayPal" % inv["number"], actor="paypal")


def paypal_webhook(headers, raw):
    """Returns an HTTP status. Only events PayPal vouches for are acted on."""
    client = paypal_client()
    if not client:
        return 503
    try:
        event = json.loads(raw)
    except ValueError:
        return 400
    eid, etype = event.get("id"), event.get("event_type", "")
    if not eid:
        return 400
    with LOCK:
        if q1("SELECT id FROM webhook_events WHERE id=? AND verified=1", (eid,)):
            return 200  # PayPal redelivers until it sees a 2xx
    try:
        verified = client.verify_webhook(headers, event, PAYPAL_WEBHOOK_ID)
    except PayPalError as e:
        print("webhook verification failed:", e)
        return 502
    with LOCK:
        ex("INSERT OR REPLACE INTO webhook_events(id,type,verified,received_at) VALUES(?,?,?,?)",
           (eid, etype, int(verified), iso(now())))
        if not verified:
            log("webhook_rejected", "Ignored a %s webhook whose signature PayPal didn't confirm" % etype,
                actor="paypal")
            return 401
    error = None
    try:
        if etype == "PAYMENT.CAPTURE.COMPLETED":
            # Normally recorded when the invoice page captures; this covers a lost response.
            cap = event.get("resource", {})
            m = re.match(r"^lp:(\d+)$", cap.get("custom_id") or "")
            with LOCK:
                inv = q1("SELECT * FROM invoices WHERE id=?", (int(m.group(1)),)) if m else None
                if inv and inv["status"] == "sent" and not q1("SELECT id FROM payments WHERE reference=?",
                                                               ("capture:%s" % cap["id"],)):
                    settle_payment(inv, float(cap["amount"]["value"]), "capture:%s" % cap["id"], "PAYPAL", "paypal")
                    process_queue()
        if etype.startswith("PAYMENT.PAYOUTS-ITEM."):
            item = event.get("resource", {})
            status = etype.rsplit(".", 1)[1].lower()
            with LOCK:
                row = q1("SELECT * FROM payouts WHERE item_id=?", (item.get("payout_item_id"),))
                if row and row["status"] != "cancelled":
                    ex("UPDATE payouts SET status=?, updated_at=? WHERE id=?", (status, iso(now()), row["id"]))
                    sub = q1("SELECT * FROM subcontractors WHERE id=?", (row["sub_id"],))
                    log("payout_" + status, "Payout of %s to %s: %s" % (money(row["amount"]), sub["name"], status),
                        actor="paypal", data={"payout_id": row["id"]})
        if etype.startswith("INVOICING.INVOICE."):
            pp = event.get("resource", {}).get("invoice") or event.get("resource", {})
            # INVOICING.INVOICE.PAID also covers partial payments, so read the invoice itself.
            fresh = client.get_invoice(pp["id"]) if pp.get("id") else pp
            with LOCK:
                reconcile_invoice(fresh)
                process_queue()
    except (PayPalError, KeyError, ValueError) as e:
        error = repr(e)
    with LOCK:
        ex("UPDATE webhook_events SET processed_at=?, error=? WHERE id=?", (iso(now()), error, eid))
    return 500 if error else 200


def poll_paypal():
    """Backup for missed webhooks (and the only path before a public URL exists)."""
    client = paypal_client()
    if not client:
        return
    with LOCK:
        open_ids = [r["paypal_id"] for r in q("SELECT paypal_id FROM invoices WHERE status='sent' AND paypal_id IS NOT NULL")]
    for pid in open_ids:
        fresh = client.get_invoice(pid)
        with LOCK:
            reconcile_invoice(fresh)
    with LOCK:
        process_queue()
