# Name: (redacted for public release)
# College: IIT Mandi
# Roll Number: (redacted for public release)

"""
my_bot.py -- Divided Oracle entry, V2
=======================================

V1 (archived as my_bot_v1.py, scored +51.53 on the practice board) used a
point estimate for S everywhere and several hand-tuned constants: a fixed
accept margin, a fixed auction shade, "always quote at the floor width",
and a flat abs(k_mine)<=1 TRANSFORM threshold. Those aren't wrong, but
they're guesses standing in for numbers the engine actually lets you
compute exactly. V2 replaces each guess with the corresponding closed-form
calculation wherever config exposes the machinery to do it, and keeps the
old heuristic as a documented fallback only where no such machinery exists.

WHAT CHANGED, AND WHY, ONE BLOCK AT A TIME
-------------------------------------------

1. UNCERTAINTY IS NOW TRACKED, NOT JUST THE MEAN.
   Every fair coin has variance 1, and revealed coins carry zero residual
   variance since they're known exactly. So at any point I can state an
   EFFECTIVE UNSEEN COUNT: the number of coins (mine + the opponent's)
   still unaccounted for, given whatever anchor I currently trust:

       effective_unseen = (my own unrevealed coins)
                         + (opponent coins beyond the freshest anchor I hold)

   With no anchor at all this collapses to 40 - 4*round, exactly the
   default `unseen` config.straddle_prob() uses for an honest Maker who
   only knows their own coins -- a good sign the accounting is consistent
   with the engine's own model. With a FORESIGHT leak or a fresh opponent
   quote, effective_unseen shrinks, because I actually know more.

2. QUOTE WIDTH IS COMPUTED, NOT ASSUMED.
   V1 always opened at obs.final_cap on the argument that MAKER_OBLIGATION
   and WIDTH_PREMIUM should favour the tightest legal width. That argument
   is now a two-line computation instead of an assumption: for every legal
   width w in [final_cap, spread_cap],

       EV(w) = MAKER_OBLIGATION * (P_true(w) - P_naive(w))
               - WIDTH_PREMIUM * max(0, w - final_cap)

   where P_true is my ACTUAL straddle probability (using effective_unseen,
   via config.straddle_prob(..., unseen=effective_unseen)) and P_naive is
   what the obligation formula assumes I can achieve (the default unseen).
   I take the width that maximises this, straight out of config's own
   exact lattice calculation -- no normal approximation, no guess.

3. THE ACCEPT/COUNTER MARGIN SCALES WITH ACTUAL UNCERTAINTY.
   V1 used a flat 0.35-tick margin before crossing a spread. That number
   should shrink as effective_unseen shrinks (round 5 with a fresh anchor
   is a very different confidence level than round 1 with none). The
   margin is now proportional to sqrt(effective_unseen), the true standard
   deviation of the residual under the fair-coin model.

4. TRANSFORM IS AN EV COMPARISON, NOT A FIXED THRESHOLD.
   Winning TRANSFORM swaps whole hands, REVEALED COINS INCLUDED (rulebook
   5). That means if I hold a trustworthy read of the opponent's revealed
   sum (FORESIGHT, or a recent honest quote), I can compute EXACTLY what my
   new k_mine would be if I swap: their current revealed sum. So the
   decision becomes a straight comparison of informational edge --
   |their revealed sum| vs |my revealed sum| -- rather than a guess about
   whether MY hand alone looks "flat". A bigger |revealed sum| is a bigger
   trading edge for whoever holds it for the rest of the deal (it lets that
   side price the remaining rounds with much less residual variance), so I
   want to end up holding the more extreme of the two.

   With no opponent read available, I fall back to comparing my own
   |k_mine| against E[|sum of n fair coins|] for n = 4*round -- the EXACT
   combinatorial baseline for "how extreme is average", computed with
   math.comb rather than approximated.

5. AUCTION SHADE ADAPTS WITHIN THE DEAL.
   Powers are still valued off a per-round table and shaded in a first-
   price auction (bidding true value captures zero surplus by
   construction). But the shade itself now moves within a deal based on
   what obs.auction_log has shown me about this specific opponent's
   aggression so far: if they've been paying up for powers, I raise my
   own shade toward true value to keep contesting; if they've been
   passive, I shade harder and extract more surplus. auction_log is
   public and already handed to me every round -- V1 never looked at it
   after the fact.

6. OPPONENT QUOTES ARE TRUSTED, BUT NOT BLINDLY.
   RULEBOOK.md's own measurements (echoed in rational.py's docstring) say
   quote distortion is priced monotonically and does not pay under the
   maker obligation -- so treating an opponent's opening quote as a
   genuine signal is the right baseline assumption, not a leap of faith.
   Still, every anchor read is clipped to what's physically possible: a
   maker who has revealed n coins cannot have a revealed sum outside
   [-n, n], however they chose to quote. This costs nothing against an
   honest opponent and caps the damage from a dishonest one.

Everything above is either read straight from config's own exact methods
or derived from the fair-coin model with no free parameters. The only
remaining hand-tuned numbers are the per-round power values (no closed
form exists for those without the actual field's bid distribution) and a
small number of shading/margin scale constants, each documented at its
definition and each cross-checked empirically against the reference bots
before submission.
"""

import random
from math import comb, sqrt


# Calibrated tick value of each power, by round. No closed form exists for
# this without knowing the field's bid distribution, so it stays a
# hand-tuned table -- but every OTHER number in this file that has a closed
# form now uses one instead of a guess.
POWER_VALUES = {
    "FORESIGHT":    {1: 0.80, 2: 1.20, 3: 1.55, 4: 2.00, 5: 2.05},
    "TRICK_ROOM":   {1: 1.15, 2: 0.85, 3: 0.60, 4: 0.60, 5: 0.50},
    "SUBSTITUTE":   {1: 1.45, 2: 1.15, 3: 0.95, 4: 0.60, 5: 0.30},
    "STEALTH_ROCK": {1: 1.55, 2: 1.10, 3: 0.80, 4: 0.75, 5: 0.00},
    "TRANSFORM":    {1: 1.55, 2: 1.25, 3: 1.30, 4: 0.00, 5: 0.00},
}

#: Baseline fraction of fair value to bid (first price -> bid true value
#: captures zero surplus). Adapted per-round within a deal by
#: _adaptive_shade() using observed opponent aggression; this is the
#: centre of that adjustment, not a fixed number used everywhere.
BASE_SHADE = 0.58
SHADE_MIN = 0.35
SHADE_MAX = 0.85

#: Scale on sqrt(effective_unseen) for the accept/counter margin. Small,
#: since it's a genuine tick-value threshold, not a probability.
MARGIN_SCALE = 0.14

#: Extra ticks required (beyond raw edge) before paying purely for
#: TRANSFORM as a denial play when neither side's revealed sum is
#: available for comparison -- kept modest, the same spirit as V1's
#: DENIAL_WEIGHT, folded into the EV comparison instead of a separate flag.
DENIAL_MARGIN = 0.5

#: How much a revealed-sum anchor read is discounted before use, as a hedge
#: against a Maker who distorts their quote. Costs ~nothing against an
#: honest opponent (rulebook + rational.py's own measurements say honest
#: centring is the equilibrium play) and caps exposure to a dishonest one.
ANCHOR_TRUST = 0.92


def _expected_abs_sum(n: int) -> float:
    """Exact E[|sum of n fair +-1 coins|], via direct enumeration.

    n <= 20 always in this game, so this is at most a few thousand
    arithmetic ops -- irrelevant next to the 50ms hard limit, and it uses
    only math.comb, which is on the allowed import list.
    """
    if n <= 0:
        return 0.0
    total = 0.0
    for k in range(n + 1):
        s = abs(2 * k - n)
        total += comb(n, k) * s
    return total / (2 ** n)


class Bot:
    name = "OracleCandidate_V2"

    # ------------------------------------------------------------ reset --
    def reset(self, seat, config, seed):
        self.seat = seat
        self.config = config
        self.rng = random.Random(seed)
        # No other per-deal state: everything below is re-derived from obs
        # (my_revealed, contracts, auction_log, foresight) on every call.

    # --------------------------------------------------------- geometry --
    def _shift_magnitude(self, powers):
        total = 0
        for name in ("TRICK_ROOM", "STEALTH_ROCK"):
            if name in powers:
                spec = self.config.POWERS.get(name)
                if spec:
                    total += int(spec["magnitude"])
        return total

    def _feasible_bound(self, n_revealed: int) -> int:
        """Max |revealed sum| physically possible with n coins revealed."""
        return max(0, int(n_revealed))

    # ---------------------------------------------------- opponent read --
    def _opponent_anchor(self, obs):
        """Best (estimate, anchor_round) read of the opponent's revealed sum.

        anchor_round is how many of THEIR reveal-rounds the estimate
        reflects (0 if nothing is known), used to size effective_unseen.
        Every read is clipped to what n=4*anchor_round coins can produce,
        then shrunk by ANCHOR_TRUST as a hedge against quote distortion.
        """
        # 1. FORESIGHT: near-exact ground truth on their currently
        #    revealed coins (a full sample through round 4, a 16/20 sample
        #    in round 5).
        if obs.foresight:
            n = 4 * obs.round
            est = float(sum(obs.foresight))
            bound = self._feasible_bound(n)
            est = max(-bound, min(est, bound))
            return est * ANCHOR_TRUST, obs.round

        # 2/3. Most recent round (this deal) the opponent opened as Maker,
        # read off obs.contracts -- available even while I'm Maker myself.
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
        if best is None:
            return None, 0
        bound = self._feasible_bound(4 * best_round)
        best = max(-bound, min(best, bound))
        return best * ANCHOR_TRUST, best_round

    def _live_anchor(self, obs, quote):
        """Anchor from the CURRENT round's live quote, when it's fresher
        than anything obs.contracts could hold (contracts only records
        rounds already settled this deal).
        """
        if obs.foresight:
            return self._opponent_anchor(obs)
        if quote is not None and not obs.is_maker:
            try:
                mid = (quote[0] + quote[1]) / 2.0
                bound = self._feasible_bound(4 * obs.round)
                mid = max(-bound, min(mid, bound))
                return mid * ANCHOR_TRUST, obs.round
            except (TypeError, IndexError):
                pass
        return self._opponent_anchor(obs)

    # --------------------------------------------------- S estimation ----
    def _estimate(self, obs, quote=None):
        """Return (S_point_estimate, effective_unseen)."""
        try:
            k_mine = float(obs.k_mine)
        except Exception:
            k_mine = float(sum(obs.my_revealed)) if obs.my_revealed else 0.0

        if quote is not None:
            opp_est, anchor_round = self._live_anchor(obs, quote)
        else:
            opp_est, anchor_round = self._opponent_anchor(obs)

        if opp_est is None:
            opp_est = 0.0
            anchor_round = 0

        my_unrevealed = max(0, self.config.N_PRIVATE - 4 * obs.round)
        opp_unrevealed = max(0, self.config.N_PRIVATE - 4 * anchor_round)
        effective_unseen = my_unrevealed + opp_unrevealed

        return k_mine + opp_est, effective_unseen

    # ------------------------------------------------------- power vals --
    def _power_value(self, obs, name):
        table = POWER_VALUES.get(name)
        if not table:
            return 0.5
        return table.get(obs.round, 0.5)

    def _adaptive_shade(self, obs):
        """Shift BASE_SHADE toward SHADE_MAX/MIN using in-deal auction
        history (obs.auction_log is public and persists for the whole
        deal). A first-price bid should respond to how hard the opponent
        has actually been contesting, not use a single constant blindly.
        """
        try:
            opp_seat = 1 - obs.seat
            opp_wins = [e for e in obs.auction_log
                       if e.get("seat") == opp_seat and e.get("cost", 0) > 0]
            if not opp_wins:
                return BASE_SHADE
            avg_opp_cost = sum(e["cost"] for e in opp_wins) / len(opp_wins)
            # Rough per-TE aggression signal: an opponent regularly paying
            # >8 TE for a power (roughly a 1-tick value at full shade) is
            # bidding aggressively; scale toward SHADE_MAX. A passive
            # opponent (small or zero wins) lets us shade harder.
            aggression = min(1.0, avg_opp_cost / 12.0)
            shade = BASE_SHADE + aggression * (SHADE_MAX - BASE_SHADE)
            return max(SHADE_MIN, min(SHADE_MAX, shade))
        except Exception:
            return BASE_SHADE

    def _transform_value(self, obs):
        """EV of winning TRANSFORM, in ticks -- see module docstring 4."""
        try:
            my_k = float(obs.k_mine)
        except Exception:
            my_k = 0.0
        opp_est, _ = self._opponent_anchor(obs)
        swap = self._power_value(obs, "TRANSFORM")

        if opp_est is not None:
            # Direct comparison: whichever side is more extreme is worth
            # holding for the rest of the deal.
            edge_gain = abs(opp_est) - abs(my_k)
            if edge_gain > 0:
                return swap
            if edge_gain > -DENIAL_MARGIN:
                # Close call: still worth a partial bid to deny the
                # opponent a hand that looks about as good as mine.
                return swap * 0.4
            return 0.0

        # No opponent read at all: compare my own revealed sum against the
        # exact expected magnitude of an average hand at this reveal count.
        baseline = _expected_abs_sum(4 * obs.round)
        if abs(my_k) < baseline:
            return swap
        return 0.0

    # ---------------------------------------------------------- bid ------
    def bid(self, obs, offered):
        try:
            if not offered or obs.te_mine <= 0:
                return {}

            shade = self._adaptive_shade(obs)
            salvage = max(self.config.TE_SALVAGE, 1e-9)

            out = {}
            for name in offered:
                if name == "TRANSFORM":
                    v = self._transform_value(obs)
                else:
                    v = self._power_value(obs, name)
                if v <= 0:
                    continue
                fair_te = v / salvage
                amount = int(fair_te * shade)
                amount = max(0, min(amount, obs.te_mine))
                if amount > 0:
                    out[name] = amount

            total = sum(out.values())
            if total > obs.te_mine:
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
            return self._transform_value(obs)
        return self._power_value(obs, name)

    # -------------------------------------------------------- quote ------
    def quote(self, obs):
        try:
            v, eu = self._estimate(obs)
            r = obs.round
            floor = obs.final_cap
            cap = obs.spread_cap
            lam = self.config.MAKER_OBLIGATION
            prem = self.config.WIDTH_PREMIUM

            best_w = floor
            if lam == 0 and prem == 0:
                width = floor
            else:
                best_ev = None
                for w in range(floor, cap + 1):
                    p_true = self.config.straddle_prob(r, w, unseen=eu)
                    p_naive = self.config.straddle_prob(r, w)
                    ev = lam * (p_true - p_naive) - prem * max(0, w - floor)
                    if best_ev is None or ev > best_ev + 1e-9:
                        best_ev = ev
                        best_w = w
                width = best_w

            lo = round(v) - width // 2
            return (lo, lo + width)
        except Exception:
            cap = getattr(obs, "final_cap", 4)
            return (-(cap // 2), cap - (cap // 2))

    # ------------------------------------------------------- respond -----
    def respond(self, obs, quote, turn):
        try:
            bid, ask = quote
            v, eu = self._estimate(obs, quote)
            n_turns = self.config.N_TURNS
            min_reduction = self.config.MIN_REDUCTION

            margin = MARGIN_SCALE * sqrt(max(0, eu))
            if "SUBSTITUTE" in obs.powers_mine:
                # Downside capped this round -- worth crossing on a
                # thinner edge since the tail risk is bounded.
                margin = max(0.0, margin - 1.0)

            edge_buy = v - ask
            edge_sell = bid - v

            if turn == n_turns:
                my_shift = self._shift_magnitude(obs.powers_mine)
                their_shift = self._shift_magnitude(obs.powers_theirs)
                shift = my_shift - their_shift
                mid = (bid + ask) // 2 + shift
                force_val = (mid - v) - self.config.FORCED_FILL_FEE

                if edge_buy >= edge_sell and edge_buy >= force_val:
                    return "ACCEPT_BUY"
                if edge_sell >= edge_buy and edge_sell >= force_val:
                    return "ACCEPT_SELL"
                w = max(0, (ask - bid) - min_reduction)
                center = max(bid, min(round(v), ask - w))
                return ("COUNTER", center, center + w)

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
            my_k = float(obs.k_mine)
        except Exception:
            my_k = 0.0
        opp_est, _ = self._opponent_anchor(obs)
        if opp_est is not None:
            return abs(opp_est) > abs(my_k)
        baseline = _expected_abs_sum(4 * obs.round)
        return abs(my_k) < baseline
