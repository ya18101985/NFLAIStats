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
- Pregame only, no live betting. Unfilled orders expire 1 minute before kickoff.
- Singles: 5-15% of the wave bankroll each (half-Kelly, clamped). 80% of a wave to singles,
  20% to 3-4 parlays of 3-5 legs, each leg >= 55%. Parlays go first; money they don't spend
  goes to singles. Stakes are scaled up so the full wave bankroll is used; if they can't be,
  edge bars step down (RELAX_STEPS) but never below break-even.
- Single-game waves (1:05, SNF, MNF, TNF, Europe): singles first (15% cap, filler bets allowed
  down to 1c below break-even, ONE_GAME_FLOOR), then everything
  left goes to parlays (split over however many qualify, up to 4; spares tried if quotes are poor).
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
- Wave bankroll = available cash, max $5,000. Sunday: 10 AM PT wave 80%; 1:05 wave gets the
  20% reserve + 50% of 10 AM payouts so far; 1:25 wave and the night game get all cash.
  TNF/MNF/Europe games: 100% of cash.
- Safety stop: skip waves if the account is below 40% of the starting balance.
- Fully automatic placement (user's choice). The user prefers plain language and one decision
  at a time.
