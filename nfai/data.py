"""nflverse data: download, cache, and team/player/defense tables used by the model."""

from pathlib import Path

import numpy as np
import pandas as pd
import requests

DATA = Path(__file__).resolve().parents[1] / "data"
RELEASES = "https://github.com/nflverse/nflverse-data/releases/download"
SEASON = 2026

FILES = {
    "pbp": "pbp/play_by_play_{season}.csv.gz",
    "stats": "stats_player/stats_player_week_{season}.csv",
    "snaps": "snap_counts/snap_counts_{season}.csv",
    "ngs_receiving": "nextgen_stats/ngs_receiving.csv.gz",
    "ngs_rushing": "nextgen_stats/ngs_rushing.csv.gz",
    "depth": "depth_charts/depth_charts_{season}.csv",   # ESPN depth charts, snapshotted twice a day
    "injuries": "injuries/injuries_{season}.csv",        # official weekly report: game status + practice
}

# depth-chart rank that counts as a starter, by position
STARTER_DEPTH = {"QB": 1, "RB": 1, "WR": 3, "TE": 1}

# nflverse team codes that differ from Kalshi's
NFLVERSE_TO_KALSHI = {"LA": "LAR"}
KALSHI_TO_NFLVERSE = {v: k for k, v in NFLVERSE_TO_KALSHI.items()}


def refresh(season=SEASON, names=FILES):
    """Re-download the current season's files (nflverse updates nightly in season)."""
    DATA.mkdir(exist_ok=True)
    for name in names:
        rel = FILES[name].format(season=season)
        r = requests.get(f"{RELEASES}/{rel}", timeout=120)
        r.raise_for_status()
        (DATA / Path(rel).name).write_bytes(r.content)


def _load(name, season=SEASON):
    path = DATA / Path(FILES[name].format(season=season)).name
    if not path.exists():
        refresh(season, [name])
    return pd.read_csv(path, low_memory=False)


SCHEDULE_URL = "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv"


def schedule(max_age_hours=12):
    """The nflverse schedule (data/games.csv), re-downloaded when missing or older than max_age_hours.
    Its spread/total are only a fallback: bets use ESPN's current line (slate.espn_line)."""
    import time
    path = DATA / "games.csv"
    if not path.exists() or time.time() - path.stat().st_mtime > max_age_hours * 3600:
        DATA.mkdir(exist_ok=True)
        r = requests.get(SCHEDULE_URL, timeout=120)
        r.raise_for_status()
        path.write_bytes(r.content)
    return pd.read_csv(path)


def player_weeks(season=SEASON):
    """One row per player-game (regular season), with snap share joined on."""
    s = _load("stats", season)
    s = s[s.season_type == "REG"].copy()
    s["touches"] = s.carries.fillna(0) + s.receptions.fillna(0)
    s["rr_yards"] = s.rushing_yards.fillna(0) + s.receiving_yards.fillna(0)
    try:
        sn = _load("snaps", season)
        sn = sn[sn.game_type == "REG"][["season", "week", "player", "team", "offense_pct"]]
        s = s.merge(sn, left_on=["season", "week", "player_display_name", "team"],
                    right_on=["season", "week", "player", "team"], how="left").drop(columns="player")
    except Exception:
        s["offense_pct"] = np.nan
    return s


def plays(season=SEASON):
    p = _load("pbp", season)
    p = p[(p.season_type == "REG") & p.play_type.isin(["pass", "run"]) & p.posteam.notna()]
    return p


def team_profile(p):
    """Per-team offense/defense rates from play-by-play: volume, pace, pass rate, EPA."""
    g = p.groupby(["posteam", "game_id"])
    per_game = g.agg(plays=("play_id", "size"), dropbacks=("pass", "sum"), rushes=("rush", "sum")).reset_index()
    off = per_game.groupby("posteam").agg(plays=("plays", "mean"), dropbacks=("dropbacks", "mean"),
                                          rushes=("rushes", "mean"), games=("game_id", "nunique"))
    off["pass_oe"] = p.groupby("posteam").pass_oe.mean()
    off["off_epa_pass"] = p[p["pass"] == 1].groupby("posteam").epa.mean()
    off["off_epa_rush"] = p[p["rush"] == 1].groupby("posteam").epa.mean()

    dper = p.groupby(["defteam", "game_id"]).agg(plays=("play_id", "size")).reset_index()
    de = dper.groupby("defteam").agg(plays_allowed=("plays", "mean"))
    de["def_epa_pass"] = p[p["pass"] == 1].groupby("defteam").epa.mean()
    de["def_epa_rush"] = p[p["rush"] == 1].groupby("defteam").epa.mean()
    de["sack_rate"] = p[p["pass"] == 1].groupby("defteam").sack.mean()
    return off.join(de, how="outer")


def defense_vs_position(p, rosters):
    """How each defense performs against targets to WR/TE/RB and against rushes, relative to league,
    shrunk toward 1.0 by sample size. rosters maps player_id -> position."""
    pa = p[(p["pass"] == 1) & p.receiver_player_id.notna() & (p.sack != 1)].copy()
    pa["pos"] = pa.receiver_player_id.map(rosters)
    pa = pa[pa.pos.isin(["WR", "TE", "RB"])]
    pa["yds"] = pa.yards_gained.where(pa.complete_pass == 1, 0)
    lg = pa.groupby("pos").agg(lg_ypt=("yds", "mean"), lg_cr=("complete_pass", "mean"))
    d = pa.groupby(["defteam", "pos"]).agg(n=("yds", "size"), ypt=("yds", "mean"), cr=("complete_pass", "mean")).reset_index()
    d = d.join(lg, on="pos")
    k = 60  # targets of prior weight
    w = d.n / (d.n + k)
    d["ypt_factor"] = w * d.ypt / d.lg_ypt + (1 - w)
    d["cr_factor"] = w * d.cr / d.lg_cr + (1 - w)
    out = {(r.defteam, r.pos): (r.ypt_factor, r.cr_factor) for r in d.itertuples()}

    ru = p[(p["rush"] == 1) & p.rusher_player_id.notna()]
    ru = ru[ru.rusher_player_id.map(rosters) == "RB"]
    lg_ypc = ru.yards_gained.mean()
    dr = ru.groupby("defteam").yards_gained.agg(["size", "mean"])
    w = dr["size"] / (dr["size"] + 120)
    for t, f in (w * dr["mean"] / lg_ypc + (1 - w)).items():
        out[(t, "RUSH")] = (f, 1.0)

    qb = p[(p["pass"] == 1) & (p.sack != 1)]
    lg_ypa = qb.yards_gained.mean()
    dq = qb.groupby("defteam").yards_gained.agg(["size", "mean"])
    w = dq["size"] / (dq["size"] + 150)
    for t, f in (w * dq["mean"] / lg_ypa + (1 - w)).items():
        out[(t, "PASS")] = (f, 1.0)
    return out


def depth_ranks(season=SEASON, lookback_days=14):
    """{gsis_id: (rank_now, rank_then)} for offensive skill players, where rank is the player's
    place at his position on the latest depth chart and on the one from lookback_days earlier
    (None if he wasn't listed)."""
    d = _load("depth", season)
    d = d[d.pos_abb.isin(list(STARTER_DEPTH)) & (d.pos_grp != "Special Teams") & d.gsis_id.notna()]
    d["dt"] = pd.to_datetime(d.dt)

    def ranks(snap):
        best = snap.groupby(["team", "pos_abb", "gsis_id"]).pos_rank.min().reset_index()
        best["rank"] = best.groupby(["team", "pos_abb"]).pos_rank.rank(method="first").astype(int)
        return dict(zip(best.gsis_id, best["rank"]))

    now = d.dt.max()
    earlier = d.dt[d.dt <= now - pd.Timedelta(days=lookback_days)]
    cur = ranks(d[d.dt == now])
    old = ranks(d[d.dt == earlier.max()]) if len(earlier) else {}
    return {pid: (r, old.get(pid)) for pid, r in cur.items()}


def injury_report(season=SEASON, week=None):
    """{gsis_id: (game_status, practice_status)} from the official report for one week, e.g.
    ("", "Limited"). Practice status is the player's last listed practice of the week."""
    if week is None:
        return {}
    try:
        r = _load("injuries", season)
    except Exception:
        return {}
    r = r[(r.week == week) & (r.game_type == "REG") & r.gsis_id.notna()]
    practice = r.practice_status.fillna("").str.extract(r"^(Full|Limited|Did Not)", expand=False).fillna("")
    return dict(zip(r.gsis_id, zip(r.report_status.fillna(""), practice)))


def ngs_receiving(season=SEASON):
    n = _load("ngs_receiving")
    n = n[(n.season == season) & (n.week == 0)]  # week 0 = season-to-date summary
    return n.set_index("player_display_name")[["avg_separation", "avg_yac_above_expectation"]]
