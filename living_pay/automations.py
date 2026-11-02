"""The automations, and the executor that proposes, checks, runs and undoes their actions."""

from datetime import date, timedelta, datetime
import json
import re

from living_pay.paypal_api import PayPalError
from living_pay.config import LOCK
from living_pay.store import ex, first_name, get_setting, iso, log, money, nice_date, now, post, q, q1, who
from living_pay.invoices import cash, invoice_link, invoice_total, plan_rows
from living_pay.connectors import RetryLater, connector, paypal_mode
from living_pay.llm import LLM


def payment_received(inv, result, actor):
    """Tell the owner about a payment and let automations react to it."""
    if result.get("duplicate"):
        return
    c = q1("SELECT * FROM contacts WHERE id=?", (inv["contact_id"],))
    if result["fully_paid"]:
        log("invoice_paid", "%s paid in full by %s" % (inv["number"], who(c)), actor=actor,
            data={"invoice_id": inv["id"], "amount": result["amount"]})
        post("bot", "**Paid:** %s paid %s, and %s is now settled." % (who(c), money(result["amount"]), inv["number"]))
        enqueue("accounting.invoice_paid", {"invoice_id": inv["id"]}, "paid:%d" % inv["id"])
    else:
        log("payment_received", "%s paid %s towards %s" % (who(c), money(result["amount"]), inv["number"]),
            actor=actor, data={"invoice_id": inv["id"], "amount": result["amount"], "balance": result["balance"]})
        post("bot", "**Payment:** %s paid %s towards %s. %s still due."
             % (who(c), money(result["amount"]), inv["number"], money(result["balance"])))
        enqueue("accounting.payment_received", {"invoice_id": inv["id"], "amount": result["amount"]})


def enqueue(etype, payload, dedupe=None):
    """Every inbound event is stored before processing, deduplicated by key."""
    ex("INSERT OR IGNORE INTO queue(type,payload,dedupe,created_at) VALUES(?,?,?,?)",
       (etype, json.dumps(payload), dedupe, iso(now())))


TRUST_LABELS = {
    "observe": "watch only",
    "suggest": "asks you first",
    "auto_notice": "automatic, tells you after",
    "silent": "automatic, weekly summary only",
}


class Template:
    key = name = ""
    triggers = ()
    default_trust = "suggest"
    default_params = {}
    minutes_saved = 5
    listed = True  # shown under Automations in Settings

    def describe(self, p):
        return ""

    def evaluate(self, auto, params, event):
        return []

    def precheck(self, draft):
        return None


def invoice_draft(c, title, amount, due_days, sub_id=None, sub_share=0):
    """The steps for one invoice. PayPal delivers real ones; without it, a practice email stands in."""
    biz = get_setting("business")
    body = ("Hi %s,\n\nThank you for your business. Please find invoice {s0.number} for %s "
            "for “%s”. Payment is due by {s0.due}.\n\nYou can pay it, ask about it, or arrange "
            "a payment plan here: {s0.link}\n\nBest regards,\n%s" % (first_name(c), money(amount), title, biz))
    real = paypal_mode() == "paypal"
    steps = [{"connector": "accounting", "op": "create_invoice",
              "label": "Send the invoice to %s through PayPal" % c["email"] if real else "Create the invoice",
              "params": {"contact_id": c["id"], "title": title, "amount": amount,
                         "due_days": due_days, "sub_id": sub_id, "sub_share": sub_share}}]
    if not real:
        steps.append({"connector": "email", "op": "send", "label": "Email the invoice to %s" % c["email"],
                      "params": {"contact_id": c["id"], "subject": "Invoice {s0.number} from %s" % biz, "body": body,
                                 "correction_subject": "Please disregard invoice {s0.number}",
                                 "correction_body": "Hi %s,\n\nPlease disregard invoice {s0.number}, which was sent "
                                                    "in error. Apologies for the confusion.\n\n%s" % (first_name(c), biz)}})
    return {"contact_id": c["id"], "amount": amount, "steps": steps,
            "preview": body.replace("{s0.number}", "INV-xxxx").replace("{s0.due}", "the due date")
                           .replace("{s0.link}", "(invoice link)")}


class SendInvoices(Template):
    """What the New invoice form runs on. The owner asked for it, so it never waits for approval."""
    key, name = "send_invoices", "Send invoices"
    listed = False
    default_params = {"due_days": 14}
    minutes_saved = 12

    def describe(self, p):
        return "When you create an invoice, send it through PayPal with its own page (due in %d days)." % p["due_days"]


class OverdueReminders(Template):
    key, name = "overdue_reminders", "Chase overdue invoices"
    triggers = ("clock.tick", "clock.day")
    default_params = {"stages": [7, 14, 30]}
    minutes_saved = 6

    def describe(self, p):
        days = ", ".join(str(s) for s in p["stages"])
        return ("When an invoice is %s days overdue, send the client a reminder by email, "
                "firmer each time." % days)

    def evaluate(self, auto, p, event):
        drafts = []
        today = now().date()
        stages = sorted(p["stages"])
        for inv in q("SELECT * FROM invoices WHERE status='sent'"):
            due = datetime.fromisoformat(inv["due_at"])
            days = (today - due.date()).days
            hit = [s for s in stages if s <= days]
            if not hit:
                continue
            stage = hit[-1]
            dedupe = "rem:%d:%d" % (inv["id"], stage)
            if q1("SELECT id FROM proposals WHERE dedupe=?", (dedupe,)):
                continue
            tone = ["friendly", "firm", "final"][min(stages.index(stage), 2)]
            c = q1("SELECT * FROM contacts WHERE id=?", (inv["contact_id"],))
            drafts.append(self._draft(p, inv, c, days, stage, tone, dedupe))
        return drafts

    def _draft(self, p, inv, c, days, stage, tone, dedupe):
        biz, amt, num = get_setting("business"), money(inv["amount"]), inv["number"]
        due = nice_date(inv["due_at"])
        name = first_name(c)
        if tone == "friendly":
            subject = "Friendly reminder: invoice %s" % num
            body = ("Hi %s,\n\nJust a quick reminder that invoice %s for %s was due on %s. "
                    "If payment is already on its way, thank you, and please ignore this.\n\n"
                    "Best regards,\n%s" % (name, num, amt, due, biz))
        elif tone == "firm":
            subject = "Invoice %s is %d days overdue" % (num, days)
            body = ("Hi %s,\n\nInvoice %s for %s is now %d days past its due date of %s. "
                    "Could you let me know when we can expect payment?\n\nThank you,\n%s"
                    % (name, num, amt, days, due, biz))
        else:
            subject = "Final reminder: invoice %s" % num
            body = ("Hi %s,\n\nThis is a final reminder that invoice %s for %s is %d days overdue. "
                    "Please arrange payment within 7 days, or reply so we can agree a plan.\n\n%s"
                    % (name, num, amt, days, biz))
        body = LLM.polish(tone, {"invoice": num, "amount": amt, "days_overdue": days,
                                 "due_date": due, "client_first_name": name, "business": biz},
                                body, must_include=[num, amt])
        step = reminder_step(inv, c, subject, body, tone + " reminder")
        return {
            "summary": "Send a %s reminder to %s for %s (%s)" % (tone, who(c), num, amt),
            "explanation": "%s is %d days past due (%s) and still unpaid. This is the day-%d %s reminder."
                           % (num, days, due, stage, tone),
            "dedupe": dedupe, "contact_id": c["id"], "invoice_id": inv["id"],
            "preview": body, "steps": [step],
        }

    def precheck(self, draft):
        inv = q1("SELECT * FROM invoices WHERE id=?", (draft["invoice_id"],))
        if not inv or inv["status"] != "sent":
            return "%s is now %s" % (inv["number"], inv["status"]) if inv else "the invoice is gone"
        return None


class WeeklySummary(Template):
    key, name = "weekly_summary", "Monday summary"
    triggers = ("clock.day",)
    default_trust = "auto_notice"
    default_params = {"weekday": 0}
    DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

    def describe(self, p):
        return ("Every %s morning, send you a summary: invoiced, collected, overdue, "
                "and the admin time I saved you." % self.DAYS[p["weekday"]])

    def evaluate(self, auto, p, event):
        day = datetime.fromisoformat(event["date"])
        if day.weekday() != p["weekday"]:
            return []
        return [{"summary": "Weekly summary for %s" % nice_date(day),
                 "explanation": "It's %s." % self.DAYS[p["weekday"]],
                 "dedupe": "week:%s" % event["date"], "owner_only": True,
                 "steps": [{"connector": "owner", "op": "summary", "label": "Message you the summary",
                            "params": {}}]}]


def reminder_step(inv, contact, subject, body, what):
    """PayPal emails the reminder for its own invoices; without it, a practice email stands in."""
    if inv.get("paypal_id"):
        return {"connector": "accounting", "op": "remind", "label": "PayPal %s to %s" % (what, contact["email"]),
                "params": {"invoice_id": inv["id"], "subject": subject, "note": body}}
    return {"connector": "email", "op": "send", "label": "Email %s to %s" % (what, contact["email"]),
            "params": {"contact_id": contact["id"], "subject": subject, "body": body}}


class PayoutOnPaid(Template):
    key, name = "payout_on_paid", "Pay subcontractors"
    triggers = ("accounting.invoice_paid",)
    minutes_saved = 8

    def describe(self, p):
        return ("When an invoice is paid in full, send the subcontractor on that job their share "
                "with PayPal Payouts.")

    def evaluate(self, auto, p, event):
        inv = q1("SELECT * FROM invoices WHERE id=?", (event["invoice_id"],))
        sub_id, share = (inv or {}).get("sub_id"), (inv or {}).get("sub_share")
        if not inv or inv["status"] != "paid" or not sub_id or not share:
            return []
        title = inv["title"] or "Services"
        sub = q1("SELECT * FROM subcontractors WHERE id=?", (sub_id,))
        amount = round(min(share, invoice_total(inv)), 2)
        return [{
            "summary": "Pay %s %s for “%s”" % (sub["name"], money(amount), title),
            "explanation": "%s is paid in full, and %s's share of this job is %s."
                           % (inv["number"], first_name(sub), money(amount)),
            "dedupe": "payout:%d" % inv["id"], "invoice_id": inv["id"], "sub_id": sub["id"],
            "steps": [{"connector": "payouts", "op": "send_payout",
                       "label": "Send %s to %s (%s) with PayPal Payouts" % (money(amount), sub["name"], sub["email"]),
                       "params": {"invoice_id": inv["id"], "sub_id": sub["id"], "amount": amount,
                                  "note": "Your share of “%s” for %s (%s). Thank you!"
                                          % (title, get_setting("business"), inv["number"])}}],
        }]

    def precheck(self, draft):
        inv = q1("SELECT * FROM invoices WHERE id=?", (draft["invoice_id"],))
        if not inv or inv["status"] != "paid":
            return "%s is no longer paid in full" % inv["number"] if inv else "the invoice is gone"
        if q1("SELECT id FROM payouts WHERE invoice_id=? AND sub_id=? AND status NOT IN ('cancelled','failed')",
              (draft["invoice_id"], draft["sub_id"])):
            return "the subcontractor has already been paid for this job"
        return None


REMIND_DAYS_BEFORE = 3


class PaymentReminders(Template):
    """Reminds clients who asked for it, ahead of each payment on their plan."""
    key, name = "payment_reminders", "Remind clients about plan payments"
    triggers = ("clock.tick", "clock.day")
    default_trust = "silent"  # the client asked for these, so they don't need the owner
    minutes_saved = 3

    def describe(self, p):
        return ("When a client on a payment plan asks for reminders, email them %d days before each payment."
                % REMIND_DAYS_BEFORE)

    def evaluate(self, auto, p, event):
        drafts, today = [], now().date()
        for inv in q("SELECT * FROM invoices WHERE status='sent' AND remind_email=1"):
            left = [r for r in plan_rows(inv) if r["owed"] > 0]
            if not left or not 0 <= (date.fromisoformat(left[0]["due"]) - today).days <= REMIND_DAYS_BEFORE:
                continue
            row, c = left[0], q1("SELECT * FROM contacts WHERE id=?", (inv["contact_id"],))
            when = nice_date(row["due"] + "T12:00")
            body = ("Hi %s,\n\nHere's the reminder you asked for: your next payment of %s for invoice %s is due on "
                    "%s.\n\nYou can pay it here: %s\n\nThank you,\n%s"
                    % (first_name(c), cash(row["owed"]), inv["number"], when, invoice_link(inv), get_setting("business")))
            drafts.append({
                "summary": "Remind %s about %s due %s" % (who(c), cash(row["owed"]), when),
                "explanation": "They asked for reminders on %s." % inv["number"],
                "dedupe": "planrem:%d:%s" % (inv["id"], row["due"]), "invoice_id": inv["id"], "due": row["due"],
                "contact_id": c["id"], "preview": body,
                "steps": [reminder_step(inv, c, "Your next payment for %s is due %s" % (inv["number"], when),
                                        body, "payment reminder")],
            })
        return drafts

    def precheck(self, draft):
        inv = q1("SELECT * FROM invoices WHERE id=?", (draft["invoice_id"],))
        if not inv or inv["status"] != "sent" or not inv["remind_email"]:
            return "reminders no longer apply to this invoice"
        if not [r for r in plan_rows(inv) if r["owed"] > 0 and r["due"] == draft["due"]]:
            return "that payment has already been made"
        return None


class NegotiateInvoices(Template):
    """Driven by the client chat on each invoice page, not by queued events."""
    key, name = "invoice_negotiation", "Negotiate on invoices"
    default_trust = "auto_notice"
    minutes_saved = 15

    def describe(self, p):
        return ("When a client asks their invoice for a discount, a payment plan or more time, agree to what "
                "your rules allow, ask you about anything else, and decline the rest.")

    def precheck(self, draft):
        inv = q1("SELECT * FROM invoices WHERE id=?", (draft["invoice_id"],))
        if not inv or inv["status"] != "sent":
            return "%s is now %s" % (inv["number"], inv["status"]) if inv else "the invoice is gone"
        return None


TEMPLATES = {t.key: t for t in [SendInvoices(), OverdueReminders(), WeeklySummary(), NegotiateInvoices(),
                                 PayoutOnPaid(), PaymentReminders()]}


def summary_text():
    t = now()
    week = iso(t - timedelta(days=7))
    biz = get_setting("business")
    invoiced = q1("SELECT COALESCE(SUM(amount),0) s, COUNT(*) n FROM invoices "
                  "WHERE issued_at>=? AND status!='void'", (week,))
    collected = q1("SELECT COALESCE(SUM(amount),0) s, COUNT(*) n FROM invoices "
                   "WHERE paid_at>=? AND status='paid'", (week,))
    open_inv = q("SELECT * FROM invoices WHERE status='sent'")
    overdue = [i for i in open_inv if i["due_at"] < iso(t)]
    done = q("SELECT p.*, a.template FROM proposals p LEFT JOIN automations a ON a.id=p.automation_id "
             "WHERE p.status='executed' AND p.decided_at>=? AND p.kind='action'", (week,))
    minutes = sum(TEMPLATES[d["template"]].minutes_saved for d in done if d["template"] in TEMPLATES)
    lines = ["**Your week at %s**" % biz,
             "Invoiced: %s across %d invoice%s" % (money(invoiced["s"]), invoiced["n"], "" if invoiced["n"] == 1 else "s"),
             "Collected: %s" % money(collected["s"]),
             "Outstanding: %s across %d invoice%s, %d overdue (%s)" % (
                 money(sum(i["amount"] for i in open_inv)), len(open_inv), "" if len(open_inv) == 1 else "s",
                 len(overdue), money(sum(i["amount"] for i in overdue)))]
    if done:
        lines.append("I handled %d task%s for you, about %dh %02dm of admin." % (
            len(done), "" if len(done) == 1 else "s", minutes // 60, minutes % 60))
    return "\n".join(lines)


def automation_params(auto):
    p = dict(TEMPLATES[auto["template"]].default_params)
    p.update(json.loads(auto["params"] or "{}"))
    return p


def process_queue():
    with LOCK:
        for item in q("SELECT * FROM queue WHERE status='pending' ORDER BY id"):
            event = json.loads(item["payload"])
            try:
                for auto in q("SELECT * FROM automations WHERE enabled=1 ORDER BY id"):
                    tpl = TEMPLATES[auto["template"]]
                    if item["type"] not in tpl.triggers:
                        continue
                    for draft in tpl.evaluate(auto, automation_params(auto), event):
                        dispatch(auto, draft)
                ex("UPDATE queue SET status='done', processed_at=? WHERE id=?", (iso(now()), item["id"]))
            except Exception as e:  # keep the queue moving; the event stays visible as failed
                ex("UPDATE queue SET status='failed', error=? WHERE id=?", (repr(e), item["id"]))
        sweep_stale()


def hard_limit_reasons(draft):
    """Limits no trust level overrides."""
    reasons = []
    ceiling = float(get_setting("invoice_ceiling", 5000))
    for step in draft["steps"]:
        if step["op"] == "create_invoice" and step["params"]["amount"] > ceiling:
            reasons.append("the amount is above your %s limit" % money(ceiling))
        if step["op"] == "send_payout":
            sub = q1("SELECT * FROM subcontractors WHERE id=?", (step["params"]["sub_id"],))
            if not q1("SELECT id FROM payouts WHERE sub_id=? AND status NOT IN ('cancelled','failed')", (sub["id"],)):
                reasons.append("it's the first payout to %s" % sub["name"])
            if step["params"]["amount"] > ceiling:
                reasons.append("the payout is above your %s limit" % money(ceiling))
        if step["op"] == "send":
            c = q1("SELECT * FROM contacts WHERE id=?", (step["params"]["contact_id"],))
            if c and not c["contacted"]:
                reasons.append("it's the first message to %s" % c["name"])
    return sorted(set(reasons))


# What the executor reports back to the negotiator and the rules, which sit above this module.
offer_hooks = {"settled": lambda draft, outcome: None, "undone": lambda offer_id: None}


def dispatch(auto, draft):
    if q1("SELECT id FROM proposals WHERE dedupe=?", (draft["dedupe"],)):
        return
    reasons = [] if draft.get("owner_only") else hard_limit_reasons(draft) + draft.get("policy_reasons", [])
    trust = auto["trust"]
    pid = ex("INSERT INTO proposals(kind,automation_id,summary,explanation,draft,reasons,status,dedupe,created_at) "
             "VALUES('action',?,?,?,?,?,?,?,?)",
             (auto["id"], draft["summary"], draft["explanation"], json.dumps(draft),
              json.dumps(reasons), "new", draft["dedupe"], iso(now())))
    log("proposed", draft["summary"], pid, auto["id"], data={"why": draft["explanation"], "limits": reasons})

    if trust == "observe" and not draft.get("owner_only"):
        ex("UPDATE proposals SET status='observed', decided_at=? WHERE id=?", (iso(now()), pid))
        log("observed", "Watch only: would have done this", pid, auto["id"])
        return
    if draft.get("owner_only") or (trust in ("auto_notice", "silent") and not reasons):
        execute(pid, actor="living_pay")
        return

    ex("UPDATE proposals SET status='pending' WHERE id=?", (pid,))
    msg = ["**Approve?** %s" % draft["summary"], "Why: %s" % draft["explanation"]]
    if reasons:
        msg.append("Asking because %s." % " and ".join(reasons))
    if draft.get("preview"):
        msg.append("> " + draft["preview"].replace("\n\n", "\n").replace("\n", "\n> "))
    if any(s["op"] == "send" for s in draft["steps"]):
        msg.append("_Messages can't be unsent. Undo would send a correction where one applies._")
    if draft.get("offer_id"):
        msg.append("_Or make a counter-offer: type “counter $750”._")
    post("bot", "\n".join(msg), pid, [{"label": "Approve", "send": "approve #%d" % pid},
                                      {"label": "Decline" if draft.get("offer_id") else "Skip", "send": "skip #%d" % pid}])


def fill(value, ctx):
    """Substitute {s0.number}-style references to earlier step results."""
    if isinstance(value, str):
        return re.sub(r"\{s(\d+)\.(\w+)\}", lambda m: str(ctx[int(m.group(1))][m.group(2)]), value)
    if isinstance(value, dict):
        return {k: fill(v, ctx) for k, v in value.items()}
    return value


def execute(pid, actor):
    p = q1("SELECT * FROM proposals WHERE id=?", (pid,))
    draft = json.loads(p["draft"])
    auto = q1("SELECT * FROM automations WHERE id=?", (p["automation_id"],))
    tpl = TEMPLATES[auto["template"]]
    stale = tpl.precheck(draft)
    if stale:
        ex("UPDATE proposals SET status='cancelled', decided_at=? WHERE id=?", (iso(now()), pid))
        log("cancelled", "Not done: %s" % stale, pid, auto["id"], actor)
        post("bot", "I didn't do this after all: %s, because %s." % (p["summary"], stale), pid)
        return False

    ctx, done = [], []
    try:
        for step in draft["steps"]:
            if step["connector"] == "owner":
                post("bot", summary_text(), pid)
                result, inverse = {}, None
            else:
                conn = connector(step["connector"])
                params = fill(step["params"], ctx)
                params["proposal_id"] = pid
                result = conn.execute(step["op"], params)
                inverse = conn.inverse(step["op"], params, result)
                if inverse:
                    inverse = fill(inverse, ctx)
            ctx.append(result)
            done.append({"label": fill(step["label"], ctx), "connector": step["connector"],
                         "op": step["op"], "result": result, "inverse": inverse})
    except Exception as e:
        ex("UPDATE proposals SET status='failed', executed=?, decided_at=? WHERE id=?",
           (json.dumps(done), iso(now()), pid))
        log("failed", "Failed: %r" % e, pid, auto["id"], actor, {"steps": done})
        post("bot", "Something went wrong with: %s. Nothing else was changed. (%s)" % (p["summary"], e), pid)
        return False

    ex("UPDATE proposals SET status='executed', executed=?, decided_at=? WHERE id=?",
       (json.dumps(done), iso(now()), pid))
    log("executed", p["summary"], pid, auto["id"], actor, {"steps": done, "why": p["explanation"]})
    if draft.get("offer_id"):
        offer_hooks["settled"](draft, "approved" if actor == "owner" else "applied")

    if draft.get("owner_only"):
        return True
    if actor == "living_pay" and auto["trust"] == "auto_notice":
        undoable = any(d["inverse"] for d in done)
        msg = "**Done:** %s\nWhy: %s" % (p["summary"], p["explanation"])
        post("bot", msg, pid, [{"label": "Undo", "send": "undo #%d" % pid}] if undoable else [])
    return True


def undo(pid, actor="owner"):
    p = q1("SELECT * FROM proposals WHERE id=?", (pid,))
    if not p or p["status"] != "executed" or p["kind"] != "action":
        return "There's nothing to undo for #%d." % pid
    steps = json.loads(p["executed"] or "[]")
    reversed_labels, kept = [], []
    for s in reversed(steps):
        inv = s.get("inverse")
        if inv:
            try:
                connector(inv["connector"]).execute(inv["op"], dict(inv["params"], proposal_id=pid))
                reversed_labels.append(inv["label"])
            except RetryLater as e:
                if not reversed_labels:  # nothing changed yet, so leave the action undoable
                    log("undo_deferred", "%s: %s" % (p["summary"], e), pid, p["automation_id"], actor)
                    return "I couldn't undo that yet: %s. Nothing has changed." % e
                kept.append("%s (%s)" % (s["label"], e))
            except (PayPalError, ValueError) as e:
                kept.append("%s (%s)" % (s["label"], getattr(e, "message", None) or e))
        elif s["connector"] != "owner":
            kept.append(s["label"])
    ex("UPDATE proposals SET status='undone', decided_at=? WHERE id=?", (iso(now()), pid))
    offer_hooks["settled"](json.loads(p["draft"] or "{}"), "reverted")
    log("undone", "Undid: %s" % p["summary"], pid, p["automation_id"], actor,
        {"reversed": reversed_labels, "not_reversible": kept})
    auto = q1("SELECT * FROM automations WHERE id=?", (p["automation_id"],))
    msg = ["**Undone:** %s" % p["summary"]]
    if reversed_labels:
        msg.append("I did: " + "; ".join(reversed_labels) + ".")
    if kept:
        msg.append("Couldn't reverse: " + "; ".join(kept) + ".")
    ex("UPDATE automations SET streak=0 WHERE id=?", (auto["id"],))
    shrunk = offer_hooks["undone"](json.loads(p["draft"] or "{}").get("offer_id"))
    if shrunk:
        msg.append(shrunk)
    elif auto["template"] == "invoice_negotiation":
        pass  # the agent's rules are its trust level; one undo inside them doesn't change them
    elif auto["trust"] in ("auto_notice", "silent"):
        ex("UPDATE automations SET trust='suggest' WHERE id=?", (auto["id"],))
        log("trust_changed", "%s: back to asking first after an undo" % TEMPLATES[auto["template"]].name,
            None, auto["id"])
        msg.append("I'll ask you first for **%s** from now on. Reply “auto %s” to change that."
                   % (TEMPLATES[auto["template"]].name.lower(), TEMPLATES[auto["template"]].name.lower()))
    return "\n".join(msg)


def sweep_stale():
    """Pending proposals whose reason disappeared (the invoice was paid, say) are withdrawn."""
    for p in q("SELECT p.*, a.template FROM proposals p JOIN automations a ON a.id=p.automation_id "
               "WHERE p.status='pending' AND p.kind='action'"):
        stale = TEMPLATES[p["template"]].precheck(json.loads(p["draft"]))
        if stale:
            ex("UPDATE proposals SET status='cancelled', decided_at=? WHERE id=?", (iso(now()), p["id"]))
            log("cancelled", "Withdrawn: %s" % stale, p["id"], p["automation_id"])
            post("bot", "No longer needed, so I've withdrawn it: %s (%s)." % (p["summary"], stale), p["id"])


def latest_pending():
    rows = q("SELECT * FROM proposals WHERE status='pending' ORDER BY id DESC")
    return rows[0] if rows else None
