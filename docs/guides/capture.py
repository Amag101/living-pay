#!/usr/bin/env python3
"""
Walks through Living Pay the way a business owner and a client would, and saves an
annotated screenshot of every step for the user guides (docs/guides/shots/).

Runs two private copies of the app: one against the fake PayPal (the whole story,
start to finish) and one against the real PayPal sandbox from .env (the real PayPal
button and PayPal's own popup). Needs Playwright:

    pip3 install --user playwright && python3 -m playwright install chromium
    python3 docs/guides/capture.py
"""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request

from PIL import Image, ImageDraw, ImageFont
from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
SHOTS = os.path.join(HERE, "shots")
SCALE = 2
VIEW = {"width": 1280, "height": 860}
ACCENT = (232, 59, 59)
FONT = None
for path in ("/System/Library/Fonts/Supplemental/Arial Bold.ttf", "/Library/Fonts/Arial Bold.ttf",
             "/System/Library/Fonts/Helvetica.ttc"):
    if os.path.exists(path):
        FONT = ImageFont.truetype(path, 30)
        break


def start(port, db, env):
    e = dict(os.environ, PORT=str(port), INVISIBLE_DB=db, PUBLIC_URL="http://localhost:%d" % port, **env)
    proc = subprocess.Popen([sys.executable, "server.py"], cwd=ROOT, env=e,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        try:
            urllib.request.urlopen("http://localhost:%d/api/state" % port, timeout=1).read()
            return proc
        except OSError:
            time.sleep(0.2)
    raise SystemExit("server on %d didn't start" % port)


def post(port, path, body):
    req = urllib.request.Request("http://localhost:%d%s" % (port, path), data=json.dumps(body).encode(),
                                 method="POST", headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=120).read())


def annotate(png, marks, crop=None):
    """marks: [(number, box)] with boxes in page CSS pixels; crop: box in CSS pixels."""
    img = Image.open(png).convert("RGB")
    ox, oy = (crop["x"], crop["y"]) if crop else (0, 0)
    d = ImageDraw.Draw(img)
    for n, b in marks:
        if not b:
            continue
        x0, y0 = (b["x"] - ox - 4) * SCALE, (b["y"] - oy - 4) * SCALE
        x1, y1 = (b["x"] - ox + b["width"] + 4) * SCALE, (b["y"] - oy + b["height"] + 4) * SCALE
        d.rounded_rectangle([x0, y0, x1, y1], radius=12, outline=ACCENT, width=5)
        r = 22
        cx, cy = max(x0, r + 4), max(y0, r + 4)
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=ACCENT)
        d.text((cx, cy), str(n), fill="white", font=FONT, anchor="mm")
    img.save(png)


class Shooter:
    def __init__(self, page, prefix):
        self.page, self.prefix = page, prefix

    def box(self, selector, nth=-1):
        loc = self.page.locator(selector)
        loc = loc.last if nth == -1 else loc.nth(nth)
        loc.scroll_into_view_if_needed()
        return loc.bounding_box()

    def shot(self, name, marks=(), crop=None, pad=10):
        self.page.wait_for_timeout(400)
        path = os.path.join(SHOTS, "%s-%s.png" % (self.prefix, name))
        region = None
        if crop:
            b = self.box(crop, 0)
            region = {"x": max(b["x"] - pad, 0), "y": max(b["y"] - pad, 0),
                      "width": b["width"] + 2 * pad, "height": b["height"] + 2 * pad}
        boxes = [(n, self.box(sel) if isinstance(sel, str) else sel) for n, sel in marks]
        self.page.screenshot(path=path, clip=region)
        annotate(path, boxes, region)
        print("saved", os.path.basename(path))


def wait_chat(page, count):
    page.wait_for_function("n => document.querySelectorAll('#msgs .msg:not(.typing)').length >= n", arg=count,
                           timeout=120000)
    page.wait_for_timeout(300)


def client_say(page, text, expect):
    page.fill("#text", text)
    page.press("#text", "Enter")
    wait_chat(page, expect)


def owner_say(page, text):
    page.fill("#msg", text)
    page.press("#msg", "Enter")
    page.wait_for_timeout(1500)


def story(pw, key):
    """The whole story against the fake PayPal."""
    tmp = tempfile.mkdtemp()
    fake = subprocess.Popen([sys.executable, "fake_paypal.py", "--port", "8895", "--webhook",
                             "http://localhost:8894/webhooks/paypal"], cwd=ROOT,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    app = start(8894, os.path.join(tmp, "story.db"), {
        "PAYPAL_API_BASE": "http://localhost:8895", "PAYPAL_CLIENT_ID": "fake", "PAYPAL_CLIENT_SECRET": "fake",
        "PAYPAL_WEBHOOK_ID": "WH-FAKE-1", "ANTHROPIC_API_KEY": key})
    browser = pw.chromium.launch()
    try:
        ctx = browser.new_context(viewport=VIEW, device_scale_factor=SCALE, color_scheme="light")
        owner = ctx.new_page()
        o = Shooter(owner, "owner")
        owner.goto("http://localhost:8894/")
        owner.wait_for_selector("#thread .bubble")
        o.shot("01-overview", [(1, "section[aria-label='Business tools']"), (2, ".phone")])

        owner.click("button[data-tab=invoice]")
        o.shot("02-new-invoice-empty", [(1, "button[data-tab=invoice]")])
        owner.select_option("#invclient", "new")
        owner.fill("#invform [name=new_name]", "Zainab Musa")
        owner.fill("#invform [name=new_company]", "Musa Bakes")
        owner.fill("#invform [name=new_email]", "zainab@musabakes.example")
        owner.fill("#invform [name=title]", "Bakery logo and menu board")
        owner.fill("#invform [name=amount]", "800")
        owner.fill("#invform [name=due_days]", "14")
        owner.fill("#invform [name=sub_share]", "200")
        owner.select_option("#invsub", "1")
        o.shot("03-new-invoice-filled", [
            (1, "#invclient"), (2, "#invform .newclient"), (3, "#invform [name=title]"),
            (4, "#invform [name=amount]"), (5, "#invform [name=due_days]"), (6, "#invform [name=sub_share]"),
            (7, "#invsub"), (8, "#invform button[type=submit]")], crop="section[aria-label='Business tools'] .card:not(.clock)")
        owner.click("#invform button[type=submit]")
        owner.wait_for_selector("#tab-accounting:not([hidden])")
        owner.wait_for_timeout(800)
        o.shot("04-sent", [(1, "#tab-accounting tbody tr >> nth=0"), (2, "#tab-accounting a:has-text('Client view') >> nth=0"),
                           (3, "#thread .bubble:has-text('Sent:')")])

        owner.click("button[data-tab=outbox]")
        o.shot("05-email", [(1, "#tab-outbox .msg-item >> nth=0")],
               crop="section[aria-label='Business tools'] .card:not(.clock)")

        db = sqlite3.connect(os.path.join(tmp, "story.db"))
        token = db.execute("SELECT token FROM invoices ORDER BY id DESC LIMIT 1").fetchone()[0]

        client = ctx.new_page()
        c = Shooter(client, "client")
        client.goto("http://localhost:8894/i/%s" % token)
        client.wait_for_selector("#paybtn")
        c.shot("01-page", [(1, "#lines"), (2, "#paybox"), (3, ".chat")])
        c.shot("02-chat-box", [(1, "#chips"), (2, "#send")], crop=".chat")

        client.click("#chips button:has-text('3 payments')")
        wait_chat(client, 2)
        c.shot("03-plan", [(1, "#msgs .msg.agent"), (2, "#plan"), (3, "#paybox .due")])

        client_say(client, "Could you do it for $700?", 4)
        client.wait_for_selector(".banner")
        c.shot("04-asked-owner", [(1, "#msgs .msg.agent"), (2, ".banner")])

        owner.reload()
        owner.wait_for_selector("#thread .bubble:has-text('Approve?')")
        owner.wait_for_timeout(600)
        o.shot("06-approve-card", [(1, "#thread .bubble:has-text('Approve?') >> nth=-1"),
                                   (2, "#thread .btnrow button:has-text('Approve') >> nth=-1"),
                                   (3, "#thread .btnrow button:has-text('Skip') >> nth=-1"),
                                   (4, "#msg")], crop=".phone", pad=4)
        owner.click("#thread .btnrow button:has-text('Approve') >> nth=-1")
        owner.wait_for_timeout(1500)
        o.shot("07-approved", [(1, "#thread .bubble >> nth=-1")], crop=".phone", pad=4)

        client.reload()
        wait_chat(client, 5)
        c.shot("05-owner-answered", [(1, "#msgs .msg.agent >> nth=-1"), (2, "#lines")])

        client.click("#paybtn")
        wait_chat(client, 6)
        client.wait_for_selector("#paybtn")
        c.shot("07-paid-installment", [(1, "#msgs .msg.agent >> nth=-1"), (2, "#plan"), (3, "#paybox .due")])

        client.check("input[name=which][value=balance]")
        client.click("#paybtn")
        wait_chat(client, 7)
        c.shot("08-paid-in-full", [(1, "#status"), (2, "#paybox"), (3, "#msgs .msg.agent >> nth=-1")])

        owner.reload()
        owner.wait_for_selector("#thread .bubble:has-text('Pay Ada Nwosu')")
        owner.wait_for_timeout(600)
        o.shot("08-payout-card", [(1, "#thread .bubble:has-text('is now settled') >> nth=-1"),
                                  (2, "#thread .bubble:has-text('Pay Ada Nwosu') >> nth=-1"),
                                  (3, "#thread .btnrow button:has-text('Approve') >> nth=-1")], crop=".phone", pad=4)
        owner.click("#thread .btnrow button:has-text('Approve') >> nth=-1")
        owner.wait_for_timeout(1500)
        owner.click("button[data-tab=accounting]")
        o.shot("09-accounting", [(1, "#tab-accounting table >> nth=0"), (2, "#tab-accounting table >> nth=1")],
               crop="section[aria-label='Business tools'] .card:not(.clock)")

        owner_say(owner, "rules")
        o.shot("10-rules", [(1, "#thread .bubble >> nth=-1"), (2, "#msg")], crop=".phone", pad=4)
        owner_say(owner, "Allow up to 4 payments and 3% off for paying today")
        o.shot("11-rules-change", [(1, "#thread .bubble >> nth=-1"),
                                   (2, "#thread .btnrow button:has-text('Yes') >> nth=-1")], crop=".phone", pad=4)
        owner.click("#thread .btnrow button:has-text('Yes') >> nth=-1")
        owner.wait_for_timeout(1200)

        ledger = ctx.new_page()
        ledger.set_viewport_size(VIEW)
        ledger.goto("http://localhost:8894/ledger")
        ledger.wait_for_selector(".ag-row")
        ledger.wait_for_timeout(1500)
        Shooter(ledger, "owner").shot("12-ledger", [(1, "#kpis"), (2, "#kinds"), (3, ".grid-card")])
    finally:
        browser.close()
        app.terminate()
        fake.terminate()


def real_paypal(pw, key):
    """The real PayPal button and popup, from the real sandbox in .env."""
    tmp = tempfile.mkdtemp()
    app = start(8892, os.path.join(tmp, "real.db"), {"ANTHROPIC_API_KEY": key})
    browser = pw.chromium.launch()
    try:
        post(8892, "/api/sandbox/invoice/new", {
            "contact_id": "new", "new_name": "Zainab Musa", "new_company": "Musa Bakes",
            "new_email": "zainab@musabakes.example", "title": "Bakery logo and menu board", "amount": "800",
            "due_days": "14", "sub_share": "0", "sub_id": ""})
        token = sqlite3.connect(os.path.join(tmp, "real.db")).execute(
            "SELECT token FROM invoices ORDER BY id DESC LIMIT 1").fetchone()[0]
        ctx = browser.new_context(viewport=VIEW, device_scale_factor=SCALE, color_scheme="light")
        page = ctx.new_page()
        c = Shooter(page, "client")
        page.goto("http://localhost:8892/i/%s" % token)
        page.wait_for_selector("#paypal-buttons iframe", timeout=60000)
        page.wait_for_timeout(4000)
        c.shot("06-paypal-button", [(1, "#paypal-buttons")], crop="#paybox")
        try:
            frame = page.frame_locator("#paypal-buttons iframe").first
            with ctx.expect_page(timeout=30000) as popup_info:
                frame.locator("[data-funding-source=paypal]").first.click()
            popup = popup_info.value
            popup.set_viewport_size({"width": 520, "height": 720})
            popup.wait_for_load_state("networkidle", timeout=60000)
            popup.wait_for_timeout(3000)
            path = os.path.join(SHOTS, "client-06b-paypal-login.png")
            popup.screenshot(path=path)
            print("saved", os.path.basename(path))
        except Exception as e:  # PayPal's popup is theirs; the guide works without this picture
            print("PayPal popup not captured:", repr(e)[:200])
    finally:
        browser.close()
        app.terminate()


def main():
    os.makedirs(SHOTS, exist_ok=True)
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    with sync_playwright() as pw:
        story(pw, key)
        real_paypal(pw, key)


if __name__ == "__main__":
    main()
