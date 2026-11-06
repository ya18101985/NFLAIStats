"""Read-only account check: balance, open positions, recent fills. Places no orders."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nfai.kalshi import Kalshi  # noqa: E402

k = Kalshi()
print(f"Available balance: ${k.balance():,.2f}")

open_pos = [p for p in k.positions() if float(p.get("position_fp") or 0) != 0]
print(f"\nOpen positions: {len(open_pos)}")
for p in open_pos:
    print(f"  {p['ticker']:55} {p.get('position_fp')} contracts")

fills = k.fills(limit=50)
print(f"\nLast {len(fills)} fills:")
for f in fills[:50]:
    price = f["yes_price_dollars"] if f["outcome_side"] == "yes" else f["no_price_dollars"]
    print(f"  {f.get('created_time', '')[:16]}  {f['outcome_side'].upper():3} {f['count_fp']:>8} @ ${price}  "
          f"fee ${f.get('fee_cost')}  {f['ticker']}")
