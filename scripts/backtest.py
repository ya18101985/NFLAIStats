"""Walk-forward calibration check: for each week, fit on earlier weeks only, then compare predicted
probabilities for common prop thresholds with what actually happened.

Usage: backtest.py [start_week] [end_week] [season ...]   e.g. backtest.py 5 18 2024 2025"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nfai import data as D  # noqa: E402
from nfai.model import GameContext, Model  # noqa: E402

THRESHOLDS = {
    "receptions": [2, 3, 4, 5, 6, 7],
    "receiving_yards": [20, 30, 40, 50, 60, 70, 80, 90],
    "rushing_yards": [30, 40, 50, 60, 70, 80, 90],
    "carries": [10, 12, 15, 18],
    "passing_yards": [175, 200, 225, 250, 275, 300],
    "attempts": [28, 31, 34, 37, 40],
    "completions": [18, 20, 22, 24, 26],
    "passing_tds": [1, 2, 3],
}

all_games = D.schedule()
start, end = int(sys.argv[1]) if len(sys.argv) > 1 else 5, int(sys.argv[2]) if len(sys.argv) > 2 else 8
seasons = [int(a) for a in sys.argv[3:]] or [D.SEASON]
rows = []
for season, week in ((s, w) for s in seasons for w in range(start, end + 1)):
    games = all_games[(all_games.season == season) & (all_games.game_type == "REG") & (all_games.week == week)]
    if games.empty or games.result.isna().all():
        continue
    m = Model(season, before_week=week)
    actual = D.player_weeks(season)
    actual = actual[actual.week == week]
    for g in games.itertuples():
        for team, opp, sign in ((g.home_team, g.away_team, 1), (g.away_team, g.home_team, -1)):
            ctx = GameContext(team, opp, spread=sign * g.spread_line, total=g.total_line, dome=g.roof in ("dome", "closed"))
            players = [m.player(n, team) for n in actual[actual.team == team].player_display_name]
            players = [p for p in players if m.eligible(p)]
            sims = m.simulate(ctx, players, n=4000, seed=week)
            for pl in players:
                a = actual[(actual.player_display_name == pl.name) & (actual.team == team)].iloc[0]
                for stat, draws in sims[pl.name].items():
                    for t in THRESHOLDS.get(stat, []):
                        rows.append((season, week, pl.pos, pl.practice or "-", stat, t, (draws >= t).mean(), float(a[stat] >= t)))

r = pd.DataFrame(rows, columns=["season", "week", "pos", "practice", "stat", "thr", "p", "hit"])
r = r[(r.p > 0.05) & (r.p < 0.95)]
r["bucket"] = (r.p * 10).astype(int) / 10
print(f"{len(r)} predictions, weeks {start}-{end} of {seasons}")
print(f"Overall Brier {((r.p - r.hit) ** 2).mean():.4f}, "
      f"log loss {-(r.hit * np.log(r.p) + (1 - r.hit) * np.log(1 - r.p)).mean():.4f} (lower is better)")
print("\nBy last practice (players on the injury report who played):")
print(r.groupby("practice").apply(lambda d: pd.Series({
    "n": len(d), "pred": d.p.mean(), "actual": d.hit.mean(), "brier": ((d.p - d.hit) ** 2).mean()})).round(4).to_string())
print("\nCalibration (predicted vs actual hit rate):")
print(r.groupby("bucket").agg(n=("hit", "size"), predicted=("p", "mean"), actual=("hit", "mean")).round(3).to_string())
print("\nBy stat (mean predicted vs actual, Brier vs coin-flip-at-base-rate):")
s = r.groupby("stat").apply(lambda d: pd.Series({
    "n": len(d), "pred": d.p.mean(), "actual": d.hit.mean(),
    "brier": ((d.p - d.hit) ** 2).mean(), "brier_base": ((d.hit.mean() - d.hit) ** 2).mean()}))
print(s.round(3).to_string())

# Logistic recalibration fit: logit(p_cal) = a + b * logit(p)
from scipy.optimize import minimize  # noqa: E402

lp = np.log(r.p / (1 - r.p))


def nll(ab):
    q = 1 / (1 + np.exp(-(ab[0] + ab[1] * lp)))
    return -np.mean(r.hit * np.log(q) + (1 - r.hit) * np.log(1 - q))


fit = minimize(nll, [0.0, 1.0]).x
print(f"\nRecalibration: a={fit[0]:.3f} b={fit[1]:.3f}")
