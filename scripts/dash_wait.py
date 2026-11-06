"""Cloud dashboard helper: rebuild the Bet Tracker until its data changes, then print one word
for the session to act on, so the AI only wakes up to publish.

  CHANGED   dashboard/index.html has new data: publish it, then run this again
  FINAL     every game is over (page rebuilt): publish once more and stop
  NOBETS    no bets today: stop

Rebuilds every 2 minutes once a game is on, every 15 before. Usage: dash_wait.py [YYYY-MM-DD]"""

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


def signature():
    return hashlib.md5(re.sub(rb'"(generatedAt|updatedAt)": ?"[^"]*"', b"", PAGE.read_bytes())).hexdigest()


def main():
    day = sys.argv[1] if len(sys.argv) > 1 else str(dt.datetime.now(ZoneInfo("America/Los_Angeles")).date())
    db = sqlite3.connect(ROOT / "ledger" / "ledger.db")
    if not db.execute("SELECT 1 FROM bets WHERE substr(wave, 1, 10) = ? LIMIT 1", (day,)).fetchone():
        print("NOBETS")
        return
    while True:
        out = subprocess.run([sys.executable, str(ROOT / "scripts" / "dashboard.py"), day],
                             capture_output=True, text=True).stdout
        sig = signature()
        if "games post" in out:
            LAST.write_text(sig)
            print("FINAL")
            return
        if not LAST.exists() or LAST.read_text() != sig:
            LAST.write_text(sig)
            print("CHANGED")
            return
        time.sleep(120 if "games in" in out else 900)


if __name__ == "__main__":
    main()
