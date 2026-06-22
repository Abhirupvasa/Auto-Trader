"""
Optibook 2026 — Index Futures Market-Making Engine
===================================================
Strategy  : Statistical-Arbitrage Market Making on OB5X Index Futures
Approach  : Compute a theoretical fair value for each futures contract
            from a weighted basket of 5 underlying equities, apply a
            cost-of-carry model (F = S * e^(rT)), and quote two-sided
            markets around an inventory-adjusted reservation price.
Signals   : 3-second rolling mean-reversion detector to skew quotes
            toward expected price recovery after short-term dislocations.

Placed 8th / 24 teams.
"""

import time
import math
import datetime
import collections
from optibook.synchronous_client import Exchange

# ---------------------------------------------------------------------------
# Configuration & Instrument Definitions
# ---------------------------------------------------------------------------

# Underlying equities in the OB5X basket
STOCKS = ["AMZN", "JPM", "NVDA", "XOM", "NVO"]

# Quarterly index futures contracts
FUTURES = ["OB5X_202609_F", "OB5X_202612_F", "OB5X_202703_F"]

# Basket weights used to compute the replicating index value
WEIGHTS = {
    "AMZN": 953.21,
    "JPM": 129.25,
    "NVDA": 908.06,
    "XOM": 2245.39,
    "NVO": 124.78,
}

# Risk-free rate for cost-of-carry pricing
RISK_FREE_RATE = 0.03

# Futures expiration timestamps (UTC)
EXPIRATIONS = {
    "OB5X_202609_F": datetime.datetime(2026, 9, 18, 12, 0, 0, tzinfo=datetime.timezone.utc),
    "OB5X_202612_F": datetime.datetime(2026, 12, 19, 12, 0, 0, tzinfo=datetime.timezone.utc),
    "OB5X_202703_F": datetime.datetime(2027, 3, 19, 12, 0, 0, tzinfo=datetime.timezone.utc),
}

# ---------------------------------------------------------------------------
# Strategy Parameters
# ---------------------------------------------------------------------------

REVERSION_THRESHOLD = 0.07      # Min index move (3s) to trigger mean-reversion signal
REVERSION_SKEW = 0.35           # Price skew applied when signal fires
MAX_POSITION = 95               # Hard cap per instrument (exchange limit is 100)
BASE_ORDER_VOLUME = 40          # Default order size
INVENTORY_PENALTY = 0.002       # Linear penalty per lot of inventory (kappa)
SPREAD_HALF_WIDTH = 0.2         # Half-spread around reservation price
MIN_TICK = 0.1                  # Minimum price increment

# ---------------------------------------------------------------------------
# Global State
# ---------------------------------------------------------------------------

market_history = collections.deque(maxlen=100)  # Rolling window of index snapshots
exchange = None

# ---------------------------------------------------------------------------
# API Rate Limiter  (rolling-window token bucket, 23 calls / 1.05s)
# ---------------------------------------------------------------------------

_api_timestamps = collections.deque(maxlen=23)


def rate_limit():
    """Block until we have capacity under the exchange's API rate limit."""
    now = time.time()
    if len(_api_timestamps) == 23:
        elapsed = now - _api_timestamps[0]
        if elapsed < 1.05:
            time.sleep(1.05 - elapsed)
            now = time.time()
    _api_timestamps.append(now)


# ---------------------------------------------------------------------------
# Helper Functions
# ---------------------------------------------------------------------------

def time_to_expiry(expiry_dt):
    """Return years to expiry from now (floored at 0)."""
    delta = expiry_dt - datetime.datetime.now(datetime.timezone.utc)
    return max(0.0, delta.total_seconds() / (365.25 * 24 * 3600))


def connect():
    """Establish (or re-establish) a connection to the Optibook exchange."""
    global exchange
    print("[INFO] Connecting to exchange...")
    try:
        exchange = Exchange()
        exchange.connect()
        print("[INFO] Connected successfully.")
    except Exception as e:
        print(f"[WARN] Connection failed: {e}. Will retry.")
        time.sleep(1.0)


def fetch_market_snapshot():
    """
    Pull the top-of-book for every instrument.
    Returns a dict keyed by instrument with best bid/ask prices, volumes, and mid.
    """
    snapshot = {}
    for instrument in STOCKS + FUTURES:
        rate_limit()
        book = exchange.get_last_price_book(instrument)
        if book and book.bids and book.asks:
            snapshot[instrument] = {
                "best_bid": book.bids[0].price,
                "best_bid_vol": book.bids[0].volume,
                "best_ask": book.asks[0].price,
                "best_ask_vol": book.asks[0].volume,
                "mid": (book.bids[0].price + book.asks[0].price) / 2.0,
            }
    return snapshot


def compute_index_fair(snapshot):
    """Compute the replicating-basket fair value of the OB5X index."""
    return sum(snapshot[s]["mid"] * WEIGHTS[s] for s in STOCKS) / 1000.0


# ---------------------------------------------------------------------------
# 1. Initial Connection
# ---------------------------------------------------------------------------

connect()

# ---------------------------------------------------------------------------
# 2. Warm-Up Phase — buffer ~3.5 seconds of index history
# ---------------------------------------------------------------------------

print("[INFO] Buffering market history...")
while len(market_history) < 35:
    if exchange is None or not exchange.is_connected():
        connect()
        continue
    try:
        snap = fetch_market_snapshot()
        if all(s in snap for s in STOCKS):
            market_history.append({
                "time": time.time(),
                "index_fair": compute_index_fair(snap),
            })
        time.sleep(0.1)
    except Exception:
        time.sleep(0.1)

print("[INFO] Warm-up complete. Starting main trading loop.\n")

# ---------------------------------------------------------------------------
# 3. Main Trading Loop
# ---------------------------------------------------------------------------

while True:
    if exchange is None or not exchange.is_connected():
        connect()
        time.sleep(0.2)
        continue

    try:
        loop_start = time.time()
        snap = fetch_market_snapshot()

        # Ensure we have quotes for every instrument before acting
        if not all(s in snap for s in STOCKS) or not all(f in snap for f in FUTURES):
            time.sleep(0.02)
            continue

        # --- Fair Value Computation ---
        index_fair = compute_index_fair(snap)

        # --- Mean-Reversion Signal ---
        # Compare current index to its value ~3 seconds ago
        hist_3s = min(market_history, key=lambda h: abs(h["time"] - (loop_start - 3.0)))
        index_return_3s = index_fair - hist_3s["index_fair"]

        reversion_skew = 0.0
        if index_return_3s < -REVERSION_THRESHOLD:
            reversion_skew = REVERSION_SKEW   # index dropped -> expect bounce -> bid higher
        elif index_return_3s > REVERSION_THRESHOLD:
            reversion_skew = -REVERSION_SKEW  # index spiked  -> expect pull-back -> ask lower

        market_history.append({"time": loop_start, "index_fair": index_fair})

        # --- Fetch Current Positions ---
        rate_limit()
        positions = exchange.get_positions()

        # --- Quote Each Futures Contract ---
        for fut in FUTURES:
            pos = positions.get(fut, 0)
            tau = time_to_expiry(EXPIRATIONS[fut])

            # Cost-of-carry theoretical fair value: F = S * e^(r * T)
            theo = index_fair * math.exp(RISK_FREE_RATE * tau)

            # Reservation price: theo + signal skew - inventory penalty
            reservation = (theo + reversion_skew) - (pos * INVENTORY_PENALTY)

            # Symmetric quotes around reservation price
            target_bid = round(reservation - SPREAD_HALF_WIDTH, 1)
            target_ask = round(reservation + SPREAD_HALF_WIDTH, 1)

            # Tighten the aggressive side when a reversion signal is active
            best_bid = snap[fut]["best_bid"]
            best_ask = snap[fut]["best_ask"]

            if reversion_skew > 0:
                target_bid = min(target_bid, round(best_ask - MIN_TICK, 1))
            elif reversion_skew < 0:
                target_ask = max(target_ask, round(best_bid + MIN_TICK, 1))

            # Volume sizing: cap to stay within position limits
            bid_vol = min(BASE_ORDER_VOLUME, max(0, MAX_POSITION - pos))
            ask_vol = min(BASE_ORDER_VOLUME, max(0, MAX_POSITION + pos))

            # If at position limit, push price far away as a safety net
            if bid_vol <= 0:
                target_bid = round(target_bid - 5.0, 1)
            if ask_vol <= 0:
                target_ask = round(target_ask + 5.0, 1)

            # Sanity check: ask must always be above bid
            if target_ask <= target_bid:
                target_bid = round(reservation - 0.1, 1)
                target_ask = round(reservation + 0.1, 1)

            # --- Order Management (cancel-and-replace) ---
            rate_limit()
            live_orders = exchange.get_outstanding_orders(fut)

            live_bid_id, live_bid_price = None, None
            live_ask_id, live_ask_price = None, None

            for oid, order in live_orders.items():
                if order.side == "bid":
                    live_bid_id, live_bid_price = oid, order.price
                elif order.side == "ask":
                    live_ask_id, live_ask_price = oid, order.price

            # Only replace if the price has moved at least one tick
            needs_new_bid = (live_bid_price is None or abs(target_bid - live_bid_price) >= MIN_TICK) and bid_vol > 0
            needs_new_ask = (live_ask_price is None or abs(target_ask - live_ask_price) >= MIN_TICK) and ask_vol > 0

            # Cancel stale orders
            if (needs_new_bid or bid_vol <= 0) and live_bid_id is not None:
                rate_limit()
                exchange.delete_order(fut, order_id=live_bid_id)
            if (needs_new_ask or ask_vol <= 0) and live_ask_id is not None:
                rate_limit()
                exchange.delete_order(fut, order_id=live_ask_id)

            # Place fresh orders
            if needs_new_bid:
                rate_limit()
                exchange.insert_order(fut, price=target_bid, volume=bid_vol, side="bid", order_type="limit")
            if needs_new_ask:
                rate_limit()
                exchange.insert_order(fut, price=target_ask, volume=ask_vol, side="ask", order_type="limit")

        # Throttle to ~20 iterations/sec
        elapsed = time.time() - loop_start
        time.sleep(max(0.01, 0.05 - elapsed))

    except Exception as e:
        err = str(e).lower()
        if "connect" in err or "session" in err or "timeout" in err:
            connect()
        time.sleep(0.1)
