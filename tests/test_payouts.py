"""Subcontractor payouts: proposed when an invoice is paid in full, sent with PayPal Payouts,
undoable while unclaimed. Runs against the fake PayPal sandbox.

    python3 tests/test_payouts.py
"""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import fake_paypal  # noqa: E402
from fake_paypal import FAKE  # noqa: E402

_server, BASE = fake_paypal.serve()
os.environ.update({
    "LIVING_DB": os.path.join(tempfile.mkdtemp(), "test.db"), "LIVING_LLM": "off",
    "PAYPAL_API_BASE": BASE, "PAYPAL_CLIENT_ID": "cid", "PAYPAL_CLIENT_SECRET": "secret",
    "PAYPAL_WEBHOOK_ID": FAKE.webhook_id, "PUBLIC_URL": "https://living.example",
})

from support import app as s  # noqa: E402


def bot():
    return s.q1("SELECT body FROM chat WHERE sender='bot' ORDER BY id DESC")["body"]


def pending():
    return s.q1("SELECT * FROM proposals WHERE status='pending' ORDER BY id DESC")


class Payouts(unittest.TestCase):
    def setUp(self):
        FAKE.reset(client_id="cid", client_secret="secret")
        s._paypal = None
        s._chat_times.clear()
        s.reset()
        s.new_invoice({"contact_id": 1, "title": "Brand workshop", "amount": 1200, "sub_id": 1, "sub_share": 300})  # Ada's share is $300
        self.inv = s.q1("SELECT * FROM invoices ORDER BY id DESC")

    def pay_in_full(self):
        order = s.create_checkout(self.inv["token"], "balance")
        s.fake_approve(self.inv["token"], order["order_id"])
        s.capture_checkout(self.inv["token"], order["order_id"])

    def test_paid_invoice_proposes_the_share_and_first_payout_asks(self):
        self.pay_in_full()
        self.assertIn("**Approve?** Pay Ada Nwosu $300 for “Brand workshop”", bot())
        self.assertIn("first payout to Ada Nwosu", bot())
        s.owner_message("approve")
        row = s.q1("SELECT * FROM payouts")
        self.assertEqual((row["amount"], row["status"]), (300, "unclaimed"))
        item = FAKE.items[row["item_id"]]
        self.assertEqual(item["payout_item"]["receiver"], "ada.nwosu@illustrators.example")
        self.assertEqual(item["payout_item"]["amount"], {"value": "300.00", "currency": "USD"})

    def test_part_payment_does_not_pay_the_subcontractor(self):
        s.client_message(self.inv["token"], "Can I split this into 3 payments?")
        order = s.create_checkout(self.inv["token"], "now")
        s.fake_approve(self.inv["token"], order["order_id"])
        s.capture_checkout(self.inv["token"], order["order_id"])
        self.assertIsNone(s.q1("SELECT id FROM proposals WHERE dedupe LIKE 'payout:%'"))

    def test_undo_cancels_an_unclaimed_payout(self):
        self.pay_in_full()
        s.owner_message("approve")
        s.owner_message("undo")
        row = s.q1("SELECT * FROM payouts")
        self.assertEqual(row["status"], "cancelled")
        self.assertEqual(FAKE.items[row["item_id"]]["transaction_status"], "RETURNED")

    def test_undo_while_paypal_is_still_processing_waits(self):
        FAKE.hold_payouts = True
        self.pay_in_full()
        s.owner_message("approve")
        self.assertEqual(s.q1("SELECT status FROM payouts")["status"], "pending")
        s.owner_message("undo")
        self.assertIn("I couldn't undo that yet: PayPal is still processing this payout", bot())
        self.assertEqual(s.q1("SELECT status FROM proposals WHERE dedupe LIKE 'payout:%'")["status"], "executed")
        FAKE.hold_payouts = False
        s.owner_message("undo")
        self.assertEqual(s.q1("SELECT status FROM payouts")["status"], "cancelled")

    def test_paypal_error_on_cancel_leaves_the_payout_undoable(self):
        self.pay_in_full()
        s.owner_message("approve")
        FAKE.fail_cancel = True
        s.owner_message("undo")
        self.assertIn("PayPal couldn't cancel it just now", bot())
        self.assertIn("returns unclaimed payouts after 30 days", bot())
        self.assertEqual(s.q1("SELECT status FROM payouts")["status"], "unclaimed")
        FAKE.fail_cancel = False
        s.owner_message("undo")
        self.assertEqual(s.q1("SELECT status FROM payouts")["status"], "cancelled")

    def test_undo_after_the_money_is_claimed_says_so(self):
        FAKE.known_receivers.add("ada.nwosu@illustrators.example")  # Ada has PayPal: claimed at once
        self.pay_in_full()
        s.owner_message("approve")
        self.assertEqual(s.q1("SELECT status FROM payouts")["status"], "success")
        s.owner_message("undo")
        self.assertIn("couldn't undo", bot())
        self.assertIn("Only items in Unclaimed status can be cancelled", bot())
        self.assertEqual(s.q1("SELECT status FROM payouts")["status"], "success")

    def test_payout_webhooks_update_status(self):
        self.pay_in_full()
        s.owner_message("approve")
        row = s.q1("SELECT * FROM payouts")
        FAKE.items[row["item_id"]]["transaction_status"] = "SUCCESS"
        event = FAKE.emit("PAYMENT.PAYOUTS-ITEM.SUCCEEDED", FAKE.items[row["item_id"]])
        hook = FAKE.webhooks[-1]
        self.assertEqual(s.paypal_webhook(hook["headers"], hook["body"]), 200)
        self.assertEqual(s.q1("SELECT status FROM payouts")["status"], "succeeded")
        self.assertEqual(event["event_type"], "PAYMENT.PAYOUTS-ITEM.SUCCEEDED")

    def test_refund_warns_about_a_share_already_paid_out(self):
        self.pay_in_full()
        s.owner_message("approve")
        cap = list(FAKE.captures.values())[-1]
        s.paypal_client().call("POST", "/v2/payments/captures/%s/refund" % cap["id"], {})
        hook = next(h for h in reversed(FAKE.webhooks) if h["event"]["event_type"] == "PAYMENT.CAPTURE.REFUNDED")
        s.paypal_webhook(hook["headers"], hook["body"])
        self.assertIn("You already paid Ada Nwosu $300", bot())

    def test_second_payout_to_a_known_payee_can_be_automatic(self):
        self.pay_in_full()
        s.owner_message("approve")
        s.owner_message("auto pay subcontractors")
        s.new_invoice({"contact_id": 2, "title": "Menu redesign", "amount": 800, "sub_id": 1, "sub_share": 200})
        inv = s.q1("SELECT * FROM invoices ORDER BY id DESC")
        order = s.create_checkout(inv["token"], "balance")
        s.fake_approve(inv["token"], order["order_id"])
        s.capture_checkout(inv["token"], order["order_id"])
        self.assertEqual(s.q("SELECT amount FROM payouts ORDER BY id"), [{"amount": 300}, {"amount": 200}])
        self.assertIn("**Done:** Pay Ada Nwosu $200", bot())

    def test_offline_payouts_without_paypal(self):
        saved = os.environ.pop("PAYPAL_CLIENT_ID")
        try:
            s.mark_paid(self.inv["id"])
            s.owner_message("approve")
            self.assertEqual(s.q1("SELECT status FROM payouts")["status"], "sent")
        finally:
            os.environ["PAYPAL_CLIENT_ID"] = saved


if __name__ == "__main__":
    unittest.main(verbosity=2)
