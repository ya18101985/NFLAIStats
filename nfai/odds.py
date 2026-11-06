"""Sportsbook player-prop lines from The Odds API, with the bookmaker margin removed.

Books post one main line per prop (e.g. over/under 64.5 yards); Kalshi has a ladder of
thresholds. book_prob() moves the model's simulated distribution sideways until it agrees with a
book at the book's line, then reads off the probability at the Kalshi threshold."""

import os
import numpy as np
import requests

from .kalshi import KEY_DIR
from .slate import norm_name

API = "https://api.the-odds-api.com/v4/sports/americanfootball_nfl"
KEY_FILE = KEY_DIR / "odds_api_key.txt"
# sharpest first; Pinnacle counts double when averaging
BOOKS = {"pinnacle": 2.0, "draftkings": 1.0, "fanduel": 1.0, "betmgm": 1.0, "williamhill_us": 1.0, "betonlineag": 1.0}
MARKETS = {
    "player_reception_yds": "receiving_yards",
    "player_receptions": "receptions",
    "player_rush_yds": "rushing_yards",
    "player_rush_attempts": "carries",
    "player_rush_reception_yds": "rr_yards",
    "player_pass_yds": "passing_yards",
    "player_pass_attempts": "attempts",
    "player_pass_completions": "completions",
    "player_pass_tds": "passing_tds",
}
TEAM_NAMES = {
    "Arizona Cardinals": "ARI", "Atlanta Falcons": "ATL", "Baltimore Ravens": "BAL", "Buffalo Bills": "BUF",
    "Carolina Panthers": "CAR", "Chicago Bears": "CHI", "Cincinnati Bengals": "CIN", "Cleveland Browns": "CLE",
    "Dallas Cowboys": "DAL", "Denver Broncos": "DEN", "Detroit Lions": "DET", "Green Bay Packers": "GB",
    "Houston Texans": "HOU", "Indianapolis Colts": "IND", "Jacksonville Jaguars": "JAX", "Kansas City Chiefs": "KC",
    "Los Angeles Rams": "LA", "Los Angeles Chargers": "LAC", "Las Vegas Raiders": "LV", "Miami Dolphins": "MIA",
    "Minnesota Vikings": "MIN", "New England Patriots": "NE", "New Orleans Saints": "NO", "New York Giants": "NYG",
    "New York Jets": "NYJ", "Philadelphia Eagles": "PHI", "Pittsburgh Steelers": "PIT", "Seattle Seahawks": "SEA",
    "San Francisco 49ers": "SF", "Tampa Bay Buccaneers": "TB", "Tennessee Titans": "TEN", "Washington Commanders": "WAS",
}


def _key():
    """Cloud: ODDS_API_KEY environment variable; Mac: ~/.kalshi/odds_api_key.txt."""
    return os.environ.get("ODDS_API_KEY") or (KEY_FILE.read_text().strip() if KEY_FILE.exists() else "")


def available():
    return bool(_key())


def _get(path, **params):
    params["apiKey"] = _key()
    r = requests.get(f"{API}{path}", params=params, timeout=30)
    if not r.ok:   # not raise_for_status: its message contains the URL, and so the key
        raise RuntimeError(f"Odds API {path}: HTTP {r.status_code} {r.text[:200]}")
    return r.json()


def event_id(away, home):
    """The Odds API id for a game (nflverse team codes), or None. The events list is free."""
    return next((e["id"] for e in _get("/events")
                 if TEAM_NAMES.get(e["away_team"]) == away and TEAM_NAMES.get(e["home_team"]) == home), None)


def game_lines(away, home):
    """{(normalized player name, stat): [(line, p_over, weight), ...]}, one entry per book, no-vig.
    Empty if the game isn't listed. Costs 9 API requests."""
    eid = event_id(away, home)
    if eid is None:
        return {}
    d = _get(f"/events/{eid}/odds", bookmakers=",".join(BOOKS), markets=",".join(MARKETS), oddsFormat="decimal")
    out = {}
    for b in d.get("bookmakers", []):
        for m in b["markets"]:
            sides = {}
            for o in m["outcomes"]:
                sides.setdefault((o["description"], o.get("point")), {})[o["name"]] = o["price"]
            for (name, point), s in sides.items():
                if point is None or "Over" not in s or "Under" not in s:
                    continue
                over, under = 1 / s["Over"], 1 / s["Under"]
                out.setdefault((norm_name(name), MARKETS[m["key"]]), []).append(
                    (float(point), over / (over + under), BOOKS[b["key"]]))
    return out


def book_prob(lines, draws, thr):
    """Books' probability that the stat goes over thr, weighted across books."""
    ps, ws = [], []
    for line, p_over, w in lines:
        ps.append(p_over if abs(line - thr) < 1e-9 else _tilted_prob(draws, line, p_over, thr))
        ws.append(w)
    return float(np.average(ps, weights=ws))


def _tilted_prob(draws, line, p_over, thr):
    """Reweight the simulations (exponential tilting) until P(over line) matches the book, then
    read P(over thr). Unlike shifting the draws, this works for counts like receptions too."""
    draws = np.asarray(draws, float)
    z = (draws - draws.mean()) / (draws.std() or 1)
    over_line, over_thr = draws > line, draws > thr

    def weights(theta):
        w = np.exp(theta * z - np.max(theta * z))
        return w / w.sum()

    lo, hi = -4.0, 4.0
    for _ in range(40):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if weights(mid) @ over_line < p_over else (lo, mid)
    return float(weights((lo + hi) / 2) @ over_thr)
