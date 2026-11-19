"""Head-to-head evaluation of the V1 and V2 bots against the organiser's
reference strategies, using the official Divided Oracle engine.

The engine is not redistributed in this repository. Point --kit at a local
copy of the QuantStorm 2026 participant kit (the folder that contains
engine.py, game_config.py, bot_loader.py and strategies/).

    python eval/run_eval.py --kit /path/to/kit --seeds 1 2 3 --out eval/results.csv

Every match is mirrored (each deal is replayed with hands and Maker roles
swapped), so luck of the deal cancels and the result reflects decisions only.
PnL is reported in ticks from the first-named bot's seat.
"""

from __future__ import annotations

import argparse
import csv
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

BOTS = {
    "V1": ROOT / "my_bot.py",
    "V2": ROOT / "quantstorm_v2.py",
}
REFERENCE = ("naive_ev", "rational", "adaptive_bidder")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kit", required=True, help="path to the organiser kit")
    ap.add_argument("--seeds", type=int, nargs="+", default=[1, 2, 3])
    ap.add_argument("--n_deals", type=int, default=None,
                    help="direct deals per match (default: engine default)")
    ap.add_argument("--out", default=None, help="optional CSV output path")
    args = ap.parse_args()

    kit = Path(args.kit).resolve()
    sys.path.insert(0, str(kit))
    from bot_loader import load_for_testing  # noqa: E402
    from engine import play_match  # noqa: E402
    from game_config import DEFAULT_CONFIG  # noqa: E402

    classes = {k: load_for_testing(p, quiet=True) for k, p in BOTS.items()}
    for name in REFERENCE:
        classes[name] = load_for_testing(kit / "strategies" / f"{name}.py", quiet=True)

    pairs = [(b, r) for b in BOTS for r in REFERENCE] + [("V2", "V1")]
    rows = []
    for a, b in pairs:
        for seed in args.seeds:
            res = play_match(classes[a], classes[b], config=DEFAULT_CONFIG,
                             seed=seed, mirror=True, n_deals=args.n_deals,
                             bot_a_name=a, bot_b_name=b)
            rows.append({"bot": a, "opponent": b, "seed": seed,
                         "deals": len(res.deals), "pnl": round(res.pnl[0], 2)})

    print(f"{'bot':<4} {'opponent':<16} " + " ".join(f"seed{s:>3}" for s in args.seeds)
          + "     mean")
    for a, b in pairs:
        vals = [r["pnl"] for r in rows if r["bot"] == a and r["opponent"] == b]
        print(f"{a:<4} {b:<16} " + " ".join(f"{v:>+7.2f}" for v in vals)
              + f"  {statistics.mean(vals):>+7.2f}")

    if args.out:
        with open(args.out, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
