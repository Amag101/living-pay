"""The SQLite database, the clock, formatting helpers, and the owner's chat and audit log."""

from datetime import datetime
import json
import sqlite3

from living_pay.config import DB_PATH


_db = None
SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS contacts (
  id INTEGER PRIMARY KEY, name TEXT, company TEXT, email TEXT,
  contacted INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS invoices (
  id INTEGER PRIMARY KEY, number TEXT, contact_id INTEGER,
  amount REAL, status TEXT, issued_at TEXT, due_at TEXT, paid_at TEXT);
CREATE TABLE IF NOT EXISTS outbox (
  id INTEGER PRIMARY KEY, channel TEXT, contact_id INTEGER, to_addr TEXT,
  subject TEXT, body TEXT, sent_at TEXT, proposal_id INTEGER);
CREATE TABLE IF NOT EXISTS chat (
  id INTEGER PRIMARY KEY, sender TEXT, body TEXT, at TEXT,
  proposal_id INTEGER, buttons TEXT);
CREATE TABLE IF NOT EXISTS queue (
  id INTEGER PRIMARY KEY, type TEXT, payload TEXT, dedupe TEXT UNIQUE,
  status TEXT DEFAULT 'pending', created_at TEXT, processed_at TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS automations (
  id INTEGER PRIMARY KEY, template TEXT UNIQUE, params TEXT, trust TEXT,
  streak INTEGER DEFAULT 0, enabled INTEGER DEFAULT 1, created_at TEXT);
CREATE TABLE IF NOT EXISTS proposals (
  id INTEGER PRIMARY KEY, kind TEXT, automation_id INTEGER, summary TEXT,
  explanation TEXT, draft TEXT, reasons TEXT, status TEXT, dedupe TEXT UNIQUE,
  executed TEXT, created_at TEXT, decided_at TEXT);
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY, at TEXT, proposal_id INTEGER, automation_id INTEGER,
  event TEXT, actor TEXT, detail TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS refunds (
  id INTEGER PRIMARY KEY, invoice_id INTEGER, amount REAL, kind TEXT, reference TEXT UNIQUE, at TEXT, payment_ref TEXT);
CREATE TABLE IF NOT EXISTS payments (
  id INTEGER PRIMARY KEY, invoice_id INTEGER, amount REAL, method TEXT,
  reference TEXT UNIQUE, external_id TEXT, at TEXT);
CREATE TABLE IF NOT EXISTS invoice_chat (
  id INTEGER PRIMARY KEY, invoice_id INTEGER, role TEXT, body TEXT, at TEXT, offer_id INTEGER);
CREATE TABLE IF NOT EXISTS offers (
  id INTEGER PRIMARY KEY, invoice_id INTEGER, kind TEXT, offer TEXT, decision TEXT, reasons TEXT,
  terms TEXT, status TEXT, source TEXT, at TEXT, decided_at TEXT);
CREATE TABLE IF NOT EXISTS subcontractors (
  id INTEGER PRIMARY KEY, name TEXT, email TEXT, role TEXT);
CREATE TABLE IF NOT EXISTS payouts (
  id INTEGER PRIMARY KEY, invoice_id INTEGER, sub_id INTEGER, amount REAL, status TEXT,
  batch_id TEXT, item_id TEXT, proposal_id INTEGER, at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS checkout_orders (
  id TEXT PRIMARY KEY, invoice_id INTEGER, amount REAL, status TEXT, at TEXT);
CREATE TABLE IF NOT EXISTS webhook_events (
  id TEXT PRIMARY KEY, type TEXT, verified INTEGER, received_at TEXT,
  processed_at TEXT, error TEXT);
"""


def db():
    global _db
    if _db is None:
        _db = sqlite3.connect(DB_PATH, check_same_thread=False)
        _db.row_factory = sqlite3.Row
        _db.execute("PRAGMA journal_mode=WAL")
    return _db


def close_db():
    global _db
    if _db is not None:
        _db.close()
        _db = None


def q(sql, args=()):
    return [dict(r) for r in db().execute(sql, args).fetchall()]


def q1(sql, args=()):
    r = db().execute(sql, args).fetchone()
    return dict(r) if r else None


def ex(sql, args=()):
    cur = db().execute(sql, args)
    db().commit()
    return cur.lastrowid


def get_setting(key, default=None):
    r = q1("SELECT value FROM settings WHERE key=?", (key,))
    return r["value"] if r else default


def set_setting(key, value):
    ex("INSERT INTO settings(key,value) VALUES(?,?) "
       "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def now():
    """A practice clock in the demo; real time with real PayPal."""
    if get_setting("clock") == "real":
        return datetime.now().replace(second=0, microsecond=0)
    return datetime.fromisoformat(get_setting("now"))


def iso(dt):
    return dt.isoformat(timespec="minutes")


def nice(dt):
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt)
    return dt.strftime("%a %d %b, %H:%M")


def nice_date(dt):
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt)
    return dt.strftime("%a %d %b")


def money(x):
    return "%s%s" % (get_setting("currency", "$"), format(round(x), ","))


def first_name(contact):
    return contact["name"].split()[0]


def who(contact):
    if contact.get("company"):
        return "%s (%s)" % (contact["name"], contact["company"])
    return contact["name"]


MIGRATIONS = {
    "refunds": [("payment_ref", "TEXT")],
    "outbox": [("external_id", "TEXT"), ("note", "TEXT")],
    # amount is the invoiced price; the negotiated terms sit next to it.
    "invoices": [("token", "TEXT"), ("title", "TEXT"), ("discount_pct", "REAL DEFAULT 0"),
                 ("min_due", "REAL DEFAULT 0"), ("amount_paid", "REAL DEFAULT 0"),
                 ("paypal_id", "TEXT"), ("paypal_status", "TEXT"), ("payer_url", "TEXT"),
                 ("snapshot", "TEXT"), ("plan", "TEXT"), ("sub_id", "INTEGER"), ("sub_share", "REAL DEFAULT 0"),
                 ("remind_email", "INTEGER DEFAULT 0"), ("tour", "INTEGER DEFAULT 0"), ("refunded", "REAL DEFAULT 0")],
}


def migrate():
    for table, cols in MIGRATIONS.items():
        have = {r["name"] for r in q("PRAGMA table_info(%s)" % table)}
        for name, decl in cols:
            if name not in have:
                db().execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, name, decl))
    # Features that were removed, in databases from before.
    db().execute("DROP TABLE IF EXISTS cal_events")
    db().execute("DROP TABLE IF EXISTS connections")
    db().execute("DELETE FROM automations WHERE template='appointment_reminders'")
    db().execute("UPDATE automations SET template='send_invoices' WHERE template='invoice_on_completion'")
    db().commit()


def post(sender, body, proposal_id=None, buttons=None):
    ex("INSERT INTO chat(sender,body,at,proposal_id,buttons) VALUES(?,?,?,?,?)",
       (sender, body, iso(now()), proposal_id, json.dumps(buttons or [])))


def log(event, detail, proposal_id=None, automation_id=None, actor="living_pay", data=None):
    """Append-only audit log. Rows are never updated or deleted."""
    ex("INSERT INTO audit(at,proposal_id,automation_id,event,actor,detail,data) VALUES(?,?,?,?,?,?,?)",
       (iso(now()), proposal_id, automation_id, event, actor, detail, json.dumps(data or {})))
