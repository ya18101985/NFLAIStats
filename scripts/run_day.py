"""Run a game day: wait for each wave's bet time, size its bankroll by the user's rules, pick bets,
and place them. Dry run unless --live.

    caffeinate -i .venv/bin/python scripts/run_day.py 2026-11-01 2026-11-02 --live

Wave bankroll rules (user's choice, 2026-11-08), bankroll always capped at $5,000; "cash" is free
cash, i.e. the balance less what resting orders would still cost:
  Europe (before 9 AM PT) / night (after 3 PM PT) / any non-Sunday game: 100% of cash.
  Sunday 10 AM PT wave: 80% of cash. It bets from 15 min before kickoff and keeps topping up as Europe
  payouts arrive, until the Europe bets have all settled or 10 minutes after kickoff (at live prices).
  Sunday afternoon waves (1:05, 1:25): all cash.
Every wave puts 20% into 2-3 leg parlays first; what they can't spend goes to singles.
No balance floor (user's choice, 2026-11-08): every wave spends all it is allowed, down to $0.
Kill switch: create ~/.kalshi/STOP, or a file named STOP at the repo root (editable from GitHub on
a phone), to skip all further waves.
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
TOP_UP_AFTER_KICKOFF = dt.timedelta(minutes=10)
LOGS = Path(__file__).resolve().parents[1] / "logs"


def notify(msg):
    try:   # macOS only; the cloud has no notifications
        subprocess.run(["osascript", "-e", f'display notification "{msg}" with title "NFAIstats"'], check=False)
    except FileNotFoundError:
        pass


def stopped():
    return (KEY_DIR / "STOP").exists() or (LOGS.parent / "STOP").exists()


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
            rule, lead = "sun_early", dt.timedelta(minutes=15)
        elif sunday and 12 <= local.hour < 15:
            rule = "sun_afternoon_first" if afternoon_seen == 0 else "sun_afternoon_rest"
            lead = dt.timedelta(minutes=15 if afternoon_seen == 0 else 10)
            afternoon_seen += 1
        else:
            rule, lead = "all_cash", dt.timedelta(minutes=35 if sunday else 60)
        name = f"{local:%Y-%m-%d %H%M} {'+'.join(g.code[7:] for g in by_kick[kick])}"
        w = dict(name=name, kickoff=kick, bet_at=kick - lead, rule=rule, games=by_kick[kick])
        if rule == "sun_early":
            w["top_up_until"] = kick + TOP_UP_AFTER_KICKOFF
            w["earlier_games"] = [g.code for k2, gs in by_kick.items() if k2 < kick for g in gs]
        waves.append(w)
    return waves


def free_cash(k):
    """Balance less the cost of resting orders (Kalshi doesn't hold cash for them)."""
    cash = float(k.balance())
    for o in k.orders(status="resting"):
        side = o.get("outcome_side") or o.get("side")
        cash -= float(o.get("remaining_count_fp") or 0) * float(o.get(f"{side}_price_dollars") or 0)
    return cash


def committed(ledger, wave, k=None):
    """Dollars this wave has already bet: parlays bought, plus singles filled or still resting (from
    Kalshi when k is given, so expired unfilled orders don't count; else the stakes placed)."""
    q = "SELECT SUM(stake) FROM bets WHERE wave=? AND status NOT IN ('error', 'skipped')"
    if k is None:
        return float(ledger.db.execute(q, (wave,)).fetchone()[0] or 0)
    total = float(ledger.db.execute(q + " AND kind='parlay'", (wave,)).fetchone()[0] or 0)
    ids = {r[0] for r in ledger.db.execute("SELECT client_order_id FROM bets WHERE wave=? AND kind='single'", (wave,))}
    if ids:
        for o in k.orders(min_ts=int(dt.datetime.now(dt.timezone.utc).timestamp()) - 6 * 3600):
            if o.get("client_order_id") in ids:
                side = o.get("outcome_side") or o.get("side")
                total += float(o.get("taker_fill_cost_dollars") or 0) + float(o.get("maker_fill_cost_dollars") or 0)
                if o.get("status") == "resting":
                    total += float(o.get("remaining_count_fp") or 0) * float(o.get(f"{side}_price_dollars") or 0)
    return total


def wave_bankroll(w, cash, ledger, k):
    if w["rule"] == "sun_early":
        spent = committed(ledger, w["name"], k if k.live else None)
        return max(0.0, min(0.8 * (cash + spent), MAX_BANKROLL) - spent)
    return min(cash, MAX_BANKROLL)


def earlier_open(k, w):
    """True while earlier games' bets (e.g. Europe) still hold unsettled positions."""
    codes = w.get("earlier_games") or []
    for p in k.positions():
        pos = float(p.get("position_fp") or p.get("position") or 0)
        if pos and any(c in p.get("ticker", "") for c in codes):
            return True
    return False


def email_placed(w, live, log):
    """Email 1 (bets placed). Never lets an email problem touch the betting."""
    if not live:
        return
    try:
        import emails
        subject = emails.placed(w["name"], kickoff=f"{w['kickoff'].astimezone(PT):%a %-I:%M %p} PT")
        if subject:
            log(f"emailed: {subject}")
    except Exception as e:
        log(f"email FAILED: {e!r}")


def run_top_ups(w, k, ledger, day, live, log, stop_by=None):
    """10 AM wave: bet now, then keep betting newly arrived cash every 90 s until the earlier
    games have settled or top_up_until; stop_by caps one call (the cloud's 10-minute limit).
    Returns True when the wave is finished."""
    while True:
        settled = not earlier_open(k, w)
        run_wave(w, k, ledger, day, live, log)
        now = dt.datetime.now(dt.timezone.utc)
        if settled or now >= w["top_up_until"] or stopped():
            ledger.record_wave(w["name"], status="done", cash_after=float(k.balance()))
            log(f"{w['name']}: top-ups finished ({'earlier games settled' if settled else 'time up'}); "
                f"${committed(ledger, w['name'], k if live else None):,.0f} bet in all")
            email_placed(w, live, log)
            return True
        if stop_by and now + dt.timedelta(seconds=90) >= stop_by:
            return False
        time.sleep(90)


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
    if stopped():
        log("STOP file present; skipping wave")
        return
    if ledger.wave(w["name"]) and ledger.wave(w["name"])["status"] == "done":
        log("already done")
        return
    bal = k._request("GET", "/portfolio/balance")
    cash = free_cash(k)
    total = float(Decimal(bal["balance_dollars"])) + bal.get("portfolio_value", 0) / 100
    k.live = live
    bankroll = wave_bankroll(w, cash, ledger, k)
    top_up = "top_up_until" in w
    log(f"cash ${cash:,.2f}, account ${total:,.2f}, wave bankroll ${bankroll:,.2f} ({w['rule']})")
    first = ledger.wave(w["name"]) is None
    ledger.record_wave(w["name"], **(dict(started_at=dt.datetime.now(dt.timezone.utc).isoformat(), cash_before=cash)
                                     if first else {}),
                       bankroll=bankroll + committed(ledger, w["name"]), status="running")
    if bankroll < 20:
        log("bankroll too small; nothing to do")
        if not top_up:
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
    parlays = P.parlay_candidates(cands, k=2 * P.PARLAYS_PER_WAVE)   # spares, in case no quote comes
    k.max_order_cost = Decimal(str(round(P.MAX_STAKE * bankroll * 1.02 + 1, 2)))   # singles only; parlays are RFQs
    k.live = live
    # parlays first, so whatever they don't spend goes to singles and the whole bankroll is used
    spent = place_parlays(k, ledger, w, parlays, P.PARLAY_SHARE * bankroll, live, log)
    singles = P.select_singles(cands, bankroll, budget=bankroll - spent, one_game=len(w["games"]) == 1)
    log(f"{len(cands)} priced sides ({sum(c.p_book is not None for c in cands)} with sportsbook lines) -> "
        f"{len(singles)} singles (${sum(s for *_, s in singles):,.0f}), parlays ${spent:,.0f}")
    place_singles(k, ledger, w["name"], singles, w["kickoff"], log=log)
    time.sleep(3)
    cash_after = float(k.balance())
    ledger.record_wave(w["name"], status="running" if top_up else "done", cash_after=cash_after)
    msg = f"{w['name']}: placed {len(singles)} singles; cash now ${cash_after:,.0f}"
    log(msg)
    notify(msg)
    if not top_up:
        email_placed(w, live, log)


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
            if "top_up_until" in w:
                run_top_ups(w, k, ledger, day, live, log)
            else:
                run_wave(w, k, ledger, day, live, log)
        except Exception as e:
            log(f"WAVE FAILED: {e!r}")
            notify(f"{w['name']} failed: {e}")
    log("=== day complete ===")


if __name__ == "__main__":
    main()
