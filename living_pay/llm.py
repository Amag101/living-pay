"""The Claude layer. If it's off or over its daily limit, simple rules take over."""

from datetime import date
import json
import os

from living_pay.config import LLM_MODEL


def nullable_enum(values):
    """One of `values`, or null. Structured outputs reject an enum mixed into a ["string", "null"] type."""
    return {"anyOf": [{"type": "string", "enum": values}, {"type": "null"}]}


CLAUDE_DAILY_LIMIT = int(os.environ.get("LIVING_CLAUDE_DAILY_LIMIT", "400"))


class _LLM:
    def __init__(self):
        self.client = None
        self.spent_on, self.calls = None, 0
        self.status = "off (pip install anthropic to enable)"
        if os.environ.get("LIVING_LLM", "on") == "off":
            self.status = "off (LIVING_LLM=off)"
            return
        try:
            import anthropic
        except ImportError:
            return
        try:
            self.anthropic = anthropic
            self.client = anthropic.Anthropic()
            self.status = "on (%s)" % LLM_MODEL
        except Exception as e:
            self.client, self.status = None, "off (%s)" % e

    @property
    def on(self):
        return self.client is not None

    def within_budget(self):
        """A public link can't run up the Claude bill: past the day's limit, the keyword rules take over."""
        today = date.today()
        if self.spent_on != today:
            self.spent_on, self.calls = today, 0
        self.calls += 1
        return self.calls <= CLAUDE_DAILY_LIMIT

    def _json(self, system, user, schema):
        if not self.within_budget():
            return None
        kwargs = dict(model=LLM_MODEL, max_tokens=4000, system=system,
                      messages=[{"role": "user", "content": user}],
                      output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}})
        try:
            try:
                resp = self.client.beta.messages.create(
                    betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
            except TypeError:  # older SDK without the fallbacks parameter
                resp = self.client.messages.create(**kwargs)
        except (self.anthropic.AuthenticationError, self.anthropic.PermissionDeniedError) as e:
            self.client, self.status = None, "off (%s)" % e.__class__.__name__
            return None
        except Exception as e:
            print("LLM call failed:", repr(e))
            return None
        if resp.stop_reason in ("refusal", "max_tokens"):
            return None
        text = "".join(b.text for b in resp.content if b.type == "text")
        try:
            return json.loads(text)
        except ValueError:
            return None

    def create(self, **kwargs):
        """A raw Messages call (used by the invoice negotiator's tool loop). None if the AI layer is off."""
        if not self.on or not self.within_budget():
            return None
        kwargs = dict(model=LLM_MODEL, output_config={"effort": "low"}, **kwargs)
        try:
            try:
                return self.client.beta.messages.create(
                    betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
            except TypeError:  # older SDK without the fallbacks parameter
                return self.client.messages.create(**kwargs)
        except (self.anthropic.AuthenticationError, self.anthropic.PermissionDeniedError) as e:
            self.client, self.status = None, "off (%s)" % e.__class__.__name__
        except Exception as e:
            print("LLM call failed:", repr(e))
        return None

    def parse(self, text, templates, trust_levels):
        """Maps an owner's message onto the automations they can change."""
        if not self.on:
            return None
        autos = "\n".join("- %s: %s" % (t.key, t.describe(t.default_params)) for t in templates)
        schema = {
            "type": "object", "additionalProperties": False,
            "required": ["intent", "template", "trust", "stages", "weekday"],
            "properties": {
                "intent": {"type": "string", "enum": ["create_or_update", "set_trust", "pause", "resume",
                                                       "list", "summary", "help", "unknown"]},
                "template": nullable_enum([t.key for t in templates]),
                "trust": nullable_enum(list(trust_levels)),
                "stages": {"type": ["array", "null"], "items": {"type": "integer"}},
                "weekday": {"type": ["integer", "null"], "description": "0 = Monday"},
            },
        }
        system = ("You map a small-business owner's chat message onto an automation catalogue. "
                  "Pick the closest catalogue entry; never invent one. Use null for anything the "
                  "message does not state. Trust levels: observe = watch only, suggest = ask first, "
                  "auto_notice = act then tell, silent = act and only include in the weekly summary.\n"
                  "Catalogue:\n" + autos)
        return self._json(system, text, schema)

    def polish(self, tone, facts, default, must_include):
        """Redraft a client message. Falls back to the template if facts go missing."""
        if not self.on:
            return default
        schema = {"type": "object", "additionalProperties": False, "required": ["body"],
                  "properties": {"body": {"type": "string"}}}
        system = ("Rewrite this payment reminder so it reads naturally and is %s in tone. "
                  "Use only the facts given; do not add dates, amounts, fees or threats. "
                  "Keep it under 90 words. Plain text, no subject line." % tone)
        out = self._json(system, "Facts: %s\n\nDraft:\n%s" % (json.dumps(facts), default), schema)
        body = (out or {}).get("body", "")
        if body and all(s in body for s in must_include):
            return body
        return default


LLM = _LLM()
