"""End-to-end checks of the engine, run against a throwaway database.

    python3 tests/test_flow.py
"""
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
os.environ["LIVING_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ["LIVING_LLM"] = "off"

from support import app as s  # noqa: E402


BRAND = {"contact_id": 1, "title": "Brand workshop", "amount": 1200}


def last_bot():
    return s.q1("SELECT * FROM chat WHERE sender='bot' ORDER BY id DESC")["body"]


def pending():
    return s.q("SELECT * FROM proposals WHERE status='pending' ORDER BY id")


def say(text):
    s.owner_message(text)
    return last_bot()


class Flow(unittest.TestCase):
    def setUp(self):
        s.reset()

    def test_new_invoice_goes_out_at_once_with_a_practice_email(self):
        s.new_invoice(BRAND)
        self.assertEqual(pending(), [])
        inv = s.q1("SELECT * FROM invoices ORDER BY id DESC")
        self.assertEqual((inv["number"], inv["status"]), ("INV-1003", "sent"))
        mail = s.q1("SELECT * FROM outbox ORDER BY id DESC")
        self.assertIn("INV-1003", mail["subject"])
        self.assertIn("$1,200", mail["body"])

    def test_undo_voids_invoice_and_sends_correction(self):
        s.new_invoice(BRAND)
        self.assertIn("Undone", say("undo"))
        self.assertEqual(s.q1("SELECT status FROM invoices ORDER BY id DESC")["status"], "void")
        self.assertIn("disregard", s.q1("SELECT * FROM outbox ORDER BY id DESC")["subject"].lower())
        self.assertEqual(pending(), [])

    def test_clock_advance_triggers_overdue_reminders(self):
        s.advance_clock(24)
        summaries = [p["summary"] for p in pending()]
        self.assertTrue(any("INV-1001" in x and "friendly" in x for x in summaries), summaries)
        self.assertTrue(any("INV-1002" in x and "firm" in x for x in summaries), summaries)

    def test_a_first_message_always_asks(self):
        s.set_trust("overdue_reminders", "auto_notice")
        s.ex("UPDATE contacts SET contacted=0 WHERE name='Tunde Bello'")
        s.advance_clock(24)
        tunde = [p for p in pending() if "Tunde" in p["summary"]]
        self.assertEqual(len(tunde), 1)
        self.assertIn("first message", json.loads(tunde[0]["reasons"])[0])

    def test_auto_notice_acts_and_reports(self):
        s.set_trust("overdue_reminders", "auto_notice")
        s.advance_clock(24)
        self.assertEqual(pending(), [])
        self.assertTrue(s.q("SELECT id FROM proposals WHERE status='executed' AND summary LIKE '%INV-1001%'"))

    def test_paying_withdraws_pending_reminder(self):
        s.advance_clock(24)
        rem = [p for p in pending() if "INV-1001" in p["summary"]][0]
        s.mark_paid(2)
        self.assertEqual(s.q1("SELECT status FROM proposals WHERE id=?", (rem["id"],))["status"], "cancelled")
        self.assertIn("withdrawn", last_bot())

    def test_three_approvals_promote_to_automatic(self):
        s.advance_clock(24)
        replies = [say("approve #%d" % p["id"]) for p in pending()]
        s.advance_clock(24 * 7)
        replies += [say("approve #%d" % p["id"]) for p in pending()]
        self.assertTrue(any("automatically" in r for r in replies), replies)
        self.assertEqual(s.q1("SELECT trust FROM automations WHERE template='overdue_reminders'")["trust"],
                         "auto_notice")

    def test_skip_does_nothing(self):
        s.advance_clock(24)
        pid = pending()[0]["id"]
        self.assertIn("Skipped", say("skip #%d" % pid))
        self.assertEqual(s.q1("SELECT status FROM proposals WHERE id=?", (pid,))["status"], "skipped")
        self.assertEqual(s.q("SELECT id FROM outbox"), [])

    def test_monday_summary(self):
        s.advance_clock(48)  # Sat 09:00 -> Mon 09:00
        bodies = [m["body"] for m in s.q("SELECT body FROM chat WHERE sender='bot'")]
        self.assertTrue(any(b.startswith("**Your week at Kora Studio**") for b in bodies))

    def test_overdue_stages_from_text(self):
        say("chase overdue invoices at 5, 15 and 30 days")
        say("yes")
        auto = s.q1("SELECT * FROM automations WHERE template='overdue_reminders'")
        self.assertEqual(json.loads(auto["params"]), {"stages": [5, 15, 30]})

    def test_trust_commands_and_pause(self):
        self.assertIn("asks you first", say("ask first for overdue reminders"))
        self.assertIn("Paused", say("pause summary"))
        self.assertIn("Resumed", say("resume summary"))

    def test_undo_of_automatic_action_demotes_trust(self):
        s.set_trust("overdue_reminders", "auto_notice")
        s.advance_clock(24)
        done = s.q1("SELECT * FROM proposals WHERE summary LIKE '%INV-1001%' AND status='executed'")
        reply = say("undo #%d" % done["id"])
        self.assertIn("ask you first", reply)
        self.assertIn("Couldn't reverse", reply)

    def test_audit_log_is_append_only_record(self):
        s.advance_clock(24)
        say("approve #%d" % pending()[0]["id"])
        events = [r["event"] for r in s.q("SELECT event FROM audit ORDER BY id")]
        self.assertEqual(events[-2:], ["approved", "executed"])

    def test_ledger_views_add_up(self):
        s.new_invoice(BRAND)
        led = s.ledger_state()
        ruth = [c for c in led["clients"] if c["client"] == "Acme Dental"][0]
        self.assertEqual((ruth["invoices"], ruth["total"], ruth["paid"], ruth["balance"]), (2, 2100, 900, 1200))
        newest = led["invoices"][0]
        self.assertEqual((newest["invoice"], newest["balance"], newest["plan"]), ("INV-1003", 1200, ""))
        self.assertTrue([i for i in led["invoices"] if i["invoice"] == "INV-1002"][0]["overdue"])

    def test_old_databases_lose_removed_features(self):
        s.ex("UPDATE automations SET template='invoice_on_completion' WHERE template='send_invoices'")
        s.ex("INSERT INTO automations(template,params,trust,created_at) VALUES('appointment_reminders','{}','suggest','x')")
        s.migrate()
        self.assertIsNone(s.q1("SELECT id FROM automations WHERE template='appointment_reminders'"))
        self.assertIsNotNone(s.q1("SELECT id FROM automations WHERE template='send_invoices'"))
        self.assertTrue(s.audit_state()["automations"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
