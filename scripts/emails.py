"""The two per-wave emails (user's request, 2026-11-08), built from the ledger and Kalshi:
placed(): right after a wave's bets are in (called by run_day).
settled(): once every filled bet of a wave has settled (dash_wait checks each rebuild).
Sent waves are listed in data/emails_sent.json, pushed to GitHub at once so a restarted cloud
session never sends the same report twice.

Usage: emails.py placed "<wave>" | emails.py settled YYYY-MM-DD   (--dry: print subject, don't send)"""

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from nfai import report  # noqa: E402
from nfai.kalshi import Kalshi  # noqa: E402

LEDGER = ROOT / "ledger" / "ledger.db"
SENT = ROOT / "data" / "emails_sent.json"


def f(x):
    return float(x or 0)


def _sent():
    return json.loads(SENT.read_text()) if SENT.exists() else {}


def _mark(key, push):
    s = _sent()
    s[key] = True
    SENT.write_text(json.dumps(s, indent=1))
    if push:
        run = lambda *a: subprocess.run(["git", *a], cwd=ROOT, capture_output=True).returncode  # noqa: E731
        run("add", "-f", str(SENT))
        if run("commit", "-qm", f"Email sent: {key}") == 0:
            for _ in range(3):
                run("pull", "-q", "--rebase", "--autostash", "origin", "main")
                if run("push", "-q", "origin", "HEAD:main") == 0:
                    break


def _rows(wave):
    db = sqlite3.connect(LEDGER)
    db.row_factory = sqlite3.Row
    rows = [dict(r) for r in db.execute("SELECT * FROM bets WHERE wave=? AND status NOT IN ('error', 'skipped') ORDER BY id",
                                        (wave,))]
    w = db.execute("SELECT bankroll FROM waves WHERE wave=?", (wave,)).fetchone()
    return rows, (w[0] if w else 0) or 0


def placed(wave, kickoff="", k=None, dry=False, push=False):
    key = f"placed:{wave}"
    if key in _sent():
        return None
    rows, bankroll = _rows(wave)
    if not rows:
        return None
    singles = [dict(bet=r["title"], side=r["outcome"], contracts=r["contracts"] or 0, price=r["limit_price"] or 0,
                    stake=r["stake"] or 0, p=r["p"] or 0, edge=r["ev"] or 0, reason=r["note"] or "")
               for r in rows if r["kind"] == "single"]
    parlays = []
    for r in rows:
        if r["kind"] != "parlay":
            continue
        forced = "forced" in (r["note"] or "")
        why = (f"Our joint chance {100 * (r['p'] or 0):.0f}% vs quote {100 * (r['limit_price'] or 0):.0f}¢"
               + ("; forced buy (best quote, above our target) since parlays are never skipped" if forced else ""))
        parlays.append(dict(legs=r["title"].split(" + "), price=r["limit_price"] or 0, stake=r["stake"] or 0,
                            p=r["p"] or 0, note=why))
    subject, body = report.placed_email(wave, kickoff, bankroll, singles, parlays)
    if dry:
        print(subject)
        return subject
    report.send(subject, body)
    _mark(key, push)
    return subject


def settled(day, k=None, dry=False, push=False, log=print):
    """Send the settlement email for every wave of `day` whose filled bets have all settled."""
    k = k or Kalshi()
    db = sqlite3.connect(LEDGER)
    waves = [r[0] for r in db.execute("SELECT DISTINCT wave FROM bets WHERE substr(wave, 1, 10)=? AND status "
                                      "NOT IN ('error', 'skipped')", (day,))]
    todo = [w for w in waves if f"settled:{w}" not in _sent()]
    if not todo:
        return []
    by_order = {}
    for x in k.fills():
        by_order.setdefault(x["order_id"], []).append(x)
    stl = {s["ticker"]: s for s in k.settlements()}
    out = []
    for wave in todo:
        rows, _ = _rows(wave)
        tickers = {}
        for r in rows:
            filled = r["kind"] == "parlay" or any(f(x["count_fp"]) > 0 for x in by_order.get(r["order_id"], []))
            if filled:
                tickers.setdefault(r["ticker"], r)
        if not all(t in stl for t in tickers):
            continue
        bets = []
        for t, r in tickers.items():
            s = stl[t]
            cost = f(s["yes_total_cost_dollars"]) + f(s["no_total_cost_dollars"]) + f(s["fee_cost"])
            paid = f(s.get("revenue")) / 100
            bets.append(dict(bet=r["title"], kind=r["kind"], side=r["outcome"], stake=cost, payout=paid,
                             result="won" if paid > 0 else "lost"))
        if not bets:
            _mark(f"settled:{wave}", push)   # nothing filled: nothing to report
            continue
        try:
            sys.path.insert(0, str(ROOT / "scripts"))
            import dashboard
            season = dashboard.pnl(k, [])["season"]["profit"]
        except Exception:
            season = 0.0
        subject, body = report.settled_email(wave, bets, float(k.balance()), season)
        if dry:
            print(subject)
        else:
            report.send(subject, body)
            _mark(f"settled:{wave}", push)
            log(f"emailed: {subject}")
        out.append(subject)
    return out


if __name__ == "__main__":
    dry = "--dry" in sys.argv
    args = [a for a in sys.argv[1:] if a != "--dry"]
    if args[0] == "placed":
        print(placed(args[1], dry=dry))
    else:
        print(settled(args[1], dry=dry))
