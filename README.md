# Divided Oracle: Market-Making and Auction Agent

A trading bot for a two-player, zero-sum game of incomplete information: price a hidden sum of fair +/-1 coins, make and take markets on it, and bid for information and settlement powers in a first-price auction under a fixed budget.

## Context

- Event: QuantStorm 2026, Round 1 ("Divided Oracle"), an inter-college quant trading-bot competition. Submission deadline 17 Aug 2026.
- Solo entry. All code in this repository is my own.
- Constraints set by the organisers: one Python file, standard library only (`math`, `random`, `statistics`, `collections`, ...), a sandboxed runtime, and a hard 50 ms per-call time limit.

## Problem

My summary of the game (the official rulebook and engine belong to the organisers and are not redistributed here):

- A hidden score `S` is the sum of 40 independent fair +/-1 coins. Each player privately holds 20; neither ever sees the other's hand directly.
- A deal is 5 rounds. Each round, 4 more of your own coins are revealed to you, then:
  1. **Auction.** A sealed-bid, first-price auction for "powers", paid from a 24-point budget per deal. Unspent budget settles as PnL at 0.08 ticks per point, so every bid has a real opportunity cost. Powers include a partial leak of the opponent's revealed coins (`FORESIGHT`), loss caps on the round's contract (`SUBSTITUTE`), price shifts on forced fills (`TRICK_ROOM`, `STEALTH_ROCK`) and an option to swap entire hands (`TRANSFORM`).
  2. **Negotiation.** One side (alternating) is the Maker and opens a two-sided quote whose width must lie in a band that tightens over the deal. The Taker accepts or counters with a narrower range inside it. If no one accepts by the last turn, the contract is forced at the midpoint and the forcing side pays a fee.
- All five contracts settle against `S` at the end of the deal. The Maker is scored on a straddle obligation (rewarded if the true `S` falls inside the quote, measured against what an honest Maker could achieve) and penalised for width above the floor.
- Every deal is replayed with hands and roles swapped (mirrored), so ranking is on decisions, not on the deal.

The two halves interact: good pricing without bidding loses to a bot that bids, and the auction is only worth winning if the information or settlement edge can be monetised in the negotiation.

## Approach

Two versions are included.

**V1 (`my_bot.py`)** - a point-estimate bot.
- Estimates `S` as my revealed sum plus the best available read of the opponent's revealed sum: a `FORESIGHT` leak if held, else the mid of the opponent's live quote, else the mid of their most recent opening quote this deal. Unrevealed coins are mean-zero, so this is unbiased.
- Always quotes at the floor width, centred on that estimate: under honest centring the straddle reward is largest at the tightest width.
- Bids a shaded fraction (0.60) of a per-round power value table converted to budget units, trimming to budget by value.
- On the final negotiation turn explicitly compares accept-buy, accept-sell and forcing the midpoint fill (including the fee and any fill-shift powers held by either side).

**V2 (`quantstorm_v2.py`)** - replaces V1's hand-tuned constants with closed-form quantities wherever the game exposes them.
- **Residual uncertainty is tracked, not just the mean.** An "effective unseen" coin count (my unrevealed coins plus opponent coins not covered by the freshest anchor) gives the true residual variance, since each unseen fair coin contributes variance 1.
- **Quote width is optimised, not assumed.** For each legal width the bot evaluates `MAKER_OBLIGATION * (P_true(w) - P_naive(w)) - WIDTH_PREMIUM * (w - floor)`, using the engine config's exact lattice straddle probability with the effective unseen count, and picks the maximiser.
- **Accept margin scales with `sqrt(effective_unseen)`**, the standard deviation of the residual, instead of a flat threshold.
- **`TRANSFORM` is an EV comparison.** With an opponent read, swap if their revealed sum is more extreme than mine; without one, compare `|k_mine|` to the exact `E|sum of n fair coins|` computed by enumeration with `math.comb`.
- **Auction shade adapts within a deal** to the opponent's observed spend on the public auction tape (range 0.35 to 0.85 around a 0.58 base).
- **Opponent quotes are trusted but bounded.** Anchors are clipped to the feasible range `[-n, n]` for `n` revealed coins and shrunk by a 0.92 trust factor, limiting damage from a Maker that distorts its quote.

Every method is wrapped so that unexpected observation shapes degrade to a safe action instead of raising (an exception forfeits the deal to engine fallbacks).

## Results

During the event (logs not retained in this repository):
- V1 scored **+51.53** cumulative PnL on the organisers' practice leaderboard, which plays each entry against 10 hidden standard bots on a fixed seed (figure as recorded in the V2 docstring, `quantstorm_v2.py:9`; the leaderboard is not archived).

Reproducible re-run against the three public reference strategies, using the official engine (`eval/run_eval.py`). Each cell is total PnL in ticks for the row bot over one mirrored match of 20 deals; positive means the row bot won.

| Bot | Opponent | Seed 1 | Seed 2 | Seed 3 | Mean |
|---|---|---:|---:|---:|---:|
| V1 | naive_ev | +130.96 | +192.24 | +91.24 | +138.15 |
| V1 | rational | +168.96 | +196.24 | +165.24 | +176.81 |
| V1 | adaptive_bidder | +72.48 | +138.52 | +4.84 | +71.95 |
| V2 | naive_ev | +128.79 | +227.71 | +105.31 | +153.94 |
| V2 | rational | +169.79 | +241.71 | +187.31 | +199.60 |
| V2 | adaptive_bidder | +86.11 | +125.43 | +34.87 | +82.14 |
| V2 | V1 | +33.31 | -7.41 | +79.39 | +35.10 |

Raw numbers: `eval/results.csv`.

Reading of the results:
- Both versions beat all three reference strategies on every seed. The margin over `adaptive_bidder` (the only reference that also bids in the auction) is the smallest and the most variable; V1 was nearly flat against it on seed 3 (+4.84).
- V2 improves on V1 against every reference opponent on average and wins the direct V2-vs-V1 match on 2 of 3 seeds, but it lost seed 2 (-7.41). Three seeds of 20 deals is a small sample; the V2-over-V1 edge should be read as suggestive, not established.
- These opponents are the organisers' public baselines, not the competition field. The final tournament standing is not recorded here.

## Repository layout

```
my_bot.py            V1 submission-format bot
quantstorm_v2.py     V2 bot (closed-form width, margin, TRANSFORM and adaptive shade)
eval/run_eval.py     Mirrored head-to-head evaluation against the reference strategies
eval/results.csv     Output of the run above (seeds 1-3)
```

## How to run

Requires Python 3.10+ and no third-party packages. The game engine is part of the organisers' participant kit and is not included; obtain a copy of the QuantStorm 2026 "Divided Oracle" kit (the folder containing `engine.py`, `game_config.py`, `bot_loader.py`, `backtester.py` and `strategies/`).

```bash
# Re-run the evaluation table
python eval/run_eval.py --kit /path/to/kit --seeds 1 2 3 --out eval/results.csv

# Duel one bot against a reference strategy with the kit's backtester
python /path/to/kit/backtester.py --bot1 quantstorm_v2.py --bot2 /path/to/kit/strategies/adaptive_bidder.py

# Check a file would pass the submission gate (imports, forbidden constructs, interface)
python /path/to/kit/backtester.py --validate quantstorm_v2.py
```

Both bot files passed the kit's `--validate` check when run locally (output not committed).

## Notes and limitations

- The per-round power value table is hand-calibrated and shared by V1 and V2. No closed form exists for it without the field's bid distribution, so it is the main remaining source of tuning risk.
- The opponent model assumes honest Maker centring (the rulebook's own measurements indicate quote distortion does not pay under the maker obligation). Against a deliberately deceptive Maker the trust factor and feasibility clipping cap, but do not remove, the damage.
- The submission header's name and roll-number fields have been redacted for public release. V2's docstring titles itself `my_bot.py` and refers to V1 as `my_bot_v1.py` (the submission file names); in this repository they are `quantstorm_v2.py` and `my_bot.py`. References to `RULEBOOK.md` in the docstrings point to the organisers' rulebook, which is not redistributed.
