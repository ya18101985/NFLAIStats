"""One hourly check, for the cloud scheduler: run every wave whose bet time falls before the next
check (waiting for it), then commit the ledger and logs back to GitHub. Exits in seconds when
nothing is due, so a quiet hour costs almost nothing.

Usage: cloud_tick.py [--live] [--window 70]   (dry run unless --live, or NFAI_LIVE=1)"""

import argparse
import datetime as dt
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from nfai import data as D, slate  # noqa: E402
from nfai.execute import Ledger  # noqa: E402
from nfai.kalshi import Kalshi  # noqa: E402
import run_day as R  # noqa: E402

LATE_OK = dt.timedelta(minutes=10)   # a late check still bets if kickoff is at least this far off


def git_save(msg):
    """Commit the ledger, logs and dashboard state, rebasing over any code pushed meanwhile."""
    paths = ["ledger/ledger.db", "logs", "data/reasons.json", "data/dashboard_history.json"]
    subprocess.run(["git", "add", "-A", *paths], cwd=ROOT, check=False)
    if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ROOT).returncode == 0:
        return
    subprocess.run(["git", "commit", "-qm", msg], cwd=ROOT, check=True)
    for _ in range(3):
        subprocess.run(["git", "pull", "-q", "--rebase", "origin", "main"], cwd=ROOT, check=False)
        if subprocess.run(["git", "push", "-q", "origin", "HEAD:main"], cwd=ROOT).returncode == 0:
            return
        time.sleep(5)
    print("PUSH FAILED: ledger changes are only in this cloud session")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--window", type=int, default=70, help="minutes ahead to look for bet times")
    a = ap.parse_args()
    live = a.live or os.environ.get("NFAI_LIVE") == "1"
    now = dt.datetime.now(dt.timezone.utc)
    date = now.astimezone(R.PT).date()
    k = Kalshi()
    ledger = Ledger() if live else Ledger(ROOT / "ledger" / "dryrun.db")
    due = [w for w in R.make_waves(slate.games_on(date, k))
           if w["bet_at"] <= now + dt.timedelta(minutes=a.window) and w["kickoff"] - LATE_OK > now
           and not (ledger.wave(w["name"]) or {}).get("status") == "done"]
    if not due:
        print(f"nothing due ({date})")
        return
    R.LOGS.mkdir(exist_ok=True)
    logf = open(R.LOGS / f"{date}.log", "a")

    def log(msg):
        line = f"{dt.datetime.now(R.PT):%H:%M:%S} {msg}"
        print(line, flush=True)
        logf.write(line + "\n")
        logf.flush()

    log(f"=== cloud {'LIVE' if live else 'DRY RUN'} {date}: {', '.join(w['name'] for w in due)} ===")
    D.refresh(names=["stats", "pbp", "snaps"])
    for w in due:
        wait = (w["bet_at"] - dt.datetime.now(dt.timezone.utc)).total_seconds()
        if wait > 0:
            log(f"sleeping {wait / 60:.0f} min until {w['name']}")
            time.sleep(wait)
        log(f"--- {w['name']} ---")
        try:
            R.run_wave(w, k, ledger, str(date), live, log)
        except Exception as e:
            log(f"WAVE FAILED: {e!r}")
        if live:
            git_save(f"Wave {w['name']}")


if __name__ == "__main__":
    main()
