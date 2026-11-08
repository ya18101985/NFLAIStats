"""Placing bets (single orders and parlay RFQs) and recording everything in the ledger."""

import datetime as dt
import json
import sqlite3
import time
import uuid
from pathlib import Path

from . import picks as P, report

LEDGER = Path(__file__).resolve().parents[1] / "ledger" / "ledger.db"
COMBO_COLLECTION = "KXMVECROSSCATEGORY-R"


class Ledger:
    def __init__(self, path=LEDGER):
        path.parent.mkdir(exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS bets (
                id INTEGER PRIMARY KEY, wave TEXT, created_at TEXT, kind TEXT, ticker TEXT, title TEXT,
                outcome TEXT, limit_price REAL, contracts REAL, stake REAL, p_model REAL, p REAL, ev REAL,
                order_id TEXT, client_order_id TEXT, filled REAL, status TEXT, legs TEXT, note TEXT);
            CREATE TABLE IF NOT EXISTS waves (
                wave TEXT PRIMARY KEY, started_at TEXT, cash_before REAL, bankroll REAL, cash_after REAL,
                status TEXT, note TEXT);
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
        """)

    def get(self, key, default=None):
        r = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r[0] if r else default

    def set(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, str(value)))
        self.db.commit()

    def wave(self, name):
        r = self.db.execute("SELECT wave, started_at, cash_before, bankroll, cash_after, status FROM waves WHERE wave=?",
                            (name,)).fetchone()
        return dict(zip(["wave", "started_at", "cash_before", "bankroll", "cash_after", "status"], r)) if r else None

    def record_wave(self, name, **kw):
        cur = self.wave(name) or {"wave": name}
        cur.update(kw)
        self.db.execute("INSERT OR REPLACE INTO waves (wave, started_at, cash_before, bankroll, cash_after, status, note) "
                        "VALUES (?,?,?,?,?,?,?)", (name, cur.get("started_at"), cur.get("cash_before"), cur.get("bankroll"),
                                                   cur.get("cash_after"), cur.get("status"), cur.get("note")))
        self.db.commit()

    def record_bet(self, **kw):
        kw.setdefault("created_at", dt.datetime.now(dt.timezone.utc).isoformat())
        cols = ",".join(kw)
        self.db.execute(f"INSERT INTO bets ({cols}) VALUES ({','.join('?' * len(kw))})", list(kw.values()))
        self.db.commit()


def place_singles(k, ledger, wave, selected, kickoff, log=print):
    """selected: [(Candidate, contracts, stake)] from picks.select_singles. Orders rest until one
    minute before kickoff, then expire, so a stale pregame price can never fill during the game.
    Top-ups placed after that (10 AM wave, user's choice 2026-11-08) rest for 2 minutes."""
    expire = max(int(kickoff.timestamp()) - 60, int(time.time()) + 120)
    for c, n, stake in selected:
        cid = str(uuid.uuid4())
        try:
            r = k.place_order(c.ticker, c.outcome, c.price, n, expiration_time=expire, client_order_id=cid)
            filled = float(r.get("fill_count", 0) or 0)
            status = "filled" if filled >= n else ("partial" if filled else "resting")
            log(f"  placed {c.title} {c.outcome.upper()} {n} @ {c.price:.2f} (${stake:,.0f}) -> {status} {filled:.0f}/{n}")
            ledger.record_bet(wave=wave, kind="single", ticker=c.ticker, title=c.title, outcome=c.outcome,
                              limit_price=c.price, contracts=n, stake=stake, p_model=c.p_model, p=c.p, ev=c.ev,
                              order_id=r.get("order_id"), client_order_id=cid, filled=filled, status=status,
                              note=report.reason(c))
        except Exception as e:
            log(f"  FAILED {c.title}: {e}")
            ledger.record_bet(wave=wave, kind="single", ticker=c.ticker, title=c.title, outcome=c.outcome,
                              limit_price=c.price, contracts=n, stake=stake, p_model=c.p_model, p=c.p, ev=c.ev,
                              client_order_id=cid, status="error", note=str(e)[:500])


def place_parlay(k, ledger, wave, parlay, budget, log=print, wait_s=25, force=True):
    """Ask market makers to quote the combo; buy at once if a quote is at least PARLAY_MIN_EDGE below
    our joint probability, else (force, the user's rule since 2026-11-08) take the best quote at the
    deadline. Returns the dollars spent. Buying YES on a combo means accepting the
    maker's NO bid (confirmed from the user's past combo fills); our price is 1 - no_bid."""
    legs = parlay["legs"]
    desc = " + ".join(f"{c.title} {c.outcome}" for c in legs)
    max_price = parlay["p"] * (1 - P.PARLAY_MIN_EDGE)
    try:
        m = k._request("POST", f"/multivariate_event_collections/{COMBO_COLLECTION}", json={"selected_markets": [
            {"market_ticker": c.ticker, "event_ticker": c.event_ticker, "side": c.outcome} for c in legs]})
        ticker = m["market_ticker"]
        rfq = k._request("POST", "/communications/rfqs", json={
            "market_ticker": ticker, "target_cost_dollars": f"{budget:.2f}", "rest_remainder": False,
            "obscure_creator_id": True})
        rfq_id = rfq["id"]
    except Exception as e:
        log(f"  parlay setup FAILED ({desc}): {e}")
        return 0.0
    best = None
    deadline = time.time() + wait_s
    while time.time() < deadline:
        time.sleep(2)
        qs = k._request("GET", "/communications/quotes",
                        params={"rfq_id": rfq_id, "rfq_user_filter": "self", "status": "open"}).get("quotes", [])
        for q in qs:
            nb = float(q.get("no_bid_dollars") or 0)
            if nb > 0 and (best is None or 1 - nb < best[0]):
                best = (1 - nb, q)
        if best and best[0] <= max_price:
            break
    note = f"best quote {best[0]:.3f}" if best else "no quotes"
    forced = bool(best and best[0] > max_price and force)
    if forced:
        log(f"  parlay forced: best quote {best[0]:.3f} above our max {max_price:.3f}")
    elif not best or best[0] > max_price:
        log(f"  parlay skipped ({note}, our max {max_price:.3f}): {desc}")
        _delete_rfq(k, rfq_id)
        ledger.record_bet(wave=wave, kind="parlay", ticker=ticker, title=desc, outcome="yes", p=parlay["p"],
                          stake=0, status="skipped", note=note, legs=json.dumps([c.ticker for c in legs]))
        return 0.0
    price, q = best
    try:
        k._request("PUT", f"/communications/rfqs/{rfq_id}/quotes/{q['id']}/accept", json={"accepted_side": "no"})
        log(f"  parlay accepted @ {price:.3f} (p={parlay['p']:.3f}, ${budget:,.0f}): {desc}")
        status = "accepted"
    except Exception as e:
        log(f"  parlay accept FAILED: {e}")
        status = "error"
    ledger.record_bet(wave=wave, kind="parlay", ticker=ticker, title=desc, outcome="yes", limit_price=price,
                      stake=budget, p=parlay["p"], ev=parlay["p"] - price - P.fee(price), status=status,
                      order_id=q["id"], note=f"rfq {rfq_id}{' forced' if forced else ''}", legs=json.dumps([c.ticker for c in legs]))
    return budget if status == "accepted" else 0.0


def _delete_rfq(k, rfq_id):
    try:
        k._request("DELETE", f"/communications/rfqs/{rfq_id}")
    except Exception:
        pass
