"""The owner's login, the page data and the web server."""

import hashlib
import hmac
from http.cookies import SimpleCookie, CookieError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import sys
import threading
import time
from urllib.parse import urlparse

from living_pay.paypal_api import PayPalError
from living_pay.config import APP_DIR, LOCK, PORT
from living_pay.store import get_setting, iso, log, money, nice, nice_date, now, q, q1, set_setting
from living_pay.invoices import (
    refundable,
    days_late, describe_ask, describe_terms, invoice_balance, invoice_total, missed_rows, plan_rows,
)
from living_pay.connectors import connector, paypal_mode
from living_pay.llm import LLM
from living_pay.automations import TEMPLATES, TRUST_LABELS, automation_params
from living_pay.negotiator import client_message, living_state, mandate
from living_pay.rules import rules_lines
from living_pay.checkout import (
    decline_refund_request, refund_invoice,
    calendar_file, capture_checkout, create_checkout, demo_pay, fake_approve, set_reminders,
)
from living_pay.paypal_events import paypal_webhook, scheduler
from living_pay.tour import judge_may_chat, judge_may_invoice, miss_payment, start_tour, tour_state
from living_pay.owner import advance_clock, mark_paid, new_invoice, owner_message, paypal_ask_on
from living_pay.database import init_db, reset


SESSION_SECONDS = 14 * 86400
LOGIN_LIMIT = (20, 600)  # wrong passwords allowed from one address, per this many seconds
_login_fails = {}


def owner_password():
    return os.environ.get("LIVING_OWNER_PASSWORD", "")


def judge_password():
    return os.environ.get("LIVING_JUDGE_PASSWORD", "")


def session_value(expires, role="owner"):
    # Signed with the owner's password, so changing it signs everyone out. LIVING_SESSION_SECRET, if it's set,
    # stops a judge guessing that password from their own cookie. The judge's password is mixed in for their cookies.
    key = os.environ.get("LIVING_SESSION_SECRET", "") + owner_password() + (judge_password() if role == "judge" else "")
    sig = hmac.new(key.encode(), b"%s:%d" % (role.encode(), expires), hashlib.sha256).hexdigest()
    return "%s.%d.%s" % (role, expires, sig)


def signed_in(cookie_header):
    """The role of whoever sent this cookie, "owner" or "judge", or None."""
    if not owner_password():  # only possible on this computer: main() won't serve publicly without one
        return "owner"
    try:
        morsel = SimpleCookie(cookie_header or "").get("living_owner")
    except CookieError:
        return None
    parts = morsel.value.split(".") if morsel else []
    if len(parts) != 3 or parts[0] not in ("owner", "judge") or not parts[1].isdigit() or int(parts[1]) <= time.time():
        return None
    return parts[0] if hmac.compare_digest(morsel.value, session_value(int(parts[1]), parts[0])) else None


def check_login(password, address="local"):
    """The role this password signs in as, or None. Too many wrong guesses lock out that address only,
    so a stranger guessing can't keep the owner or a judge from signing in."""
    now_s = time.time()
    for key in [k for k, stamps in _login_fails.items() if now_s - stamps[-1] >= LOGIN_LIMIT[1]]:
        del _login_fails[key]
    if len([s for s in _login_fails.get(address, []) if now_s - s < LOGIN_LIMIT[1]]) >= LOGIN_LIMIT[0]:
        raise ValueError("Too many tries. Please wait a few minutes.")
    given = str(password or "").encode()
    if hmac.compare_digest(given, owner_password().encode()):
        return "owner"
    if judge_password() and hmac.compare_digest(given, judge_password().encode()):
        return "judge"
    _login_fails.setdefault(address, []).append(now_s)
    return None


WEBHOOK_MAX_BYTES = 200_000
WEBHOOK_REJECTIONS = (30, 60)  # rejected webhooks allowed, per this many seconds
_webhook_rejections = []


def webhook_allowed():
    """A flood of forged webhooks would otherwise have each one checked with PayPal."""
    now_s = time.time()
    _webhook_rejections[:] = [s for s in _webhook_rejections if now_s - s < WEBHOOK_REJECTIONS[1]]
    return len(_webhook_rejections) < WEBHOOK_REJECTIONS[0]


def is_public(path):
    return path in ("/login", "/style.css", "/logo.svg", "/webhooks/paypal") \
        or path.startswith(("/i/", "/api/i/"))


def state():
    with LOCK:
        t = now()
        invoices = q("SELECT i.*, c.name, c.company FROM invoices i JOIN contacts c ON c.id=i.contact_id ORDER BY i.id DESC")
        for i in invoices:
            i["days_overdue"] = days_late(i)
            i["late_amount"] = round(sum(r["owed"] for r in missed_rows(i)), 2)
            i["refundable"] = refundable(i)
            i["total"], i["balance"] = invoice_total(i), invoice_balance(i) if i["status"] == "sent" else 0
        return {
            "now": iso(t), "now_nice": nice(t), "demo": paypal_mode() != "paypal", "ask_paypal": paypal_ask_on(),
            "business": get_setting("business"), "currency": get_setting("currency"),
            "contacts": q("SELECT * FROM contacts ORDER BY id"),
            "subcontractors": q("SELECT id, name, email FROM subcontractors ORDER BY id"),
            "invoices": invoices,
            "payouts": q("SELECT p.*, s.name, s.email, i.number FROM payouts p JOIN subcontractors s ON s.id=p.sub_id "
                         "JOIN invoices i ON i.id=p.invoice_id ORDER BY p.id DESC"),
            "outbox": q("SELECT o.*, c.name FROM outbox o JOIN contacts c ON c.id=o.contact_id ORDER BY o.id DESC"),
            "chat": [dict(m, buttons=json.loads(m["buttons"] or "[]")) for m in q(
                "SELECT m.*, p.status AS pstatus FROM chat m LEFT JOIN proposals p ON p.id=m.proposal_id "
                "ORDER BY m.id")],
            "pending": q1("SELECT COUNT(*) n FROM proposals WHERE status='pending'")["n"],
            "clock": get_setting("clock", "simulated"),
        }


def webhook_status():
    good = q1("SELECT received_at FROM webhook_events WHERE verified=1 ORDER BY received_at DESC")
    return "last confirmed event %s" % nice(good["received_at"]) if good else "off: no events received yet"


def audit_state():
    with LOCK:
        autos = q("SELECT * FROM automations ORDER BY id")
        real = paypal_mode() == "paypal"
        return {
            "now_nice": nice(now()), "business": get_setting("business"),
            "discount_chip": get_setting("discount_chip", "0") == "1", "login": bool(owner_password()),
            "automations": [{"id": a["id"], "name": TEMPLATES[a["template"]].name,
                             "rule": TEMPLATES[a["template"]].describe(automation_params(a)),
                             "trust": TRUST_LABELS[a["trust"]], "enabled": bool(a["enabled"]),
                             "streak": a["streak"]} for a in autos if TEMPLATES[a["template"]].listed],
            "connectors": [{"name": c.label, "status": c.health()}
                           for c in (connector(n) for n in ["accounting", "payouts"] + ([] if real else ["email"]))]
                          + ([{"name": "Email", "status": "PayPal sends invoices and reminders"}] if real else [])
                          + ([{"name": "PayPal webhook", "status": webhook_status()}] if paypal_mode() == "paypal" else [])
                          + [{"name": "Claude (the assistant)", "status": LLM.status}],
            "rules": rules_lines(mandate()),
            "log": [dict(r, data=json.loads(r["data"] or "{}")) for r in q(
                "SELECT a.*, p.status AS proposal_status, p.kind FROM audit a "
                "LEFT JOIN proposals p ON p.id=a.proposal_id ORDER BY a.id DESC LIMIT 500")],
            "queue": q("SELECT id,type,status,created_at,error FROM queue ORDER BY id DESC LIMIT 50"),
        }


def invoice_rows():
    """One row per invoice, for the ledger's Invoices view."""
    rows, today = [], now().date().isoformat()
    for i in q("SELECT i.*, c.name, c.company FROM invoices i JOIN contacts c ON c.id=i.contact_id "
               "WHERE i.status!='void' ORDER BY i.id DESC"):
        plan = plan_rows(i)
        left = [r for r in plan if r["owed"] > 0]
        balance = invoice_balance(i) if i["status"] == "sent" else 0
        due = (left[0]["due"] if left else i["due_at"][:10]) if balance else None
        rows.append({"id": "row-%d" % i["id"], "invoice": i["number"], "client": i["company"] or i["name"],
                     "what": i["title"] or "Services", "total": invoice_total(i),
                     "paid": round((i["amount_paid"] or 0) - (i["refunded"] or 0), 2),
                     "balance": balance, "next_due": nice_date(due + "T12:00") if due else "", "due_sort": due or "",
                     "overdue": bool(due and due < today),
                     "plan": "%d of %d paid" % (len(plan) - len(left), len(plan)) if plan else "",
                     "status": i["status"]})
    return rows


def client_rows():
    """One row per client, for the ledger's Clients view."""
    rows = {}
    for r in invoice_rows():
        c = rows.setdefault(r["client"], {"id": r["client"], "client": r["client"], "invoices": 0, "total": 0,
                                          "paid": 0, "balance": 0, "overdue": 0, "requests": 0, "refused": 0})
        c["invoices"] += 1
        c["total"] += r["total"]
        c["paid"] += r["paid"]
        c["balance"] += r["balance"]
        c["overdue"] += 1 if r["overdue"] else 0
    for o in q("SELECT o.decision, c.name, c.company FROM offers o JOIN invoices i ON i.id=o.invoice_id "
               "JOIN contacts c ON c.id=i.contact_id WHERE o.source='agent'"):
        c = rows.get(o["company"] or o["name"])
        if c:
            c["requests"] += 1
            c["refused"] += 1 if o["decision"] == "DENY" else 0
    return sorted(rows.values(), key=lambda c: -c["balance"])


def ledger_state():
    """Every money event in one list for the AG Grid ledger, plus headline totals."""
    with LOCK:
        props = {r["dedupe"]: r for r in q("SELECT id, dedupe, status, kind FROM proposals WHERE dedupe IS NOT NULL")}
        by_id = {r["id"]: r for r in props.values()}
        rows = []

        def undoable(p):
            return p["id"] if p and p["status"] == "executed" and p["kind"] == "action" else None

        for i in q("SELECT i.*, c.name, c.company FROM invoices i JOIN contacts c ON c.id=i.contact_id"):
            rows.append({"id": "inv-%d" % i["id"], "at": i["issued_at"], "kind": "Invoice", "invoice": i["number"],
                         "client": i["company"] or i["name"], "what": i["title"] or "Services",
                         "amount": invoice_total(i), "status": i["status"], "verdict": None, "by": "automation",
                         "why": "Balance %s" % money(invoice_balance(i)) if i["status"] == "sent" else "",
                         "paypal": i["paypal_id"], "undo": None})
        for o in q("SELECT o.*, i.number, i.amount AS price, c.name, c.company FROM offers o "
                   "JOIN invoices i ON i.id=o.invoice_id JOIN contacts c ON c.id=i.contact_id"):
            terms = json.loads(o["terms"] or "{}")
            p = props.get("offer:%d" % o["id"])
            asked = json.loads(o["offer"])
            try:
                what = describe_terms(o["kind"], terms) if terms else describe_ask(asked)
            except KeyError:
                what = describe_ask(asked)
            rows.append({"id": "offer-%d" % o["id"], "at": o["at"], "kind": "Request", "invoice": o["number"],
                         "client": o["company"] or o["name"], "what": what,
                         "amount": terms.get("new_total"), "status": o["status"],
                         "verdict": "OWNER" if o["source"] == "owner" else o["decision"],
                         "by": {"owner": "you", "agent": "client via agent"}.get(o["source"], o["source"]),
                         "why": "; ".join(json.loads(o["reasons"] or "[]")), "paypal": None, "undo": undoable(p)})
        for pay in q("SELECT p.*, i.number, c.name, c.company FROM payments p JOIN invoices i ON i.id=p.invoice_id "
                     "JOIN contacts c ON c.id=i.contact_id"):
            rows.append({"id": "pay-%d" % pay["id"], "at": pay["at"], "kind": "Payment", "invoice": pay["number"],
                         "client": pay["company"] or pay["name"],
                         "what": {"PAYPAL": "Paid with PayPal",
                                  "PAY_LATER": "Paid with PayPal Pay Later", "BANK_TRANSFER": "Marked paid by you"}.get(pay["method"], "Payment"),
                         "amount": pay["amount"], "status": "received", "verdict": None, "by": "client",
                         "why": "", "paypal": pay["external_id"] or pay["reference"], "undo": None})
        for po in q("SELECT p.*, s.name, i.number FROM payouts p JOIN subcontractors s ON s.id=p.sub_id "
                    "JOIN invoices i ON i.id=p.invoice_id"):
            rows.append({"id": "payout-%d" % po["id"], "at": po["at"], "kind": "Payout", "invoice": po["number"],
                         "client": po["name"], "what": "Share paid to %s" % po["name"], "amount": -po["amount"],
                         "status": po["status"], "verdict": None, "by": "automation", "why": "",
                         "paypal": po["item_id"], "undo": undoable(by_id.get(po["proposal_id"]))})
        for r in q("SELECT r.*, i.number, c.name, c.company FROM refunds r JOIN invoices i ON i.id=r.invoice_id "
                   "JOIN contacts c ON c.id=i.contact_id"):
            rows.append({"id": "refund-%d" % r["id"], "at": r["at"], "kind": "Refund", "invoice": r["number"],
                         "client": r["company"] or r["name"],
                         "what": "Refunded through PayPal" if r["kind"] == "refund" else "Reversed by PayPal",
                         "amount": -r["amount"], "status": r["kind"] == "refund" and "refunded" or "reversed",
                         "verdict": None, "by": "PayPal", "why": "", "paypal": r["reference"], "undo": None})
        rows.sort(key=lambda r: (r["at"] or "", r["id"]), reverse=True)
        for r in rows:
            r["when"] = nice(r["at"]) if r["at"] else ""

        offers = q("SELECT decision, status, source FROM offers WHERE invoice_id>0")
        sent = q("SELECT * FROM invoices WHERE status='sent'")
        return {
            "business": get_setting("business"), "currency": get_setting("currency", "$"), "now": nice(now()),
            "kpis": {
                "outstanding": round(sum(invoice_balance(i) for i in sent), 2),
                "collected": round(q1("SELECT COALESCE(SUM(amount_paid-COALESCE(refunded,0)),0) n FROM invoices "
                                      "WHERE status!='void'")["n"], 2),
                "paid_out": round(q1("SELECT COALESCE(SUM(amount),0) n FROM payouts "
                                     "WHERE status NOT IN ('cancelled','failed')")["n"], 2),
                "agent_agreed": sum(1 for o in offers if o["decision"] == "ALLOW" and o["status"] == "applied"
                                    and o["source"] == "agent"),
                "owner_decided": sum(1 for o in offers if o["decision"] == "ESCALATE" or o["status"] == "countered"),
                "blocked": sum(1 for o in offers if o["decision"] == "DENY"),
            },
            "rows": rows, "invoices": invoice_rows(), "clients": client_rows(), "mode": paypal_mode(),
        }


STATIC = {"/": "index.html", "/audit": "audit.html", "/ledger": "ledger.html", "/login": "login.html"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
        super().end_headers()

    def send_json(self, obj, code=200):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        if not 0 < n <= 1_000_000:
            raise ValueError("That's too much to send at once.")
        return json.loads(self.rfile.read(n).decode() or "{}")

    def living_post(self, parts):
        """Client-facing routes. Only the invoice's own token grants access, and amounts come from the server."""
        try:
            body = self.body()
            token, action = parts[0], parts[1:]
            if action == ["chat"]:
                return self.send_json(client_message(token, body.get("text")))
            if action == ["orders"]:
                return self.send_json(create_checkout(token, body.get("which"), body.get("funding")))
            if len(action) == 3 and action[0] == "orders" and action[2] == "capture":
                return self.send_json(capture_checkout(token, action[1]))
            if len(action) == 3 and action[0] == "orders" and action[2] == "fake-approve":
                return self.send_json(fake_approve(token, action[1]))
            if action == ["reminders"]:
                return self.send_json(set_reminders(token, bool(body.get("on"))))
            if action == ["demo-pay"]:
                return self.send_json(demo_pay(token, body.get("which")))
            return self.send_json({"error": "not found"}, 404)
        except KeyError:
            return self.send_json({"error": "This invoice link isn't valid."}, 404)
        except ValueError as e:
            return self.send_json({"error": str(e)}, 400)
        except PayPalError as e:
            print("PayPal error on the invoice page:", e)
            return self.send_json({"error": "PayPal couldn't complete that (%s). You haven't been charged."
                                   % e.name}, 502)

    def redirect(self, location):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        url = urlparse(self.path)
        path = url.path
        if not is_public(path) and not signed_in(self.headers.get("Cookie")):
            if path.startswith("/api/"):
                return self.send_json({"error": "Please sign in."}, 401)
            return self.redirect("/login")
        if path.startswith("/i/"):
            return self.static("invoice.html")
        if path.startswith("/api/i/") and path.endswith("/calendar.ics"):
            try:
                data, number = calendar_file(path[len("/api/i/"):-len("/calendar.ics")])
            except KeyError:
                return self.send_json({"error": "This invoice link isn't valid."}, 404)
            self.send_response(200)
            self.send_header("Content-Type", "text/calendar; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="%s-payments.ics"' % number)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return None
        if path.startswith("/api/i/"):
            try:
                return self.send_json(living_state(path[len("/api/i/"):]))
            except KeyError:
                return self.send_json({"error": "This invoice link isn't valid."}, 404)
        if path == "/api/state":
            return self.send_json(self.page_state(signed_in(self.headers.get("Cookie"))))
        if path == "/api/audit":
            return self.send_json(dict(audit_state(), role=signed_in(self.headers.get("Cookie"))))
        if path == "/api/ledger":
            return self.send_json(ledger_state())
        return self.static(STATIC.get(path) or path.lstrip("/"))

    def static(self, name):
        full = os.path.normpath(os.path.join(APP_DIR, name))
        if not full.startswith(APP_DIR) or not os.path.isfile(full):
            return self.send_json({"error": "not found"}, 404)
        ctype = {"html": "text/html", "js": "text/javascript", "css": "text/css",
                 "svg": "image/svg+xml"}.get(full.rsplit(".", 1)[-1], "application/octet-stream")
        with open(full, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_cookie(self, value, max_age):
        secure = "; Secure" if os.environ.get("PUBLIC_URL", "").startswith("https://") else ""
        data = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Set-Cookie", "living_owner=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Lax%s"
                         % (value, max_age, secure))
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def address(self):
        # Behind Render's proxy, the last entry is the one the proxy itself added, so a client can't fake it.
        forwarded = self.headers.get("X-Forwarded-For", "")
        return forwarded.rsplit(",", 1)[-1].strip() or self.client_address[0]

    def login(self):
        try:
            role = check_login(self.body().get("password"), self.address())
            if not role:
                return self.send_json({"error": "That password isn't right."}, 401)
        except ValueError as e:
            return self.send_json({"error": str(e)}, 429)
        return self.send_cookie(session_value(int(time.time()) + SESSION_SECONDS, role), SESSION_SECONDS)

    def page_state(self, role):
        data = dict(state(), role=role)
        if role == "judge":
            data["tour"] = tour_state()
        return data

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/webhooks/paypal":
            n = int(self.headers.get("Content-Length") or 0)
            if not 0 < n <= WEBHOOK_MAX_BYTES:
                return self.send_json({"ok": False}, 413)
            if not webhook_allowed():
                return self.send_json({"ok": False}, 429)
            code = paypal_webhook(self.headers, self.rfile.read(n).decode("utf-8", "replace"))
            if code == 401:
                _webhook_rejections.append(time.time())
            return self.send_json({"ok": code == 200}, code)
        if path.startswith("/api/i/"):
            return self.living_post(path[len("/api/i/"):].split("/"))
        if path == "/login":
            return self.login()
        role = signed_in(self.headers.get("Cookie"))
        if not role:
            return self.send_json({"error": "Please sign in."}, 401)
        if path == "/logout":
            return self.send_cookie("", 0)
        judge = role == "judge"
        try:
            body = self.body()
            if path == "/api/chat":
                text = (body.get("text") or "").strip()[:1000]
                if text:
                    if judge:
                        judge_may_chat()
                    owner_message(text)
            elif path == "/api/invoices/new":
                if judge:
                    judge_may_invoice()
                new_invoice(body)
            elif judge and path.startswith("/api/refunds/") and os.environ.get("PAYPAL_ENV", "sandbox") != "sandbox":
                return self.send_json({"error": "Only the owner can refund outside the sandbox."}, 403)
            elif path == "/api/refunds/send":
                refund_invoice(body["invoice_id"], body.get("amount"), body.get("note"), body.get("key"),
                               body.get("request_id"))
            elif path == "/api/refunds/decline":
                decline_refund_request(body["request_id"])
            elif judge and path == "/api/tour/start":
                start_tour()
            elif judge and path == "/api/tour/late":
                miss_payment(body.get("days"))
            elif path == "/api/invoices/paid":
                mark_paid(body["id"])
            elif path == "/api/demo/clock":
                advance_clock(body.get("hours", 24))
            elif path == "/api/reset":
                if paypal_mode() == "paypal":
                    raise ValueError("Reset is only for the demo.")
                reset()
            elif path == "/api/settings/business":
                name = str(body.get("name") or "").strip()[:127]
                if not name:
                    raise ValueError("Type your business name first")
                with LOCK:
                    if name != get_setting("business"):
                        set_setting("business", name)
                        log("setting_changed", "Business name changed to %s" % name, actor="owner")
            elif path == "/api/settings/discount_chip":
                on = bool(body.get("on"))
                with LOCK:
                    set_setting("discount_chip", "1" if on else "0")
                    log("setting_changed", "Discount suggestion %s on invoices" % ("shown" if on else "hidden"),
                        actor="owner")
            else:
                return self.send_json({"error": "not found"}, 404)
        except ValueError as e:
            return self.send_json({"error": str(e)}, 400)
        except (KeyError, TypeError) as e:
            return self.send_json({"error": "bad request: %r" % e}, 400)
        except PayPalError as e:
            return self.send_json({"error": str(e)}, 502)
        return self.send_json(self.page_state(role))


def main():
    init_db()
    threading.Thread(target=scheduler, daemon=True).start()
    host = os.environ.get("HOST", "127.0.0.1")
    if host not in ("127.0.0.1", "localhost", "::1") and not owner_password():
        sys.exit("Set LIVING_OWNER_PASSWORD before putting Living Pay on the internet.")
    server = ThreadingHTTPServer((host, PORT), Handler)
    print("Living Pay on http://localhost:%d  (Claude layer: %s)" % (PORT, LLM.status))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
