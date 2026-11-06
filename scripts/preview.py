"""Dry run: show the picks the program would make for a wave right now. Places nothing."""

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nfai import gamelines, odds, picks as P, slate  # noqa: E402
from nfai.kalshi import Kalshi  # noqa: E402
from nfai.model import Model  # noqa: E402

date = dt.date.fromisoformat(sys.argv[1])
kick_utc = sys.argv[2] if len(sys.argv) > 2 else None   # e.g. 18:00 to pick one wave
bankroll = float(sys.argv[3]) if len(sys.argv) > 3 else 1000.0

k = Kalshi()
games = [g for g in slate.games_on(date, k) if not kick_utc or g.kickoff.strftime("%H:%M") == kick_utc]
m = Model(week=max((g.week for g in games if g.week), default=None))
cands, flags = [], []
for g in games:
    c, f = P.game_candidates(m, g, slate.prop_markets(g, k), odds.game_lines(g.away, g.home) if odds.available() else None)
    cands += c + (gamelines.candidates(g, k, P.MAX_SPREAD) if odds.available() else [])
    flags += [(g.code,) + x for x in f]
depth = [f for f in flags if f[2] == "depth chart"]
print(f"{len(games)} games, {len(cands)} priced sides ({sum(c.p_book is not None for c in cands)} with sportsbook lines), {len(flags) - len(depth)} props skipped by the sanity check")
for f in depth:
    print("  depth chart skip:", f[0], f[1], "-", f[3])
for f in [f for f in flags if f[2] != "depth chart"][:15]:
    print("  sanity skip:", f)
singles = P.select_singles(cands, bankroll)
print(f"\nSINGLES (wave bankroll ${bankroll:,.0f}, singles budget ${P.SINGLES_SHARE * bankroll:,.0f})")
for c, n, stake in singles:
    bk = f"books {c.p_book:.2f}" if c.p_book is not None else "no books"
    why = "game line, books only" if c.hit is None else f"model {c.p_model:.2f}, {bk}, model-only EV {c.ev_model*100:.1f}c"
    print(f"  [{c.group}] {c.title:50} {c.outcome.upper():3} @{c.price*100:3.0f}c  p={c.p:.2f}  EV {c.ev*100:4.1f}c  "
          f"({why})  {n} contracts ${stake:,.0f}")
print(f"  total ${sum(s for _, _, s in singles):,.0f}")
print("\nPARLAY CANDIDATES")
for pl in P.parlay_candidates(cands):
    print(f"  joint p={pl['p']:.3f} vs product of prices {pl['p_singles_price']:.3f}: " + " | ".join(
        f"{c.title} {c.outcome}" for c in pl["legs"]))
