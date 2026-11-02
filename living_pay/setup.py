"""Creating, seeding and resetting the database."""

from datetime import datetime
import json
import os
import secrets

from living_pay.config import DB_PATH, LOCK
from living_pay.store import close_db, SCHEMA, db, ex, get_setting, iso, migrate, now, post, q1, set_setting
from living_pay.connectors import paypal_mode
from living_pay.automations import TEMPLATES


def seed():
    demo = paypal_mode() != "paypal"  # real PayPal starts empty, with today's date
    start = datetime(2026, 10, 24, 9, 0) if demo else datetime.now().replace(second=0, microsecond=0)
    for k, v in {
        "now": iso(start), "business": "Kora Studio" if demo else os.environ.get("LIVING_BUSINESS", "My Business"),
        "currency": "$",
        "invoice_ceiling": 5000, "next_invoice": 1003, "clock": "simulated",
        # PayPal invoice numbers must be unique across the whole sandbox account,
        # which outlives a demo reset, so each database gets its own suffix.
        "paypal_suffix": secrets.token_hex(2).upper(),
    }.items():
        set_setting(k, v)

    contacts = [
        ("Ruth Okafor", "Acme Dental", "ruth@acmedental.example", 1),
        ("Tunde Bello", "Brightside Cafe", "tunde@brightside.example", 1),
        ("Chioma Obi", "Lagos Print Co", "chioma@lagosprint.example", 1),
        ("Nnamdi Eze", "", "nnamdi.eze@mail.example", 0),
    ]
    if not demo:
        contacts = []
    for c in contacts:
        ex("INSERT INTO contacts(name,company,email,contacted) VALUES(?,?,?,?)", c)

    # Subcontractors whose share of a job is paid out once the client pays.
    for sub in [("Ada Nwosu", "ada.nwosu@illustrators.example", "Illustrator"),
                ("Femi Adeyemi", "femi@printworks.example", "Print production")] if demo else []:
        ex("INSERT INTO subcontractors(name,email,role) VALUES(?,?,?)", sub)

    invoices = [
        ("INV-1000", 1, 900, "paid", "2026-09-20T12:00", "2026-10-04T12:00", "2026-10-02T09:30",
         "Logo refresh", 900),
        ("INV-1001", 2, 650, "sent", "2026-10-03T12:00", "2026-10-17T12:00", None, "Menu photography", 0),
        ("INV-1002", 3, 2400, "sent", "2026-09-16T12:00", "2026-09-30T12:00", None, "Print catalogue", 0),
    ]
    for i in invoices if demo else []:
        ex("INSERT INTO invoices(number,contact_id,amount,status,issued_at,due_at,paid_at,"
           "title,amount_paid,token) VALUES(?,?,?,?,?,?,?,?,?,?)", i + (secrets.token_urlsafe(16),))

    for t in TEMPLATES.values():
        ex("INSERT INTO automations(template,params,trust,created_at) VALUES(?,?,?,?)",
           (t.key, json.dumps(t.default_params), t.default_trust, iso(start)))

    post("bot",
         "Hi, I'm your **Living Pay** assistant.\n\n"
         "When a client needs a decision I'll ask you here, and I'll tell you what I've done. "
         "Send an invoice to get started, or type **help**.")


def init_db():
    db().executescript(SCHEMA)
    db().commit()
    migrate()
    if not get_setting("now"):
        seed()
    for t in TEMPLATES.values():  # databases from before an automation existed
        if q1("SELECT id FROM automations WHERE template=?", (t.key,)):
            continue
        ex("INSERT INTO automations(template,params,trust,created_at) VALUES(?,?,?,?)",
           (t.key, json.dumps(t.default_params), t.default_trust, iso(now())))
    if paypal_mode() == "paypal":  # PayPal dates its invoices with today's date, so ours must match
        set_setting("clock", "real")


def reset():
    with LOCK:
        close_db()
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(DB_PATH + suffix):
                os.remove(DB_PATH + suffix)
        init_db()
