"""Kalshi Trade API client: public market data, read-only portfolio, and guarded order placement.

Credentials live outside the repo in ~/.kalshi/ (key_id.txt + private_key.pem), or in the
KALSHI_KEY_ID / KALSHI_PRIVATE_KEY_PATH environment variables on a cloud host.

Orders use the V2 endpoint, which quotes everything from the YES side:
  buy YES at p  -> side="bid", price=p
  buy NO  at p  -> side="ask", price=1-p
place_order() takes the outcome and the price of that outcome and does this conversion.
"""

import base64
import os
import time
import uuid
from decimal import ROUND_DOWN, Decimal
from pathlib import Path

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

BASE_URL = "https://api.elections.kalshi.com"
API_PREFIX = "/trade-api/v2"
KEY_DIR = Path.home() / ".kalshi"


class OrderRejected(Exception):
    """Raised before anything is sent when an order breaks a local safety rule."""


def _cents(x) -> Decimal:
    return Decimal(str(x)).quantize(Decimal("0.01"))


class Kalshi:
    def __init__(self, key_id=None, private_key_path=None, max_order_cost=None, live=False):
        """max_order_cost: hard dollar cap per order (price x contracts). live=False means
        place_order() only prints what it would send."""
        self.session = requests.Session()
        self.max_order_cost = Decimal(str(max_order_cost)) if max_order_cost is not None else None
        self.live = live
        key_id = key_id or os.environ.get("KALSHI_KEY_ID") or os.environ.get("KALSHI_API_KEY_ID")
        key_path = private_key_path or os.environ.get("KALSHI_PRIVATE_KEY_PATH")
        if not key_id and (KEY_DIR / "key_id.txt").exists():
            key_id = (KEY_DIR / "key_id.txt").read_text().strip()
        if not key_path and (KEY_DIR / "private_key.pem").exists():
            key_path = KEY_DIR / "private_key.pem"
        self.key_id = key_id
        self._key = None
        pem = os.environ.get("KALSHI_PRIVATE_KEY")   # cloud: the key's PEM text in an environment variable
        if pem:
            pem = pem.replace("\\n", "\n").strip()
            if not pem.startswith("-----BEGIN"):   # just the key body was pasted: add the PEM wrapper
                pem = f"-----BEGIN PRIVATE KEY-----\n{pem}\n-----END PRIVATE KEY-----"
            self._key = serialization.load_pem_private_key(pem.encode(), password=None)
        elif key_path:
            self._key = serialization.load_pem_private_key(Path(key_path).read_bytes(), password=None)

    # ---- transport -------------------------------------------------------------------------

    def _headers(self, method, path):
        if not (self.key_id and self._key):
            raise RuntimeError("No Kalshi API key found in ~/.kalshi/ or the environment")
        ts = str(int(time.time() * 1000))
        msg = (ts + method + path.split("?")[0]).encode()
        if isinstance(self._key, Ed25519PrivateKey):
            sig = self._key.sign(msg)
        else:
            sig = self._key.sign(
                msg,
                padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
                hashes.SHA256(),
            )
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
        }

    def _request(self, method, endpoint, auth=True, params=None, json=None):
        path = API_PREFIX + endpoint
        for attempt in range(6):
            headers = self._headers(method, path) if auth else {}
            r = self.session.request(method, BASE_URL + path, params=params, json=json, headers=headers, timeout=30)
            # rate limits and gateway hiccups are safe to retry; a POST that got a 5xx may have
            # gone through, so orders carry a client_order_id that Kalshi rejects if repeated
            if r.status_code != 429 and not (r.status_code >= 500 and method == "GET"):
                break
            time.sleep(0.5 * 2 ** attempt)
        if r.status_code >= 400:
            raise requests.HTTPError(f"{method} {endpoint} -> {r.status_code}: {r.text}", response=r)
        return r.json() if r.content else {}

    def _paged(self, endpoint, key, auth, params):
        params = dict(params, limit=params.get("limit", 1000))
        while True:
            d = self._request("GET", endpoint, auth=auth, params=params)
            yield from d.get(key, [])
            if not d.get("cursor"):
                return
            params["cursor"] = d["cursor"]

    # ---- public market data ----------------------------------------------------------------

    def markets(self, **params):
        """e.g. markets(event_ticker="KXNFLRECYDS-26NOV01PHIWAS", status="open")"""
        return list(self._paged("/markets", "markets", False, params))

    def market(self, ticker):
        return self._request("GET", f"/markets/{ticker}", auth=False)["market"]

    # ---- read-only portfolio ---------------------------------------------------------------

    def balance(self):
        """Available cash in dollars."""
        return Decimal(self._request("GET", "/portfolio/balance")["balance_dollars"])

    def positions(self, **params):
        return list(self._paged("/portfolio/positions", "market_positions", True, params))

    def fills(self, **params):
        return list(self._paged("/portfolio/fills", "fills", True, params))

    def settlements(self, **params):
        return list(self._paged("/portfolio/settlements", "settlements", True, params))

    def orders(self, **params):
        return list(self._paged("/portfolio/orders", "orders", True, params))

    # ---- trading ---------------------------------------------------------------------------

    def place_order(self, ticker, outcome, price, count, time_in_force="good_till_canceled",
                    expiration_time=None, client_order_id=None):
        """Buy `count` contracts of `outcome` ("yes"/"no") at limit `price` (dollars, price of
        that outcome). Raises OrderRejected if the order breaks a local rule."""
        outcome = outcome.lower()
        if outcome not in ("yes", "no"):
            raise OrderRejected(f"outcome must be yes or no, got {outcome!r}")
        price, count = _cents(price), Decimal(str(count)).quantize(Decimal("0.01"), ROUND_DOWN)
        if not Decimal("0.01") <= price <= Decimal("0.99"):
            raise OrderRejected(f"price {price} outside 0.01-0.99")
        if count <= 0:
            raise OrderRejected("count must be positive")
        cost = price * count
        if self.max_order_cost is None:
            raise OrderRejected("max_order_cost not set; refusing to trade without a cap")
        if cost > self.max_order_cost:
            raise OrderRejected(f"order cost ${cost} exceeds cap ${self.max_order_cost}")

        body = {
            "ticker": ticker,
            "client_order_id": client_order_id or str(uuid.uuid4()),
            "side": "bid" if outcome == "yes" else "ask",
            "price": str(price if outcome == "yes" else Decimal("1.00") - price),
            "count": str(count),
            "time_in_force": time_in_force,
            "self_trade_prevention_type": "taker_at_cross",
        }
        if expiration_time:
            body["expiration_time"] = int(expiration_time)
        if not self.live:
            print(f"[dry run] would buy {count} {outcome.upper()} @ ${price} on {ticker} (${cost}): {body}")
            return {"dry_run": True, "body": body}
        return self._request("POST", "/portfolio/events/orders", json=body)

    def cancel_order(self, order_id, ticker):
        return self._request("DELETE", f"/portfolio/events/orders/{order_id}", params={"market_ticker": ticker})
