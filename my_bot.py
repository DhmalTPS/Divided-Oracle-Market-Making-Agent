# Name: (redacted for public release)
# College: IIT Mandi
# Roll Number: (redacted for public release)

"""
my_bot.py -- Divided Oracle entry
==================================

Strategy summary
-----------------
PRICING
  S is estimated as my_revealed_sum + best_available_estimate_of_opponent's
  revealed_sum (their unrevealed coins and my own unrevealed coins are both
  mean-zero, so this is an unbiased estimator of S).

  The opponent-sum estimate is built, in order of preference:
    1. A live FORESIGHT leak this round -- exact ground truth on a (near-)
       complete sample of their currently-revealed coins.
    2. The current round's live quote, when I am Taker and the opponent is
       Maker this round (freshest anchor: an honestly-centred quote is
       centred on the maker's own revealed sum).
    3. The most recent PAST round in which the opponent was Maker, read off
       obs.contracts (open_bid/open_ask survive there for the whole deal --
       RULEBOOK.md 9 calls this "the only clean read of a Maker's
       information"). This lets me use opponent information even in
       quote(), when I'm the Maker and have no live quote to read.
    4. 0.0 if nothing is available yet (round 1, no foresight).

  As Maker I still use this estimate to centre my own quote. The maker
  obligation (RULEBOOK.md 7.2) is scored against config.straddle_prob(),
  which assumes the Maker only knows its own coins -- so if I actually know
  more (via an anchor or FORESIGHT), centring on the better estimate raises
  my true straddle rate above what the formula assumes I can achieve, which
  is strictly positive EV. It never costs anything to centre correctly.

WIDTH
  Always the round's floor (obs.final_cap). WIDTH_PREMIUM strictly penalises
  width above the floor, and MAKER_OBLIGATION's expected value under honest
  centring is lam * (P_true(straddle) - p_w(width)); p_w(width) rises with
  width just as fast under the naive model as any information edge I might
  have, so the informational gap -- and therefore the EV -- is largest at
  the tightest legal width.

AUCTION
  Powers are bid at a shaded fraction of their calibrated per-round tick
  value (first price: you pay your own bid, so bidding fair value captures
  zero surplus). TRANSFORM is bid from both sides: fire it from a flat hand
  (upgrade), and optionally contest it as a defensive veto from a decisive
  hand when the read says the opponent's hand looks flat too (denial).

NEGOTIATION ENDGAME
  On the final turn, countering forces a midpoint fill: I become short,
  I pay the forcing fee, and the price shifts by whatever TRICK_ROOM /
  STEALTH_ROCK magnitude either side holds. I explicitly value all three
  choices (ACCEPT_BUY, ACCEPT_SELL, force) against my S-estimate and the
  forcing fee, and take whichever is best, rather than defaulting to a
  fixed threshold.

TRANSFORM
  Fire the swap from a flat revealed hand (little decided, upside from a
  new hand); decline from a decisive one, which is a legitimate defensive
  play since winning the power removes it from the deal either way.

Robustness
  Every method is wrapped so that a surprising obs shape (frozensets,
  empty tuples, round 1 with no history) degrades gracefully rather than
  raising -- a raised exception forfeits the rest of the deal to fallbacks,
  which is far worse than a slightly-suboptimal number.
"""

import random


# Calibrated tick value of each power, by round. Same order-of-magnitude
# shape as the reference measurements in RULEBOOK.md/adaptive_bidder.py:
# FORESIGHT climbs (more of the opponent's hand is revealed as rounds pass),
# SUBSTITUTE and TRICK_ROOM/STEALTH_ROCK fall (less deal left to protect /
# fewer future forced fills to shift), TRANSFORM falls off after round 3
# (not drawable there) and is state-dependent besides.
POWER_VALUES = {
    "FORESIGHT":    {1: 0.80, 2: 1.20, 3: 1.55, 4: 2.00, 5: 2.05},
    "TRICK_ROOM":   {1: 1.15, 2: 0.85, 3: 0.60, 4: 0.60, 5: 0.50},
    "SUBSTITUTE":   {1: 1.45, 2: 1.15, 3: 0.95, 4: 0.60, 5: 0.30},
    "STEALTH_ROCK": {1: 1.55, 2: 1.10, 3: 0.80, 4: 0.75, 5: 0.00},
    "TRANSFORM":    {1: 1.55, 2: 1.25, 3: 1.30, 4: 0.00, 5: 0.00},
}

#: Fraction of fair value to bid in a first-price auction. Broad basin
#: (0.55-0.65 measured indistinguishable on the reference engine); shaded
#: rather than bidding true value, since paying your own bid means honest
#: value-bidding captures zero surplus by construction.
SHADE = 0.60

#: How flat MY revealed sum has to be before the hand is worth trading away.
FLAT_THRESHOLD = 1.0
#: How flat the OPPONENT has to look (by the anchor read) before paying to
#: deny them the swap is worth considering at all.
OPP_FLAT_THRESHOLD = 2.5
#: Fraction of the swap's own value to bid purely for denial, when my hand
#: is decisive but the opponent's looks flat too. Kept modest: at 24 TE the
#: budget itself is worth only 1.92 ticks (TE_SALVAGE=0.08), so overpaying
#: for a defensive veto is a real opportunity cost, not a free option.
DENIAL_WEIGHT = 0.35

#: Small positive margin (in ticks) required before crossing a spread
#: instead of countering -- avoids trading on estimation noise alone.
ACCEPT_MARGIN = 0.35


class Bot:
    name = "OracleCandidate"

    # ------------------------------------------------------------ reset --
    def reset(self, seat, config, seed):
        self.seat = seat
        self.config = config
        self.rng = random.Random(seed)
        # No further per-deal state is needed: everything else is derived
        # fresh from obs (my_revealed, contracts, foresight) every call, so
        # there is nothing to get out of sync across the five rounds.

    # ------------------------------------------------------- helpers -----
    def _shift_magnitude(self, powers):
        """Total fill-shift magnitude a set of powers controls.

        engine.shift_sources computes the same quantity but lives in
        `engine`, which is not on the permitted import list, so this is
        the documented reimplementation RULEBOOK.md 5 asks for.
        """
        total = 0
        for name in ("TRICK_ROOM", "STEALTH_ROCK"):
            if name in powers:
                spec = self.config.POWERS.get(name)
                if spec:
                    total += int(spec["magnitude"])
        return total

    def _opponent_anchor(self, obs):
        """Best available read of the opponent's CURRENT revealed sum.

        Returns a float estimate, or None if nothing is known yet.
        Order of preference is documented in the module docstring.
        """
        # 1. A live FORESIGHT leak this round is exact ground truth.
        if obs.foresight:
            return float(sum(obs.foresight))

        # 2/3. The most recent quote (this deal) opened by the opponent as
        # Maker -- from obs.contracts, which the engine populates for every
        # round already played this deal, in both roles.
        best = None
        best_round = -1
        opp_seat = 1 - obs.seat
        for c in obs.contracts:
            try:
                if c.maker_seat == opp_seat and c.round > best_round:
                    best = (c.open_bid + c.open_ask) / 2.0
                    best_round = c.round
            except (AttributeError, TypeError, IndexError):
                continue
        return best

    def _live_anchor(self, obs, quote):
        """Anchor from the quote currently on the table, when it is fresh
        (this round, opponent is Maker, I have no better FORESIGHT read).
        """
        if obs.foresight:
            return float(sum(obs.foresight))
        if quote is not None and not obs.is_maker:
            try:
                return (quote[0] + quote[1]) / 2.0
            except (TypeError, IndexError):
                pass
        return self._opponent_anchor(obs)

    def _estimate_S(self, obs, quote=None):
        """Unbiased point estimate of the hidden score S."""
        try:
            k_mine = obs.k_mine
        except Exception:
            k_mine = sum(obs.my_revealed) if obs.my_revealed else 0

        if quote is not None:
            opp = self._live_anchor(obs, quote)
        else:
            opp = self._opponent_anchor(obs)

        if opp is None:
            opp = 0.0
        return float(k_mine) + float(opp)

    def _power_value(self, obs, name):
        table = POWER_VALUES.get(name)
        if not table:
            return 0.5
        return table.get(obs.round, 0.5)

    def _transform_value(self, obs):
        """What winning TRANSFORM is worth to me right now, in ticks."""
        swap = self._power_value(obs, "TRANSFORM")
        try:
            k = obs.k_mine
        except Exception:
            k = 0
        if abs(k) <= FLAT_THRESHOLD:
            return swap

        opp = self._opponent_anchor(obs)
        if opp is not None and abs(opp) <= OPP_FLAT_THRESHOLD:
            return swap * DENIAL_WEIGHT
        return 0.0

    # ---------------------------------------------------------- bid ------
    def bid(self, obs, offered):
        try:
            if not offered or obs.te_mine <= 0:
                return {}

            out = {}
            for name in offered:
                if name == "TRANSFORM":
                    v = self._transform_value(obs)
                else:
                    v = self._power_value(obs, name)

                if v <= 0:
                    continue

                fair_te = v / max(self.config.TE_SALVAGE, 1e-9)
                amount = int(fair_te * SHADE)
                amount = max(0, min(amount, obs.te_mine))
                if amount > 0:
                    out[name] = amount

            # Guard the total against our own rounding drift -- never bid
            # more than te_mine in aggregate, since an over-budget vector
            # is zeroed outright rather than scaled down.
            total = sum(out.values())
            if total > obs.te_mine:
                # Trim the cheapest-value contested powers first, keeping
                # whichever bids are worth the most per TE spent.
                order = sorted(out.keys(), key=lambda n: -self._value_per_te(obs, n))
                budget = obs.te_mine
                trimmed = {}
                for name in order:
                    amt = min(out[name], budget)
                    if amt > 0:
                        trimmed[name] = amt
                        budget -= amt
                out = trimmed

            return out
        except Exception:
            return {}

    def _value_per_te(self, obs, name):
        if name == "TRANSFORM":
            v = self._transform_value(obs)
        else:
            v = self._power_value(obs, name)
        return v

    # -------------------------------------------------------- quote ------
    def quote(self, obs):
        try:
            v = self._estimate_S(obs)
            cap = obs.final_cap
            lo = round(v) - cap // 2
            return (lo, lo + cap)
        except Exception:
            cap = getattr(obs, "final_cap", 4)
            return (-(cap // 2), cap - (cap // 2))

    # ------------------------------------------------------- respond -----
    def respond(self, obs, quote, turn):
        try:
            bid, ask = quote
            v = self._estimate_S(obs, quote)
            n_turns = self.config.N_TURNS
            min_reduction = self.config.MIN_REDUCTION

            margin = ACCEPT_MARGIN
            if "SUBSTITUTE" in obs.powers_mine:
                # Downside capped this round -- worth crossing on a
                # thinner edge since the tail risk is bounded.
                margin -= 1.0

            edge_buy = v - ask
            edge_sell = bid - v

            if turn == n_turns:
                # Countering here forces the midpoint fill: I become short
                # (last_quoter_sells), pay FORCED_FILL_FEE, and the price
                # shifts by the net TRICK_ROOM/STEALTH_ROCK magnitude in
                # MY favour minus the opponent's.
                my_shift = self._shift_magnitude(obs.powers_mine)
                their_shift = self._shift_magnitude(obs.powers_theirs)
                shift = my_shift - their_shift
                mid = (bid + ask) // 2 + shift
                # As short seat, raw pnl ~= price - S; subtract the fee.
                force_val = (mid - v) - self.config.FORCED_FILL_FEE

                best = max(edge_buy, edge_sell, force_val)
                if best <= max(edge_buy, edge_sell) + 1e-9 and (
                    edge_buy >= edge_sell and edge_buy >= force_val
                ):
                    return "ACCEPT_BUY"
                if edge_sell >= edge_buy and edge_sell >= force_val:
                    return "ACCEPT_SELL"
                if force_val >= edge_buy and force_val >= edge_sell:
                    # Force the fill: counter with whatever legal (tight)
                    # range -- the engine forces at the midpoint on the
                    # final turn regardless of exactly what we submit here,
                    # as long as it's a legal COUNTER.
                    w = max(0, (ask - bid) - min_reduction)
                    center = max(bid, min(round(v), ask - w))
                    return ("COUNTER", center, center + w)
                return "ACCEPT_BUY"

            # Non-final turn: no forcing risk yet.
            if edge_buy > margin and edge_buy >= edge_sell:
                return "ACCEPT_BUY"
            if edge_sell > margin:
                return "ACCEPT_SELL"

            w = max(0, (ask - bid) - min_reduction)
            center = max(bid, min(round(v), ask - w))
            return ("COUNTER", center, center + w)
        except Exception:
            try:
                bid, ask = quote
                return "ACCEPT_BUY" if (ask - bid) >= 0 else "ACCEPT_SELL"
            except Exception:
                return "ACCEPT_BUY"

    # -------------------------------------------------- use_transform ----
    def use_transform(self, obs):
        try:
            k = obs.k_mine
        except Exception:
            k = 0
        return abs(k) <= FLAT_THRESHOLD
