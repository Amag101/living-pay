"""What an invoice owes: totals, balances and payment plan schedules."""

import json
from datetime import date, datetime, timedelta

from living_pay import policy
from living_pay.config import PUBLIC_URL
from living_pay.store import get_setting, nice_date, now, q, q1


def invoice_total(inv):
    """What the client owes in total after any negotiated discount."""
    return round(inv["amount"] * (1 - (inv.get("discount_pct") or 0) / 100.0), 2)


def invoice_balance(inv):
    return round(max(invoice_total(inv) - (inv.get("amount_paid") or 0), 0), 2)


def refund_sources(inv):
    """The payments this app can send money back through, newest first, with what's left on each.
    A bank transfer the owner marked as paid can't be refunded through PayPal, so it isn't here."""
    rows = []
    for p in q("SELECT * FROM payments WHERE invoice_id=? AND (reference LIKE 'capture:%' OR reference LIKE 'demo:%') "
               "ORDER BY id DESC", (inv["id"],)):
        back = q1("SELECT COALESCE(SUM(amount),0) n FROM refunds WHERE payment_ref=?", (p["reference"],))["n"]
        left = round(p["amount"] - back, 2)
        if left > 0:
            rows.append({"reference": p["reference"], "left": left})
    return rows


def refundable(inv):
    if inv["status"] not in ("sent", "paid"):
        return 0
    net = round((inv["amount_paid"] or 0) - (inv["refunded"] or 0), 2)
    return max(0, min(round(sum(r["left"] for r in refund_sources(inv)), 2), net))


def invoice_link(inv):
    return "%s/i/%s" % (PUBLIC_URL, inv["token"])


TERMS = ("discount_pct", "min_due", "due_at", "plan")  # plan is local: PayPal only knows min_due


def paypal_note(inv):
    """The note on PayPal's invoice page. PayPal has no field for a schedule (only a minimum
    payment), so the agreed plan is written out here for anyone paying on PayPal's own page."""
    ask = "Questions, or need a payment plan? Ask the invoice: %s" % invoice_link(inv)
    left = [r for r in plan_rows(inv) if r["owed"] > 0]
    if not left:
        return ask
    return "Payment plan agreed: %s. Next payment: %s by %s. Questions, or want to change the plan? Ask the invoice: %s" % (
        "; ".join("%s by %s" % (cash(r["owed"]), nice_date(r["due"] + "T12:00")) for r in left),
        cash(due_now(inv)), nice_date(left[0]["due"] + "T12:00"), invoice_link(inv))


def cash(x):
    """Money for the client page: whole amounts without cents, others with them."""
    x = round(float(x), 2)
    sym = get_setting("currency", "$")
    return "%s%s" % (sym, format(int(x), ",") if x == int(x) else format(x, ",.2f"))


def discount_needs_full_payment(inv):
    """Is the invoice's current discount one that was given for paying in full now?"""
    if not inv.get("discount_pct") or inv.get("amount_paid"):
        return False
    o = q1("SELECT terms FROM offers WHERE invoice_id=? AND kind='discount' AND status IN ('applied','approved') "
           "ORDER BY id DESC", (inv["id"],))
    terms = json.loads(o["terms"]) if o else {}
    return bool(terms.get("requires_full_payment_now")) and terms.get("discount_pct") == inv["discount_pct"]


def plan_of(inv):
    return json.loads(inv["plan"]) if inv.get("plan") else None


def plan_rows(inv):
    """The plan's installments with what is still owed on each."""
    plan = plan_of(inv)
    if not plan:
        return []
    paid = round((inv["amount_paid"] or 0) - plan["base_paid"], 2)
    rows = []
    for p in plan["schedule"]:
        covered = min(max(paid, 0), p["amount"])
        paid = round(paid - p["amount"], 2)
        rows.append(dict(p, owed=round(p["amount"] - covered, 2)))
    return rows


def missed_rows(inv):
    """Plan payments whose date has passed and that are still owed."""
    if inv["status"] != "sent":
        return []
    today = now().date().isoformat()
    return [r for r in plan_rows(inv) if r["owed"] > 0 and r["due"] < today]


def days_late(inv):
    """How late the invoice is: from the oldest missed plan payment, or from its due date with no plan."""
    if inv["status"] != "sent":
        return 0
    if plan_rows(inv):
        missed = missed_rows(inv)
        return (now().date() - date.fromisoformat(missed[0]["due"])).days if missed else 0
    return max(0, (now().date() - datetime.fromisoformat(inv["due_at"]).date()).days)


def due_now(inv):
    """What the client should pay today: anything missed plus the next installment, or the whole balance."""
    if inv["status"] != "sent":
        return 0
    owed = [r["owed"] for r in plan_rows(inv) if r["owed"] > 0]
    if not owed:
        return invoice_balance(inv)
    missed = round(sum(r["owed"] for r in missed_rows(inv)), 2)
    return min(missed or owed[0], invoice_balance(inv))


def next_due(inv):
    """(is it due today or earlier, the date in words) for the next payment. A plan's later
    installments aren't due yet, so nothing should call them "due now"."""
    left = [r for r in plan_rows(inv) if r["owed"] > 0]
    day = left[0]["due"] if left else inv["due_at"][:10]
    return day <= now().date().isoformat() or not left, "today" if day <= now().date().isoformat() else nice_date(day + "T12:00")


def describe_ask(offer):
    """What was asked for, in words, for offers that never got as far as terms."""
    kind = offer.get("kind")
    try:
        if kind == "discount":
            return "%g%% off" % round(float(offer["pct"]), 1)
        if kind == "payment_plan":
            return "%s payments" % offer.get("installments")
        if kind == "extension":
            return "%s more days to pay" % offer.get("days")
    except (KeyError, TypeError, ValueError):
        pass
    return "a request the agent can't make (%s)" % kind


def describe_terms(kind, terms):
    if kind == "discount":
        text = "%g%% off, so the total becomes %s" % (round(terms["discount_pct"], 1), cash(terms["new_total"]))
        return text + (" if the full balance is paid now" if terms.get("requires_full_payment_now") else "")
    if kind == "payment_plan":
        text = "%d payments: %s" % (terms["installments"], ", ".join(
            "%s on %s" % (cash(p["amount"]), nice_date(p["due"] + "T12:00")) for p in terms["schedule"]))
        if terms.get("dropped_discount_pct"):
            text += " (the %g%% discount was for paying in full now, so it no longer applies)" % (
                terms["dropped_discount_pct"])
        return text
    if kind == "extension":
        if terms.get("shifts_plan"):
            return "the remaining payments moved %d days later" % terms["days"]
        return "a new due date of %s" % nice_date(terms["due_at"] + "T12:00")
    return kind


def shift_plan(inv, days):
    """More time on a plan moves its unpaid payments later. Payments already made stay where they were."""
    plan = plan_of(inv)
    plan["schedule"] = [dict(p, due=(date.fromisoformat(p["due"]) + timedelta(days=days)).isoformat())
                        if r["owed"] > 0 else p for p, r in zip(plan["schedule"], plan_rows(inv))]
    return json.dumps(plan)


def resplit_plan(inv, params):
    """A discount changes what's left to pay, so any plan on the invoice at that moment is re-split
    over its remaining dates. That moment can be long after the discount was asked for."""
    if params.get("discount_pct") is None or "plan" in params:
        return params
    unpaid = [r for r in plan_rows(inv) if r["owed"] > 0]
    total = round(inv["amount"] * (1 - params["discount_pct"] / 100.0), 2)
    left = policy.cents(total) - policy.cents(inv["amount_paid"] or 0)
    if not unpaid or left <= 0:
        return params
    each, extra = divmod(left, len(unpaid))
    schedule = [{"due": r["due"], "amount": (each + (1 if i < extra else 0)) / 100.0} for i, r in enumerate(unpaid)]
    return dict(params, min_due=schedule[0]["amount"],
                plan=json.dumps({"schedule": schedule, "base_paid": inv["amount_paid"] or 0}))
