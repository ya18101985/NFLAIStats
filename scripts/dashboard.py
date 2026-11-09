"""Build the Bet Tracker dashboard page (dashboard/index.html) from the live ledger, Kalshi
(fills, live prices, results, settlements) and ESPN (scores, box scores, injuries).
Claude republishes the page to the claude.ai artifact after each run.

    .venv/bin/python scripts/dashboard.py [YYYY-MM-DD]     # default: today in PT

Live win chance is Kalshi's own mid price for the side we hold (the market is the best
in-game estimate); a parlay without a quote uses the product of its legs. The "Why" text is
worked out once per bet, the first time it appears, and cached in data/reasons.json."""

import datetime as dt
import json
import re
import sqlite3
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from nfai import data as D, gamelines as GL, odds, slate  # noqa: E402
from nfai.kalshi import Kalshi  # noqa: E402
from nfai.picks import fee  # noqa: E402
from nfai.slate import K2N, N2K, norm_name  # noqa: E402

PT, ET = ZoneInfo("America/Los_Angeles"), ZoneInfo("America/New_York")
ESPN = slate.ESPN
OUT = ROOT / "dashboard" / "index.html"
TEMPLATE = ROOT / "dashboard" / "template.html"
PNL_BASELINE = ROOT / "dashboard" / "pnl_baseline.json"   # weeks 4-8, incl. bets placed outside Kalshi
BASELINE_CUTOFF = "2026-10-30T12:00:00Z"                    # Kalshi settlements after this are new
HISTORY = D.DATA / "dashboard_history.json"
REASONS = D.DATA / "reasons.json"
LEDGER = Path(__import__("os").environ.get("NFAI_LEDGER", ROOT / "ledger" / "ledger.db"))

# Kalshi prop series -> (dashboard stat key, unit, words)
PROPS = {"KXNFLRECYDS": ("recYds", "yds", "receiving yards"), "KXNFLREC": ("receptions", "rec", "catches"),
         "KXNFLRSHYDS": ("rushYds", "yds", "rushing yards"), "KXNFLRSHATT": ("rushAtt", "car", "carries"),
         "KXNFLRRYDS": ("rushRecYds", "yds", "rush + rec yards"), "KXNFLPASSYDS": ("passYds", "yds", "passing yards"),
         "KXNFLPASSATT": ("passAtt", "att", "pass attempts"), "KXNFLPASSCOMP": ("passComp", "cmp", "completions"),
         "KXNFLPASSTDS": ("passTds", "TD", "passing TDs"), "KXNFLTD": ("tds", "TD", "rush + rec TDs (anytime TD)")}
MODEL_STAT = {"KXNFLRECYDS": "receiving_yards", "KXNFLREC": "receptions", "KXNFLRSHYDS": "rushing_yards",
              "KXNFLRSHATT": "carries", "KXNFLRRYDS": "rr_yards", "KXNFLPASSYDS": "passing_yards",
              "KXNFLPASSATT": "attempts", "KXNFLPASSCOMP": "completions", "KXNFLPASSTDS": "passing_tds"}
NAME_ALIAS = {"Hollywood Brown": "Marquise Brown"}   # Kalshi nickname -> nflverse name
SEG_WORDS = {"": "game", "1H": "1st half", "2H": "2nd half", "1Q": "1st quarter", "2Q": "2nd quarter",
             "3Q": "3rd quarter", "4Q": "4th quarter"}


def f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def slug(name):
    return norm_name(name).replace(" ", "-")


def nfl_week(day):
    """NFL weeks run Tuesday to Monday; Week 8 began Tue 2026-10-27."""
    return 8 + (day - dt.date(2026, 10, 27)).days // 7


# ---- ESPN ---------------------------------------------------------------------------------

def espn_games(day):
    evs = requests.get(f"{ESPN}/scoreboard", params={"dates": day.strftime("%Y%m%d")}, timeout=30).json()["events"]
    games = {}
    for e in evs:
        c = e["competitions"][0]
        comps = []
        for t in c["competitors"]:
            ab = t["team"]["abbreviation"]
            comps.append(dict(abbr=ab, name=t["team"]["displayName"], homeAway=t["homeAway"], score=int(f(t.get("score"))),
                              linescores=[int(f(x.get("value"))) for x in t.get("linescores", [])]))
        away = next(x for x in comps if x["homeAway"] == "away")
        home = next(x for x in comps if x["homeAway"] == "home")
        st = e["status"]
        key = frozenset(slate.ESPN2N.get(x["abbr"], x["abbr"]) for x in comps)
        games[key] = dict(id=e["id"], label=f"{away['name'].split()[-1]} @ {home['name'].split()[-1]}", state=st["type"]["state"],
                          start=e["date"], detail=st["type"].get("shortDetail", ""), period=st.get("period", 0),
                          clock=st.get("displayClock", ""), competitors=comps,
                          score={x["abbr"]: x["score"] for x in comps}, teams=[away["abbr"], home["abbr"]])
    return games


def box_scores(game_id):
    d = requests.get(f"{ESPN}/summary", params={"event": game_id}, timeout=30).json()
    out = {}
    for t in d.get("boxscore", {}).get("players", []):
        team = t["team"]["abbreviation"]
        for st in t["statistics"]:
            labels = st.get("labels", [])
            for a in st.get("athletes", []):
                name = a["athlete"]["displayName"]
                v = dict(zip(labels, a.get("stats", [])))
                p = out.setdefault(slug(name), {"name": name, "team": team})
                if st["name"] == "passing":
                    c, att = (v.get("C/ATT", "0/0").split("/") + ["0"])[:2]
                    p.update(cmpAtt=v.get("C/ATT"), passYds=int(f(v.get("YDS"))), passTds=int(f(v.get("TD"))),
                             ints=int(f(v.get("INT"))), passComp=int(f(c)), passAtt=int(f(att)))
                elif st["name"] == "rushing":
                    p.update(carries=int(f(v.get("CAR"))), rushAtt=int(f(v.get("CAR"))), rushYds=int(f(v.get("YDS"))),
                             rushTds=int(f(v.get("TD"))), rushLong=int(f(v.get("LONG"))))
                elif st["name"] == "receiving":
                    p.update(receptions=int(f(v.get("REC"))), recYds=int(f(v.get("YDS"))), recTds=int(f(v.get("TD"))),
                             recLong=int(f(v.get("LONG"))), targets=int(f(v.get("TGTS"))))
    return out


def stat_value(p, key):
    if not p:
        return 0
    if key == "rushRecYds":
        return p.get("rushYds", 0) + p.get("recYds", 0)
    if key == "tds":   # anytime TD: rushing + receiving touchdowns
        return p.get("rushTds", 0) + p.get("recTds", 0)
    return p.get(key, 0)


def seg_points(game, abbr, seg):
    c = next((x for x in game["competitors"] if x["abbr"] == abbr), None)
    if not c:
        return 0
    ls = c["linescores"] + [0] * 5
    if not seg:
        return c["score"]
    if seg == "1H":
        return ls[0] + ls[1]
    if seg == "2H":
        return ls[2] + ls[3]
    return ls[int(seg[0]) - 1]


# ---- Kalshi -------------------------------------------------------------------------------

def mid(m, side):
    yb, ya, last = f(m.get("yes_bid_dollars")), f(m.get("yes_ask_dollars")), f(m.get("last_price_dollars"))
    y = (yb + ya) / 2 if 0 < yb < ya else (last or None)
    if y is None:
        return None
    return y if side == "yes" else 1 - y


def result(m, side):
    r = m.get("result")
    if r in ("yes", "no") and m.get("status") in ("closed", "determined", "settled", "finalized"):
        return "won" if r == side else "lost"
    return "live"


def parse_ticker(t):
    """Series, game code, rest; e.g. KXNFLRECYDS-26NOV01PHIWAS-WASSDIGGS3-110."""
    parts = t.split("-")
    return parts[0], (parts[1] if len(parts) > 1 else ""), parts[2:]


def ticker_team(ticker, games_by_code):
    """A prop's team from its ticker, for players with no box score yet (pregame)."""
    _, code, rest = parse_ticker(ticker)
    g = games_by_code.get(code)
    if not g or not rest:
        return ""
    return next((t for t in g["teams"] if rest[0].startswith(N2K.get(slate.ESPN2N.get(t, t), slate.ESPN2N.get(t, t)))), "")


def game_line_info(series):
    m = re.match(r"KXNFL(1H|2H|1Q|2Q|3Q|4Q)?(SPREAD|TOTAL|TEAMTOTAL)?$", series)
    if series == "KXNFLGAME":
        return "", "winner"
    if series in ("KXNFL1H", "KXNFL2H", "KXNFL1Q", "KXNFL2Q", "KXNFL3Q", "KXNFL4Q"):
        return series[5:], "winner"
    if m and m.group(2):
        return m.group(1) or "", {"SPREAD": "margin", "TOTAL": "total", "TEAMTOTAL": "tt"}[m.group(2)]
    return None, None


# ---- one leg ------------------------------------------------------------------------------

def build_leg(k, ticker, side, games_by_code, boxes, mkt_cache):
    m = mkt_cache.setdefault(ticker, k.market(ticker))
    series, code, rest = parse_ticker(ticker)
    g = games_by_code.get(code)
    p = mid(m, side)
    status = result(m, side)
    p_win = 1.0 if status == "won" else 0.0 if status == "lost" else (p if p is not None else 0.5)
    strike = m.get("floor_strike")
    leg = dict(side=side, status=status, pWin=round(p_win, 4), unknown=False, ticker=ticker)
    if series in PROPS:
        key, unit, words = PROPS[series]
        name = m["title"].split(":")[0].strip()
        target = int(round(f(strike) + 0.5))
        cur = stat_value(boxes.get(slug(name)), key)
        leg.update(player=slug(name), playerName=name, stat=key, threshold=target, target=target, unit=unit, current=cur,
                   label=f"{name} {target}+ {words}", progress=min(1.0, cur / target) if target else 0,
                   playerStatus="active")
        return leg, name
    seg, kind = game_line_info(series)
    title = m.get("yes_sub_title") or m.get("title", ticker)
    leg.update(player=None, stat="line", label=f"{SEG_WORDS.get(seg, '')} · {title}", progress=p_win)
    if g is not None and kind:
        a, h = g["teams"]
        pa, ph = seg_points(g, a, seg), seg_points(g, h, seg)
        sw = SEG_WORDS.get(seg, "game")
        if kind == "total":
            leg.update(text=f"{pa + ph} pts so far in the {sw}", right=f"line {f(strike):g}")
        elif kind == "tt":
            team = next((t for t in (a, h) if rest and rest[0].startswith(N2K.get(slate.ESPN2N.get(t, t), t))), a)
            pts = seg_points(g, team, seg)
            leg.update(text=f"{team} {pts} pts in the {sw}", right=f"line {f(strike):g}")
        else:
            leg.update(text=f"{a} {pa} – {ph} {h} ({sw})", right=("strike " + f"{f(strike):g}") if strike else "")
    return leg, None


# ---- reasons (cached) ---------------------------------------------------------------------

class Reasoner:
    """Works out the numbers behind a bet once: model projection, sportsbook price, recent
    games, matchup and game script. Loaded lazily because the model takes a while."""

    def __init__(self, k):
        self.k, self._model, self._book, self._gl, self.lines = k, None, {}, {}, {}

    @property
    def model(self):
        if self._model is None:
            from nfai.model import Model
            self._model = Model()
        return self._model

    def books_props(self, away, home):
        if (away, home) not in self._book:
            self._book[(away, home)] = odds.game_lines(away, home) if odds.available() else {}
        return self._book[(away, home)]

    def books_lines(self, away, home):
        if (away, home) not in self._gl:
            self._gl[(away, home)] = GL.book_lines(away, home) if odds.available() else ({}, {})
        return self._gl[(away, home)]

    def explain(self, row, legs, game_teams):
        kind, side, price = row["kind"], row["outcome"], row["limit_price"] or 0
        why = dict(p=row["p"], pModel=row["p_model"], ev=row["ev"], wave=row["wave"], pBook=None, points=[], group="")
        if kind == "parlay" and row["p"] is None:
            return self._explain_manual_parlay(row, why, legs, game_teams)
        if kind == "parlay":
            why["group"] = "Parlay"
            why["points"] = [f"{len(legs)} legs: " + "; ".join(l["label"] + (" (NO)" if l["side"] == "no" else "") for l in legs),
                             f"Model's joint chance that every leg hits: {row['p']:.0%}. Legs on the same team are simulated "
                             "together, so the model knows when they tend to hit together.",
                             f"Bought at {price * 100:.0f}¢ only because a market maker quoted at least 15% below that chance."]
            why["verdict"] = "Parlays are long shots by design; this one gets its value from legs that move together."
            return why
        series, code, rest = parse_ticker(row["ticker"])
        away, home = game_teams.get(code, (None, None))
        if series in MODEL_STAT and away:
            return self._explain_prop(row, why, series, away, home)
        if series in PROPS:   # a prop our model doesn't price (e.g. anytime TD)
            return why
        if away:
            return self._explain_line(row, why, series, code, away, home)
        return why

    def _explain_prop(self, row, why, series, away, home):
        from nfai.model import GameContext
        from nfai.picks import calibrate
        m = self.k.market(row["ticker"])
        name = m["title"].split(":")[0].strip()
        name = NAME_ALIAS.get(name, name)
        thr = f(m.get("floor_strike"))
        stat = MODEL_STAT[series]
        side = row["outcome"]
        over = side == "yes"
        M = self.model
        team = next((t for t in (away, home) if norm_name(name) in
                     {norm_name(n) for n in M.weeks[M.weeks.team == t].player_display_name.dropna().unique()}), None)
        pts = []
        if team is None:
            why["points"] = [f"{name}: no season data found for the explanation."]
            return why
        opp = home if team == away else away
        nfl_name = next(n for n in M.weeks[M.weeks.team == team].player_display_name.dropna().unique() if norm_name(n) == norm_name(name))
        pl = M.player(nfl_name, team)
        sched = D.schedule()
        r = sched[(sched.season == D.SEASON) & (sched.away_team == away) & (sched.home_team == home)].tail(1)
        spread_home, total = (float(r.spread_line.iloc[0]), float(r.total_line.iloc[0])) if len(r) else (0.0, 45.0)
        spread_home, total = self.lines.get((away, home), (spread_home, total))
        spread = spread_home if team == home else -spread_home
        ctx = GameContext(team, opp, spread=spread, total=total, wind_mph=0, dome=False)
        draws = M.simulate(ctx, [pl], n=20000, seed=1)[pl.name][stat]
        med, lo, hi = np.median(draws), np.quantile(draws, 0.1), np.quantile(draws, 0.9)
        p_model_over = float(calibrate((draws > thr).mean()))
        if why["pModel"] is None:   # bet placed outside our bot: fill in the model's view
            why["pModel"] = p_model_over if over else 1 - p_model_over
        col = {"passing_yards": "passing_yards", "receptions": "receptions", "rushing_yards": "rushing_yards",
               "carries": "carries", "rr_yards": "rr_yards", "receiving_yards": "receiving_yards", "attempts": "attempts",
               "completions": "completions", "passing_tds": "passing_tds", "receiving_yards ": "receiving_yards"}[stat]
        hist = M.weeks[(M.weeks.player_display_name == nfl_name) & (M.weeks.team == team)].sort_values("week")
        last5 = [int(x) for x in hist[col].fillna(0).tail(5)]
        hits = sum((x > thr) == over for x in last5)
        words = PROPS[series][2]
        pts.append(f"Model projects {med:.0f} {words} (80% range {lo:.0f}–{hi:.0f}) vs the line of {thr:g}: "
                   f"{(p_model_over if over else 1 - p_model_over):.0%} for the {'over' if over else 'under'}.")
        lines = self.books_props(away, home).get((norm_name(name), stat))
        if lines:
            pb_over = odds.book_prob(lines, draws, thr)
            pb = pb_over if over else 1 - pb_over
            why["pBook"] = pb
            main = sorted({l for l, _, _ in lines})
            pts.append(f"Sportsbooks (Pinnacle, DraftKings, FanDuel...) post {', '.join(f'{x:g}' for x in main[:3])}; "
                       f"converted to Kalshi's line that's {pb:.0%} for this side, vs a price of {row['limit_price'] * 100:.0f}¢.")
        pts.append(f"Last {len(last5)} games: {', '.join(map(str, last5))}. This side would have won {hits} of {len(last5)}.")
        rec = pl.recent
        usage = {"QB": f"{rec['attempts']:.0f} pass attempts", "RB": f"{rec['touches']:.0f} touches"}.get(
            pl.pos, f"{rec['targets']:.1f} targets")
        pts.append(f"Usage over the last 4 games: {usage} a game ({pl.pos}).")
        fav = f"{abs(spread):g}-point {'favorite' if spread > 0 else 'underdog'}" if spread else "pick'em"
        t = M.teams
        rk = lambda col_, team_: int(t[col_].rank().get(team_, 0))
        pts.append(f"{team} is a {fav} vs {opp} (total {total:g}). {opp}'s defense ranks #{rk('def_epa_pass', opp)} vs the "
                   f"pass and #{rk('def_epa_rush', opp)} vs the run by EPA allowed (1 = best).")
        why["points"] = pts
        pb = why["pBook"]
        if (row.get("note") or "").startswith("Likely winner"):
            why["group"] = "C · likely winner"
            why["verdict"] = "The model and the books both make this a likely winner, with a small edge after fees."
        elif pb is not None and pb >= row["limit_price"] + 0.005:
            why["group"] = "A · books back it"
            why["verdict"] = "Both the sportsbooks and the model rate this side above Kalshi's price."
        else:
            why["group"] = "B · model's call"
            why["verdict"] = ("The books price this near fair; the bet rests on the model's read "
                              f"({'more' if (med > thr) == over else 'less'} production than the line)." if pb is not None
                              else "No sportsbook line; the bet rests on the model.")
        return why

    def _explain_manual_parlay(self, row, why, legs, game_teams):
        """A parlay the user picked by hand: our model's and the books' chance for each leg, and the
        joint chance from simulating each team's players together (so linked legs are counted right)."""
        from nfai.model import GameContext
        from nfai.picks import calibrate
        import pandas as pd
        price = row["limit_price"] or 0
        M = self.model
        sched = D.schedule()
        info, by_team = [], {}
        for t in json.loads(row["legs"]):
            series, code, _ = parse_ticker(t)
            away, home = game_teams[code]
            m = self.k.market(t)
            name, thr = m["title"].split(":")[0].strip(), f(m.get("floor_strike"))
            team = next(tm for tm in (away, home) if norm_name(name) in
                        {norm_name(n) for n in M.weeks[M.weeks.team == tm].player_display_name.dropna().unique()})
            nfl_name = next(n for n in M.weeks[M.weeks.team == team].player_display_name.dropna().unique() if norm_name(n) == norm_name(name))
            info.append(dict(name=name, label=f"{name} {int(thr + 0.5)}+ {PROPS[series][2]}", stat=MODEL_STAT.get(series), thr=thr,
                             team=team, away=away, home=home, pl=M.player(nfl_name, team)))
            by_team.setdefault(team, []).append(info[-1])
        hit = np.ones(20000, bool)
        for team, ls in by_team.items():
            a, h = ls[0]["away"], ls[0]["home"]
            r = sched[(sched.season == D.SEASON) & (sched.away_team == a) & (sched.home_team == h)].tail(1)
            sh, total = (float(r.spread_line.iloc[0]), float(r.total_line.iloc[0])) if len(r) else (0.0, 45.0)
            sh, total = self.lines.get((a, h), (sh, total))
            ctx = GameContext(team, h if team == a else a, spread=sh if team == h else -sh, total=total, wind_mph=0, dome=False)
            sims = M.simulate(ctx, [l["pl"] for l in ls], n=20000, seed=1)
            for l in ls:
                d = sims[l["pl"].name][l["stat"]]
                hit &= d > l["thr"]
                l["pm"] = float(calibrate((d > l["thr"]).mean()))
                lines = self.books_props(l["away"], l["home"]).get((norm_name(l["name"]), l["stat"]))
                l["pb"] = odds.book_prob(lines, d, l["thr"]) if lines else None
        joint = float(hit.mean())
        pb_prod = float(np.prod([l["pb"] for l in info])) if all(l["pb"] is not None for l in info) else None
        why.update(group="Your parlay", pModel=joint, pBook=pb_prod)
        p = joint if pb_prod is None else 0.5 * joint + 0.5 * pb_prod
        why["p"], why["ev"] = p, p - price - fee(price)
        why["points"] = [f"{l['label']}: model {l['pm']:.0%}" + (f", books {l['pb']:.0%}" if l["pb"] is not None else "") for l in info]
        why["points"] += [f"All legs together: model {joint:.0%} (players on the same team simulated together)"
                          + (f"; books, treating legs as independent, {pb_prod:.0%}" if pb_prod is not None else "") + ".",
                          f"Bought at {price * 100:.0f}¢ from a market maker, after kickoff (one-time exception to the pregame rule)."]
        even = price + fee(price)
        why["verdict"] = (f"Placed at your request. Break-even after fees is {even:.0%}; our blended read is {p:.0%}, "
                          + ("so the price looks good." if p >= even else "so the price is a bit rich." if p >= even - 0.05
                             else "so our numbers don't support this price."))
        return why

    def _explain_line(self, row, why, series, code, away, home):
        seg, kind = game_line_info(series)
        lines, three = self.books_lines(away, home)
        m = self.k.market(row["ticker"])
        side = row["outcome"]
        title = m.get("yes_sub_title") or m.get("title")
        g = slate.Game(code, away, home, kickoff=None)
        pb = None
        for r in GL.kalshi_lines(g, self.k):
            if r["ticker"] == row["ticker"]:
                qk = r["q"][0]
                sd = GL.SD[seg][{"margin": 0, "total": 1, "tt": 2}[qk]]
                if r["kind"] == "winner" and seg:
                    e = three.get((seg, r["q"][1]), [])
                    pb = sum(p * w for p, w in e) / sum(w for _, w in e) if e else None
                else:
                    pb = GL.prob_over(lines.get((seg, r["q"]), []), r["strike"], sd, GL.MAX_LINE_GAP[seg])
        if pb is not None and side == "no":
            pb = 1 - pb
        why["pBook"] = pb
        why["group"] = "A · game line"
        why["points"] = [f"Kalshi market: \"{title}\" ({SEG_WORDS.get(seg, 'game')}), we hold {side.upper()} at {row['limit_price'] * 100:.0f}¢.",
                         (f"Sportsbooks' price for the same outcome, with their margin removed: {pb:.0%} "
                          "(Pinnacle counts double; alternate lines used where Kalshi's number differs)." if pb is not None
                          else "Sportsbook price unavailable now (lines are pulled after kickoff)."),
                         "No stat model for game lines: the bet is that Kalshi is cheaper than the sportsbooks."]
        why["verdict"] = "Price-shopping bet: same outcome, cheaper on Kalshi than at the books."
        return why


def other_bot_verdict(why, price, in_game=False):
    """Bets from the user's other bot: our own read of them, with the blended chance and edge
    worked out the same way as for our bets, and a plain verdict on whether the numbers back it."""
    pm, pb = why.get("pModel"), why.get("pBook")
    why["group"] = "Your in-game bet" if in_game else "Other bot"
    who = "You placed this during the game" if in_game else "Placed by your other bot"
    if in_game and not any(p.startswith("Placed mid-game") for p in why.get("points", [])):
        why.setdefault("points", []).append("Placed mid-game: the model and book numbers above are pregame, so they ignore "
                                            "what had already happened; the live Kalshi price is the better guide.")
    if pm is None and pb is None:
        why["verdict"] = who + ". Our model has no read on this market."
        return why
    p = pm if pb is None else pb if pm is None else 0.6 * pb + 0.4 * pm
    why["p"], why["ev"] = p, p - price - fee(price)
    even = price + fee(price)   # chance needed to break even after Kalshi's fee
    fair = lambda q: q is not None and q >= even
    close = lambda q: q is not None and q >= even - 0.03
    lead = f"{who}. Break-even after fees is {even:.0%}{' (pregame read)' if in_game else ''}. "
    if fair(pm) and fair(pb):
        why["verdict"] = lead + "Our model and the sportsbooks both rate it above that: a sound bet."
    elif fair(pb) or fair(pm):
        why["verdict"] = lead + ("The sportsbooks" if fair(pb) else "Our model") + " rate it above that; " + \
            ("our model" if fair(pb) else "the sportsbooks") + " don't, so it's a judgment call."
    elif close(pm) and close(pb):
        why["verdict"] = lead + "Model and books both put it within 3 points of that: roughly a fair-price bet, small negative edge."
    else:
        why["verdict"] = lead + "Neither our model nor the sportsbooks get there; our numbers don't support this one."
    return why


# ---- P&L ----------------------------------------------------------------------------------

def pnl(k, open_bets):
    base = json.loads(PNL_BASELINE.read_text())
    groups = {"weeks": {r["key"]: dict(r) for r in base["weeks"]}, "months": {r["key"]: dict(r) for r in base["months"]},
              "quarters": {r["key"]: dict(r) for r in base["quarters"]}}
    season = dict(base["season"])
    for s in k.settlements():
        if s["settled_time"] <= BASELINE_CUTOFF or not s["ticker"].startswith(("KXNFL", "KXMVE")):
            continue
        cost = f(s["yes_total_cost_dollars"]) + f(s["no_total_cost_dollars"]) + f(s["fee_cost"])
        ret = f(s.get("revenue")) / 100
        won = ret > 0
        day = dt.datetime.fromisoformat(s["settled_time"].replace("Z", "+00:00")).astimezone(ET).date()
        wk = nfl_week(day - dt.timedelta(days=1) if day.weekday() == 1 else day)   # Monday-night games settle Tuesday
        keys = {"weeks": (f"{wk:02d}", f"Week {wk}"), "months": (day.strftime("%Y-%m"), day.strftime("%b %Y")),
                "quarters": (f"{day.year}-Q{(day.month - 1) // 3 + 1}", f"{day.year} Q{(day.month - 1) // 3 + 1}")}
        for gname, (key, label) in keys.items():
            r = groups[gname].setdefault(key, dict(key=key, label=label, bets=0, won=0, lost=0, staked=0, returned=0, profit=0, roi=0))
            for rr in (r,):
                rr["bets"] += 1; rr["won"] += won; rr["lost"] += not won
                rr["staked"] = round(rr["staked"] + cost, 2); rr["returned"] = round(rr["returned"] + ret, 2)
        season["bets"] += 1; season["won"] += won; season["lost"] += not won
        season["staked"] = round(season["staked"] + cost, 2); season["returned"] = round(season["returned"] + ret, 2)
    for rows in list(groups.values()) + [{"s": season}]:
        for r in rows.values():
            r["profit"] = round(r["returned"] - r["staked"], 2)
            r["roi"] = round(100 * r["profit"] / r["staked"], 1) if r["staked"] else 0
    out = {g: sorted(v.values(), key=lambda r: r["key"]) for g, v in groups.items()}
    live = [b for b in open_bets if b["status"] == "live"]
    out.update(season=season, open=dict(bets=len(live), staked=round(sum(b["cost"] for b in live), 2),
                                        expected=round(sum(b["contracts"] * b["pWin"] for b in live), 2),
                                        maxReturn=round(sum(b["contracts"] for b in live), 2)),
               updatedAt=dt.datetime.now(dt.timezone.utc).isoformat())
    return out


# ---- main ---------------------------------------------------------------------------------

def main():
    day = dt.date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else dt.datetime.now(PT).date()
    k = Kalshi()
    games = espn_games(day)
    tag = day.strftime("%y%b%d").upper()

    rows = []
    if LEDGER.exists():
        db = sqlite3.connect(LEDGER)
        db.row_factory = sqlite3.Row
        rows = [dict(r) for r in db.execute("SELECT * FROM bets WHERE wave LIKE ? AND status != 'error' AND status != 'skipped' "
                                            "ORDER BY id", (f"{day}%",))]
    fills = list(k.fills())
    by_order, by_ticker = {}, {}
    for x in fills:
        by_order.setdefault(x["order_id"], []).append(x)
        by_ticker.setdefault(x["ticker"], []).append(x)
    teams_all = set(slate.STADIUMS) | {"JAC", "LAR"}
    # today's NFL singles bought outside our bot (e.g. the user's other bot): one row per ticker/side
    mine = {r["order_id"] for r in rows} | {r["ticker"] for r in rows if r["kind"] == "parlay"}
    other = {}
    for x in fills:
        series, code, _ = parse_ticker(x["ticker"])
        if (x["ticker"].startswith("KXNFL") and code.startswith(tag) and x.get("action", "buy") == "buy"
                and x["order_id"] not in mine and x["ticker"] not in mine):
            other.setdefault((x["ticker"], x["side"]), []).append(x)
    for (t, side), fl in other.items():
        n = sum(f(x["count_fp"]) for x in fl)
        price = sum(f(x["count_fp"]) * f(x[f"{side}_price_dollars"]) for x in fl) / n
        try:
            a, h = slate._split_code(parse_ticker(t)[1], teams_all)
            g = games.get(frozenset({K2N.get(a, a), K2N.get(h, h)}))
        except ValueError:
            g = None
        in_game = g is not None and min(x["created_time"] for x in fl) > g["start"]
        rows.append(dict(ticker=t, outcome=side, kind="single", wave="Manual (in-game)" if in_game else "Other bot", p=None, p_model=None, ev=None,
                         limit_price=price, order_id=None, legs="[]", title="", fills=fl))
    game_teams, games_by_code = {}, {}
    for r in rows:
        for t in [r["ticker"]] + json.loads(r["legs"] or "[]"):
            code = parse_ticker(t)[1]
            if code.startswith(tag) and code not in game_teams:
                try:
                    a, h = slate._split_code(code, teams_all)
                except ValueError:
                    continue
                game_teams[code] = (K2N.get(a, a), K2N.get(h, h))
    for code, (a, h) in game_teams.items():
        g = games.get(frozenset({a, h}))
        if g:
            games_by_code[code] = g
    boxes = {}
    for g in games_by_code.values():
        if g["state"] != "pre":
            boxes.update(box_scores(g["id"]))

    reasons = json.loads(REASONS.read_text()) if REASONS.exists() else {}
    reasoner = Reasoner(k)
    reasoner.lines = slate.espn_lines(day)
    mkt_cache, bets, players = {}, [], {}
    for r in rows:
        fl = r.get("fills") or (by_ticker.get(r["ticker"], []) if r["kind"] == "parlay" else by_order.get(r["order_id"], []))
        side = r["outcome"]
        n = sum(f(x["count_fp"]) for x in fl)
        if n <= 0:
            continue
        cost = sum(f(x["count_fp"]) * f(x["yes_price_dollars"] if x["side"] == "yes" else x["no_price_dollars"]) for x in fl)
        fees = sum(f(x["fee_cost"]) for x in fl)
        if r["kind"] == "parlay":
            legs = []
            for i, t in enumerate(json.loads(r["legs"] or "[]")):
                leg_side = r["title"].split(" + ")[i].rsplit(" ", 1)[-1] if " + " in r["title"] else "yes"
                leg, name = build_leg(k, t, leg_side, games_by_code, boxes, mkt_cache)
                legs.append(leg)
            m = mkt_cache.setdefault(r["ticker"], k.market(r["ticker"]))
            status = result(m, "yes")
            if status == "live":
                status = "lost" if any(l["status"] == "lost" for l in legs) else "won" if all(l["status"] == "won" for l in legs) else "live"
            q = mid(m, "yes")
            p_win = 1.0 if status == "won" else 0.0 if status == "lost" else (q if q else float(np.prod([l["pWin"] for l in legs])))
            title = " & ".join(l["label"] if l["side"] == "yes" else "No · " + l["label"] for l in legs)
        else:
            leg, name = build_leg(k, r["ticker"], side, games_by_code, boxes, mkt_cache)
            legs = [leg]
            status, p_win = leg["status"], leg["pWin"]
            title = ("No · " if side == "no" else "") + leg["label"]
        for l in legs:
            if l.get("player"):
                players[l["player"]] = boxes.get(l["player"]) or {"name": l["playerName"], "team": ticker_team(l["ticker"], games_by_code)}
        code = parse_ticker(r["ticker"])[1] if r["kind"] != "parlay" else parse_ticker(json.loads(r["legs"])[0])[1]
        g = games_by_code.get(code)
        bid = "k-" + r["ticker"].lower() + ("-no" if side == "no" else "")
        if bid not in reasons:
            try:
                reasons[bid] = reasoner.explain(r, legs, game_teams)
            except Exception as e:   # explanation is nice to have; never block the dashboard on it
                reasons[bid] = dict(points=[f"Explanation unavailable: {e}"], wave=r["wave"], p=r["p"], pModel=r["p_model"], ev=r["ev"])
        if r["wave"] in ("Other bot", "Manual (in-game)"):
            reasons[bid] = other_bot_verdict(reasons[bid], r["limit_price"], in_game=r["wave"] != "Other bot")
        first = min(x["created_time"] for x in fl)
        bets.append(dict(id=bid, title=title, contracts=round(n, 2), price=round(cost / n, 4), cost=round(cost + fees, 2),
                         fees=round(fees, 2), filled=dt.datetime.fromisoformat(first.replace("Z", "+00:00")).astimezone(PT).strftime("%-I:%M %p PT"),
                         placed=first, game=g["id"] if g else code, gameLabel=g["label"] if g else code, source="kalshi",
                         ticker=r["ticker"], legs=legs, pWin=round(p_win, 4), status=status, payout=round(n, 2),
                         expectedValue=round(n * p_win, 2), why=reasons[bid]))
    REASONS.write_text(json.dumps(reasons, indent=1, default=float))

    gl = [dict(g, fracLeft=0, remaining=0, firstTd=None) for g in sorted(games.values(), key=lambda g: g["start"])]
    ours = [g for g in gl if g["id"] in {x["id"] for x in games_by_code.values()}] or gl
    state_all = "post" if ours and all(g["state"] == "post" for g in ours) else "in" if any(g["state"] == "in" for g in ours) else "pre"

    # whole-day outcome distribution (bets independent; settled bets are fixed)
    rng = np.random.default_rng(0)
    pw = np.array([b["pWin"] for b in bets]) if bets else np.zeros(0)
    pay = np.array([b["payout"] for b in bets]) if bets else np.zeros(0)
    cost_all = sum(b["cost"] for b in bets)
    draws = (rng.random((4000, len(bets))) < pw) @ pay - cost_all if bets else np.zeros(1)
    outlook = dict(pProfit=float((draws > 0).mean() * 100), pBigWin=float((draws > 0.25 * cost_all).mean() * 100),
                   pBigLoss=float((draws < -0.5 * cost_all).mean() * 100), p10=float(np.quantile(draws, .1)),
                   p50=float(np.median(draws)), p90=float(np.quantile(draws, .9)), mean=float(draws.mean()), draws=4000)
    totals = dict(cost=round(cost_all, 2), maxPayout=round(sum(b["payout"] for b in bets if b["status"] != "lost"), 2),
                  expectedValue=round(sum(b["expectedValue"] for b in bets), 2),
                  won=round(sum(b["payout"] for b in bets if b["status"] == "won"), 2))

    hist = json.loads(HISTORY.read_text()) if HISTORY.exists() else {}
    h = hist.setdefault(str(day), [])
    live_g = next((g for g in gl if g["state"] == "in"), None)
    if bets and (state_all == "in" or not h):
        h.append(dict(t=dt.datetime.now(dt.timezone.utc).isoformat(),
                      clock=(f"{live_g['clock']} - {live_g['period']}" if live_g else "Pregame"),
                      p={b["id"]: b["pWin"] for b in bets}))
    HISTORY.write_text(json.dumps(hist))

    injuries = []
    try:
        names = {norm_name(p["name"]): pid for pid, p in players.items()}
        for t in requests.get(f"{ESPN}/injuries", timeout=30).json().get("injuries", []):
            for i in t.get("injuries", []):
                nn = norm_name(i["athlete"]["displayName"])
                if nn in names:
                    injuries.append(dict(player=names[nn], status=i.get("status", ""), detail=i.get("shortComment", "")[:200]))
    except Exception:
        pass

    wk = nfl_week(day - dt.timedelta(days=1) if day.weekday() == 0 else day)
    label = {6: "Sunday", 0: "Monday Night", 3: "Thursday Night"}.get(day.weekday(), day.strftime("%A"))
    state = dict(generatedAt=dt.datetime.now(dt.timezone.utc).isoformat(), slate=dict(date=str(day), label=f"NFL Week {wk} · {label}"),
                 state=state_all, games=gl, injuries=[], playerStats=players, bets=bets, outlook=outlook, totals=totals,
                 injuryReport=injuries, news=[], notes={"players": {}, "news": [], "updated": None}, history=h,
                 pnl=pnl(k, bets))
    js = json.dumps(state, default=float).replace("</", "<\\/")
    OUT.write_text(TEMPLATE.read_text().replace("__STATE__", js))
    print(f"{OUT} · {len(bets)} bets · games {state_all} · {sum(b['status'] == 'won' for b in bets)}W "
          f"{sum(b['status'] == 'lost' for b in bets)}L {sum(b['status'] == 'live' for b in bets)} live")


if __name__ == "__main__":
    main()
