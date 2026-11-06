"""Cloud dashboard helper: rebuild the Bet Tracker until its data changes, then print one word
for the session to act on, so the AI only wakes up to publish.

  CHANGED   dashboard/index.html has new data: publish it, then run this again
  WAIT      nothing changed for 8 minutes (keeps each call under the cloud's 10-minute limit): run again
  FINAL     every game is over (page rebuilt, dashboard state saved to GitHub): publish once more and stop
  NOBETS    no bets today: stop

Rebuilds every 2 minutes once a game is on, every 15 before. With --pull (cloud), each rebuild first
takes the latest ledger from GitHub, where the betting runs save it.
Usage: dash_wait.py [YYYY-MM-DD] [--pull]"""

import datetime as dt
import hashlib
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "dashboard" / "index.html"
LAST = ROOT / "dashboard" / ".last_published"   # signature of the last page the session published
MAX_WAIT = 8 * 60


def signature():
    return hashlib.md5(re.sub(rb'"(generatedAt|updatedAt)": ?"[^"]*"', b"", PAGE.read_bytes())).hexdigest()


def git(*args):
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True).returncode


def pull_ledger():
    if git("fetch", "-q", "origin", "main") == 0:
        git("checkout", "origin/main", "--", "ledger/ledger.db")


def save_state(day):
    """Commit the reasons cache and P&L history so the next day's dashboard starts from them."""
    paths = ["data/reasons.json", "data/dashboard_history.json"]
    git("add", *paths)
    if git("diff", "--cached", "--quiet") == 0:
        return
    git("commit", "-qm", f"Dashboard state {day}")
    for _ in range(3):
        git("pull", "-q", "--rebase", "--autostash", "origin", "main")
        if git("push", "-q", "origin", "HEAD:main") == 0:
            return
        time.sleep(5)
    print("PUSH FAILED")


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    pull = "--pull" in sys.argv
    day = args[0] if args else str(dt.datetime.now(ZoneInfo("America/Los_Angeles")).date())
    start = time.time()
    while True:
        if pull:
            pull_ledger()
        db = sqlite3.connect(ROOT / "ledger" / "ledger.db")
        has_bets = db.execute("SELECT 1 FROM bets WHERE substr(wave, 1, 10) = ? LIMIT 1", (day,)).fetchone()
        db.close()
        if not has_bets:
            print("NOBETS")
            return
        out = subprocess.run([sys.executable, str(ROOT / "scripts" / "dashboard.py"), day],
                             capture_output=True, text=True).stdout
        sig = signature()
        if "games post" in out:
            LAST.write_text(sig)
            if pull:
                save_state(day)
            print("FINAL")
            return
        if not LAST.exists() or LAST.read_text() != sig:
            LAST.write_text(sig)
            print("CHANGED")
            return
        left = MAX_WAIT - (time.time() - start)
        if left < 60:
            print("WAIT")
            return
        time.sleep(min(120 if "games in" in out else 900, left))


if __name__ == "__main__":
    main()
