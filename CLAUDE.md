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
- Parlays (user's choice, 2026-11-08): every wave puts 20% first into up to 4 parlays of 2-3 legs,
  each leg 60-75% to win. Never skipped: a quote 15% under our odds is taken at once, otherwise
  the best quote at the deadline. Money they can't spend (no quotes at all) goes to singles.
- Singles: the rest, 5-15% of the wave bankroll each (half-Kelly, clamped), scaled up so the full
  wave bankroll is used; if they can't be, edge bars step down (RELAX_STEPS, ONE_GAME_FLOOR in
  single-game waves, then FULL_ALLOCATION_FLOORS down to 3c below break-even).
- Sportsbook lines (The Odds API, key in `~/.kalshi/odds_api_key.txt`) are a third opinion.
  Singles are a hybrid (user's choice, 2026-10-31), half the budget each:
  A = books and model both say Kalshi is cheap (EV >= 0.5c); B = model-only EV >= 2c and the
  books at most 2c against. Singles must be 45-65% to win.
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
- No balance floor (user's choice, 2026-11-08): the 40% safety stop was removed so every wave can
  spend all available funds. Only the STOP file halts betting.
- Fully automatic placement (user's choice). The user prefers plain language and one decision
  at a time.

## Cloud operations (since 2026-11-06)
- Live bets run as one-off Claude Code cloud routines, one per wave, about 6 minutes before its bet time,
  each running `scripts/cloud_tick.py --live --window 15` (the 10 AM wave loops while it prints MORE).
  New weeks need new routines; get wave times from `run_day.make_waves`.
- Never run `run_day.py` or `cloud_tick.py --live` by hand while routines are scheduled: double bets.
- Dashboard routines loop on `scripts/dash_wait.py DAY --pull` and publish the Bet Tracker artifact
  (https://claude.ai/artifact/3zjxWGVFxLY2cP3PmH7tNo).
- Emails: each wave emails yash.us.visa@gmail.com twice (bets placed; settlement), through the user's
  Apps Script mailer (`nfai/report.py`, key in env NFAI_MAIL_KEY). The cloud has no SMTP.
- Kill switch from a phone: add a file named `STOP` at the repo root on GitHub.
