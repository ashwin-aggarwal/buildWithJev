"""Make ONE real Jev call to confirm the key, billing, and schema all work.

This is the ONLY script that spends money, and only one call's worth
(fractions of a cent). Run it after you've set OPENROUTER_API_KEY in .env and
added credits (see SETUP.md).

Usage:
    python scripts/smoke_test.py           # one real call
    python scripts/smoke_test.py --mock    # no network / no spend (plumbing test)
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from detective_jev import query, JevError, config  # noqa: E402

TOY_CONTEXT = (
    "Lady Ashby was found dead in the locked study. The butler was seen leaving "
    "the room minutes before. The gardener had argued with her that morning about "
    "unpaid wages. The niece stood to inherit the entire estate."
)
TOY_QUESTION = "Who is the most likely killer?"
TOY_CHOICES = {
    "butler": "The butler, last seen leaving the study",
    "gardener": "The gardener, who argued about wages",
    "niece": "The niece, who inherits the estate",
}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mock", action="store_true", help="Dry run: no network, no spend.")
    args = ap.parse_args()

    if not args.mock and config.PROVIDER == "openrouter" and not os.getenv(config.OPENROUTER_KEY_ENV):
        print(f"{config.OPENROUTER_KEY_ENV} is not set.")
        print("Set it in .env (see SETUP.md), or run: python scripts/smoke_test.py --mock")
        raise SystemExit(1)

    mode = "MOCK (no spend)" if args.mock else f"REAL via {config.PROVIDER}"
    print(f"Smoke test — {mode}")
    print(f"Model (pinned): {config.MODEL_ID}\n")

    try:
        result = query(TOY_CONTEXT, TOY_QUESTION, TOY_CHOICES, mock=args.mock, use_cache=False)
    except JevError as exc:
        print(f"FAILED: {exc}")
        raise SystemExit(1)

    print("Probabilities:")
    for opt, p in sorted(result["probabilities"].items(), key=lambda kv: kv[1], reverse=True):
        bar = "#" * int(round(p * 30))
        print(f"  {opt:10s} {p:5.2f}  {bar}")
    print()
    print(f"Winner        : {result['answer']}")
    print(f"Confidence    : {result['confidence']}")
    print(f"Latency       : {result['latency_ms']} ms")
    print(f"Input tokens  : {result['input_tokens']}")
    print(f"Resolved model: {result['model']}")
    print(f"Cost          : {result['cost']}")
    print()
    print("Smoke test passed — key, billing, and schema all work."
          if not args.mock else "Mock smoke test passed — plumbing works (no spend).")


if __name__ == "__main__":
    main()
