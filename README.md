# Auto-Trader — Optibook 2026 — Index Futures Market-Making Algorithm

> **Result:** Placed **8th out of 24 teams** in the Optibook Algorithmic Trading Challenge, hosted as part of Optiver's Future Focus Program (June 2026).

---

## What is Optibook?

Optibook is a simulated electronic exchange built by **Optiver** for educational trading competitions. Participants write automated trading algorithms in Python that connect to the exchange via an API and trade in real time against other teams.

The exchange hosts a set of financial instruments — in our case, **5 underlying equities** (`AMZN`, `JPM`, `NVDA`, `XOM`, `NVO`) and **3 quarterly index futures** (`OB5X_202609_F`, `OB5X_202612_F`, `OB5X_202703_F`). The futures track a weighted basket of these equities, and the challenge is to figure out how to price them correctly and profit from mispricings — all while managing risk, respecting position limits (±100 lots per instrument), and staying within an API rate limit of ~25 calls/second.

Teams are ranked by **total Profit and Loss (PnL)** at the end of each trading session. There's no manual trading — your code is your entire strategy.

---

## Strategy Overview

The algorithm is a **statistical-arbitrage market maker** on the index futures. The core idea is straightforward:

1. **Compute what the future _should_ be worth** from the underlying stocks.
2. **Quote a bid and ask** around that fair value.
3. **Lean your quotes** when the market looks like it's about to revert.
4. **Don't blow up** — respect position limits and handle exchange errors gracefully.

### Fair Value: Replicating Basket + Cost of Carry

Each futures contract is priced using a **cost-of-carry model**:

```
F = S × e^(r × T)
```

where `S` is the current index spot value (computed as a weighted sum of the 5 equity mid-prices), `r` is the risk-free rate, and `T` is time to expiry in years. This gives us a theoretical fair value (`theo`) for each futures contract at every point in time.

### Mean-Reversion Signal

The algorithm maintains a rolling window of index values. On each tick, it compares the current index level to where it was **~3 seconds ago**. If the index has moved more than a calibrated threshold (0.07) in either direction, it assumes the move will partially revert and **skews the reservation price** toward the expected bounce:

- Index dropped sharply → skew bid higher (buy the dip)
- Index spiked sharply → skew ask lower (sell the rip)

This is a simple but effective alpha signal for a mean-reverting simulated market.

### Inventory-Aware Pricing

Holding a large position is risky. The algorithm applies a **linear inventory penalty** to the reservation price:

```
reservation = theo + reversion_skew - (position × κ)
```

If we're long 50 lots, the reservation price shifts down, making the ask more competitive and naturally working us out of the position. This is a classic Avellaneda-Stoikov style adjustment.

### Dynamic Volume Sizing

Order sizes aren't fixed — they're **dynamically capped** based on how much room is left before the position limit:

```python
bid_vol = min(BASE_VOLUME, max(0, MAX_POSITION - current_position))
ask_vol = min(BASE_VOLUME, max(0, MAX_POSITION + current_position))
```

If we're at +95 lots, the bid volume drops to 0 and the bid price gets pushed 5 ticks away as an extra safety net. This completely prevents position-limit breaches.

---

## Why Keep It Simple?

We experimented extensively with more complex approaches during development — adaptive Kalman filters for fair value, multi-regime volatility models, cross-instrument spread arbitrage, delta hedging, and more. Some of these improved accuracy in isolation, but in practice they hurt performance for one key reason:

**Latency eats alpha.**

Every additional computation costs time. The exchange has a strict API rate limit (~25 calls/sec), and every `get_price_book`, `insert_order`, or `delete_order` call counts toward it. A complex strategy that computes a slightly better fair value but takes 3× longer per loop simply gets fewer quotes out, reacts slower to market moves, and **misses trades that a faster algorithm would have captured**.

The final algorithm prioritises:
- **Fast loop time** (~50ms per full cycle across all 3 futures)
- **Minimal API calls** — lazy order amendments that only cancel-and-replace when the price has moved ≥ 1 tick
- **Simple arithmetic** — weighted sums and a single `exp()` call, nothing heavier

This "less is more" approach turned out to be the right tradeoff for this particular exchange environment.

---

## Code Walkthrough 

| Section | Lines | What it does |
|---|---|---|
| **Configuration** | 21–60 | Instrument lists, basket weights, expiration dates, and all tunable strategy parameters in one place |
| **Rate Limiter** | 70–84 | A rolling-window token bucket that sleeps just long enough to stay under the 25 calls/sec API limit — simple and zero-overshoot |
| **Market Snapshot** | 110–132 | Pulls top-of-book for all 8 instruments in a single pass and computes the basket index fair value |
| **Warm-Up** | 142–161 | Buffers ~3.5 seconds of index history before trading starts, so the mean-reversion signal has data to compare against from the first real tick |
| **Main Loop** | 167–282 | The core cycle: snapshot → fair value → reversion signal → reservation price → volume sizing → cancel-and-replace orders → sleep |

### Interesting Implementation Details

- **Lazy order management:** Orders are only cancelled and replaced when the target price has drifted by at least 1 tick (0.1) from the resting order. This avoids burning API calls on no-ops and keeps the rate limiter budget free for things that matter.

- **Token bucket rate limiter:** Instead of a simple `time.sleep(0.04)` between calls, the limiter tracks a deque of the last 23 timestamps and only blocks when the window is full. This allows burst activity (e.g., cancelling + replacing 6 orders quickly) while staying within the 1-second rolling budget.

- **Position-limit safety net:** When volume sizing drops to 0 (we're at the limit), the algorithm doesn't just skip the order — it actively pushes the price 5 ticks away. If a resting order on that side somehow still exists, it gets cancelled. Belt and suspenders.

- **Crossed-quote guard:** If the reversion skew + inventory adjustment accidentally pushes the ask below the bid, the algorithm snaps both quotes back to a tight ±0.1 spread around the reservation price rather than quoting a negative spread.

---

## Tech Stack

- **Language:** Python 3
- **Exchange API:** `optibook.synchronous_client`
- **Dependencies:** Standard library only (`time`, `math`, `datetime`, `collections`)
