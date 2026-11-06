"""Player prop model: project each player's usage and efficiency for one game, then simulate the
game many times so props on the same game share volume and script (needed for parlays).

Projection = team volume (pace, pass rate, game script from the Kalshi spread/total)
           x player share (targets / carries, recent games weighted)
           x player efficiency shrunk toward position norms
           x opponent factor (what this defense allows to the position, from play-by-play)
           x weather.
"""

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import data as D

LEAGUE_TEAM_POINTS = 22.5
LEAGUE_TOTAL = 45.0
SCRAMBLE_RATE = 0.04  # dropbacks that become QB runs rather than attempts
# position priors: catch rate, yards per reception, prior weights (targets, receptions)
REC_PRIOR = {"WR": (0.63, 12.8), "TE": (0.70, 10.5), "RB": (0.77, 7.5)}
# players on the week's injury report who still play: target/carry share multiplier by last practice.
# Picked by backtest (2024-25, weeks 5-18); cutting their efficiency too made predictions worse.
INJURY_USAGE = {"Full": 0.96, "Limited": 0.92, "Did Not": 0.92}


@dataclass
class GameContext:
    team: str            # nflverse code
    opp: str
    spread: float        # points this team is expected to win by (negative = underdog)
    total: float         # expected combined points
    wind_mph: float = 0.0
    dome: bool = False

    @property
    def implied_points(self):
        return (self.total + self.spread) / 2


@dataclass
class Player:
    name: str
    player_id: str
    team: str
    pos: str
    games: int
    tgt_share: float = 0.0
    carry_share: float = 0.0
    catch_rate: float = 0.0
    ypr: float = 0.0
    ypc: float = 0.0
    # QB
    comp_pct: float = 0.0
    ypa: float = 0.0
    td_rate: float = 0.0
    qb_carries: float = 0.0
    practice: str = ""   # last practice status if on this week's injury report
    recent: dict = field(default_factory=dict)


def _wmean(values, recent_n=4, recent_w=0.6):
    v = np.asarray(values, float)
    if len(v) == 0:
        return 0.0
    return recent_w * v[-recent_n:].mean() + (1 - recent_w) * v.mean()


class Model:
    def __init__(self, season=D.SEASON, before_week=None, week=None):
        """before_week: only use games before this week (for backtesting). week: the week being
        bet, for its injury report (defaults to before_week)."""
        self.weeks = D.player_weeks(season)
        self.plays = D.plays(season)
        if before_week:
            self.weeks = self.weeks[self.weeks.week < before_week]
            self.plays = self.plays[self.plays.week < before_week]
        self.roster = dict(zip(self.weeks.player_id, self.weeks.position))
        self.depth = None if before_week else D.depth_ranks(season)   # past depth charts aren't kept by week
        self.injuries = D.injury_report(season, week or before_week)
        self.teams = D.team_profile(self.plays)
        self.dvp = D.defense_vs_position(self.plays, self.roster)
        p = self.plays
        tg = p.groupby(["game_id", "posteam"]).agg(
            team_att=("pass_attempt", "sum"), team_sacks=("sack", "sum"), team_rush=("rush", "sum")).reset_index()
        tg["team_att"] -= tg.team_sacks
        self.team_games = tg.rename(columns={"posteam": "team"})

    # ---- player profiles -------------------------------------------------------------------

    def player(self, name, team):
        w = self.weeks[(self.weeks.player_display_name == name) & (self.weeks.team == team)].sort_values("week")
        if w.empty:
            return None
        w = w.merge(self.team_games, on=["game_id", "team"], how="left")
        pos = w.position.iloc[-1]
        pl = Player(name, w.player_id.iloc[-1], team, pos, games=len(w))
        att, rush = w.team_att.clip(lower=1), w.team_rush.clip(lower=1)
        pl.tgt_share = _wmean(w.targets.fillna(0) / att)
        pl.carry_share = _wmean(w.carries.fillna(0) / rush)
        if pos in REC_PRIOR:
            cr0, ypr0 = REC_PRIOR[pos]
            tg, rec, yds = w.targets.sum(), w.receptions.sum(), w.receiving_yards.sum()
            pl.catch_rate = (rec + 40 * cr0) / (tg + 40)
            pl.ypr = (yds + 25 * ypr0) / (rec + 25)
        car, ryd = w.carries.sum(), w.rushing_yards.sum()
        ypc0 = 4.3 if pos == "RB" else 4.0
        pl.ypc = (ryd + 60 * ypc0) / (car + 60)
        if pos == "QB":
            a, c, y, td = w.attempts.sum(), w.completions.sum(), w.passing_yards.sum(), w.passing_tds.sum()
            pl.comp_pct = (c + 100 * 0.65) / (a + 100)
            pl.ypa = (y + 150 * 6.9) / (a + 150)
            pl.td_rate = (td + 200 * 0.045) / (a + 200)
            pl.qb_carries = _wmean(w.carries.fillna(0))
        pl.practice = self.injuries.get(pl.player_id, ("", ""))[1]
        pl.tgt_share *= INJURY_USAGE.get(pl.practice, 1.0)
        pl.carry_share *= INJURY_USAGE.get(pl.practice, 1.0)
        last4 = w.tail(4)
        pl.recent = {
            "targets": last4.targets.mean(), "touches": last4.touches.mean(),
            "attempts": last4.attempts.mean(), "snap_pct": last4.offense_pct.mean(),
        }
        return pl

    def eligible(self, pl):
        """The user's 'Standard' rule over the last 4 games, plus enough games to trust the data."""
        if pl is None or pl.games < 3 or self.depth_problem(pl):
            return False
        r = pl.recent
        if pl.pos == "QB":
            return r["attempts"] >= 20
        if pl.pos == "RB":
            return r["touches"] >= 12
        if pl.pos in ("WR", "TE"):
            return r["targets"] >= 5 and (np.isnan(r["snap_pct"]) or r["snap_pct"] >= 0.5)
        return False

    def depth_problem(self, pl):
        """Why the depth chart says our recent-games data no longer fits this player, or None.
        A role change either way (demoted, or promoted into a bigger role) makes his last
        four games a poor guide."""
        if self.depth is None or pl.pos not in D.STARTER_DEPTH:
            return None
        if pl.player_id not in self.depth:
            return "not on the current depth chart"
        now, then = self.depth[pl.player_id]
        top = D.STARTER_DEPTH[pl.pos]
        if pl.pos == "QB" and now != 1:
            return f"not the starting QB (QB{now})"
        if (now <= top) != (then is not None and then <= top):
            return f"role changed: {pl.pos}{then or '-'} two weeks ago, {pl.pos}{now} now"
        return None

    # ---- team volume -----------------------------------------------------------------------

    def team_volume(self, ctx):
        o, d = self.teams.loc[ctx.team], self.teams.loc[ctx.opp]
        plays = 0.5 * o.plays + 0.5 * d.plays_allowed
        plays *= np.sqrt(ctx.total / LEAGUE_TOTAL)
        pass_rate = o.dropbacks / o.plays
        pass_rate += np.clip(-0.007 * ctx.spread, -0.08, 0.08)   # trailing teams throw more
        sack = 0.5 * d.sack_rate + 0.5 * 0.065
        return plays, pass_rate, sack

    # ---- simulation ------------------------------------------------------------------------

    def simulate(self, ctx, players, n=20000, seed=0):
        """Joint draws for every stat of every player on one team in one game.
        Returns {player_name: {stat: np.array}}."""
        rng = np.random.default_rng(seed)
        plays, pass_rate, sack = self.team_volume(ctx)
        g = rng.lognormal(0, 0.11, n)                         # pace / game length
        s = np.clip(pass_rate + rng.normal(0, 0.06, n), 0.3, 0.8)  # game script
        dropbacks = rng.poisson(plays * g * s)
        att = rng.binomial(dropbacks, 1 - sack - SCRAMBLE_RATE)
        rushes = rng.poisson(np.maximum(plays * g * (1 - s), 1))
        pts_factor = ctx.implied_points / LEAGUE_TEAM_POINTS
        wind = 1.0 if ctx.dome else 1 - 0.006 * max(0.0, ctx.wind_mph - 10)

        out = {}
        for pl in players:
            st = {}
            if pl.pos in REC_PRIOR and pl.tgt_share > 0:
                ypt_f, cr_f = self.dvp.get((ctx.opp, pl.pos), (1.0, 1.0))
                share = rng.beta(pl.tgt_share * 30 + 0.1, (1 - pl.tgt_share) * 30 + 0.1, n)
                tg = rng.binomial(att, share)
                cr = np.clip(pl.catch_rate * cr_f, 0.3, 0.95)
                rec = rng.binomial(tg, cr)
                ypr = pl.ypr * (ypt_f / cr_f) * wind
                k = 1.3
                yds = np.where(rec > 0, rng.gamma(np.maximum(rec, 1) * k, ypr / k), 0.0)
                st.update(targets=tg, receptions=rec, receiving_yards=yds)
            if pl.pos in ("RB", "QB") or pl.carry_share > 0.05:
                rush_f = self.dvp.get((ctx.opp, "RUSH"), (1.0, 1.0))[0]
                if pl.pos == "QB":
                    car = rng.poisson(np.full(n, max(pl.qb_carries, 0.3)) * g)
                else:
                    share = rng.beta(pl.carry_share * 22 + 0.1, (1 - pl.carry_share) * 22 + 0.1, n)
                    car = rng.binomial(rushes, share)
                ypc = pl.ypc * rush_f
                p_long, long_mean = 0.025, 22.0
                nlong = rng.binomial(car, p_long)
                base = car * (ypc - p_long * long_mean) + np.sqrt(car) * 5.0 * rng.standard_normal(n)
                yds = base + np.where(nlong > 0, rng.gamma(np.maximum(nlong, 1), long_mean), 0.0)
                st.update(carries=car, rushing_yards=yds)
            if pl.pos == "QB":
                ypa_f = self.dvp.get((ctx.opp, "PASS"), (1.0, 1.0))[0]
                comp = rng.binomial(att, pl.comp_pct)
                ypc_comp = pl.ypa / pl.comp_pct * ypa_f * wind
                k = 2.0
                pyds = np.where(comp > 0, rng.gamma(np.maximum(comp, 1) * k, ypc_comp / k), 0.0)
                ptd = rng.poisson(att * pl.td_rate * pts_factor)
                st.update(attempts=att, completions=comp, passing_yards=pyds, passing_tds=ptd)
            if "rushing_yards" in st and "receiving_yards" in st:
                st["rr_yards"] = st["rushing_yards"] + st["receiving_yards"]
            out[pl.name] = st
        return out
