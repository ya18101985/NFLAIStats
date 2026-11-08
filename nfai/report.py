"""Per-wave emails (user's request, 2026-11-08): one when a wave's bets are placed, one when they
have all settled. Plain HTML with inline styles so Gmail renders it as designed."""

import html
import os
import smtplib
from email.message import EmailMessage
from pathlib import Path

TO = "yash.us.visa@gmail.com"
FROM = "nfai.reports@gmail.com"
PASSWORD_FILE = Path.home() / ".kalshi" / "gmail_app_password.txt"   # cloud: GMAIL_APP_PASSWORD
# The cloud allows only web traffic, so mail goes through the user's Apps Script web app (owned by
# nfai.reports, hard-wired to email TO only), which checks this key. Cloud: NFAI_MAIL_KEY.
HOOK_URL = ("https://script.google.com/macros/s/AKfycbzw-ZqwiQVGbD0jrSlKAooEwfVAbavfIZF4A6b_JwdlzzsyjaLAYcTLr5p5PD-rGBDR"
            "/exec")
HOOK_KEY_FILE = Path.home() / ".kalshi" / "mail_hook_key.txt"

FONT = "font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif"
TH = "padding:8px 10px;text-align:left;font-size:12px;color:#555;border-bottom:2px solid #ddd;white-space:nowrap"
TD = "padding:8px 10px;font-size:13px;border-bottom:1px solid #eee;vertical-align:top"
NUM = TD + ";text-align:right;white-space:nowrap"
GREEN, RED, GREY = "#1a7f37", "#cf222e", "#666"


def usd(x, sign=False):
    s = f"${abs(x):,.2f}"
    return ("+" if x >= 0 else "−") + s if sign else ("−" + s if x < 0 else s)


def american(price):
    """Kalshi price (chance implied, 0-1) as sportsbook odds, e.g. 0.40 -> +150, 0.60 -> -150."""
    if price <= 0 or price >= 1:
        return ""
    return f"+{round(100 * (1 - price) / price)}" if price < 0.5 else f"−{round(100 * price / (1 - price))}"


def _table(head, rows, num_cols=()):
    h = "".join(f'<th style="{TH}{";text-align:right" if i in num_cols else ""}">{c}</th>' for i, c in enumerate(head))
    body = "".join("<tr>" + "".join(f'<td style="{NUM if i in num_cols else TD}">{c}</td>' for i, c in enumerate(r))
                   + "</tr>" for r in rows)
    return f'<table cellspacing="0" style="border-collapse:collapse;width:100%;{FONT}"><tr>{h}</tr>{body}</table>'


def _tiles(items):
    cells = "".join(f'<td style="padding:10px 14px;background:#f6f8fa;border-radius:6px;{FONT}">'
                    f'<div style="font-size:11px;color:{GREY};text-transform:uppercase;letter-spacing:.04em">{k}</div>'
                    f'<div style="font-size:20px;font-weight:600;color:{c or "#111"}">{v}</div></td><td width="8"></td>'
                    for k, v, c in items)
    return f'<table cellspacing="0"><tr>{cells}</tr></table>'


def _page(title, sub, inner):
    return (f'<div style="{FONT};max-width:900px;margin:0 auto;color:#111">'
            f'<h2 style="margin:0 0 4px">{title}</h2><div style="color:{GREY};font-size:13px;margin-bottom:16px">{sub}</div>'
            f'{inner}<p style="color:{GREY};font-size:11px;margin-top:24px">Sent automatically by NFAIstats. '
            f'Prices are Kalshi contract prices: a 40¢ contract pays $1 if it wins (40% implied chance, +150 in sportsbook odds).</p></div>')


def placed_email(wave, kickoff, bankroll, singles, parlays):
    """singles: dicts with bet, side, contracts, price, stake, p, edge, reason.
    parlays: dicts with legs (list of str), price, stake, p, note."""
    rows = [[html.escape(b["bet"]), b["side"].upper(), f'{b["contracts"]:,.0f}',
             f'{100 * b["price"]:.0f}¢ <span style="color:{GREY}">({american(b["price"])})</span>',
             usd(b["stake"]), f'{100 * b["p"]:.0f}%',
             f'<span style="color:{GREEN if b["edge"] >= 0 else RED}">{100 * b["edge"]:+.1f}¢</span>',
             html.escape(b["reason"])] for b in singles]
    prow = [["<br>".join(html.escape(x) for x in p["legs"]), f'{100 * p["price"]:.0f}¢ <span style="color:{GREY}">'
             f'({american(p["price"])})</span>', usd(p["stake"]), f'{100 * p["p"]:.0f}%', html.escape(p["note"])]
            for p in parlays]
    spent = sum(b["stake"] for b in singles) + sum(p["stake"] for p in parlays)
    inner = (_tiles([("Wave bankroll", usd(bankroll), None), ("Bet", usd(spent), None),
                     ("Singles", str(len(singles)), None), ("Parlays", str(len(parlays)), None)])
             + '<h3 style="margin:20px 0 8px">Singles</h3>'
             + _table(["Bet", "Side", "Contracts", "Price (odds)", "Stake", "Our chance", "Edge", "Why"], rows, (2, 3, 4, 5, 6))
             + '<h3 style="margin:20px 0 8px">Parlays</h3>'
             + _table(["Legs", "Price (odds)", "Stake", "Our chance", "Why"], prow, (1, 2, 3)))
    subject = f"NFAI bets placed: {wave} ({len(singles) + len(parlays)} bets, {usd(spent)})"
    return subject, _page(f"Bets placed: {html.escape(wave)}", f"Kickoff {kickoff} · edge = expected profit per $1 contract after Kalshi's fee", inner)


def settled_email(wave, bets, balance, season_profit):
    """bets: dicts with bet, kind ('single'/'parlay'), side, stake, payout, result ('won'/'lost')."""
    staked = sum(b["stake"] for b in bets)
    returned = sum(b["payout"] for b in bets)
    profit = returned - staked
    won = sum(b["result"] == "won" for b in bets)
    rows = [[html.escape(b["bet"]), b["kind"], b["side"].upper(), usd(b["stake"]), usd(b["payout"]),
             f'<b style="color:{GREEN if b["result"] == "won" else RED}">{b["result"].upper()}</b>',
             f'<span style="color:{GREEN if b["payout"] - b["stake"] >= 0 else RED}">{usd(b["payout"] - b["stake"], True)}</span>']
            for b in sorted(bets, key=lambda b: b["stake"] - b["payout"])]
    inner = (_tiles([("Net", usd(profit, True), GREEN if profit >= 0 else RED), ("Staked", usd(staked), None),
                     ("Returned", usd(returned), None), ("Won", f"{won} of {len(bets)}", None),
                     ("ROI", f"{100 * profit / staked:+.1f}%" if staked else "–", GREEN if profit >= 0 else RED)])
             + '<h3 style="margin:20px 0 8px">Every bet</h3>'
             + _table(["Bet", "Type", "Side", "Staked", "Paid", "Result", "Profit"], rows, (3, 4, 6))
             + f'<p style="font-size:13px;margin-top:16px">Kalshi cash now <b>{usd(balance)}</b> · '
               f'season profit <b style="color:{GREEN if season_profit >= 0 else RED}">{usd(season_profit, True)}</b></p>')
    subject = f"NFAI settled: {wave} {usd(profit, True)} ({won}/{len(bets)} won)"
    return subject, _page(f"Settlement report: {html.escape(wave)}", "All bets in this wave have settled", inner)


def send(subject, body_html):
    """Send through the Apps Script web app if its key is set up, else Gmail SMTP with the sender's
    app password. Raises on failure (callers catch)."""
    key = os.environ.get("NFAI_MAIL_KEY") or (HOOK_KEY_FILE.read_text().strip() if HOOK_KEY_FILE.exists() else None)
    if key:
        import requests
        r = requests.post(HOOK_URL, json={"key": key, "subject": subject, "html": body_html}, timeout=60)
        if r.text.strip() != "OK":
            raise RuntimeError(f"mail hook answered {r.status_code} {r.text[:80]!r}")
        return
    pw = os.environ.get("GMAIL_APP_PASSWORD") or (PASSWORD_FILE.read_text().strip() if PASSWORD_FILE.exists() else None)
    if not pw:
        raise RuntimeError("no Gmail app password configured")
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, f"NFAIstats <{FROM}>", TO
    msg.set_content("This report is HTML; open it in Gmail.")
    msg.add_alternative(body_html, subtype="html")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
        smtp.login(FROM, pw.replace(" ", ""))
        smtp.send_message(msg)


def reason(c):
    """One line on why a single was picked, from the candidate's numbers."""
    edge = f"edge {100 * c.ev:+.1f}¢"
    if c.group == "A" or (c.hit is None and c.p_book is not None):
        why = f"Books ({100 * c.p_book:.0f}%) and model ({100 * c.p_model:.0f}%) both above Kalshi's {100 * c.price:.0f}¢"
    elif c.p_book is not None:
        why = f"Model {100 * c.p_model:.0f}% vs Kalshi {100 * c.price:.0f}¢; books {100 * c.p_book:.0f}%, close enough"
    else:
        why = f"Model {100 * c.p_model:.0f}% vs Kalshi {100 * c.price:.0f}¢; no sportsbook line"
    if c.ev < 0:
        why += "; filler bet to use the full bankroll"
    return f"{why} ({edge})."
