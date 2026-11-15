"""Print today's run times (Pacific) from ESPN's current kickoffs, for a backup scheduler outside
GitHub (GitHub's own cron did not fire on 2026-11-15). One line per run:
  HH:MM bets        start bets.yml (selftest=false); covers every wave betting within 60 minutes
  HH:MM dashboard   start dashboard.yml
  HH:MM settle      start bets.yml once more so settlement emails go out
Usage: today_runs.py [YYYY-MM-DD]"""

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_day as R  # noqa: E402
import schedule_watch as S  # noqa: E402


def main():
    day = dt.date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else dt.datetime.now(R.PT).date()
    waves = S.todays_waves(day)
    if not waves:
        print(f"no games still to be bet on {day}")
        return
    out, last = [], None
    for w in sorted(waves, key=lambda w: w["bet_at"]):
        t = w["bet_at"] - dt.timedelta(minutes=20)
        if last is None or w["bet_at"] - last > dt.timedelta(minutes=55):   # one run reaches waves within its hour
            out.append((t, "bets"))
            last = t
    first, end = min(w["bet_at"] for w in waves), max(w["kickoff"] for w in waves)
    t = first - dt.timedelta(minutes=5)
    while t < end + dt.timedelta(hours=4):
        out.append((t, "dashboard"))
        t += dt.timedelta(hours=5, minutes=10)
    out.append((end + dt.timedelta(hours=4, minutes=30), "settle"))
    for t, what in sorted(out):
        print(f"{t.astimezone(R.PT):%Y-%m-%d %H:%M} {what}")


if __name__ == "__main__":
    main()
