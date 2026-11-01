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

import paypal_mcp  # noqa: E402
import server as s  # noqa: E402

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
        self.assertEqual(s.get_setting("discount_chip", "0"), "0")
        self.hit("/api/settings/discount_chip", {"on": True}, cookie)
        self.assertEqual(s.get_setting("discount_chip"), "1")
        self.assertEqual(self.hit("/api/settings/business", {"name": " "}, cookie)[0], 400)


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
    def test_paypal_sends_reminders_for_its_invoices_without_gmail(self):
        c = {"id": 1, "email": "a@example.com", "phone": None}
        step = s.reminder_step({"id": 7, "paypal_id": "INV2-1"}, c, "email", "Subject", "Body", "reminder")
        self.assertEqual((step["connector"], step["op"]), ("accounting", "remind"))
        self.assertEqual(s.reminder_step({"id": 7, "paypal_id": None}, c, "email", "S", "B", "r")["connector"], "email")


if __name__ == "__main__":
    unittest.main()
