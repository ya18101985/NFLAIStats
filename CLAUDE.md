# NFAIstats

## Project
Automated NFL betting on Kalshi, focused on player props, with a few parlays. A model projects
player stats, compares them with Kalshi prices, sizes bets by the user's rules, and places them
on the user's real Kalshi account through the API. Standalone project; unrelated to the Stock
Trading / trading-bot project.

## Layout
- `nfai/kalshi.py`: Kalshi API client (public data, portfolio, guarded orders). V2 orders quote
  from the YES side: buying NO at p is `side=ask, price=1-p`. Buying a combo (parlay) means
  accepting the maker's NO bid (`accepted_side="no"`), price 1 - no_bid.
- `nfai/data.py`: nflverse downloads (play-by-play, weekly stats, snaps, NGS, injury reports) into `data/`.
- `nfai/model.py`: usage x efficiency x opponent x weather projections, simulated per team. Players on
  the week's injury report who still play get their usage cut by last practice (INJURY_USAGE).
- `nfai/slate.py`: games, kickoffs, lines, ESPN injuries, Open-Meteo wind, Kalshi prop markets.
- `nfai/picks.py`: calibration, market blend, filters, sizing, parlay selection.
- `nfai/execute.py`: order placement, parlay RFQs, SQLite ledger (`ledger/ledger.db`).
- `scripts/run_day.py`: waits for each wave and runs it. `scripts/preview.py`: dry-run picks.
  `scripts/backtest.py [start] [end] [seasons...]`: walk-forward calibration over any seasons
  (refit CAL_A/CAL_B in picks.py from it; judge model changes by its overall Brier score).
  `scripts/clv.py`: read-only check of each single's price paid vs. Kalshi's price at kickoff.
  `scripts/kalshi_check.py`: read-only balance/positions/fills.

## Credentials
Kalshi key lives in `~/.kalshi/` (`key_id.txt`, `private_key.pem`), never in the repo. Never
print or read the key contents into the conversation. `~/.kalshi/STOP` halts all waves.

## Betting rules (agreed with the user, 2026-10-30)
- Pregame only, except the Sunday 10 AM wave's top-ups (see below). Unfilled pregame orders
  expire 1 minute before kickoff; top-ups after kickoff rest 2 minutes.
- Parlays: OFF since the 2026-11-08 post-mortem (PARLAY_SHARE = 0; 21 settled parlays returned -51%,
  mostly bought above our own odds). Earlier rule: 20% into up to 4 parlays, best quote at the deadline.
- Singles: the rest, 5-15% of the wave bankroll each (half-Kelly, clamped), scaled up so the full
  wave bankroll is used; if they can't be, edge bars step down (RELAX_STEPS) but never below
  break-even: no filler bets (user's choice, 2026-11-08; negative-edge singles returned -22%).
  Full allocation goes into edge bets instead: if the 15% cap can't place the budget, the per-bet
  cap rises (BIG_STAKE_CAPS 25/40/60/100%) until the break-even-or-better bets absorb it.
- Sportsbook lines (The Odds API, key in `~/.kalshi/odds_api_key.txt`) are a third opinion.
  Singles are a hybrid (user's choice, 2026-10-31), half the budget each:
  A = books and model both say Kalshi is cheap (EV >= 0.5c); B = model-only EV >= 2c and the
  books at most 2c against. Singles must be 45-65% to win.
  C = "likely winner" (user's choice, 2026-11-08): model and blend 58-72%, books 55%+, EV >= 1c
  after fees. C only fills what A and B (at non-negative bars) leave, and always before any
  below-break-even filler bets.
- Game lines (`nfai/gamelines.py`): winner/spread/total for full game, halves and quarters, plus
  team totals (full, 1H), priced only against the books (70% books / 30% Kalshi mid); they
  join group A. Quarter lines need an exact book line; partial-game winners need a 3-way book
  price. Not used in parlays.
- Props only on players with real usage over the last 4 games: RB 12+ touches, WR/TE 5+ targets,
  QB confirmed starter; skip anyone Questionable/Doubtful/Out/IR. Depth chart (nflverse, refreshed
  each wave): QB must be QB1; skip players off the chart or whose starter status (QB1/RB1/WR1-3/TE1)
  changed in the last 14 days.
- Wave bankroll = free cash (balance less resting orders), max $5,000 (user's choice, 2026-11-08):
  Europe 100%. Sunday 10 AM wave 80%: bets from 9:45 PT, then every ~90 s bets newly arrived
  Europe payouts (at live prices, no guard, user's choice) until the Europe bets settle or 10:10.
  1:05, 1:25 and the night game: all cash. TNF/MNF: 100%.
  Night game (user's choice, 2026-11-08): after its first bets, keeps betting newly settled afternoon
  payouts every ~90 s until those games settle or 3 min before kickoff (pregame only).
- No balance floor (user's choice, 2026-11-08): the 40% safety stop was removed so every wave can
  spend all available funds. Only the STOP file halts betting.
- Fully automatic placement (user's choice). The user prefers plain language and one decision
  at a time.

## Cloud operations (since 2026-11-06)
- Live bets run automatically on GitHub Actions (`.github/workflows/bets.yml`, since 2026-11-12, user's
  choice: no manual setup per week through the end of the season). Every 30 min it runs
  `scripts/cloud_tick.py --live --window 70` (looping while it prints MORE), then sends any due
  settlement emails. Secrets in the repo's Actions settings: KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY,
  ODDS_API_KEY, NFAI_MAIL_KEY. Without the Kalshi secrets each run exits doing nothing.
- Never run `run_day.py` or `cloud_tick.py --live` by hand while the workflow is enabled: it uses a
  concurrency group, but a hand run outside it can double-bet. Pause: Actions > Bets > Disable workflow.
- Dashboard routines loop on `scripts/dash_wait.py DAY --pull` and publish the Bet Tracker artifact
  (https://claude.ai/artifact/3zjxWGVFxLY2cP3PmH7tNo).
- Emails: each wave emails yash.us.visa@gmail.com twice (bets placed; settlement), through the user's
  Apps Script mailer (`nfai/report.py`, key in env NFAI_MAIL_KEY). The cloud has no SMTP.
- Kill switch from a phone: add a file named `STOP` at the repo root on GitHub.
