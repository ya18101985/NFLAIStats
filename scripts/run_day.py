"""Run a game day: wait for each wave's bet time, size its bankroll by the user's rules, pick bets,
and place them. Dry run unless --live.

    caffeinate -i .venv/bin/python scripts/run_day.py 2026-11-01 2026-11-02 --live

Wave bankroll rules (agreed 2026-10-30), bankroll always capped at $5,000:
  Europe (before 9 AM PT) / night (after 3 PM PT) / any non-Sunday game: 100% of cash.
  Sunday 10 AM PT wave: 80% of cash; the other 20% is reserved for the first afternoon wave.
  First Sunday afternoon wave (1:05): the reserve + 50% of what the 10 AM wave has paid back so far.
  Later afternoon waves (1:25): all cash.
Safety stop: skip a wave if cash + open positions is below 40% of the starting balance.
Kill switch: create ~/.kalshi/STOP to skip all further waves.
"""

import argparse
import datetime as dt
import subprocess
import sys
import time
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nfai import data as D, gamelines, odds, picks as P, slate  # noqa: E402
from nfai.execute import Ledger, place_parlay, place_singles  # noqa: E402
from nfai.kalshi import KEY_DIR, Kalshi  # noqa: E402
from nfai.model import Model  # noqa: E402

PT = ZoneInfo("America/Los_Angeles")
MAX_BANKROLL = 5000.0
SAFETY_FLOOR = 0.40
LOGS = Path(__file__).resolve().parents[1] / "logs"


def notify(msg):
    subprocess.run(["osascript", "-e", f'display notification "{msg}" with title "NFAIstats"'], check=False)


def make_waves(games):
    """Group games by kickoff and decide each wave's bet time and bankroll rule."""
    by_kick = {}
    for g in games:
        by_kick.setdefault(g.kickoff, []).append(g)
    waves, afternoon_seen = [], 0
    for kick in sorted(by_kick):
        local = kick.astimezone(PT)
        sunday = local.weekday() == 6
        if sunday and 9 <= local.hour < 12:
            rule, lead = "sun_early", dt.timedelta(minutes=90)
        elif sunday and 12 <= local.hour < 15:
            rule = "sun_afternoon_first" if afternoon_seen == 0 else "sun_afternoon_rest"
            lead = dt.timedelta(minutes=15 if afternoon_seen == 0 else 10)
            afternoon_seen += 1
        else:
            rule, lead = "all_cash", dt.timedelta(minutes=35 if sunday else 60)
        name = f"{local:%Y-%m-%d %H%M} {'+'.join(g.code[7:] for g in by_kick[kick])}"
        waves.append(dict(name=name, kickoff=kick, bet_at=kick - lead, rule=rule, games=by_kick[kick]))
    return waves


def wave_bankroll(w, cash, ledger, day):
    early = ledger.wave(ledger.get(f"{day}:early_wave", "")) if ledger.get(f"{day}:early_wave") else None
    if w["rule"] == "sun_early":
        b = min(cash, MAX_BANKROLL)
        ledger.set(f"{day}:reserve", 0.2 * b)
        return 0.8 * b
    if w["rule"] == "sun_afternoon_first":
        reserve = float(ledger.get(f"{day}:reserve", 0))
        paid_back = max(0.0, cash - (early["cash_after"] if early and early["cash_after"] is not None else cash))
        return min(cash, reserve + 0.5 * paid_back, MAX_BANKROLL)
    return min(cash, MAX_BANKROLL)


def book_lines(g, log):
    """Sportsbook prop lines for a game; the wave goes ahead on model + Kalshi alone if they fail."""
    if not odds.available():
        return {}
    try:
        return odds.game_lines(g.away, g.home)
    except Exception as e:
        log(f"  sportsbook lines unavailable for {g.code}: {e!r}")
        return {}


def game_line_candidates(g, k, log):
    """Spreads/totals/winners for the game, halves and quarters, priced against the books."""
    if not odds.available():
        return []
    try:
        return gamelines.candidates(g, k, P.MAX_SPREAD)
    except Exception as e:
        log(f"  game lines unavailable for {g.code}: {e!r}")
        return []


def place_parlays(k, ledger, w, parlays, budget, live, log):
    """Buy up to PARLAYS_PER_WAVE parlays sharing budget, working down the candidate list when a
    quote isn't good enough. Returns dollars spent."""
    spent, bought = 0.0, 0
    for i, pl in enumerate(parlays):
        if bought == P.PARLAYS_PER_WAVE or budget - spent < 5:
            break
        stake = (budget - spent) / min(P.PARLAYS_PER_WAVE - bought, len(parlays) - i)
        if live:
            got = place_parlay(k, ledger, w["name"], pl, stake, log=log)
        else:
            got = stake
            log(f"  [dry run] parlay p={pl['p']:.3f} ${stake:,.0f}: " + " + ".join(f"{c.title} {c.outcome}" for c in pl["legs"]))
        spent += got
        bought += got > 0
    return spent


def run_wave(w, k, ledger, day, live, log):
    if (KEY_DIR / "STOP").exists():
        log("STOP file present; skipping wave")
        return
    if ledger.wave(w["name"]) and ledger.wave(w["name"])["status"] == "done":
        log("already done")
        return
    bal = k._request("GET", "/portfolio/balance")
    cash = float(Decimal(bal["balance_dollars"]))
    total = cash + bal.get("portfolio_value", 0) / 100
    start = float(ledger.get("start_balance") or total)
    if ledger.get("start_balance") is None and live:
        ledger.set("start_balance", total)
    if total < SAFETY_FLOOR * start:
        msg = f"Safety stop: account ${total:,.0f} is below 40% of start ${start:,.0f}. Skipping {w['name']}."
        log(msg)
        notify(msg)
        ledger.record_wave(w["name"], status="safety_stop", cash_before=cash)
        return
    bankroll = wave_bankroll(w, cash, ledger, day)
    log(f"cash ${cash:,.2f}, account ${total:,.2f}, wave bankroll ${bankroll:,.2f} ({w['rule']})")
    ledger.record_wave(w["name"], started_at=dt.datetime.now(dt.timezone.utc).isoformat(), cash_before=cash,
                       bankroll=bankroll, status="running")
    if bankroll < 20:
        log("bankroll too small; nothing to do")
        ledger.record_wave(w["name"], status="done", cash_after=cash)
        return

    slate.refresh_injuries(w["games"])
    D.refresh(names=["depth", "injuries"])
    model = Model(week=max((g.week for g in w["games"] if g.week), default=None))
    cands = []
    for g in w["games"]:
        c, flags = P.game_candidates(model, g, slate.prop_markets(g, k), book_lines(g, log))
        cands += c + game_line_candidates(g, k, log)
        for f in flags:
            log(f"  skip {g.code}: {f}")
    one_game = len(w["games"]) == 1
    parlays = P.parlay_candidates(cands, k=2 * P.PARLAYS_PER_WAVE)   # spares, in case quotes are poor
    k.max_order_cost = Decimal(str(round(P.MAX_STAKE * bankroll * 1.02 + 1, 2)))   # singles only; parlays are RFQs
    k.live = live
    if one_game:
        # few singles exist in one game: singles first (filler allowed down to 1c below break-even),
        # everything left to parlays
        singles = P.select_singles(cands, bankroll, budget=None if parlays else bankroll, one_game=True)
        singles_cost = sum(n * (c.price + P.fee(c.price)) for c, n, _ in singles)
        spent = place_parlays(k, ledger, w, parlays, bankroll - singles_cost, live, log)
    else:
        # parlays first, so whatever they don't spend goes to singles and the whole bankroll is used
        spent = place_parlays(k, ledger, w, parlays, (1 - P.SINGLES_SHARE) * bankroll, live, log)
        singles = P.select_singles(cands, bankroll, budget=bankroll - spent)
    log(f"{len(cands)} priced sides ({sum(c.p_book is not None for c in cands)} with sportsbook lines) -> "
        f"{len(singles)} singles (${sum(s for *_, s in singles):,.0f}), parlays ${spent:,.0f}")
    place_singles(k, ledger, w["name"], singles, w["kickoff"], log=log)
    time.sleep(3)
    cash_after = float(k.balance())
    ledger.record_wave(w["name"], status="done", cash_after=cash_after)
    if w["rule"] == "sun_early":
        ledger.set(f"{day}:early_wave", w["name"])
    msg = f"{w['name']}: placed {len(singles)} singles; cash now ${cash_after:,.0f}"
    log(msg)
    notify(msg)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dates", nargs="+", type=dt.date.fromisoformat, help="one or more game days, run in order")
    ap.add_argument("--live", action="store_true", help="really place orders")
    ap.add_argument("--now", action="store_true", help="run every wave immediately (for testing)")
    a = ap.parse_args()
    LOGS.mkdir(exist_ok=True)
    ledger = Ledger() if a.live else Ledger(Path(__file__).resolve().parents[1] / "ledger" / "dryrun.db")
    k = Kalshi()
    for date in a.dates:
        run_date(date, k, ledger, a.live, a.now)


def run_date(date, k, ledger, live, now):
    logf = open(LOGS / f"{date}.log", "a")

    def log(msg):
        line = f"{dt.datetime.now(PT):%H:%M:%S} {msg}"
        print(line, flush=True)
        logf.write(line + "\n")
        logf.flush()

    day = str(date)
    log(f"=== {'LIVE' if live else 'DRY RUN'} {date} ===")
    D.refresh(names=["stats", "pbp", "snaps"])
    waves = make_waves(slate.games_on(date, k))
    for w in waves:
        log(f"wave {w['name']}: bets at {w['bet_at'].astimezone(PT):%a %I:%M %p} PT, rule {w['rule']}")
    for w in waves:
        if not now:
            wait = (w["bet_at"] - dt.datetime.now(dt.timezone.utc)).total_seconds()
            if wait < -15 * 60:
                log(f"missed {w['name']} (bet time passed); skipping")
                continue
            if wait > 0:
                log(f"sleeping {wait / 60:.0f} min until {w['name']}")
                time.sleep(wait)
        log(f"--- {w['name']} ---")
        try:
            run_wave(w, k, ledger, day, live, log)
        except Exception as e:
            log(f"WAVE FAILED: {e!r}")
            notify(f"{w['name']} failed: {e}")
    log("=== day complete ===")


if __name__ == "__main__":
    main()
