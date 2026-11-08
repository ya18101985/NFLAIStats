"""Send the two per-wave emails filled with example bets, marked [SAMPLE], to check the layout and
that mail gets through from wherever this runs."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nfai import report as R  # noqa: E402

WAVE = "Sun 11-08 Europe wave (CIN @ ATL)"
SINGLES = [
    dict(bet="Chase Brown 12+ carries", side="yes", contracts=520, price=0.49, stake=254.80, p=0.58, edge=0.07,
         reason="Books (57%) and model (60%) both above Kalshi's 49¢ (edge +7.0¢)."),
    dict(bet="Joe Burrow 300+ passing yards", side="no", contracts=470, price=0.53, stake=249.10, p=0.61, edge=0.06,
         reason="Model 63% vs Kalshi 53¢; books 60%, close enough (edge +6.0¢)."),
    dict(bet="Drake London 100+ receiving yards", side="no", contracts=395, price=0.62, stake=244.90, p=0.66, edge=0.02,
         reason="Books (65%) and model (68%) both above Kalshi's 62¢ (edge +2.0¢)."),
    dict(bet="1st half: Cincinnati over 10.5 points", side="yes", contracts=430, price=0.52, stake=223.60, p=0.51, edge=-0.01,
         reason="Books (51%) and model (52%) both near Kalshi's 52¢; filler bet to use the full bankroll (edge -1.0¢)."),
]
PARLAYS = [
    dict(legs=["Chase Brown 12+ carries", "Tee Higgins 5+ receptions"], price=0.36, stake=185, p=0.44,
         note="Our joint chance 44% vs quote 36¢"),
    dict(legs=["Joe Burrow under 300 pass yds", "Joe Burrow under 42 attempts", "Bijan Robinson 60+ rush yds"],
         price=0.31, stake=185, p=0.33,
         note="Our joint chance 33% vs quote 31¢; forced buy (best quote, above our target) since parlays are never skipped"),
]


def main():
    s, h = R.placed_email(WAVE, "Sun 6:30 AM PT", 3701.46, SINGLES, PARLAYS)
    R.send("[SAMPLE] " + s, h)
    print("sent:", s)
    results = [True, True, False, True]
    bets = [dict(bet=b["bet"], kind="single", side=b["side"], stake=b["stake"], payout=b["contracts"] if w else 0,
                 result="won" if w else "lost") for b, w in zip(SINGLES, results)]
    bets += [dict(bet=" + ".join(p["legs"]), kind="parlay", side="yes", stake=p["stake"],
                  payout=round(p["stake"] / p["price"], 2) if w else 0, result="won" if w else "lost")
             for p, w in zip(PARLAYS, [True, False])]
    s, h = R.settled_email(WAVE, bets, balance=4128.30, season_profit=2054.77)
    R.send("[SAMPLE] " + s, h)
    print("sent:", s)


if __name__ == "__main__":
    main()
