"""Everything about one slate of games at bet time: kickoffs, lines, injuries, weather, and the
Kalshi prop markets matched to nflverse players."""

import datetime as dt
import re
import unicodedata
from dataclasses import dataclass, field

import pandas as pd
import requests

from . import data as D

ESPN = "https://site.api.espn.com/apis/site/v2/sports/football/nfl"

# Kalshi team codes -> nflverse
K2N = {"JAC": "JAX", "LAR": "LA"}
N2K = {v: k for k, v in K2N.items()}
ESPN2N = {"WSH": "WAS", "LAR": "LA"}

PROP_SERIES = {
    "KXNFLRECYDS": "receiving_yards",
    "KXNFLREC": "receptions",
    "KXNFLRSHYDS": "rushing_yards",
    "KXNFLRSHATT": "carries",
    "KXNFLRRYDS": "rr_yards",
    "KXNFLPASSYDS": "passing_yards",
    "KXNFLPASSATT": "attempts",
    "KXNFLPASSCOMP": "completions",
    "KXNFLPASSTDS": "passing_tds",
}
SKIP_STATUS = {"Out", "Doubtful", "Questionable", "Injured Reserve", "Suspension", "Physically Unable to Perform"}

# home stadium coordinates for weather lookups (outdoor and open-air venues)
STADIUMS = {
    "ARI": (33.528, -112.263), "ATL": (33.755, -84.401), "BAL": (39.278, -76.623), "BUF": (42.774, -78.787),
    "CAR": (35.226, -80.853), "CHI": (41.862, -87.617), "CIN": (39.095, -84.516), "CLE": (41.506, -81.700),
    "DAL": (32.748, -97.093), "DEN": (39.744, -105.020), "DET": (42.340, -83.046), "GB": (44.501, -88.062),
    "HOU": (29.685, -95.411), "IND": (39.760, -86.164), "JAX": (30.324, -81.637), "KC": (39.049, -94.484),
    "LA": (33.953, -118.339), "LAC": (33.953, -118.339), "LV": (36.091, -115.184), "MIA": (25.958, -80.239),
    "MIN": (44.974, -93.258), "NE": (42.091, -71.264), "NO": (29.951, -90.081), "NYG": (40.814, -74.074),
    "NYJ": (40.814, -74.074), "PHI": (39.901, -75.168), "PIT": (40.447, -80.016), "SEA": (47.595, -122.332),
    "SF": (37.403, -121.970), "TB": (27.976, -82.503), "TEN": (36.166, -86.771), "WAS": (38.908, -76.864),
}


def norm_name(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[.'’,-]", " ", s)
    s = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b", " ", s)
    return " ".join(s.split())


@dataclass
class Game:
    code: str               # Kalshi event code, e.g. 26NOV01MINDET
    away: str               # nflverse codes
    home: str
    kickoff: dt.datetime    # UTC
    espn_id: str = ""
    week: int = None        # NFL week, for that week's injury report
    spread_home: float = 0.0    # points home expected to win by
    total: float = 45.0
    dome: bool = False
    wind_mph: float = 0.0
    line_live: bool = False     # spread/total from today's market (ESPN), not the schedule file
    out_players: set = field(default_factory=set)   # normalized names to skip
    ruled_out: set = field(default_factory=set)     # Out/Doubtful only: usage gets redistributed


def _split_code(code, teams):
    pair = code[7:]
    for i in range(2, 4):
        a, h = pair[:i], pair[i:]
        if a in teams and h in teams:
            return a, h
    raise ValueError(f"cannot split {code}")


def games_on(date, kalshi):
    """All NFL games on a calendar date (Eastern), with lines, roof, weather and injuries."""
    kteams = {N2K.get(t, t) for t in D.NFLVERSE_TO_KALSHI} | set(STADIUMS) | set(N2K.values()) | {"JAC", "LAR"}
    tag = date.strftime("%y%b%d").upper()
    evs = {m["event_ticker"].split("-")[1] for m in kalshi.markets(series_ticker="KXNFLGAME", status="open")}
    sched = pd.read_csv(D.DATA / "games.csv")
    sched = sched[sched.season == D.SEASON]
    espn = requests.get(f"{ESPN}/scoreboard", params={"dates": date.strftime("%Y%m%d")}, timeout=30).json()["events"]
    out = []
    for code in sorted(e for e in evs if e.startswith(tag)):
        ka, kh = _split_code(code, kteams)
        a, h = K2N.get(ka, ka), K2N.get(kh, kh)
        g = Game(code, a, h, kickoff=None)
        row = sched[(sched.away_team == a) & (sched.home_team == h)].tail(1)
        if len(row):
            r = row.iloc[0]
            g.spread_home, g.total = float(r.spread_line), float(r.total_line)
            g.dome = r.roof in ("dome", "closed")
            g.week = int(r.week)
        for e in espn:
            abbrs = {ESPN2N.get(c["team"]["abbreviation"], c["team"]["abbreviation"]) for c in e["competitions"][0]["competitors"]}
            if abbrs == {a, h}:
                g.espn_id = e["id"]
                g.kickoff = dt.datetime.fromisoformat(e["date"].replace("Z", "+00:00"))
                g.dome = g.dome or bool(e["competitions"][0]["venue"].get("indoor"))
                line = espn_line(e)   # ESPN's current line beats the schedule file's, set days earlier
                if line:
                    (g.spread_home, g.total), g.line_live = line, True
        out.append(g)
    refresh_injuries(out)
    for g in out:
        if not g.dome and g.kickoff:
            g.wind_mph = wind_at(g.home, g.kickoff)
    return out


def espn_line(event):
    """(home spread, total) from an ESPN scoreboard event's current odds, or None."""
    odds = (event["competitions"][0].get("odds") or [{}])[0]
    if odds.get("spread") is None or not odds.get("overUnder"):
        return None
    home_fav = odds.get("homeTeamOdds", {}).get("favorite")
    return abs(float(odds["spread"])) * (1 if home_fav else -1), float(odds["overUnder"])


def espn_lines(date):
    """{(away, home): (home spread, total)} for every game on a date, nflverse codes."""
    evs = requests.get(f"{ESPN}/scoreboard", params={"dates": date.strftime("%Y%m%d")}, timeout=30).json()["events"]
    out = {}
    for e in evs:
        side = {c["homeAway"]: ESPN2N.get(c["team"]["abbreviation"], c["team"]["abbreviation"])
                for c in e["competitions"][0]["competitors"]}
        line = espn_line(e)
        if line:
            out[(side["away"], side["home"])] = line
    return out


def refresh_injuries(games):
    """League-wide injury report (the per-game summary truncates to 5 players a team)."""
    d = requests.get(f"{ESPN}/injuries", timeout=30).json()
    skip, out = {}, {}
    for t in d.get("injuries", []):
        for i in t.get("injuries", []):
            abbr = i["athlete"].get("team", {}).get("abbreviation", "")
            team, name = ESPN2N.get(abbr, abbr), norm_name(i["athlete"]["displayName"])
            if i.get("status") in SKIP_STATUS:
                skip.setdefault(team, set()).add(name)
            if i.get("status") in ("Out", "Doubtful"):
                out.setdefault(team, set()).add(name)
    for g in games:
        g.out_players = skip.get(g.away, set()) | skip.get(g.home, set())
        g.ruled_out = out.get(g.away, set()) | out.get(g.home, set())


def wind_at(team, kickoff):
    lat, lon = STADIUMS[team]
    try:
        r = requests.get("https://api.open-meteo.com/v1/forecast", timeout=30, params={
            "latitude": lat, "longitude": lon, "hourly": "wind_speed_10m", "wind_speed_unit": "mph",
            "timezone": "UTC", "forecast_days": 7}).json()
        hours = [dt.datetime.fromisoformat(t).replace(tzinfo=dt.timezone.utc) for t in r["hourly"]["time"]]
        i = min(range(len(hours)), key=lambda j: abs((hours[j] - kickoff).total_seconds()))
        return float(r["hourly"]["wind_speed_10m"][i])
    except Exception:
        return 0.0


def prop_markets(g, kalshi):
    """Open prop markets for a game, one row per threshold, with the player's team in nflverse codes."""
    rows = []
    for series, stat in PROP_SERIES.items():
        for m in kalshi.markets(event_ticker=f"{series}-{g.code}", status="open"):
            if m.get("floor_strike") is None or ":" not in m["title"]:
                continue
            seg = m["ticker"].split("-")[2]
            kteam = next((t for t in (N2K.get(g.away, g.away), N2K.get(g.home, g.home)) if seg.startswith(t)), None)
            if not kteam:
                continue
            f = lambda k: float(m.get(k) or 0)
            rows.append(dict(
                ticker=m["ticker"], event_ticker=m["event_ticker"], series=series, stat=stat,
                name=m["title"].split(":")[0].strip(), team=K2N.get(kteam, kteam), thr=float(m["floor_strike"]),
                yes_bid=f("yes_bid_dollars"), yes_ask=f("yes_ask_dollars"),
                no_bid=f("no_bid_dollars"), no_ask=f("no_ask_dollars"),
                yes_ask_size=f("yes_ask_size_fp"), no_ask_size=f("yes_bid_size_fp"), volume=f("volume_fp")))
    return pd.DataFrame(rows)
