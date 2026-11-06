"""Game lines (winner, spread, total, team total) for the full game, both halves and every
quarter, priced against the sportsbooks. There is no stat model here: the bet is that Kalshi
is cheaper than the books' no-vig price.

Every line is a question about one quantity being over a strike: a team's margin (spreads and
winners), the combined score (totals), or one team's score (team totals). For each Kalshi strike
we use a book's price at the same line if it has one, otherwise its nearest line within
MAX_LINE_GAP points, moved along a normal curve."""

from statistics import NormalDist

from . import odds
from .slate import K2N, N2K

SEGMENTS = {        # Kalshi series infix -> Odds API market suffix
    "": "", "1H": "_h1", "2H": "_h2", "1Q": "_q1", "2Q": "_q2", "3Q": "_q3", "4Q": "_q4",
}
WINNER_SERIES = {"": "KXNFLGAME", "1H": "KXNFL1H", "2H": "KXNFL2H", "1Q": "KXNFL1Q", "2Q": "KXNFL2Q",
                 "3Q": "KXNFL3Q", "4Q": "KXNFL4Q"}
TEAM_TOTAL_SEGMENTS = ("", "1H")
# typical standard deviation of margin / combined score / one team's score, by segment
SD = {"": (13.5, 13.5, 9.5), "1H": (9.5, 9.5, 6.5), "2H": (9.5, 9.5, 6.5),
      "1Q": (6.5, 6.0, 4.5), "2Q": (6.5, 6.5, 4.5), "3Q": (6.5, 6.0, 4.5), "4Q": (6.5, 6.5, 4.5)}
# how far a book's line may be from the Kalshi strike; quarters score in jumps of 3 and 7, so exact only
MAX_LINE_GAP = {"": 3.0, "1H": 1.5, "2H": 1.5, "1Q": 0.0, "2Q": 0.0, "3Q": 0.0, "4Q": 0.0}
BOOK_WEIGHT = 0.7           # vs Kalshi's own mid
MAX_GAP_TO_KALSHI = 0.15    # books and Kalshi further apart than this: probably a mismatch, skip


def _markets():
    out = []
    for seg, suf in SEGMENTS.items():
        out += [f"h2h{suf}", f"spreads{suf}", f"totals{suf}"]
        if seg in ("", "1H", "1Q"):
            out += [f"alternate_spreads{suf}", f"alternate_totals{suf}"]
        if seg in ("1H", "1Q"):
            out.append(f"h2h_3_way{suf}")
        if seg in TEAM_TOTAL_SEGMENTS:
            out += [f"team_totals{suf}", f"alternate_team_totals{suf}"]
    return out


def book_lines(away, home):
    """{(segment, quantity): [(line, p_over, weight)]} and {(segment, team): [(p_win, weight)]} from
    3-way markets. Quantities: ("margin", team), ("total", ""), ("tt", team). Costs ~33 requests."""
    eid = odds.event_id(away, home)
    if eid is None:
        return {}, {}
    d = odds._get(f"/events/{eid}/odds", bookmakers=",".join(odds.BOOKS), markets=",".join(_markets()),
                  oddsFormat="decimal")
    suffix_to_seg = {v: k for k, v in SEGMENTS.items()}
    lines, three_way = {}, {}
    for b in d.get("bookmakers", []):
        w = odds.BOOKS[b["key"]]
        for m in b["markets"]:
            key = m["key"]
            seg = suffix_to_seg.get(key[key.rfind("_"):] if key[-3:-1] in ("_h", "_q") else "", "")
            kind = key[:-len(SEGMENTS[seg])] if SEGMENTS[seg] else key
            kind = kind.replace("alternate_", "")
            outs = m["outcomes"]
            if kind == "h2h_3_way":
                tot = sum(1 / o["price"] for o in outs)
                for o in outs:
                    t = odds.TEAM_NAMES.get(o["name"])
                    if t:
                        three_way.setdefault((seg, t), []).append((1 / o["price"] / tot, w))
            elif kind == "spreads" or (kind == "h2h" and not seg):   # partial-game h2h refunds ties
                # pair each team's price with the opposite side at the mirrored point
                by = {(odds.TEAM_NAMES.get(o["name"]), o.get("point", 0.0)): o["price"] for o in outs}
                for (t, pt), price in by.items():
                    opp = next((x for (x, p2) in by if x != t and abs(p2 + pt) < 1e-9), None)
                    if t is None or opp is None or (kind == "spreads" and pt == int(pt)):
                        continue
                    a, b_ = 1 / price, 1 / by[(opp, -pt)]
                    lines.setdefault((seg, ("margin", t)), []).append((-pt, a / (a + b_), w))
            elif kind in ("totals", "team_totals"):
                by = {}
                for o in outs:
                    t = odds.TEAM_NAMES.get(o.get("description", ""), "") if kind == "team_totals" else ""
                    by.setdefault((t, o["point"]), {})[o["name"]] = o["price"]
                for (t, pt), s in by.items():
                    if "Over" in s and "Under" in s and pt != int(pt):
                        a, u = 1 / s["Over"], 1 / s["Under"]
                        q = ("tt", t) if kind == "team_totals" else ("total", "")
                        lines.setdefault((seg, q), []).append((pt, a / (a + u), w))
    return lines, three_way


def prob_over(entries, strike, sd, max_gap):
    """Weighted book probability that the quantity ends above strike, or None if no book has a
    line within max_gap points."""
    ps, ws = [], []
    for line, p, w in entries:
        if abs(line - strike) > max_gap + 1e-9:
            continue
        if abs(line - strike) < 1e-9:
            ps.append(p)
        else:
            mu = line + sd * NormalDist().inv_cdf(min(max(p, 1e-4), 1 - 1e-4))
            ps.append(1 - NormalDist(mu, sd).cdf(strike))
        ws.append(w / (1 + abs(line - strike)))       # nearer lines count more
    return sum(p * w for p, w in zip(ps, ws)) / sum(ws) if ws else None


def kalshi_lines(g, kalshi):
    """Open Kalshi game-line markets for a game: dicts with seg, quantity, strike, prices."""
    rows = []
    kteams = {N2K.get(g.away, g.away), N2K.get(g.home, g.home)}
    for seg in SEGMENTS:
        series = {WINNER_SERIES[seg]: "winner", f"KXNFL{seg}SPREAD": "margin", f"KXNFL{seg}TOTAL": "total"}
        if seg in TEAM_TOTAL_SEGMENTS:
            series[f"KXNFL{seg}TEAMTOTAL"] = "tt"
        for s, kind in series.items():
            for m in kalshi.markets(event_ticker=f"{s}-{g.code}", status="open"):
                tail = m["ticker"].split("-")[-1]
                kteam = next((t for t in kteams if tail.startswith(t)), None)
                if kind == "winner":
                    if kteam is None or tail != kteam:
                        continue          # the Tie market
                    q, strike = ("margin", K2N.get(kteam, kteam)), 0.0
                elif kind == "total":
                    q, strike = ("total", ""), m.get("floor_strike")
                else:
                    if kteam is None:
                        continue
                    q, strike = (kind, K2N.get(kteam, kteam)), m.get("floor_strike")
                if strike is None:
                    continue
                f = lambda k: float(m.get(k) or 0)
                rows.append(dict(ticker=m["ticker"], event_ticker=m["event_ticker"], seg=seg, kind=kind, q=q,
                                 strike=float(strike), title=m.get("yes_sub_title") or m["title"],
                                 yes_bid=f("yes_bid_dollars"), yes_ask=f("yes_ask_dollars"), no_ask=f("no_ask_dollars"),
                                 yes_ask_size=f("yes_ask_size_fp"), no_ask_size=f("yes_bid_size_fp")))
    return rows


def candidates(g, kalshi, max_spread):
    """Priced YES/NO sides for every Kalshi game line the books also price."""
    from .picks import Candidate, fee   # picks imports this module's caller; avoid a cycle at import time
    lines, three_way = book_lines(g.away, g.home)
    rows = kalshi_lines(g, kalshi)
    out = []
    for r in rows:
        if not (0 < r["yes_bid"] < r["yes_ask"] < 1) or r["yes_ask"] - r["yes_bid"] > max_spread:
            continue
        seg, (qkind, team) = r["seg"], r["q"]
        sd = SD[seg][{"margin": 0, "total": 1, "tt": 2}[qkind]]
        if r["kind"] == "winner" and seg and (seg, team) in three_way:
            e = three_way[(seg, team)]
            p_book = sum(p * w for p, w in e) / sum(w for _, w in e)
        elif r["kind"] == "winner" and seg:
            continue    # 2-way books refund ties but Kalshi's winner market loses on a tie
        else:
            p_book = prob_over(lines.get((seg, r["q"]), []), r["strike"], sd, MAX_LINE_GAP[seg])
        if p_book is None:
            continue
        mid = (r["yes_bid"] + r["yes_ask"]) / 2
        if abs(p_book - mid) > MAX_GAP_TO_KALSHI:
            continue
        p_yes = BOOK_WEIGHT * p_book + (1 - BOOK_WEIGHT) * mid
        label = f"{seg or 'Game'} {r['title']}"
        group = f"{g.code} {seg or 'FULL'}"
        stat = "side" if qkind == "margin" else ("total" if qkind == "total" else f"tt {team}")
        for outcome, price, p, pb, size in (("yes", r["yes_ask"], p_yes, p_book, r["yes_ask_size"]),
                                            ("no", r["no_ask"], 1 - p_yes, 1 - p_book, r["no_ask_size"])):
            out.append(Candidate(r["ticker"], r["event_ticker"], f"{label} {'YES' if outcome == 'yes' else 'NO'}",
                                 group, team, stat, outcome, price, pb, p, p - price - fee(price), size, None,
                                 g.code, pb, -1.0))
    return out
