"""Write the betting workflow's run times from the NFL schedule (user's request, 2026-11-12):
GitHub Actions runs only on game days, shortly before each wave's bet time, never on a blind timer.

For every game day it takes the real kickoffs from the nflverse schedule plus the standard slots
that day can still move into (Sunday flex games; Week 18 and playoff times, set late in the season),
turns them into waves with run_day.make_waves, and schedules two runs per wave: 20 and 8 minutes
before its bet time. Each run calls cloud_tick.py with a 45-minute window, so a GitHub run that
starts late, or a game whose time moved, is still covered; a run with nothing due exits in seconds.

Usage: plan_runs.py [--from YYYY-MM-DD]   rewrites the schedule block in .github/workflows/bets.yml
"""

import argparse
import datetime as dt
import re
import sys
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from nfai import data as D  # noqa: E402
import run_day as R  # noqa: E402

ET, PT, UTC = ZoneInfo("America/New_York"), ZoneInfo("America/Los_Angeles"), dt.timezone.utc
WORKFLOW = ROOT / ".github" / "workflows" / "bets.yml"
RUN_BEFORE = (dt.timedelta(minutes=20), dt.timedelta(minutes=8))

# Kickoff slots (Pacific) a day can still have even if the schedule file doesn't show them yet.
SUNDAY_SLOTS = ["10:00", "13:05", "13:25", "17:20"]                 # flex moves games between these
LATE_SEASON = {                                                      # Week 18: times set the week before
    "2027-01-09": ["13:30", "17:00"], "2027-01-10": ["10:00", "13:25", "17:20"]}
PLAYOFFS = {                                                         # matchups and exact times set late
    "2027-01-16": ["13:30", "17:15"], "2027-01-17": ["10:00", "13:30", "17:15"], "2027-01-18": ["17:15"],
    "2027-01-23": ["13:30", "17:15"], "2027-01-24": ["12:00", "15:30"],
    "2027-01-31": ["12:00", "15:30"], "2027-02-14": ["15:30"]}


def kickoffs(start):
    """{date: set of kickoff datetimes (PT)} for every game day from `start` to the Super Bowl."""
    s = D.schedule()
    s = s[(s.season == D.SEASON) & (s.gameday >= str(start))]
    days = defaultdict(set)
    for _, r in s.iterrows():
        k = dt.datetime.fromisoformat(f"{r.gameday} {r.gametime}").replace(tzinfo=ET).astimezone(PT)
        days[k.date()].add(k)
    extra = {d: v for d, v in {**LATE_SEASON, **PLAYOFFS}.items() if d >= str(start)}
    for d in list(days):
        if d.weekday() == 6:
            extra.setdefault(str(d), []).extend(SUNDAY_SLOTS)
    for d, slots in extra.items():
        day = dt.date.fromisoformat(d)
        for hm in slots:
            days[day].add(dt.datetime.combine(day, dt.time.fromisoformat(hm), PT))
    return days


def run_times(start):
    out = set()
    for day, kicks in sorted(kickoffs(start).items()):
        games = [SimpleNamespace(kickoff=k, code=f"{k:%y%b%d}SLOT{i}".upper()) for i, k in enumerate(sorted(kicks))]
        for w in R.make_waves(games):
            for before in RUN_BEFORE:
                t = (w["bet_at"] - before).astimezone(UTC).replace(second=0, microsecond=0)
                if t.date() >= start:
                    out.add(t)
    return sorted(out)


def cron_lines(times):
    """Group run times into as few cron lines as possible: one per (month, hour, minute)."""
    groups = defaultdict(set)
    for t in times:
        groups[(t.month, t.hour, t.minute)].add(t.day)
    return [f'    - cron: "{m} {h} {",".join(str(d) for d in sorted(days))} {mo} *"'
            for (mo, h, m), days in sorted(groups.items(), key=lambda x: (x[0][0] < 8, x[0]))]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="start", type=dt.date.fromisoformat, default=dt.datetime.now(PT).date())
    a = ap.parse_args()
    times = run_times(a.start)
    lines = cron_lines(times)
    text = WORKFLOW.read_text()
    block = "\n".join(lines)
    new = re.sub(r"(  schedule:\n)(?:    - cron: .*\n)+", lambda m: m.group(1) + block + "\n", text, count=1)
    WORKFLOW.write_text(new)
    days = sorted({t.astimezone(PT).date() for t in times})
    print(f"{len(times)} runs on {len(days)} game days ({days[0]} to {days[-1]}), {len(lines)} cron lines")


if __name__ == "__main__":
    main()
