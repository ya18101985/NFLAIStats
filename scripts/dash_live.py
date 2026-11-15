"""Keep the public Bet Tracker page fresh from GitHub Actions (user's request, 2026-11-14: a
dashboard link that updates on its own, with no Claude session involved).

Every 2 minutes while a game is on (10 before the first kickoff) it takes the latest ledger from
main, rebuilds dashboard/index.html, and force-pushes the page's data as state.json to the
one-commit branch `dashboard-live`. The page itself is served by GitHub Pages from the `gh-pages`
branch (pushed once per run, so Pages rebuilds rarely) and polls state.json through the GitHub API.
Stops when every game is over and every bet settled, or after --minutes; then saves the reasons
cache and P&L history to main for the next run.

Usage: dash_live.py [YYYY-MM-DD] [--minutes 315]"""

import argparse
import datetime as dt
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "dashboard" / "index.html"
STATE = re.compile(r'<script id="state" type="application/json">(.*?)</script>', re.S)


def git(*args, stdin=None):
    r = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, input=stdin)
    return r.returncode, r.stdout.strip()


def pull_ledger():
    """Take the latest ledger from main, where the betting runs save it."""
    if git("fetch", "-q", "origin", "main")[0] == 0:
        git("checkout", "origin/main", "--", "ledger/ledger.db")


def save_state(day):
    """Commit the reasons cache and P&L history to main so the next run starts from them."""
    git("add", "data/reasons.json", "data/dashboard_history.json")
    if git("diff", "--cached", "--quiet")[0] == 0:
        return
    git("commit", "-qm", f"Dashboard state {day}")
    for _ in range(3):
        git("pull", "-q", "--rebase", "--autostash", "origin", "main")
        if git("push", "-q", "origin", "HEAD:main")[0] == 0:
            return
        time.sleep(5)
    print("PUSH FAILED")


def publish(branch, files):
    """Force-push a one-commit branch holding `files` ({name: text}); no checkout needed."""
    lines = []
    for name, text in files.items():
        _, blob = git("hash-object", "-w", "--stdin", stdin=text)
        lines.append(f"100644 blob {blob}\t{name}")
    _, tree = git("mktree", stdin="\n".join(lines) + "\n")
    _, commit = git("commit-tree", tree, "-m", f"{branch} {dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%M}Z")
    code, _ = git("push", "-q", "-f", "origin", f"{commit}:refs/heads/{branch}")
    return code == 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("day", nargs="?", default=str(dt.datetime.now(ZoneInfo("America/Los_Angeles")).date()))
    ap.add_argument("--minutes", type=float, default=315)
    a = ap.parse_args()
    stop_at, shell_done, last, built = time.time() + a.minutes * 60, False, None, False
    while True:
        pull_ledger()
        db = sqlite3.connect(ROOT / "ledger" / "ledger.db")
        has_bets = db.execute("SELECT 1 FROM bets WHERE substr(wave, 1, 10) = ? LIMIT 1", (a.day,)).fetchone()
        db.close()
        out, pause = "", 300
        if has_bets:
            r = subprocess.run([sys.executable, str(ROOT / "scripts" / "dashboard.py"), a.day], capture_output=True, text=True)
            out = r.stdout.strip()
            m = STATE.search(PAGE.read_text()) if r.returncode == 0 and PAGE.exists() else None
            if not m:
                print(f"build failed: {(r.stderr or out)[-300:]}", flush=True)
            else:
                built = True
                state = m.group(1).replace("<\\/", "</")
                sig = re.sub(r'"(generatedAt|updatedAt)": ?"[^"]*"', "", state)
                if not shell_done:   # the page itself: once per run
                    shell_done = publish("gh-pages", {"index.html": PAGE.read_text(), ".nojekyll": ""})
                if sig != last and publish("dashboard-live", {"state.json": state}):
                    last = sig
                print(f"{dt.datetime.now():%H:%M:%S} {out.split(' · ', 1)[-1]}", flush=True)
                live = re.search(r"(\d+) live", out)
                if "games post" in out and not (live and int(live.group(1))):
                    print("FINAL: every game over and every bet settled")
                    break
                pause = 120 if "games in" in out else 600
        else:
            print(f"{dt.datetime.now():%H:%M:%S} no bets yet for {a.day}", flush=True)
        if time.time() + pause > stop_at:
            break
        time.sleep(pause)
    if built:
        save_state(a.day)


if __name__ == "__main__":
    main()
