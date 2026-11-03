"""The owner's rules: set in plain English, and widened when the agent earns it."""

import json
import re

from living_pay import policy
from living_pay.store import ex, get_setting, iso, log, now, post, q, q1, set_setting
from living_pay.llm import LLM, nullable_enum
from living_pay.negotiator import mandate
from living_pay.automations import offer_hooks


# ---- The owner's rules (the mandate), set in plain English -----------------

MANDATE_RE = re.compile(r"\b(discounts?|% ?off|percent off|payment plans?|instal+ments?|split|deposit|up ?front|"
                        r"first payment|extensions?|extend|more time|extra days|due dates?|returning clients?|"
                        r"repeat clients?|new clients?|any client|never (go )?below|minimum total|floor|\d+ payments|"
                        r"up to \d+ (?:payments|parts))\b", re.I)
MANDATE_LIMITS = {  # field: (lowest, highest)
    "early_pay_discount_pct": (0, 50), "max_discount_pct": (0, 50), "escalate_discount_up_to_pct": (0, 90),
    "max_installments": (2, policy.HARD_MAX_INSTALLMENTS), "min_first_payment_pct": (0, 100),
    "max_installment_gap_days": (1, 92), "max_extension_days": (0, 365), "escalate_extension_up_to_days": (0, 365),
    "floor_amount": (0, 10_000_000),
}


def rules_lines(m):
    c = get_setting("currency", "$")
    return [
        "Up to %g%% off for paying the whole balance now" % m["early_pay_discount_pct"],
        ("Up to %g%% off with no condition" % m["max_discount_pct"]) if m["max_discount_pct"]
        else "No discount unless they pay in full now",
        "Discounts above %g%% are declined without asking you" % m["escalate_discount_up_to_pct"],
        ("Never below %s%s in total" % (c, format(m["floor_amount"], ",g"))) if m.get("floor_amount")
        else "No minimum total",
        "Payment plans of up to %d payments, at least %g%% up front, at most %d days apart"
        % (m["max_installments"], m["min_first_payment_pct"], m["max_installment_gap_days"]),
        "Plans for returning clients only" if m["installments_for"] == "repeat_clients" else "Plans for any client",
        "Due dates moved by up to %d days; more than %d days is declined"
        % (m["max_extension_days"], m["escalate_extension_up_to_days"]),
    ]


def parse_mandate_rules(text):
    """Keyword fallback for when the AI layer is off. Returns only the fields the text sets."""
    t = text.lower().replace(",", "")
    out = {}
    pct = r"(\d+(?:\.\d+)?)\s*(?:%|percent)"
    if re.search(r"(today|early|now|in full|straight away|immediately)", t):
        m = re.search(pct, t)
        if m and re.search(r"discount|off", t):
            out["early_pay_discount_pct"] = float(m.group(1))
    m = re.search(r"(?:no|never|any) discount.*?(?:above|over|more than)\s*" + pct, t) or \
        re.search(r"(?:block|decline|refuse).*?(?:above|over|more than)\s*" + pct, t)
    if m:
        out["escalate_discount_up_to_pct"] = float(m.group(1))
    m = re.search(r"(?:up to|max(?:imum)?|at most|no more than)\s*(\d+)\s*(?:payments|instal+ments|parts)", t) or \
        re.search(r"(\d+)\s*(?:payments|instal+ments|parts)\s*(?:max|at most)", t)
    if m:
        out["max_installments"] = int(m.group(1))
    m = re.search(r"(?:first payment|deposit|up ?front)\D*?" + pct, t) or re.search(pct + r"\s*(?:up ?front|deposit)", t)
    if m:
        out["min_first_payment_pct"] = float(m.group(1))
    m = re.search(r"(?:extension|extend|more time|extra|due date)\D*?(\d+)\s*(days?|weeks?|months?)", t) or \
        re.search(r"(\d+)\s*(days?|weeks?|months?)\s*(?:extra|more|extension|longer)", t)
    if m:
        n = int(m.group(1))
        out["max_extension_days"] = n * 7 if m.group(2).startswith("week") else (n * 30 if m.group(2).startswith("month") else n)
    if re.search(r"(only|just).*(returning|repeat|existing) clients?|(returning|repeat|existing) clients? only", t):
        out["installments_for"] = "repeat_clients"
    elif re.search(r"(any|all|every|new) clients?", t) and re.search(r"plan|instal|split|payments", t):
        out["installments_for"] = "anyone"
    m = re.search(r"(?:never (?:go )?below|minimum total|floor (?:of|at)?)\s*[$\u00a3\u20ac\u20a6]?\s*(\d+(?:\.\d+)?)", t)
    if m:
        out["floor_amount"] = float(m.group(1))
    return out


MANDATE_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": list(MANDATE_LIMITS) + ["installments_for"],
    "properties": dict({k: {"type": ["number", "null"]} for k in MANDATE_LIMITS},
                       installments_for=nullable_enum(["anyone", "repeat_clients"])),
}


def parse_mandate(text):
    if LLM.on:
        current = mandate()
        out = LLM._json(
            "A small-business owner is changing the rules an AI agent follows when clients negotiate their "
            "invoices. Map their message onto the fields. Use null for every field the message doesn't change. "
            "Percentages are numbers like 5 for 5%. Days are whole days (a week is 7, a month is 30). "
            "early_pay_discount_pct: discount allowed when the client pays in full now. max_discount_pct: discount "
            "allowed with no condition. escalate_discount_up_to_pct: above this the agent declines without asking. "
            "floor_amount: the lowest total allowed. max_installments, min_first_payment_pct, "
            "max_installment_gap_days: payment plan limits. installments_for: who may get plans. "
            "max_extension_days: how far the agent may move a due date by itself. escalate_extension_up_to_days: "
            "above this, extensions are declined without asking.\nCurrent rules: " + json.dumps(current),
            text, MANDATE_SCHEMA)
        if out is not None:
            return {k: v for k, v in out.items() if v is not None}
    return parse_mandate_rules(text)


def clean_mandate(m):
    m = dict(m)
    for k, (lo, hi) in MANDATE_LIMITS.items():
        if m.get(k) is not None:
            m[k] = min(max(float(m[k]), lo), hi)
            if k in ("max_installments", "max_installment_gap_days", "max_extension_days",
                     "escalate_extension_up_to_days"):
                m[k] = int(m[k])
    if m.get("installments_for") not in ("anyone", "repeat_clients"):
        m["installments_for"] = "anyone"
    # The decline thresholds can't sit below what the agent may agree to by itself.
    m["escalate_discount_up_to_pct"] = max(m["escalate_discount_up_to_pct"], m["early_pay_discount_pct"],
                                           m["max_discount_pct"])
    m["escalate_extension_up_to_days"] = max(m["escalate_extension_up_to_days"], m["max_extension_days"])
    if not m.get("floor_amount"):
        m["floor_amount"] = None
    return m


def propose_mandate(text):
    current = mandate()
    changes = parse_mandate(text)
    new = clean_mandate(dict(current, **changes))
    before, after = rules_lines(current), rules_lines(new)
    diff = [(b, a) for b, a in zip(before, after) if b != a]
    if not diff:
        return ("I couldn't find a change to your negotiation rules in that. Your rules now:\n"
                + "\n".join("- " + l for l in before))
    for p in q("SELECT * FROM proposals WHERE status='pending' AND kind='mandate'"):
        ex("UPDATE proposals SET status='skipped', decided_at=? WHERE id=?", (iso(now()), p["id"]))
    pid = ex("INSERT INTO proposals(kind,summary,explanation,draft,reasons,status,created_at) "
             "VALUES('mandate',?,?,?,?,?,?)",
             ("Change the assistant's rules", text[:500], json.dumps({"rules": new}), "[]", "pending",
              iso(now())))
    log("proposed", "Change the assistant's rules", pid, data={"from": current, "to": new})
    post("bot", "Here's the change I understood for the assistant:\n%s\nShall I save it?" % "\n".join(
        "- ~~%s~~ → **%s**" % (b, a) for b, a in diff), pid,
        [{"label": "Yes", "send": "approve #%d" % pid}, {"label": "No", "send": "skip #%d" % pid}])
    return None


def apply_mandate(p):
    draft = json.loads(p["draft"])
    rules = draft["rules"]
    set_setting("mandate", json.dumps(rules))
    if draft.get("growth"):
        grown = json.loads(get_setting("mandate_growth") or "[]")
        grown.append(dict(draft["growth"], at=iso(now()), active=True, proposal_id=p["id"]))
        set_setting("mandate_growth", json.dumps(grown))
    ex("UPDATE proposals SET status='executed', decided_at=? WHERE id=?", (iso(now()), p["id"]))
    log("rules_changed", "Assistant's rules updated", p["id"], actor="owner", data={"rules": rules})
    return "Saved. Your assistant now follows these rules:\n" + "\n".join("- " + l for l in rules_lines(mandate()))


# ---- The agent earns wider rules -------------------------------------------
# Three approvals in a row of the same kind of request: offer to let the agent agree
# to those itself, at the most conservative value the owner approved. An undo of
# something the agent agreed to under a widened rule narrows that rule back.

GROWTH_STREAK = 3
GROWTH_LABELS = {"discount": "a discount without paying in full", "discount:now": "a bigger discount for paying now",
                 "payment_plan": "payment plans", "extension": "more time to pay"}


def growth_key(o):
    offer = json.loads(o["offer"])
    return o["kind"] + (":now" if o["kind"] == "discount" and offer.get("condition") == "pay_in_full_now" else "")


def approval_streak(key):
    """The owner's approvals of this kind of escalated request since they last declined or countered one,
    and since the agent last asked about this kind."""
    asked = json.loads(get_setting("growth_asked") or "{}").get(key, 0)
    streak = []
    for o in q("SELECT * FROM offers WHERE source='agent' AND decision='ESCALATE' AND id>? "
               "AND status IN ('approved','declined','countered') ORDER BY decided_at, id", (asked,)):
        if growth_key(o) == key:
            streak = streak + [o] if o["status"] == "approved" else []
    return streak


def widened(m, key, offers):
    m = dict(m)
    asks = [json.loads(o["offer"]) for o in offers]
    terms = [json.loads(o["terms"]) for o in offers]
    if key == "discount":
        m["max_discount_pct"] = max(m["max_discount_pct"], min(float(a["pct"]) for a in asks))
    elif key == "discount:now":
        m["early_pay_discount_pct"] = max(m["early_pay_discount_pct"], min(float(a["pct"]) for a in asks))
    elif key == "payment_plan":
        m["max_installments"] = max(m["max_installments"], min(t["installments"] for t in terms))
        firsts = [100.0 * t["schedule"][0]["amount"] / sum(x["amount"] for x in t["schedule"]) for t in terms]
        m["min_first_payment_pct"] = min(m["min_first_payment_pct"], int(min(firsts)))
        m["max_installment_gap_days"] = max(m["max_installment_gap_days"], min(t["gap_days"] for t in terms))
        if any("repeat clients" in r for o in offers for r in json.loads(o["reasons"])):
            m["installments_for"] = "anyone"
    elif key == "extension":
        m["max_extension_days"] = max(m["max_extension_days"], min(t["days"] for t in terms))
    return clean_mandate(m)


def maybe_grow(offer_id):
    offer = q1("SELECT * FROM offers WHERE id=?", (offer_id,)) if offer_id else None
    if not offer or offer["source"] != "agent" or offer["decision"] != "ESCALATE" or offer["status"] != "approved":
        return
    key = growth_key(offer)
    streak = approval_streak(key)
    if len(streak) < GROWTH_STREAK:
        return
    asked = json.loads(get_setting("growth_asked") or "{}")
    asked[key] = max(o["id"] for o in streak)  # count afresh from here, whatever the answer
    set_setting("growth_asked", json.dumps(asked))
    current = mandate()
    new = widened(current, key, streak[-GROWTH_STREAK:])
    changed = {k: (current[k], new[k]) for k in new if new[k] != current.get(k)}
    diff = [(b, a) for b, a in zip(rules_lines(current), rules_lines(new)) if b != a]
    if not diff:
        return
    for p in q("SELECT * FROM proposals WHERE status='pending' AND kind='mandate'"):
        ex("UPDATE proposals SET status='skipped', decided_at=? WHERE id=?", (iso(now()), p["id"]))
    summary = "Let the assistant agree to %s itself" % GROWTH_LABELS[key]
    pid = ex("INSERT INTO proposals(kind,summary,explanation,draft,reasons,status,created_at) "
             "VALUES('mandate',?,?,?,?,?,?)",
             (summary, "You approved the last %d requests for %s." % (GROWTH_STREAK, GROWTH_LABELS[key]),
              json.dumps({"rules": new, "growth": {"key": key, "before": {k: v[0] for k, v in changed.items()},
                                                   "after": {k: v[1] for k, v in changed.items()}}}),
              "[]", "pending", iso(now())))
    log("proposed", summary, pid, data={"from": current, "to": new, "approved": [o["id"] for o in streak]})
    post("bot", "You've approved the last %d requests for **%s**. Shall I agree to requests like these myself "
                "from now on?\n%s\nIf you undo one I agree to, I'll go back to asking." % (
                    GROWTH_STREAK, GROWTH_LABELS[key], "\n".join("- ~~%s~~ → **%s**" % (b, a) for b, a in diff)),
         pid, [{"label": "Yes", "send": "approve #%d" % pid}, {"label": "No", "send": "skip #%d" % pid}])


def maybe_shrink(offer_id):
    """The owner undid something the agent agreed to by itself under a rule it had earned: narrow it back."""
    offer = q1("SELECT * FROM offers WHERE id=?", (offer_id,)) if offer_id else None
    if not offer or offer["source"] != "agent" or offer["decision"] != "ALLOW":
        return None
    key = growth_key(offer)
    grown = json.loads(get_setting("mandate_growth") or "[]")
    live = [g for g in grown if g["active"] and g["key"] == key and g["at"] <= offer["at"]]
    if not live:
        return None
    g = live[-1]
    current = mandate()
    restored = dict(current, **{k: v for k, v in g["before"].items() if current.get(k) == g["after"].get(k)})
    restored = clean_mandate(restored)
    diff = [(b, a) for b, a in zip(rules_lines(current), rules_lines(restored)) if b != a]
    g["active"] = False
    set_setting("mandate_growth", json.dumps(grown))
    asked = json.loads(get_setting("growth_asked") or "{}")
    asked[key] = q1("SELECT MAX(id) m FROM offers")["m"] or 0
    set_setting("growth_asked", json.dumps(asked))
    if not diff:
        return None
    set_setting("mandate", json.dumps(restored))
    log("rules_narrowed", "Undo narrowed the rule for %s back" % GROWTH_LABELS[key], actor="owner",
        data={"from": current, "to": restored, "offer_id": offer["id"]})
    return "Because you undid that, I've narrowed the rule back, and I'll ask you about %s again:\n%s" % (
        GROWTH_LABELS[key], "\n".join("- ~~%s~~ → **%s**" % (b, a) for b, a in diff))


offer_hooks["undone"] = maybe_shrink
