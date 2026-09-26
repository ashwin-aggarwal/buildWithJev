"""One step from a book to a finished run.

    read the book (URL, .html/.txt/.pdf/.epub file, or upload)
      -> find the characters (one LLM call, or a free placeholder in mock mode)
      -> build the ledger (one Jev call per chunk)
      -> let Jev read it (one Jev call per chunk) -> data/results/<book_id>__<run_id>.jsonl

Used by `detective-jev run <file-or-url>` and by the web page's "Add a book"
tab. Every stage is idempotent and cached, so re-running is free and a crash
resumes where it stopped.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable

import yaml

from . import config, storage

logger = logging.getLogger("detective_jev.pipeline")

STAGES = ("read", "characters", "ledger", "inference")
STAGE_LABELS = {
    "read": "Read the book",
    "characters": "Find the characters",
    "ledger": "Build the case notes",
    "inference": "Jev reads the book",
}

Progress = Callable[[dict[str, Any]], None]


class Cancelled(RuntimeError):
    pass


# --- Cost estimate (arithmetic only) -----------------------------------------------

def estimate(book: dict[str, Any], n_characters: int | None = None) -> dict[str, Any]:
    """Rough cost and time of a real run, before spending anything.

    Calibrated on a real run (The Murder of Roger Ackroyd: 70k words,
    24 characters, 133 chunks: ledger ~0.65M tokens, inference ~1.16M).
    """
    from .tokens import count_tokens

    n = book["n_chunks"]
    chars = n_characters or 25
    chunk_tokens = [count_tokens(storage.chunk_text(book, t)) for t in range(1, n + 1)]
    ledger_tokens = sum(2 * c + 600 + 115 * chars for c in chunk_tokens)
    per_entry = 95
    question_tokens = 150 + 40 * (chars + 1)
    inference_tokens = sum(min(per_entry * (t - 1), config.LEDGER_TOKEN_BUDGET) + chunk_tokens[t - 1]
                           + question_tokens for t in range(1, n + 1))
    jev = (ledger_tokens + inference_tokens) / 1e6 * config.PRICE_PER_1M_INPUT_TOKENS

    book_tokens = sum(chunk_tokens) + 800
    extraction_cost = None
    for m in _prices().get("models", []):
        if m.get("id") == config.OPENROUTER_EXTRACTION_MODEL:
            extraction_cost = book_tokens / 1e6 * m["input_per_m"] + 8000 / 1e6 * m["output_per_m"]
    calls = 2 * n
    return {
        "chunks": n,
        "jev_calls": calls,
        "jev_cost": round(jev, 4),
        "characters_cost": round(extraction_cost, 4) if extraction_cost is not None else None,
        "minutes": round((calls * 0.35 + 60) / 60, 1),
        "assumed_characters": chars,
    }


def _prices() -> dict[str, Any]:
    try:
        return yaml.safe_load(config.MODEL_PRICES_PATH.read_text(encoding="utf-8")) or {}
    except OSError:
        return {}


# --- Inference loop (shared with scripts/run_curve.py --book) -----------------------

def _completed_steps(out_path: Path) -> set[int]:
    done: set[int] = set()
    if out_path.exists():
        for line in out_path.read_text(encoding="utf-8").splitlines():
            try:
                done.add(json.loads(line)["t"])
            except (json.JSONDecodeError, KeyError):
                continue
    return done


def run_inference(book_id: str, *, mock: bool = False, candidate_set: str = "full_cast",
                  condition: str = "default", posthoc: bool = False, limit: int | None = None,
                  out_path: Path | None = None, progress: Callable[..., None] | None = None
                  ) -> tuple[Path, str]:
    """Run (or resume) inference over every chunk. Returns (results path, run_id).

    progress(t, n, row) is called after each new step.
    """
    from . import inference
    from .ledger import load_ledger
    from .questions import load_inference_questions
    from .roster import load_roster

    book = storage.load_book(book_id)
    roster = load_roster(book_id, book)
    ledger = load_ledger(book_id, mock=mock)
    questions = load_inference_questions()
    run_id = inference.compute_run_id(ledger, questions, candidate_set=candidate_set,
                                      condition=condition, mock=mock, posthoc=posthoc)
    n = book["n_chunks"] if limit is None else min(limit, book["n_chunks"])
    out_path = out_path or (config.RESULTS_DIR / f"{book_id}__{run_id}.jsonl")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    done = _completed_steps(out_path)
    with out_path.open("a", encoding="utf-8") as fh:
        for t in range(1, n + 1):
            if t in done:
                continue
            result = inference.run_step(book, ledger, roster, questions, t, candidate_set=candidate_set,
                                        run_id=run_id, mock=mock, posthoc=posthoc)
            row = inference.result_row(t, result, book_id=book_id, run_id=run_id,
                                       condition=condition, candidate_set=candidate_set)
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            fh.flush()
            if progress:
                progress(t, n, row)
    return out_path, run_id


# --- Everything --------------------------------------------------------------------

def run_all(source: str | Path | None = None, *, book_id: str | None = None,
            data: bytes | None = None, filename: str | None = None, mock: bool = False,
            condition: str | None = None, chunk_size: int | None = None,
            progress: Progress | None = None,
            should_stop: Callable[[], bool] | None = None) -> dict[str, Any]:
    """Book in, finished run out. Returns {"book_id", "run_file", "run_id", "mock"}.

    Give either `source` (URL or local path), `data`+`filename` (an upload),
    or `book_id` (already read). progress(event) receives
    {"stage", "state": "active"|"done", "done", "total", "message"}.
    """
    from .ingest import ingest_source
    from .ledger import LedgerMismatchError, build_ledger
    from .roster import get_roster

    def emit(stage: str, state: str, done: int = 0, total: int = 0, message: str = "") -> None:
        if should_stop and should_stop():
            raise Cancelled("Stopped.")
        if progress:
            progress({"stage": stage, "label": STAGE_LABELS[stage], "state": state,
                      "done": done, "total": total, "message": message})

    emit("read", "active", message="Reading the book")
    if book_id is None:
        book = ingest_source(source, data=data, filename=filename, chunk_size=chunk_size)
        book_id = book["book_id"]
    else:
        book = storage.load_book(book_id)
    emit("read", "done", message=f"{book.get('title') or book_id}: {book['n_words']:,} words, "
                                 f"{book['n_chunks']} chunks")

    emit("characters", "active", message="Placeholder list (dry run)" if mock
         else "Asking a language model for the character list")
    roster = get_roster(book_id, mock=mock)
    emit("characters", "done", message=f"{len(roster['characters'])} characters")

    n = book["n_chunks"]
    emit("ledger", "active", 0, n, "Compressing each chunk")
    ledger_progress = lambda t, total: emit("ledger", "active", t, total)  # noqa: E731
    try:
        build_ledger(book_id, mock=mock, progress=ledger_progress)
    except LedgerMismatchError:
        if not mock:
            raise
        build_ledger(book_id, mock=True, force=True, progress=ledger_progress)  # free to rebuild
    emit("ledger", "done", n, n)

    emit("inference", "active", 0, n, "Jev is reading")
    out_path, run_id = run_inference(
        book_id, mock=mock, condition=condition or ("dry-run" if mock else "v1"),
        progress=lambda t, total, row: emit("inference", "active", t, total))
    emit("inference", "done", n, n)
    return {"book_id": book_id, "run_file": out_path.name, "run_id": run_id, "mock": mock,
            "title": book.get("title")}
