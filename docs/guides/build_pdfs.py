#!/usr/bin/env python3
"""
Builds the two Living Pay user guides from the screenshots in shots/:

    docs/guides/Living-Pay-Guide-1-Business-Owner.pdf
    docs/guides/Living-Pay-Guide-2-Client.pdf

    python3 docs/guides/build_pdfs.py
"""
import os

from PIL import Image as PILImage
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (Flowable, Image, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer,
                                Table, TableStyle)

HERE = os.path.dirname(os.path.abspath(__file__))
SHOTS = os.path.join(HERE, "shots")

FONT_DIR = "/System/Library/Fonts/Supplemental"
pdfmetrics.registerFont(TTFont("Body", os.path.join(FONT_DIR, "Arial.ttf")))
pdfmetrics.registerFont(TTFont("Body-Bold", os.path.join(FONT_DIR, "Arial Bold.ttf")))
pdfmetrics.registerFontFamily("Body", normal="Body", bold="Body-Bold", italic="Body", boldItalic="Body-Bold")

GREEN = colors.HexColor("#1f5f4a")
SOFT = colors.HexColor("#eef4f1")
INK = colors.HexColor("#1d1d1b")
MUTED = colors.HexColor("#6b6a65")
RED = colors.HexColor("#e83b3b")
WARM = colors.HexColor("#fff6e0")
WARM_INK = colors.HexColor("#7a5200")
LINE = colors.HexColor("#e6e3db")

S = {
    "title": ParagraphStyle("title", fontName="Body-Bold", fontSize=28, leading=33, textColor=INK, spaceAfter=6),
    "kicker": ParagraphStyle("kicker", fontName="Body-Bold", fontSize=10, leading=13, textColor=GREEN,
                             spaceAfter=4),
    "lead": ParagraphStyle("lead", fontName="Body", fontSize=13, leading=19, textColor=MUTED, spaceAfter=14),
    "h1": ParagraphStyle("h1", fontName="Body-Bold", fontSize=18, leading=23, textColor=INK, spaceBefore=2,
                         spaceAfter=4),
    "h2": ParagraphStyle("h2", fontName="Body-Bold", fontSize=13, leading=17, textColor=INK, spaceBefore=8,
                         spaceAfter=4),
    "body": ParagraphStyle("body", fontName="Body", fontSize=11, leading=16, textColor=INK, spaceAfter=6),
    "step": ParagraphStyle("step", fontName="Body", fontSize=11, leading=15.5, textColor=INK),
    "small": ParagraphStyle("small", fontName="Body", fontSize=9, leading=12, textColor=MUTED),
    "tip": ParagraphStyle("tip", fontName="Body", fontSize=10.5, leading=15, textColor=GREEN),
    "warn": ParagraphStyle("warn", fontName="Body", fontSize=10.5, leading=15, textColor=WARM_INK),
    "code": ParagraphStyle("code", fontName="Courier", fontSize=9.5, leading=13, textColor=INK),
    "toc": ParagraphStyle("toc", fontName="Body", fontSize=11.5, leading=19, textColor=INK),
    "center": ParagraphStyle("center", fontName="Body", fontSize=9, leading=12, textColor=MUTED,
                             alignment=TA_CENTER),
}

CONTENT_W = A4[0] - 36 * mm


class Badge(Flowable):
    """The red numbered circle used on the screenshots."""

    def __init__(self, n, size=16):
        super().__init__()
        self.n, self.size = n, size
        self.width = self.height = size

    def draw(self):
        c = self.canv
        c.setFillColor(RED)
        c.circle(self.size / 2, self.size / 2, self.size / 2, stroke=0, fill=1)
        c.setFillColor(colors.white)
        c.setFont("Body-Bold", self.size * 0.62)
        c.drawCentredString(self.size / 2, self.size * 0.29, str(self.n))


def shot(name, max_h=125 * mm, max_w=CONTENT_W):
    path = os.path.join(SHOTS, name)
    w, h = PILImage.open(path).size
    scale = min(max_w / w, max_h / h)
    img = Image(path, width=w * scale, height=h * scale)
    frame = Table([[img]], colWidths=[w * scale + 2])
    frame.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.6, LINE), ("LEFTPADDING", (0, 0), (-1, -1), 1),
                               ("RIGHTPADDING", (0, 0), (-1, -1), 1), ("TOPPADDING", (0, 0), (-1, -1), 1),
                               ("BOTTOMPADDING", (0, 0), (-1, -1), 1)]))
    frame.hAlign = "CENTER"
    return frame


def numbered(items):
    rows = [[Badge(n), Paragraph(text, S["step"])] for n, text in items]
    t = Table(rows, colWidths=[9 * mm, CONTENT_W - 9 * mm])
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 3),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 4), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
    return t


def box(text, kind="tip"):
    bg, style, label = (SOFT, S["tip"], "Tip") if kind == "tip" else (WARM, S["warn"], "Good to know")
    t = Table([[Paragraph("<b>%s:</b> %s" % (label, text), style)]], colWidths=[CONTENT_W])
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), bg), ("LEFTPADDING", (0, 0), (-1, -1), 10),
                           ("RIGHTPADDING", (0, 0), (-1, -1), 10), ("TOPPADDING", (0, 0), (-1, -1), 8),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 8), ("ROUNDEDCORNERS", [6, 6, 6, 6])]))
    return t


def code(text):
    t = Table([[Paragraph(text.replace(" ", "&nbsp;"), S["code"])]], colWidths=[CONTENT_W])
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f3f2ee")),
                           ("LEFTPADDING", (0, 0), (-1, -1), 8), ("TOPPADDING", (0, 0), (-1, -1), 6),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 6), ("ROUNDEDCORNERS", [4, 4, 4, 4])]))
    return t


def step(n, title, intro, image, items, extra=(), max_h=125 * mm):
    head = [Paragraph("STEP %d" % n, S["kicker"]), Paragraph(title, S["h1"]), Paragraph(intro, S["body"])]
    return [KeepTogether(head + [shot(image, max_h), Spacer(1, 8)]), numbered(items), Spacer(1, 6),
            *extra, PageBreak()]


def footer(label):
    def draw(canvas, doc):
        canvas.saveState()
        canvas.setFont("Body", 8.5)
        canvas.setFillColor(MUTED)
        canvas.drawString(18 * mm, 10 * mm, "Living Pay · %s" % label)
        canvas.drawRightString(A4[0] - 18 * mm, 10 * mm, "Page %d" % doc.page)
        canvas.setStrokeColor(LINE)
        canvas.line(18 * mm, 14 * mm, A4[0] - 18 * mm, 14 * mm)
        canvas.restoreState()
    return draw


def build(path, label, story):
    doc = SimpleDocTemplate(path, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm,
                            bottomMargin=20 * mm, title="Living Pay: %s" % label, author="Living Pay")
    doc.build(story, onFirstPage=footer(label), onLaterPages=footer(label))
    print("wrote", os.path.relpath(path, HERE))


def cover(kicker, title, lead, who, contents):
    out = [Spacer(1, 30 * mm), Paragraph(kicker, S["kicker"]), Paragraph(title, S["title"]),
           Paragraph(lead, S["lead"]), Spacer(1, 4)]
    out.append(box(who))
    out += [Spacer(1, 12), Paragraph("In this guide", S["h2"])]
    out += [Paragraph("%d.&nbsp;&nbsp;%s" % (i + 1, c), S["toc"]) for i, c in enumerate(contents)]
    out.append(PageBreak())
    return out


# ---------------------------------------------------------------------------
# Guide 1: the business owner (the person sending invoices)
# ---------------------------------------------------------------------------

def owner_guide():
    story = cover(
        "GUIDE 1 OF 2 · FOR THE BUSINESS OWNER",
        "How to send an invoice and get paid with Living Pay",
        "Living Pay sends your clients a PayPal invoice that can answer their questions, agree a payment plan "
        "or a small discount within rules you set, and ask you about anything bigger. You never chase anyone: "
        "you answer a few messages on your phone, and the money arrives in PayPal.",
        "This guide is for you, the person <b>sending</b> invoices. In the examples you are <b>Kora Studio</b>, "
        "a design studio, billing a client called <b>Zainab Musa</b> from Musa Bakes $800 for a bakery logo, "
        "with $200 of it going to your illustrator, Ada Nwosu. Guide 2 shows what Zainab sees. "
        "On screen, the app is labelled <b>Living Invoice</b>: that's Living Pay.",
        ["Open Living Pay", "Your screen at a glance", "Send an invoice", "What your client receives",
         "When a client asks for something", "Getting paid, and paying your helper",
         "Check your invoices and payouts", "See and change the agent's rules", "The money ledger",
         "Quick answers"])

    story += [Paragraph("BEFORE YOU START", S["kicker"]), Paragraph("Open Living Pay", S["h1"]),
              Paragraph("Living Pay runs on your Mac. Open the <b>Terminal</b> app (press Command + Space, "
                        "type Terminal, press Enter), paste this line and press Enter:", S["body"]),
              code('cd "/Users/amagatech/Desktop/PAYPAL hackathon /living-invoice" && python3 server.py'),
              Spacer(1, 6),
              Paragraph("Then open <b>http://localhost:8790</b> in Safari. Leave the Terminal window open while "
                        "you use Living Pay; closing it stops the app.", S["body"]),
              Spacer(1, 6),
              box("This uses your <b>PayPal sandbox</b>: PayPal's practice mode. Invoices and payments look real "
                  "but no real money moves, so you can try anything safely.", "warn"),
              Spacer(1, 8),
              Paragraph("Just want to practise without PayPal at all? Use this line instead. It runs a "
                        "pretend PayPal, so you can pay invoices with one click:", S["body"]),
              code('cd "/Users/amagatech/Desktop/PAYPAL hackathon /living-invoice" && ./scripts/demo.sh'),
              PageBreak()]

    story += step(1, "Your screen at a glance",
                  "Living Pay shows two things side by side. Everything happens on the left, and the app talks to "
                  "you on the right.",
                  "owner-01-overview.png",
                  [(1, "<b>Your business tools.</b> Tabs for your <b>Calendar</b> of jobs, <b>Accounting</b> "
                       "(invoices and payouts), <b>New invoice</b>, <b>Sent to clients</b> (emails) and "
                       "<b>New appointment</b>."),
                   (2, "<b>Your phone.</b> Living Pay messages you here when something needs a decision, or to "
                       "tell you what it did. Answer with the buttons in a message, or type in the box at the "
                       "bottom and press <b>Send</b>.")],
                  [box("In real life the phone would be WhatsApp. Here it's a pretend phone on the same screen.")])

    story += step(2, "Open the New invoice tab",
                  "To bill someone, click the <b>New invoice</b> tab at the top of the left side.",
                  "owner-02-new-invoice-empty.png",
                  [(1, "Click <b>New invoice</b>. A short form appears.")], max_h=115 * mm)

    story += step(3, "Fill in the invoice and send it",
                  "Fill in who you're billing, what for and how much. Only the client, what it's for and the "
                  "amount are required.",
                  "owner-03-new-invoice-filled.png",
                  [(1, "<b>Client.</b> Pick someone you've billed before, or choose <b>+ New client…</b>"),
                   (2, "For a new client, type their <b>name</b>, <b>company</b> (optional) and <b>email</b>. "
                       "The invoice is emailed there."),
                   (3, "<b>What it's for</b>, in a few words. The client sees this on the invoice."),
                   (4, "<b>Amount</b> to charge."),
                   (5, "<b>Due in</b>: how many days the client has to pay. 14 is fine."),
                   (6, "<b>Subcontractor's share</b> (optional): part of the money that belongs to someone who "
                       "helped you. Leave it at 0 if nobody did."),
                   (7, "<b>Paid to</b>: who gets that share. Choose <b>+ New subcontractor…</b> to add someone "
                       "with their PayPal email."),
                   (8, "Click <b>Send invoice</b>. It goes out straight away.")], max_h=118 * mm)

    story += step(4, "Your invoice is on its way",
                  "After sending, Living Pay switches to the Accounting tab and confirms on your phone.",
                  "owner-04-sent.png",
                  [(1, "Your new invoice, with its number, client, amount and due date."),
                   (2, "<b>Client view</b> opens the exact page your client sees. Use it to check the invoice, "
                       "or copy its address to send it yourself."),
                   (3, "Your phone confirms what was sent. Made a mistake? Tap <b>Undo</b>: the invoice is "
                       "cancelled in PayPal and the client gets a short \"please disregard\" email.")])

    story += step(5, "What your client receives",
                  "The client gets an email with a link to their invoice page. You can read every message Living "
                  "Pay sent in the <b>Sent to clients</b> tab.",
                  "owner-05-email.png",
                  [(1, "The email. The link at the bottom opens the invoice, where the client can ask questions, "
                       "arrange a plan and pay.")],
                  [box("Right now emails are shown here rather than really sent. To send real emails from your "
                       "Gmail, open <b>Audit log and connections</b> (top right) and click <b>Connect Google</b>. "
                       "Even then it sends to <b>you</b> first, until you switch on sending to clients.", "warn")],
                  max_h=95 * mm)

    story += step(6, "When a client asks for something",
                  "Clients can ask their invoice for a payment plan, more time or a discount. Small requests inside "
                  "your rules are agreed straight away, and you just get a <b>Done</b> message with an Undo button. "
                  "Anything bigger comes to you as a question like this one.",
                  "owner-06-approve-card.png",
                  [(1, "What the client asked for, and why it needs you. Here Zainab asked for the invoice at "
                       "$700 instead of $800."),
                   (2, "<b>Approve</b>: agree. The invoice changes in PayPal and the client is told at once."),
                   (3, "<b>Skip</b>: say no. The client is told politely, and the invoice stays as it was."),
                   (4, "Or <b>counter-offer</b> by typing, for example <b>counter $750</b>, <b>counter 5%</b>, "
                       "<b>counter 10 days</b> or <b>counter 2 payments</b>, and press Send.")],
                  [box("If you approve the same kind of request three times in a row, Living Pay offers to "
                       "handle those itself in future. If you later undo one, it goes back to asking you.")],
                  max_h=118 * mm)

    story += step(7, "You've answered",
                  "Your phone confirms what happened, and the client's page updates by itself.",
                  "owner-07-approved.png",
                  [(1, "The confirmation. <b>Undo</b> puts the old price back if you change your mind.")],
                  max_h=118 * mm)

    story += step(8, "Getting paid, and paying your helper",
                  "Each time the client pays, your phone tells you. When the invoice is paid in full and a "
                  "subcontractor is on it, Living Pay offers to pay them their share through PayPal.",
                  "owner-08-payout-card.png",
                  [(1, "<b>Paid:</b> the invoice is settled."),
                   (2, "The offer to pay Ada her $200 share. The first payment to anyone always asks you first."),
                   (3, "Tap <b>Approve</b> and PayPal sends Ada the money. She gets an email from PayPal to "
                       "collect it.")],
                  [box("Changed your mind after approving? <b>Undo</b> cancels the payout as long as Ada hasn't "
                       "collected it yet. If PayPal is still processing it, try Undo again a minute later.")],
                  max_h=118 * mm)

    story += step(9, "Check your invoices and payouts",
                  "The <b>Accounting</b> tab lists every invoice and every payout, with its status.",
                  "owner-09-accounting.png",
                  [(1, "<b>Invoices</b>: who owes what. Labels show <b>due</b>, <b>overdue</b> or <b>paid</b>. "
                       "<b>Record payment</b> is for money that arrived outside PayPal, like a bank transfer."),
                   (2, "<b>Payouts to subcontractors</b>: <b>unclaimed</b> means sent but not collected yet.")],
                  max_h=110 * mm)

    story += step(10, "See the agent's rules",
                  "The rules decide what the invoice may agree to without asking you. Type <b>rules</b> on your "
                  "phone and press Send to see them.",
                  "owner-10-rules.png",
                  [(1, "Your current rules, in plain words."),
                   (2, "Type here, then press <b>Send</b>.")], max_h=118 * mm)

    story += step(11, "Change the rules in your own words",
                  "Type a rule the way you'd say it, for example <b>Allow up to 4 payments and 3% off for paying "
                  "today</b>. Living Pay shows you exactly what would change before anything does.",
                  "owner-11-rules-change.png",
                  [(1, "The old rule is crossed out, and the new one is in bold."),
                   (2, "Tap <b>Yes</b> to save, or <b>No</b> to leave the rules as they were.")],
                  [box("Other things you can type: <b>Only offer payment plans to returning clients</b>, "
                       "<b>Allow up to 3 weeks extra</b>, <b>Decline any discount over 15%</b>.")],
                  max_h=118 * mm)

    story += step(12, "The money ledger",
                  "Click <b>Money ledger</b> at the top of the owner screen to see everything in one table: every "
                  "invoice, request, decision, payment and payout.",
                  "owner-12-ledger.png",
                  [(1, "Totals: what's still owed, what's been collected and what's been paid out, plus how many "
                       "requests the agent agreed, asked you about or blocked."),
                   (2, "Show only requests, payments, payouts or invoices. The search box finds a client or "
                       "invoice."),
                   (3, "Every event, newest first. <b>Undo</b> reverses anything that can still be reversed.")],
                  max_h=118 * mm)

    story += [Paragraph("Quick answers", S["h1"]), Spacer(1, 4)]
    qa = [
        ("I sent an invoice by mistake.", "Tap <b>Undo</b> under its \"Sent\" message on the phone. It's "
         "cancelled in PayPal and the client gets a correction email."),
        ("Where do I find the link to a client's invoice?", "<b>Accounting</b> tab → <b>Client view</b> on that "
         "invoice's row."),
        ("What can I type on the phone?", "Type <b>help</b> for the full list. The buttons under the box are "
         "shortcuts: <b>yes</b>, <b>no</b>, <b>undo</b>, <b>summary</b> and <b>what are you doing</b>."),
        ("Can the invoice give away too much?", "No. It can only agree to what your rules allow. Anything more "
         "comes to you, and requests far outside the rules, like 90% off, are declined without bothering you."),
        ("What are +1 hour / +1 day / +1 week?", "They move the practice clock forward, so you can see overdue "
         "reminders and later installments without waiting."),
        ("How do I start again from scratch?", "Click <b>Reset demo</b> at the top right. It clears the practice "
         "data on your Mac."),
        ("Is this real money?", "Not in the sandbox. Invoices, payments and payouts all happen in PayPal's practice "
         "mode."),
    ]
    for q, a in qa:
        story += [Paragraph("<b>%s</b>" % q, S["body"]), Paragraph(a, S["body"]), Spacer(1, 4)]
    build(os.path.join(HERE, "Living-Pay-Guide-1-Business-Owner.pdf"), "Guide for the business owner", story)


# ---------------------------------------------------------------------------
# Guide 2: the client (the person receiving and paying the invoice)
# ---------------------------------------------------------------------------

def client_guide():
    story = cover(
        "GUIDE 2 OF 2 · FOR THE CLIENT",
        "How to read, discuss and pay a Living Pay invoice",
        "A Living Pay invoice is a web page you can talk to. Ask what it's for, ask to pay in parts or a little "
        "later, ask about a discount, then pay with PayPal or a card. You don't need an account with Living Pay.",
        "This guide is for the person <b>receiving</b> an invoice. In the examples you are <b>Zainab Musa</b> "
        "from Musa Bakes, and you've been sent an $800 invoice by Kora Studio for a bakery logo and menu board.",
        ["Open your invoice", "Your invoice page", "Ask the invoice a question", "Pay in parts",
         "Ask for a better price", "When the business answers", "Pay with PayPal", "After you pay",
         "Paid in full", "Good to know"])

    story += step(1, "Open your invoice",
                  "You'll get an email from the business with your invoice. Click the link in it to open your "
                  "invoice page.",
                  "owner-05-email.png",
                  [(1, "The email. The last link opens your personal invoice page. Only people with this link "
                       "can see it.")], max_h=95 * mm)

    story += step(2, "Your invoice page",
                  "Your invoice page has three parts.",
                  "client-01-page.png",
                  [(1, "<b>The invoice</b>: who it's for, what the work was, the price and the due date."),
                   (2, "<b>Paying</b>: how much is due and the button to pay it."),
                   (3, "<b>Ask this invoice</b>: a chat where you can ask questions and make requests. It "
                       "answers straight away.")])

    story += step(3, "Ask the invoice a question",
                  "Tap one of the ready-made questions, or type your own. Write the way you'd talk to a person.",
                  "client-02-chat-box.png",
                  [(1, "Tap a suggestion: a payment plan, a discount for paying today, more time, or what the "
                       "invoice is for."),
                   (2, "Or type your own message and press <b>Send</b>.")], max_h=118 * mm)

    story += step(4, "Pay in parts",
                  "If you'd rather pay in several smaller amounts, just ask. Requests within the business's rules "
                  "are agreed immediately.",
                  "client-03-plan.png",
                  [(1, "The invoice confirms the plan in plain words."),
                   (2, "Your payment plan: each amount and when it's due."),
                   (3, "What to pay now, which is only the first part.")])

    story += step(5, "Ask for a better price",
                  "You can ask for a lower price or a different deal. If it's more than the invoice may agree to "
                  "itself, it asks the business for you.",
                  "client-04-asked-owner.png",
                  [(1, "The invoice tells you it has asked the business."),
                   (2, "A yellow note shows you're waiting for an answer. You can close the page and come back "
                       "later: the answer appears here by itself.")],
                  [box("You can still pay the current amount while you wait. Nothing changes on your invoice "
                       "until the business says yes.")])

    story += step(6, "When the business answers",
                  "When the business replies, your invoice page updates on its own.",
                  "client-05-owner-answered.png",
                  [(1, "The answer. Here Kora Studio agreed to $700."),
                   (2, "Your invoice already shows the new total, and the plan is adjusted to match.")])

    story += step(7, "Pay with PayPal",
                  "When you're ready, choose how much to pay, then click the yellow <b>PayPal</b> button, or "
                  "<b>Debit or Credit Card</b> if you don't use PayPal.",
                  "client-06-paypal-button.png",
                  [(1, "The payment buttons. If you're on a plan, first choose <b>Next payment</b> or "
                       "<b>Whole balance</b> just above them.")], max_h=58 * mm,
                  extra=[box("Your payment goes through PayPal itself. Living Pay never sees your card or "
                             "PayPal password.")])
    story.pop()  # keep the PayPal window on the same page as its button
    story += [Spacer(1, 6), Paragraph("A PayPal window opens", S["h2"]),
              Paragraph("Log in to PayPal and confirm the payment. When you're done the window closes, and you're "
                        "back on your invoice.", S["body"]),
              shot("client-06b-paypal-login.png", max_h=62 * mm, max_w=56 * mm),
              Spacer(1, 6),
              box("<b>Trying it in the practice sandbox?</b> Log in with a sandbox <b>Personal</b> test account, "
                  "for example <b>amagafx121@livingpay.com</b>, not a real PayPal account. You'll find its "
                  "password in the PayPal developer site under <b>Testing Tools → Sandbox Accounts</b>, then the "
                  "three-dot menu on that account's row → <b>View/Edit account</b>.", "warn"),
              PageBreak()]

    story += step(8, "After you pay",
                  "The invoice thanks you straight away and shows what's left.",
                  "client-07-paid-installment.png",
                  [(1, "Confirmation of your payment, how much is left and what's due next."),
                   (2, "Your plan shows the paid part as <b>Paid</b>."),
                   (3, "The next amount due.")])

    story += step(9, "Paid in full",
                  "Once everything is paid, the invoice says so, and there's nothing more to do.",
                  "client-08-paid-in-full.png",
                  [(1, "The invoice is marked <b>Paid</b>."),
                   (2, "No more payments are due."),
                   (3, "The final confirmation.")])

    story += [Paragraph("Good to know", S["h1"]), Spacer(1, 4)]
    notes = [
        ("What can the invoice agree to?", "A payment plan, a little more time, or a discount for paying today, "
         "as far as the business allows. Anything else, it asks the business for you."),
        ("What can't it do?", "It can't refund money, cancel the invoice or change the work that was agreed. For "
         "those, it passes your question to the business, who replies by email."),
        ("Is it a real person?", "No. It's an assistant built into the invoice, working within rules the business "
         "set. The business sees every request and can change anything it agreed."),
        ("Can I pay without a PayPal account?", "Yes. Choose <b>Debit or Credit Card</b> under the PayPal "
         "button."),
        ("I closed the page.", "Open the link in your email again. Everything you agreed is still there."),
    ]
    for q, a in notes:
        story += [Paragraph("<b>%s</b>" % q, S["body"]), Paragraph(a, S["body"]), Spacer(1, 4)]
    build(os.path.join(HERE, "Living-Pay-Guide-2-Client.pdf"), "Guide for clients", story)


if __name__ == "__main__":
    owner_guide()
    client_guide()
