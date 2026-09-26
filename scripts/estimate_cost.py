"""Estimate total tokens and dollar cost of a full Whodunit Curve run.

Because we resend paragraphs 1..t at every step t, total input tokens grow
roughly quadratically in the number of paragraphs. This script computes the
*exact* cumulative estimate (prefix sums, not just the N^2/2 rule of thumb) from
a parsed story, prints the estimated tokens and cost, and flags if the longest
prefix would exceed Jev's 32k-token input limit.

It makes NO API calls and spends nothing.

Usage:
    python scripts/estimate_cost.py data/parsed/<name>.json
    python scripts/estimate_cost.py data/parsed/<name>.json --limit 50

Caveats:
  - Token counts use tiktoken cl100k_base as a proxy for Jev's tokenizer, so
    treat the numbers as a close estimate, not a billed total.
  - Per-call overhead (question text + choice labels + JSON wrapping) is
    approximated; the friend's real build_context/build_choices may differ.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make `src/` importable when run as a plain script.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from detective_jev import config  # noqa: E402
from detective_jev.story import load_paragraphs  # noqa: E402
from detective_jev.tokens import count_tokens  # noqa: E402
from detective_jev import friend_stubs  # noqa: E402


def estimate(parsed_path: Path, limit: int | None) -> None:
    paragraphs = load_paragraphs(parsed_path)
    if limit is not None:
        paragraphs = paragraphs[:limit]
    n = len(paragraphs)
    if n == 0:
        print("No paragraphs found.")
        return

    # Fixed per-call overhead: the question + the choice labels/descriptions.
    # Uses the friend's stubs so the estimate tracks whatever they build.
    question = friend_stubs.build_question()
    choices = friend_stubs.build_choices(paragraphs)
    choice_text = " ".join(
        f"{k} {v or ''}" for k, v in (
            choices.items() if isinstance(choices, dict) else {c: None for c in choices}.items()
        )
    )
    overhead = count_tokens(question) + count_tokens(choice_text) + 20  # +20 JSON slack

    # Per-paragraph token counts, then cumulative prefix at each step t.
    per_para = [count_tokens(p) for p in paragraphs]
    prefix = 0
    total_input_tokens = 0
    longest_prefix_tokens = 0
    for t in range(1, n + 1):
        prefix += per_para[t - 1]
        call_tokens = prefix + overhead
        total_input_tokens += call_tokens
        longest_prefix_tokens = call_tokens  # last iteration is the longest

    avg_para = sum(per_para) / n
    quadratic_approx = int(avg_para * n * (n + 1) / 2 + overhead * n)
    cost = total_input_tokens / 1_000_000 * config.PRICE_PER_1M_INPUT_TOKENS

    print("=" * 60)
    print("Whodunit Curve — cost estimate")
    print("=" * 60)
    print(f"Story file           : {parsed_path}")
    print(f"Paragraphs (N)       : {n}")
    print(f"Avg tokens/paragraph : {avg_para:.1f}")
    print(f"Per-call overhead    : {overhead} tokens (question + choices)")
    print(f"Model                : {config.MODEL_ID}")
    print(f"Price                : ${config.PRICE_PER_1M_INPUT_TOKENS:.3f} / 1M input tokens (output free)")
    print("-" * 60)
    print(f"Total input tokens   : {total_input_tokens:,}")
    print(f"  (N^2/2 rule-of-thumb: ~{quadratic_approx:,})")
    print(f"Estimated cost       : ${cost:.4f}")
    print("-" * 60)
    print(f"Longest prefix (t=N) : {longest_prefix_tokens:,} tokens")
    print(f"Jev input limit      : {config.MAX_INPUT_TOKENS:,} tokens")

    if longest_prefix_tokens > config.MAX_INPUT_TOKENS:
        # Find the first t that overflows, so the friend knows where to trim.
        prefix = 0
        overflow_t = None
        for t in range(1, n + 1):
            prefix += per_para[t - 1]
            if prefix + overhead > config.MAX_INPUT_TOKENS:
                overflow_t = t
                break
        print("!" * 60)
        print(f"WARNING: prefix exceeds the {config.MAX_INPUT_TOKENS:,}-token limit "
              f"starting at paragraph t={overflow_t}.")
        print("Later calls will fail unless build_context() trims/summarizes the")
        print("earliest paragraphs. (That trimming is the friend's call.)")
        print("!" * 60)
    else:
        print("OK: even the longest prefix fits within the input limit.")
    print("=" * 60)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("parsed_path", type=Path, help="Path to data/parsed/<name>.json")
    ap.add_argument("--limit", type=int, default=None,
                    help="Only estimate the first N paragraphs (for a quick check).")
    args = ap.parse_args()

    if not args.parsed_path.exists():
        print(f"File not found: {args.parsed_path}")
        print("Parse a story first, e.g.:")
        print("  python -m detective_jev.story <gutenberg_txt_url> <name>")
        raise SystemExit(1)

    estimate(args.parsed_path, args.limit)


if __name__ == "__main__":
    main()
