"""Daily safety net for NFL schedule changes (user's request, 2026-11-14): never miss a game day.

The betting runs start from cron lines written from the schedule as it stood when
scripts/plan_runs.py last ran. The NFL moves games (flex, weather, Saturday and holiday games), and
GitHub does not let a workflow rewrite its own schedule, so this script runs three times a day and
covers the gap itself: it reads today's real kickoffs from ESPN, forms the betting waves, and for
any wave that no scheduled run reaches in time it waits and starts the Bets workflow (20 and 8
minutes before the bet time) and the Dashboard workflow, then emails a note. Days that are already
covered cost a few seconds.

Usage: schedule_watch.py [--hours 5.6] [--dry]"""

import argparse
import datetime as dt
import os
import re
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import run_day as R  # noqa: E402
from nfai import report, slate  # noqa: E402

UTC = dt.timezone.utc
RUN_BEFORE = (dt.timedelta(minutes=20), dt.timedelta(minutes=8))
# a scheduled run reaches a wave if it starts 3-55 minutes before the bet time (each run looks 60 minutes ahead)
COVER_FROM, COVER_TO = dt.timedelta(minutes=55), dt.timedelta(minutes=3)


def cron_times(workflow, around):
    """Datetimes (UTC) of a workflow's cron lines on the days next to `around`."""
    out = []
    for m, h, days, mo in re.findall(r'- cron: "(\d+) (\d+) ([\d,]+) (\d+) \*"', (ROOT / ".github/workflows" / workflow).read_text()):
        for d in days.split(","):
            for year in (around.year - 1, around.year, around.year + 1):
                try:
                    t = dt.datetime(year, int(mo), int(d), int(h), int(m), tzinfo=UTC)
                except ValueError:
                    continue
                if abs(t - around) < dt.timedelta(days=2):
                    out.append(t)
    return sorted(out)


def todays_waves(day):
    """Betting waves for `day` (Pacific date) from ESPN's current kickoff times."""
    evs = requests.get(f"{slate.ESPN}/scoreboard", params={"dates": day.strftime("%Y%m%d")}, timeout=30).json()["events"]
    games = []
    for e in evs:
        kick = dt.datetime.fromisoformat(e["date"].replace("Z", "+00:00"))
        if e["status"]["type"].get("state") == "pre" and kick.astimezone(R.PT).date() == day:
            games.append(SimpleNamespace(kickoff=kick, code=f"{kick:%y%b%d}".upper() + e.get("shortName", "").replace(" @ ", "").replace(" ", "")))
    return R.make_waves(games)


def unrecorded_waves(now):
    """Waves that kicked off since the previous watch run with no record in the ledger: a betting
    run never reached them (GitHub skipped it, or the run found nothing). [(name, kickoff)]."""
    import sqlite3
    back = dt.timedelta(hours=13.5 if now.hour < 14 else 6)
    days = {(now - back).astimezone(R.PT).date(), now.astimezone(R.PT).date()}
    games = []
    for day in days:
        evs = requests.get(f"{slate.ESPN}/scoreboard", params={"dates": day.strftime("%Y%m%d")}, timeout=30).json()["events"]
        for e in evs:
            kick = dt.datetime.fromisoformat(e["date"].replace("Z", "+00:00"))
            if e["status"]["type"].get("state") in ("in", "post") and now - back < kick <= now and e["id"] not in {g.id for g in games}:
                games.append(SimpleNamespace(kickoff=kick, code="X" * 7 + e.get("shortName", "").replace(" ", ""), id=e["id"]))
    db = sqlite3.connect(ROOT / "ledger" / "ledger.db")
    out = []
    for w in R.make_waves(games):
        prefix = f"{w['kickoff'].astimezone(R.PT):%Y-%m-%d %H%M}"
        if not db.execute("SELECT 1 FROM waves WHERE wave LIKE ?", (prefix + "%",)).fetchone():
            out.append((f"{prefix} {' + '.join(g.code[7:] for g in w['games'])}", w["kickoff"]))
    return out


def dispatch(workflow, inputs, dry):
    if dry:
        print(f"  (dry) would start {workflow} {inputs}")
        return
    r = requests.post(f"https://api.github.com/repos/{os.environ['GITHUB_REPOSITORY']}/actions/workflows/{workflow}/dispatches",
                      headers={"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}", "Accept": "application/vnd.github+json"},
                      json={"ref": "main", "inputs": inputs}, timeout=30)
    print(f"  started {workflow}: HTTP {r.status_code}", flush=True)
    if r.status_code >= 300:
        raise RuntimeError(f"could not start {workflow}: HTTP {r.status_code} {r.text[:200]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=5.6, help="how far ahead this run is responsible for")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--test-start", action="store_true", help="only check that this job may start the Bets workflow")
    a = ap.parse_args()
    if a.test_start:
        return dispatch("bets.yml", {"selftest": "false"}, a.dry)
    now = dt.datetime.now(UTC)
    end = now + dt.timedelta(hours=a.hours)
    day = now.astimezone(R.PT).date()
    try:
        lost = unrecorded_waves(now)
    except Exception as e:
        lost = []
        print(f"could not check for missed waves: {e!r}")
    if lost:
        print("MISSED: " + "; ".join(n for n, _ in lost))
        if not a.dry:
            try:
                report.send("NFAI alert: a betting wave was missed",
                            "<p>These games kicked off with no betting run on record, so no bets were placed on them:</p><ul>"
                            + "".join(f"<li>{n}</li>" for n, _ in lost) + "</ul><p>Ask Claude to check the Actions runs.</p>")
            except Exception as e:
                print(f"alert email failed: {e!r}")
    waves = todays_waves(day)
    crons = cron_times("bets.yml", now)
    missed = [w for w in waves if now < w["bet_at"] <= end + RUN_BEFORE[0]
              and not any(w["bet_at"] - COVER_FROM <= t <= w["bet_at"] - COVER_TO for t in crons)]
    print(f"{day}: {len(waves)} waves still to bet, {len(missed)} without a scheduled run in the next {a.hours:g} h")
    if not missed:
        return
    names = [f"{w['name']} (bets at {w['bet_at'].astimezone(R.PT):%-I:%M %p} PT)" for w in missed]
    try:
        if not a.dry:
            report.send("NFAI: game time changed, covered automatically",
                        "<p>Today's NFL schedule differs from the planned betting runs. These waves had no scheduled run, "
                        "so the schedule watch is starting them itself:</p><ul>" + "".join(f"<li>{n}</li>" for n in names)
                        + "</ul><p>To put them on the regular schedule, ask Claude to rerun plan_runs.py.</p>")
    except Exception as e:
        print(f"note email failed: {e!r}")
    starts = sorted({(w["bet_at"] - b, w["name"]) for w in missed for b in RUN_BEFORE})
    dash_started = False
    for t, name in starts:
        wait = (t - dt.datetime.now(UTC)).total_seconds()
        print(f"{name}: betting run due {t.astimezone(R.PT):%-I:%M %p} PT", flush=True)
        if wait > 0 and not a.dry:
            time.sleep(wait)
        if not dash_started and not any(abs((c - t).total_seconds()) < 3600 for c in cron_times("dashboard.yml", now)):
            dispatch("dashboard.yml", {}, a.dry)
        dash_started = True
        dispatch("bets.yml", {"selftest": "false"}, a.dry)


if __name__ == "__main__":
    main()
