"""One hourly check, for the cloud scheduler: run every wave whose bet time falls before the next
check (waiting for it), then commit the ledger and logs back to GitHub. Exits in seconds when
nothing is due, so a quiet hour costs almost nothing.

Usage: cloud_tick.py [--live] [--window 70]   (dry run unless --live, or NFAI_LIVE=1)
Test: cloud_tick.py --date 2026-11-08 --now   (dry-runs that day's waves immediately)
The Sunday 10 AM wave tops up for ~25 minutes; each call stops after about 8 minutes and prints MORE
as its last line while that wave still has time left, so the caller runs it again."""

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
CALL_LIMIT = dt.timedelta(minutes=5)   # last top-up starts by then; the cloud kills a command at 10 min


def git_save(msg):
    """Commit the ledger, logs and dashboard state, rebasing over any code pushed meanwhile."""
    paths = ["ledger/ledger.db", "logs", "data/reasons.json", "data/dashboard_history.json", "data/emails_sent.json"]
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
    ap.add_argument("--date", type=dt.date.fromisoformat, help="test: another day's waves (dry run only)")
    ap.add_argument("--now", action="store_true", help="test: run the waves at once, no waiting")
    a = ap.parse_args()
    live = (a.live or os.environ.get("NFAI_LIVE") == "1") and not (a.date or a.now)
    now = dt.datetime.now(dt.timezone.utc)
    date = a.date or now.astimezone(R.PT).date()
    k = Kalshi()
    ledger = Ledger() if live else Ledger(ROOT / "ledger" / "dryrun.db")
    due = [w for w in R.make_waves(slate.games_on(date, k))
           if a.now or (w["bet_at"] <= now + dt.timedelta(minutes=a.window)
                        and w.get("top_up_until", w["kickoff"] - LATE_OK) > now
                        and not (ledger.wave(w["name"]) or {}).get("status") == "done")]
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
    more = False
    for w in due:
        wait = (w["bet_at"] - dt.datetime.now(dt.timezone.utc)).total_seconds()
        if wait > 0 and not a.now:
            log(f"sleeping {wait / 60:.0f} min until {w['name']}")
            time.sleep(wait)
        log(f"--- {w['name']} ---")
        try:
            if "top_up_until" in w and not a.now:
                more = not R.run_top_ups(w, k, ledger, str(date), live, log, stop_by=now + CALL_LIMIT)
            else:
                R.run_wave(w, k, ledger, str(date), live, log)
        except Exception as e:
            log(f"WAVE FAILED: {e!r}")
            more = "top_up_until" in w and dt.datetime.now(dt.timezone.utc) < w["top_up_until"]
        if live:
            git_save(f"Wave {w['name']}")
    if more:
        print("MORE")


if __name__ == "__main__":
    main()
