"""Closing-price check: for every filled single, compare the price we paid with Kalshi's price at
kickoff. Beating the kickoff price again and again is the quickest sign an edge is real; wins and
losses take far longer to say anything. Parlays are left out (combos have no price history).

Usage: clv.py [since_date]     e.g. clv.py 2026-11-01. Read-only; kickoff prices are cached in the
ledger's `closing` table."""

import datetime as dt
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nfai import data as D  # noqa: E402
from nfai.execute import Ledger  # noqa: E402
from nfai.kalshi import Kalshi  # noqa: E402
from nfai.slate import K2N  # noqa: E402

ET = ZoneInfo("America/New_York")


def kickoff(code, sched):
    """UTC kickoff for a Kalshi game code like 26NOV02CHISEA, from the nflverse schedule."""
    day = dt.datetime.strptime(code[:7], "%y%b%d").date().isoformat()
    pair = code[7:]
    for i in (2, 3):
        a, h = K2N.get(pair[:i], pair[:i]), K2N.get(pair[i:], pair[i:])
        row = sched[(sched.gameday == day) & (sched.away_team == a) & (sched.home_team == h)]
        if len(row) and isinstance(row.gametime.iloc[0], str):
            t = dt.datetime.fromisoformat(f"{day}T{row.gametime.iloc[0]}").replace(tzinfo=ET)
            return t.astimezone(dt.timezone.utc)
    return None


def kickoff_mid(k, ticker, ko):
    """YES mid (bid/ask average) in the last minute before kickoff, or None."""
    series = ticker.split("-")[0]
    end = int(ko.timestamp())
    for interval, back in ((1, 3 * 3600), (60, 48 * 3600)):
        d = k._request("GET", f"/series/{series}/markets/{ticker}/candlesticks", auth=False,
                       params={"start_ts": end - back, "end_ts": end, "period_interval": interval})
        for c in reversed(d.get("candlesticks", [])):
            if c["end_period_ts"] > end:
                continue
            bid, ask = (c.get(s, {}).get("close_dollars") for s in ("yes_bid", "yes_ask"))
            if bid is not None and ask is not None and 0 < float(bid) < float(ask) < 1:
                return (float(bid) + float(ask)) / 2
    return None


def main():
    since = sys.argv[1] if len(sys.argv) > 1 else "2000-01-01"
    ledger, k = Ledger(), Kalshi()
    db = ledger.db
    db.execute("CREATE TABLE IF NOT EXISTS closing (bet_id INTEGER PRIMARY KEY, kickoff TEXT, close_mid REAL)")
    sched = pd.read_csv(D.DATA / "games.csv")
    bets = pd.read_sql("SELECT id, wave, ticker, title, outcome, limit_price, filled FROM bets "
                       "WHERE kind = 'single' AND filled > 0 AND substr(wave, 1, 10) >= ?", db, params=(since,))
    now = dt.datetime.now(dt.timezone.utc)
    have = set(r[0] for r in db.execute("SELECT bet_id FROM closing"))
    for b in bets[~bets.id.isin(have)].itertuples():
        ko = kickoff(b.ticker.split("-")[1], sched)
        if ko is None or ko > now:
            continue
        db.execute("INSERT OR REPLACE INTO closing VALUES (?,?,?)", (b.id, ko.isoformat(), kickoff_mid(k, b.ticker, ko)))
    db.commit()

    c = pd.read_sql("SELECT * FROM closing", db).rename(columns={"bet_id": "id"})
    r = bets.merge(c, on="id").dropna(subset=["close_mid"])
    if r.empty:
        print("No settled-kickoff singles with a kickoff price yet.")
        return
    r["close"] = r.close_mid.where(r.outcome == "yes", 1 - r.close_mid)    # price of the side we bought
    r["clv"] = r.close - r.limit_price                                     # per contract; > 0 = we beat it
    r["clv_dollars"] = r.clv * r.filled
    r["who"] = r.wave.str.contains("manual").map({True: "your picks", False: "program"})
    r["day"] = r.wave.str[:10]
    missing = len(bets[bets.id.isin(c.id)]) - len(r)

    def summary(d):
        return pd.Series({"bets": len(d), "contracts": d.filled.sum(),
                          "avg_clv_cents": 100 * d.clv_dollars.sum() / d.filled.sum(),
                          "beat_close": (d.clv > 0).mean(), "clv_dollars": d.clv_dollars.sum()})

    print("Kickoff price vs. what we paid (avg_clv_cents > 0 means the price moved our way):\n")
    print(r.groupby(["day", "who"]).apply(summary).round(2).to_string())
    print("\nAll days:")
    print(r.groupby("who").apply(summary).round(2).to_string())
    print("\nBets, worst to best:")
    for x in r.sort_values("clv").itertuples():
        print(f"  {x.day} {x.who:10} {x.title[:52]:52} {x.outcome.upper():3} paid {100 * x.limit_price:3.0f}c  "
              f"kickoff {100 * x.close:3.0f}c  {100 * x.clv:+4.0f}c")
    if missing:
        print(f"\n{missing} bet(s) had no usable kickoff price (no two-sided quote) and are left out.")


if __name__ == "__main__":
    main()
