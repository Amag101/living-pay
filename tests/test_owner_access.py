"""Only the owner reaches the owner's pages, and the assistant can only read from PayPal.

    python3 tests/test_owner_access.py
"""
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from types import SimpleNamespace as NS

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
os.environ["LIVING_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ["LIVING_LLM"] = "off"
for k in ("PAYPAL_CLIENT_ID", "PAYPAL_CLIENT_SECRET", "PAYPAL_API_BASE"):
    os.environ.pop(k, None)

from living_pay import paypal_mcp  # noqa: E402
from support import app as s  # noqa: E402

PASSWORD = "test-owner-password"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


OPENER = urllib.request.build_opener(NoRedirect)


class Login(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["LIVING_OWNER_PASSWORD"] = PASSWORD
        s.reset()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), s.Handler)
        cls.base = "http://127.0.0.1:%d" % cls.httpd.server_port
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        os.environ.pop("LIVING_OWNER_PASSWORD")

    def setUp(self):
        s._login_fails.clear()

    def hit(self, path, body=None, cookie=None):
        headers = {"Content-Type": "application/json"}
        if cookie:
            headers["Cookie"] = cookie
        data = None if body is None else json.dumps(body).encode()
        try:
            r = OPENER.open(urllib.request.Request(self.base + path, data=data, headers=headers))
        except urllib.error.HTTPError as e:
            r = e
        return r.status if hasattr(r, "status") else r.code, r.headers, r.read()

    def login(self):
        code, headers, _ = self.hit("/login", {"password": PASSWORD})
        self.assertEqual(code, 200)
        return headers["Set-Cookie"].split(";")[0]

    def test_owner_data_needs_a_login(self):
        for path in ("/api/state", "/api/audit", "/api/ledger"):
            self.assertEqual(self.hit(path)[0], 401, path)
        for path in ("/api/chat", "/api/reset", "/api/settings/business", "/api/sandbox/tick", "/logout"):
            self.assertEqual(self.hit(path, {})[0], 401, path)

    def test_owner_pages_send_you_to_login(self):
        for path in ("/", "/audit", "/ledger", "/index.html", "/audit.html", "/ledger.html"):
            code, headers, _ = self.hit(path)
            self.assertEqual((code, headers["Location"]), (302, "/login"), path)

    def test_client_pages_stay_open(self):
        for path in ("/login", "/style.css", "/logo.svg", "/i/some-token"):
            self.assertEqual(self.hit(path)[0], 200, path)
        self.assertEqual(self.hit("/api/i/not-a-real-token")[0], 404)

    def test_wrong_password_and_forged_cookies_fail(self):
        self.assertEqual(self.hit("/login", {"password": "nope"})[0], 401)
        for forged in ("living_owner=99999999999.abc", "living_owner=", "living_owner=x", 'living_owner="; x'):
            self.assertEqual(self.hit("/api/state", cookie=forged)[0], 401, forged)

    def test_login_opens_owner_pages(self):
        cookie = self.login()
        self.assertEqual(self.hit("/api/state", cookie=cookie)[0], 200)
        self.assertEqual(self.hit("/", cookie=cookie)[0], 200)

    def test_cookie_is_http_only(self):
        _, headers, _ = self.hit("/login", {"password": PASSWORD})
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("SameSite=Lax", headers["Set-Cookie"])

    def test_too_many_wrong_guesses_lock_login(self):
        for _ in range(s.LOGIN_LIMIT[0]):
            self.assertEqual(self.hit("/login", {"password": "nope"})[0], 401)
        self.assertEqual(self.hit("/login", {"password": PASSWORD})[0], 429)

    def test_refuses_to_go_public_without_a_password(self):
        keep = os.environ.pop("LIVING_OWNER_PASSWORD")
        os.environ["HOST"] = "0.0.0.0"
        try:
            with self.assertRaises(SystemExit):
                s.main()
        finally:
            os.environ["LIVING_OWNER_PASSWORD"] = keep
            os.environ.pop("HOST")

    def test_business_name_and_discount_chip(self):
        cookie = self.login()
        self.hit("/api/settings/business", {"name": "Ruth's Bakery"}, cookie)
        self.assertEqual(s.get_setting("business"), "Ruth's Bakery")
        self.hit("/api/settings/business", {"name": "Ruth's Bakery"}, cookie)  # saving again changes nothing
        self.assertEqual(len(s.q("SELECT id FROM audit WHERE event='setting_changed'")), 1)
        self.assertEqual(s.get_setting("discount_chip", "0"), "0")
        self.hit("/api/settings/discount_chip", {"on": True}, cookie)
        self.assertEqual(s.get_setting("discount_chip"), "1")
        self.assertEqual(self.hit("/api/settings/business", {"name": " "}, cookie)[0], 400)


class JudgeLogin(Login):
    """The judge password opens the app with a guided tour and limits."""

    @classmethod
    def setUpClass(cls):
        os.environ["LIVING_JUDGE_PASSWORD"] = "test-judge-password"
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        os.environ.pop("LIVING_JUDGE_PASSWORD")

    def judge(self):
        code, headers, _ = self.hit("/login", {"password": "test-judge-password"})
        self.assertEqual(code, 200)
        return headers["Set-Cookie"].split(";")[0]

    def state(self, cookie):
        return json.loads(self.hit("/api/state", cookie=cookie)[2])

    def test_judge_gets_a_tour_and_the_owner_does_not(self):
        self.assertIn("tour", self.state(self.judge()))
        self.assertNotIn("tour", self.state(self.login()))

    def test_judge_cannot_change_the_business_or_reset(self):
        cookie = self.judge()
        for path in ("/api/settings/business", "/api/settings/discount_chip", "/api/reset"):
            self.assertEqual(self.hit(path, {"name": "Hacked", "on": True}, cookie)[0], 403, path)

    def test_a_judge_cookie_cannot_be_turned_into_an_owner_cookie(self):
        value = self.judge().split("=", 1)[1]
        forged = "living_owner=" + value.replace("judge.", "owner.", 1)
        self.assertEqual(self.hit("/api/state", cookie=forged)[0], 401)

    def test_owner_cannot_use_tour_routes(self):
        self.assertEqual(self.hit("/api/tour/start", {}, self.login())[0], 404)

    def test_buyer_password_only_reaches_a_judge(self):
        os.environ["LIVING_DEMO_BUYER_PASSWORD"] = "sandbox-only"
        try:
            self.assertEqual(self.state(self.judge())["tour"]["buyer_password"], "sandbox-only")
            self.assertNotIn(b"sandbox-only", self.hit("/api/state", cookie=self.login())[2])
            self.assertEqual(self.hit("/api/state")[0], 401)
        finally:
            os.environ.pop("LIVING_DEMO_BUYER_PASSWORD")

    def test_tour_sends_a_sample_and_starting_again_replaces_it(self):
        cookie = self.judge()
        self.assertEqual(self.hit("/api/tour/start", {}, cookie)[0], 200)
        first = self.state(cookie)["tour"]
        self.assertTrue(first["token"])
        self.hit("/api/tour/start", {}, cookie)
        second = self.state(cookie)["tour"]
        self.assertNotEqual(first["token"], second["token"])
        self.assertEqual(s.q1("SELECT status FROM invoices WHERE token=?", (first["token"],))["status"], "void")

    def test_starting_again_restores_the_rules(self):
        cookie = self.judge()
        s.set_setting("mandate", json.dumps({"max_installments": 9}))
        s.ex("UPDATE automations SET enabled=0")
        self.hit("/api/tour/start", {}, cookie)
        self.assertEqual(s.mandate()["max_installments"], 3)
        self.assertFalse(s.q("SELECT id FROM automations WHERE enabled=0"))

    def test_invoice_limit_for_judges(self):
        cookie = self.judge()
        keep = s.JUDGE_INVOICES_PER_DAY
        s.JUDGE_INVOICES_PER_DAY = 1
        s._invoice_times.clear()
        try:
            self.assertEqual(self.hit("/api/tour/start", {}, cookie)[0], 200)
            self.assertEqual(self.hit("/api/tour/start", {}, cookie)[0], 400)
        finally:
            s.JUDGE_INVOICES_PER_DAY = keep
            s._invoice_times.clear()

    def test_missing_a_payment_needs_a_plan_and_only_moves_the_sample(self):
        cookie = self.judge()
        self.assertEqual(self.hit("/api/tour/late", {"days": 1}, cookie)[0], 400)
        self.hit("/api/tour/start", {}, cookie)
        inv = s.q1("SELECT * FROM invoices WHERE tour=1 ORDER BY id DESC")
        self.assertEqual(self.hit("/api/tour/late", {"days": 1}, cookie)[0], 400)  # no plan yet
        s.client_message(inv["token"], "Can I split this into 3 payments?")
        self.assertEqual(self.hit("/api/tour/late", {"days": 5}, cookie)[0], 400)
        self.assertEqual(self.hit("/api/tour/late", {"days": 1}, cookie)[0], 200)
        inv = s.q1("SELECT * FROM invoices WHERE id=?", (inv["id"],))
        self.assertEqual(s.days_late(inv), 1)
        self.assertLessEqual(inv["issued_at"][:10], json.loads(inv["plan"])["schedule"][1]["due"])
        self.assertTrue(s.q("SELECT id FROM proposals WHERE dedupe LIKE 'missed:%'"))
        self.assertEqual(self.hit("/api/tour/late", {"days": 14}, cookie)[0], 200)
        self.assertTrue(s.q("SELECT id FROM proposals WHERE dedupe LIKE 'endplan:%' AND status='pending'"))
        self.assertEqual(s.q1("SELECT plan FROM invoices WHERE number='INV-1001'")["plan"], None)


class FakeMCP:
    def __init__(self, *args):
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def tools(self):
        return [{"name": "list_invoices", "description": "x", "input_schema": {"type": "object"}}]

    def call(self, name, arguments):
        self.calls.append(name)
        return '{"items": []}'


def tool_turn():
    return NS(stop_reason="tool_use", content=[NS(type="tool_use", id="t1", name="list_invoices", input={})])


class Questions(unittest.TestCase):
    def setUp(self):
        s.reset()
        self.keep = paypal_mcp.PayPalMCP, s.paypal_ask_on, s.paypal_client, s.LLM.create
        paypal_mcp.PayPalMCP = FakeMCP
        s.paypal_ask_on = lambda: True
        s.paypal_client = lambda: NS(token=lambda: "token")

    def tearDown(self):
        paypal_mcp.PayPalMCP, s.paypal_ask_on, s.paypal_client, s.LLM.create = self.keep

    def test_only_read_tools_can_run(self):
        mcp = paypal_mcp.PayPalMCP.__new__(self.keep[0])
        for tool in ("create_refund", "cancel_sent_invoice", "create_invoice", "pay_order", "send_invoice"):
            with self.assertRaises(paypal_mcp.MCPError):
                self.keep[0].call(mcp, tool, {})

    def test_owner_question_goes_through_paypal_and_is_logged(self):
        turns = [tool_turn(), NS(stop_reason="end_turn", content=[NS(type="text", text="Nobody owes you.")])]
        s.LLM.create = lambda **kw: turns.pop(0)
        s.owner_message("Who hasn't paid yet?")
        self.assertEqual(s.q1("SELECT body FROM chat WHERE sender='bot' ORDER BY id DESC")["body"], "Nobody owes you.")
        row = s.q1("SELECT * FROM audit WHERE event='paypal_question'")
        self.assertIn("read only", row["summary"] if "summary" in row else str(row))
        self.assertIn("list_invoices", str(row))

    def test_commands_still_win_over_questions(self):
        s.LLM.create = lambda **kw: self.fail("a command shouldn't reach Claude")
        s.owner_message("what are my rules?")
        self.assertIn("rules", s.q1("SELECT body FROM chat WHERE sender='bot' ORDER BY id DESC")["body"])

    def test_a_failure_says_so(self):
        def broken(*args):
            raise paypal_mcp.MCPError("down")
        paypal_mcp.PayPalMCP = broken
        s.owner_message("Who hasn't paid yet?")
        self.assertIn("couldn't", s.q1("SELECT body FROM chat WHERE sender='bot' ORDER BY id DESC")["body"])


class Budget(unittest.TestCase):
    def test_claude_stops_at_the_daily_limit(self):
        keep = s.CLAUDE_DAILY_LIMIT, s.LLM.spent_on, s.LLM.calls
        s.CLAUDE_DAILY_LIMIT = 2
        s.LLM.spent_on = None
        try:
            self.assertEqual([s.LLM.within_budget() for _ in range(3)], [True, True, False])
        finally:
            s.CLAUDE_DAILY_LIMIT, s.LLM.spent_on, s.LLM.calls = keep


class Reminders(unittest.TestCase):
    def test_paypal_sends_reminders_for_its_invoices(self):
        c = {"id": 1, "email": "a@example.com", "phone": None}
        step = s.reminder_step({"id": 7, "paypal_id": "INV2-1"}, c, "Subject", "Body", "reminder")
        self.assertEqual((step["connector"], step["op"]), ("accounting", "remind"))
        self.assertEqual(s.reminder_step({"id": 7, "paypal_id": None}, c, "S", "B", "r")["connector"], "email")


if __name__ == "__main__":
    unittest.main()
