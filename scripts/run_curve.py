"""Run the Whodunit Curve: query Jev at every paragraph and log the distribution.

This is thin orchestration only. It wires together three pieces and makes no
decisions about choice/prompt content:
  - story paragraphs  (data/parsed/<name>.json)
  - the friend's build_choices / build_question / build_context  (friend_stubs)
  - the Jev client's query()

Output: data/results/<name>.jsonl — one JSON row per paragraph:
    {t, answer, probabilities, confidence, latency_ms, input_tokens,
     model, cost, timestamp}

Resumable: on-disk caching means reruns don't re-bill; this script also skips
rows already present in the output file, so an interrupted run can be resumed.

Usage:
    python scripts/run_curve.py data/parsed/<name>.json --mock       # free dry run
    python scripts/run_curve.py data/parsed/<name>.json              # real (spends)
    python scripts/run_curve.py data/parsed/<name>.json --limit 20

Book mode (ledger pipeline; steps are chunks, not paragraphs):
    python scripts/run_curve.py --book <book_id> --mock \
        [--candidate-set full_cast|suspects_only] [--condition LABEL] [--posthoc]
  State at step t = rendered ledger 1..t-1 + chunk t raw; every question in
  config/inference_questions.yaml goes in ONE batched call. Rows keep all the
  fields above and add book_id, run_id, condition, candidate_set,
  inference_answers, option_order. Output: data/results/<book_id>__<run_id>.jsonl
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from detective_jev import query, JevError, config  # noqa: E402
from detective_jev.story import load_paragraphs  # noqa: E402
from detective_jev import friend_stubs  # noqa: E402


def _completed_steps(out_path: Path) -> set[int]:
    """Read which t values are already logged, so we can resume."""
    done: set[int] = set()
    if not out_path.exists():
        return done
    with out_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                done.add(json.loads(line)["t"])
            except (json.JSONDecodeError, KeyError):
                continue
    return done


def _first_overflow(paragraphs: list[str], question: str, choices) -> int | None:
    """First paragraph t whose state (paragraphs 1..t + question + choices) would
    exceed Jev's input limit, or None if the whole run fits. Approximate (same
    token proxy as estimate_cost.py), and makes no API calls."""
    from detective_jev.tokens import count_tokens

    overhead = count_tokens(question) + count_tokens(json.dumps(choices)) + 20
    prefix = 0
    for t in range(1, len(paragraphs) + 1):
        prefix += count_tokens(paragraphs[t - 1])
        if prefix + overhead > config.MAX_INPUT_TOKENS:
            return t
    return None


def run_book(args: argparse.Namespace) -> None:
    """Book mode: step through chunks with the ledger as history."""
    from detective_jev.pipeline import run_inference

    tag = "MOCK" if args.mock else "REAL, spends credits"
    print(f"Running book {args.book} ({tag}) candidate_set={args.candidate_set} condition={args.condition}")

    def show(t, n, row):
        cache = "cache" if row.get("cached") else ("mock" if args.mock else "live")
        print(f"  t={t:4d}/{n}  winner={row['answer']!s:14s} conf={row['confidence']}  [{cache}]")

    try:
        out_path, run_id = run_inference(args.book, mock=args.mock, candidate_set=args.candidate_set,
                                         condition=args.condition, posthoc=args.posthoc,
                                         limit=args.limit, out_path=args.out, progress=show)
    except JevError as exc:
        print(f"Jev call failed: {exc}")
        print("Stopping. Fix the issue and rerun — completed steps are cached/logged.")
        raise SystemExit(1)
    print(f"Done. run_id={run_id}. Results in {out_path}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("parsed_path", type=Path, nargs="?", help="Path to data/parsed/<name>.json")
    ap.add_argument("--mock", action="store_true", help="Dry run: fake distributions, no spend.")
    ap.add_argument("--limit", type=int, default=None, help="Only process first N paragraphs.")
    ap.add_argument("--out", type=Path, default=None, help="Output JSONL (default: data/results/<name>.jsonl)")
    ap.add_argument("--book", default=None, help="Book mode: run over data/books/<book_id> with its ledger.")
    ap.add_argument("--candidate-set", default="full_cast", choices=["full_cast", "suspects_only"],
                    help="Book mode: which characters are culprit options (a run condition).")
    ap.add_argument("--condition", default="default", help="Book mode: free-text run condition label.")
    ap.add_argument("--posthoc", action="store_true", default=config.POSTHOC_CONTRADICTIONS,
                    help="Book mode: write contradicts_prior to a per-run sidecar for later steps.")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if args.book:
        run_book(args)
        return
    if args.parsed_path is None:
        ap.error("give a parsed_path, or --book <book_id>")

    if not args.parsed_path.exists():
        print(f"File not found: {args.parsed_path}")
        print("Parse a story first: python -m detective_jev.story <url> <name>")
        raise SystemExit(1)

    paragraphs = load_paragraphs(args.parsed_path)
    if args.limit is not None:
        paragraphs = paragraphs[: args.limit]
    n = len(paragraphs)
    if n == 0:
        print("No paragraphs to process.")
        return

    name = args.parsed_path.stem
    out_path = args.out or (config.RESULTS_DIR / f"{name}.jsonl")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Built once, up front (friend-owned content).
    choices = friend_stubs.build_choices(paragraphs)
    question = friend_stubs.build_question()

    overflow_t = _first_overflow(paragraphs, question, choices)
    if overflow_t is not None:
        print(f"Paragraph mode can't finish this text: at paragraph {overflow_t} of {n} the "
              f"state would pass Jev's {config.MAX_INPUT_TOKENS:,}-token limit (paragraph mode "
              "resends paragraphs 1..t every step).")
        print("Refusing to start rather than fail partway after spending. Use book mode instead "
              "(see SETUP.md, Part C), or pass --limit to run only the first part.")
        raise SystemExit(1)

    done = _completed_steps(out_path)
    if done:
        print(f"Resuming: {len(done)} of {n} steps already logged in {out_path}")

    print(f"Running {n} steps ({'MOCK' if args.mock else 'REAL, spends credits'}) "
          f"with model {config.MODEL_ID} -> {out_path}")

    with out_path.open("a", encoding="utf-8") as fh:
        for t in range(1, n + 1):
            if t in done:
                continue
            context = friend_stubs.build_context(paragraphs, t)
            try:
                result = query(context, question, choices, mock=args.mock)
            except JevError as exc:
                print(f"Step t={t} failed: {exc}")
                print("Stopping. Fix the issue and rerun — completed steps are cached/logged.")
                raise SystemExit(1)

            row = {
                "t": t,
                "answer": result["answer"],
                "probabilities": result["probabilities"],
                "confidence": result["confidence"],
                "latency_ms": result["latency_ms"],
                "input_tokens": result["input_tokens"],
                "model": result["model"],
                "cost": result["cost"],
                "cached": result.get("cached", False),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            fh.flush()
            tag = "cache" if result.get("cached") else ("mock" if args.mock else "live")
            print(f"  t={t:4d}/{n}  winner={result['answer']!s:14s} "
                  f"conf={result['confidence']}  [{tag}]")

    print(f"Done. Results in {out_path}")


if __name__ == "__main__":
    main()
