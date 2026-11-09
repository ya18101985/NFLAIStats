"""Turn model probabilities and Kalshi prices into sized single bets and parlay candidates,
following the user's rules (see CLAUDE.md)."""

import itertools
import zlib
from dataclasses import dataclass

import numpy as np
import pandas as pd

from . import odds
from .model import GameContext
from .slate import norm_name

# recalibration fit by scripts/backtest.py on weeks 5-18 of 2024-25 and 5-8 of 2026 (59,625
# predictions): logit(p') = A + B * logit(p)
CAL_A, CAL_B = -0.049, 0.909
MODEL_WEIGHT = 0.4          # blend with the market price; the model is new and unproven
# when sportsbooks price the prop: they are the sharpest opinion, Kalshi next, our model last
BOOK_BLEND = {"book": 0.5, "kalshi": 0.3, "model": 0.2}
MAX_DISAGREEMENT = 0.25     # skip a price when model and market differ by more (we are likely missing news)
FEE_RATE = 0.07             # Kalshi taker fee = 0.07 * price * (1 - price) per contract

MIN_PROB_SINGLE, MAX_PROB_SINGLE = 0.45, 0.65   # user's range for singles: no long shots, no tiny-payout favourites
MIN_EV = 0.03               # dollars per contract after fees
MIN_EV_RATIO = 0.06         # EV as a share of price
# Singles come from two groups that split the singles budget (user's hybrid, 2026-10-31):
#   A "books back it": sportsbooks and model both say Kalshi is cheap, by at least BOOK_MIN_EV
#   B "model's call": model-only EV >= MODEL_MIN_EV, and the books are at most BOOK_TOLERANCE against it
BOOK_MIN_EV = 0.005
BOOK_TOLERANCE = 0.02
MODEL_MIN_EV = 0.02
GROUP_A_SHARE = 0.5
# Full allocation (user's rule): if a wave can't spend its budget within the 15% cap, lower the
# edge bars step by step, (group A min EV, group B min model-only EV). Never below break-even.
RELAX_STEPS = [(BOOK_MIN_EV, MODEL_MIN_EV), (0.0, 0.01), (0.0, 0.0)]
# single-game waves only (user's choice, 2026-10-31): filler bets down to 1c below break-even
ONE_GAME_FLOOR = -0.01
# 100% allocation of every slate (user's choice, 2026-11-05): if the steps above still leave money
# unspent, keep adding the closest-to-break-even bets, down to 3c below, any wave
FULL_ALLOCATION_FLOORS = [-0.02, -0.03]
# group C "likely winner" (user's choice, 2026-11-08): fills what A and B leave, before any
# below-break-even filler. Needs the books to agree and a 1c edge after fees as a cushion.
LIKELY_MIN_PROB, LIKELY_MAX_PROB = 0.58, 0.72
LIKELY_MIN_BOOK = 0.55
LIKELY_MIN_EV = 0.01
MAX_SPREAD = 0.05
MIN_STAKE, MAX_STAKE = 0.05, 0.15   # of the wave bankroll, per single
SINGLES_SHARE = 0.80
MAX_BETS_PER_PLAYER = 2

# parlays (user's choice, 2026-11-08): 20% of every wave, 2-3 legs, each leg 60-75% to win, never skipped
# Parlays off (user's post-mortem, 2026-11-08): 21 settled parlays returned -51% (-$1,382), mostly bought
# above our own odds. 0 sends the whole wave to singles; set back to 0.20 to turn them on again.
PARLAY_SHARE = 0.0
PARLAY_LEG_MIN_PROB, PARLAY_LEG_MAX_PROB = 0.60, 0.75
PARLAY_SIZES = (2, 3)
PARLAYS_PER_WAVE = 4
PARLAY_MIN_EDGE = 0.15      # a quote this far below our joint probability is taken at once; else the best quote

# how far our median may sit from the market's before we distrust our data for that prop
SANITY_ABS = {"receptions": 1.0, "carries": 3, "attempts": 4, "completions": 3, "passing_tds": 0.6}
SANITY_REL = 0.25


def fee(price):
    return FEE_RATE * price * (1 - price)


def calibrate(p):
    p = np.clip(p, 1e-4, 1 - 1e-4)
    return 1 / (1 + np.exp(-(CAL_A + CAL_B * np.log(p / (1 - p)))))


def market_median(ladder):
    """Threshold where the market's YES mid crosses 50%, by linear interpolation."""
    lad = ladder.sort_values("thr")
    mid = ((lad.yes_bid + lad.yes_ask) / 2).to_numpy()
    thr = lad.thr.to_numpy()
    for i in range(len(thr) - 1):
        if mid[i] >= 0.5 >= mid[i + 1]:
            w = (mid[i] - 0.5) / max(mid[i] - mid[i + 1], 1e-9)
            return thr[i] + w * (thr[i + 1] - thr[i])
    return None


@dataclass
class Candidate:
    ticker: str
    event_ticker: str
    title: str
    player: str
    team: str
    stat: str
    outcome: str          # yes / no
    price: float          # ask for that outcome
    p_model: float
    p: float              # blended probability of winning
    ev: float
    ask_size: float
    hit: np.ndarray       # per-simulation win indicator (for parlay joint probabilities)
    game: str = ""
    p_book: float = None  # sportsbooks' no-vig probability, if they price this prop
    ev_model: float = 0.0 # EV using model + Kalshi only (ignoring the books)
    group: str = ""       # A or B once selected


def game_candidates(model, g, props, book=None):
    """All priced sides for one game's props, with model and blended probabilities.
    book: odds.game_lines() for the game, or None."""
    book = book or {}
    out = []
    flags = []
    if not g.line_live:   # the model's game script hinges on the line; never bet props on a stale one
        return out, [(g.code, "no current line", "props skipped: ESPN has no spread/total for this game")]
    for team, opp, spread in ((g.home, g.away, g.spread_home), (g.away, g.home, -g.spread_home)):
        tp = props[props.team == team]
        if tp.empty:
            continue
        names = {n: norm_name(n) for n in tp.name.unique()}
        nfl_names = {norm_name(n): n for n in model.weeks[model.weeks.team == team].player_display_name.dropna().unique()}
        players = {}
        for kname, nn in names.items():
            if nn in g.out_players or nn not in nfl_names:
                continue
            pl = model.player(nfl_names[nn], team)
            why = pl and model.depth_problem(pl)
            if why:
                flags.append((kname, "depth chart", why))
            elif model.eligible(pl):
                players[kname] = pl
        if not players:
            continue
        _redistribute_injured_usage(model, team, g, list(players.values()))
        ctx = GameContext(team, opp, spread=spread, total=g.total, wind_mph=g.wind_mph, dome=g.dome)
        sims = model.simulate(ctx, list(players.values()), n=20000, seed=zlib.crc32((g.code + team).encode()))
        for (kname, stat), ladder in tp[tp.name.isin(players)].groupby(["name", "stat"]):
            draws = sims[players[kname].name].get(stat)
            if draws is None:
                continue
            ours, theirs = float(np.median(draws)), market_median(ladder)
            if theirs is not None:
                tol = SANITY_ABS[stat] if stat in SANITY_ABS else max(SANITY_REL * theirs, 12)
                if abs(ours - theirs) > tol:
                    flags.append((kname, stat, round(ours, 1), round(theirs, 1)))
                    continue
            for m in ladder.itertuples():
                if not (0 < m.yes_bid < m.yes_ask < 1) or m.yes_ask - m.yes_bid > MAX_SPREAD:
                    continue
                hit_yes = draws > m.thr
                p_model = float(calibrate(hit_yes.mean()))
                mid = (m.yes_bid + m.yes_ask) / 2
                if abs(p_model - mid) > MAX_DISAGREEMENT:
                    continue
                lines = book.get((norm_name(kname), stat))
                if lines:
                    p_book = odds.book_prob(lines, draws, m.thr)
                    if abs(p_book - mid) > MAX_DISAGREEMENT:   # books and Kalshi on different pages: likely a data mismatch
                        continue
                    w = BOOK_BLEND
                    p_yes = w["book"] * p_book + w["kalshi"] * mid + w["model"] * p_model
                else:
                    p_book = None
                    p_yes = MODEL_WEIGHT * p_model + (1 - MODEL_WEIGHT) * mid
                p_yes_model = MODEL_WEIGHT * p_model + (1 - MODEL_WEIGHT) * mid
                for outcome, price, p, pm, pb, pym, size, hit in (
                        ("yes", m.yes_ask, p_yes, p_model, p_book, p_yes_model, m.yes_ask_size, hit_yes),
                        ("no", m.no_ask, 1 - p_yes, 1 - p_model, None if p_book is None else 1 - p_book, 1 - p_yes_model,
                         m.no_ask_size, ~hit_yes)):
                    out.append(Candidate(m.ticker, m.event_ticker, f"{kname} {stat} {'over' if outcome == 'yes' else 'under'} {m.thr}",
                                         kname, team, stat, outcome, price, pm, p, p - price - fee(price), size, hit, g.code, pb,
                                         pym - price - fee(price)))
    return out, flags


def _redistribute_injured_usage(model, team, g, players):
    """If a teammate ruled Out/Doubtful was part of the offense in the team's last two games (so his
    absence isn't already in our recent-games data), give our players part of his share. Only
    half is handed out, since players we don't model (backups, other positions) absorb some."""
    tw = model.weeks[model.weeks.team == team]
    last2 = sorted(tw.week.unique())[-2:]
    lost_t = lost_c = 0.0
    for name in tw[tw.week.isin(last2)].player_display_name.dropna().unique():
        if norm_name(name) in g.ruled_out:
            pl = model.player(name, team)
            if pl:
                lost_t += pl.tgt_share
                lost_c += pl.carry_share
    have_t = sum(p.tgt_share for p in players) or 1
    have_c = sum(p.carry_share for p in players) or 1
    for pl in players:
        pl.tgt_share *= min(1 + 0.5 * lost_t / have_t, 1.15)
        pl.carry_share *= min(1 + 0.5 * lost_c / have_c, 1.15)


def _group_a(c, min_ev=BOOK_MIN_EV):
    """Sportsbooks price the prop, and both they and our model say Kalshi is cheap."""
    slack = min(min_ev, 0.0)
    return (c.p_book is not None and c.p_book >= c.price + fee(c.price) + slack and c.p_model >= c.price + slack
            and c.ev >= min_ev and MIN_PROB_SINGLE <= c.p <= MAX_PROB_SINGLE)


def _group_b(c, min_ev=MODEL_MIN_EV):
    """Our model (blended with Kalshi) sees a real edge and the books aren't clearly against it."""
    p_model_blend = c.price + fee(c.price) + c.ev_model
    return (c.ev_model >= min_ev and c.ev_model / c.price >= MIN_EV_RATIO / 1.5 * min_ev / MODEL_MIN_EV
            and c.ev >= min(min_ev, 0.0) and MIN_PROB_SINGLE <= p_model_blend <= MAX_PROB_SINGLE
            and MIN_PROB_SINGLE <= c.p <= MAX_PROB_SINGLE
            and (c.p_book is None or c.p_book >= c.price + fee(c.price) - BOOK_TOLERANCE))


def select_singles(cands, bankroll, budget=None, one_game=False):
    """Pick singles with the normal edge bars; if they can't use the budget (default 80% of the
    bankroll) within the 15% cap, retry with the lower bars in RELAX_STEPS (plus the
    below-break-even ONE_GAME_FLOOR step in single-game waves)."""
    budget = SINGLES_SHARE * bankroll if budget is None else budget
    # no below-break-even filler (user's choice, 2026-11-08): negative-edge singles returned -22%;
    # stakes still scale up (15% cap each) to use as much of the budget as positive-edge bets allow
    steps = RELAX_STEPS
    for i, (min_a, min_b) in enumerate(steps):
        out = _select(cands, bankroll, budget, min_a, min_b, MAX_STAKE)
        spent = sum(n * (c.price + fee(c.price)) for c, n, _ in out)
        if spent >= 0.99 * budget or i == len(steps) - 1:
            return out


def _group_c(c):
    """Likely winner: model and blend both put it 58-72% to win, the books agree (55%+), and it
    still clears a 1c edge after fees, as a cushion against the model being over-optimistic."""
    return (c.p_book is not None and c.p_book >= LIKELY_MIN_BOOK and c.ev >= LIKELY_MIN_EV
            and LIKELY_MIN_PROB <= c.p <= LIKELY_MAX_PROB and LIKELY_MIN_PROB <= c.p_model <= LIKELY_MAX_PROB)


def _select(cands, bankroll, budget, min_a, min_b, max_stake):
    """Singles in three stages, each only spending what the earlier ones couldn't:
    1. group A (books back it) and group B (model's call) at the step's edge bars, but never below
       break-even, splitting the budget GROUP_A_SHARE / rest (or all to one group if the other is empty);
    2. group C (likely winners), user's choice 2026-11-08;
    3. only on below-break-even steps: A and B filler bets down to the step's bars.
    Within a stage: greedy by EV, one side per player-stat, at most 2 bets a player overall, each
    5-15% of the bankroll by half-Kelly; then stakes are scaled up together (still capped)."""
    for c in cands:
        c.group = ""
    cap, picked, seen, per_player = max_stake * bankroll, [], set(), {}

    def spent():
        return sum(s for _, s in picked)

    def add(pool, g, budget_g):
        left, mine = budget_g, []
        for c in pool:
            key = (c.player, c.stat)
            if c.group or key in seen or per_player.get(c.player, 0) >= MAX_BETS_PER_PLAYER:
                continue
            cost = c.price + fee(c.price)
            p = cost + c.ev_model if g == "B" else c.p
            kelly = (p - cost) / (1 - cost)
            stake = min(float(np.clip(0.5 * kelly, MIN_STAKE, max_stake)) * bankroll, left)
            if stake < MIN_STAKE * bankroll:
                break
            c.group = g
            mine.append([c, stake])
            left -= stake
            seen.add(key)
            per_player[c.player] = per_player.get(c.player, 0) + 1
        _scale_to(mine, budget_g, cap)
        picked.extend(mine)

    def add_ab(bar_a, bar_b):
        pool_a = sorted([c for c in cands if not c.group and _group_a(c, bar_a)], key=lambda c: -c.ev)
        in_a = {id(c) for c in pool_a}
        pool_b = sorted([c for c in cands if not c.group and id(c) not in in_a and _group_b(c, bar_b)],
                        key=lambda c: -c.ev_model)
        left = budget - spent()
        share_a = GROUP_A_SHARE if pool_a and pool_b else (1.0 if pool_a else 0.0)
        add(pool_a, "A", share_a * left)
        add(pool_b, "B", budget - spent())
        _scale_to(picked, budget, cap)   # one group capped out: let the other absorb the rest

    add_ab(max(min_a, 0.0), max(min_b, 0.0))
    if spent() < budget - 1:
        add(sorted([c for c in cands if not c.group and _group_c(c)], key=lambda c: -c.ev), "C", budget - spent())
        _scale_to(picked, budget, cap)
    if min(min_a, min_b) < 0 and spent() < budget - 1:
        add_ab(min_a, min_b)
    out = []
    for c, stake in picked:
        contracts = int(stake / (c.price + fee(c.price)))
        if contracts >= 1:
            out.append((c, contracts, round(contracts * c.price, 2)))
    return out


def _scale_to(picked, budget, cap):
    """Scale [candidate, stake] pairs up by one common factor, each capped, until they sum to budget."""
    if not picked or sum(min(cap, s) for _, s in picked) >= budget - 1:
        return
    lo, hi = 1.0, cap / min(s for _, s in picked)
    for _ in range(50):
        f = (lo + hi) / 2
        lo, hi = (f, hi) if sum(min(cap, s * f) for _, s in picked) < budget else (lo, f)
    for p in picked:
        p[1] = min(cap, p[1] * lo)


def parlay_candidates(cands, k=PARLAYS_PER_WAVE):
    """Best 2-3 leg parlays from legs 60-75% to win (non-negative EV preferred, any EV if that leaves
    too few legs), one leg per Kalshi event (the combo rules allow one market per series per game).
    Joint probability comes from shared simulations for legs on the same team; legs on different
    teams are multiplied."""
    legs = [c for c in cands if c.hit is not None and PARLAY_LEG_MIN_PROB <= c.p <= PARLAY_LEG_MAX_PROB]   # props only
    if len({c.event_ticker for c in legs if c.ev >= 0}) >= max(PARLAY_SIZES):
        legs = [c for c in legs if c.ev >= 0]
    best = {}
    for c in sorted(legs, key=lambda c: -c.ev):
        best.setdefault((c.event_ticker, c.player), c)
    pool = sorted({c.event_ticker: c for c in best.values()}.values(), key=lambda c: -c.ev)[:14]
    found = []
    for size in PARLAY_SIZES:
        for combo in itertools.combinations(pool, size):
            if len({c.event_ticker for c in combo}) < size:
                continue
            if max(sum(c.player == x.player for c in combo) for x in combo) > 2:
                continue
            p_model = _joint(combo)
            p_mkt = float(np.prod([c.price for c in combo]))
            p_blend = MODEL_WEIGHT * p_model + (1 - MODEL_WEIGHT) * float(np.prod([c.p for c in combo]))
            found.append((p_blend / p_mkt, p_blend, p_mkt, combo))
    found.sort(key=lambda x: -x[0])
    chosen = []
    for f in found:
        if all(len({c.ticker for c in f[3]} & {c.ticker for c in g[3]}) <= 1 for g in chosen):
            chosen.append(f)
        if len(chosen) == k:
            break
    return [dict(p=p, p_singles_price=pm, legs=list(combo)) for _, p, pm, combo in chosen]


def _joint(combo):
    groups = {}
    for c in combo:
        groups.setdefault((c.game, c.team), []).append(c.hit)
    p = 1.0
    for hits in groups.values():
        p *= float(np.logical_and.reduce(hits).mean())
    return p
