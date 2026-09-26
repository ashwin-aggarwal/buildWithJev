"""The inference call site: one batched Jev call per timestep t.

State at step t = CASE NOTES (rendered ledger entries 1..t-1, plus any rollups
active at t) + CURRENT PASSAGE (chunk t, raw). Chunk t is never in the notes
at step t. The whole history_aware question set rides in the same call.
Answers go to results rows (see `result_row`), never into ledger entries; the
only write-back is the optional per-run post-hoc sidecar (ledger.record_posthoc).

candidate_set is a RUN CONDITION: full_cast or suspects_only. It decides the
culprit options and filters the suspicion lines of the rendered notes. The
ledger itself is the same for both.

This module never reads data/answers/ (see detective_jev.scoring).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from . import config, jev_client, storage
from .ledger import active_rollups, ensure_rollups, load_posthoc, record_posthoc, save_ledger
from .questions import questions_hash, resolve
from .render import compose_state, render_ledger
from .roster import candidates


class InferenceError(RuntimeError):
    pass


def compute_run_id(ledger: dict[str, Any], inference_questions: list[dict[str, Any]], *,
                   candidate_set: str, condition: str, mock: bool, posthoc: bool) -> str:
    """Deterministic: the same inputs resume the same run (and results file)."""
    ledger_hash = hashlib.sha256(
        json.dumps(ledger["entries"], sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    payload = json.dumps([
        ledger["book_id"], config.MODEL_ID, config.PROVIDER, ledger_hash,
        questions_hash(inference_questions), candidate_set, condition, mock, posthoc,
    ])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def build_state(book: dict[str, Any], ledger: dict[str, Any], roster: dict[str, Any], t: int, *,
                suspects: list[str] | None = None, posthoc: dict[str, Any] | None = None) -> str:
    """Pure: the inference state for step t (call ensure_rollups first)."""
    if not 1 <= t <= book["n_chunks"]:
        raise InferenceError(f"t={t} out of range 1..{book['n_chunks']}")
    notes = render_ledger(ledger["entries"][: t - 1], roster, rollups=active_rollups(ledger, t),
                          suspects=suspects, posthoc=posthoc)
    return compose_state(notes, t, storage.chunk_text(book, t))


def run_step(book: dict[str, Any], ledger: dict[str, Any], roster: dict[str, Any],
             questions: list[dict[str, Any]], t: int, *, candidate_set: str, run_id: str,
             mock: bool = False, posthoc: bool = False) -> dict[str, Any]:
    """Run inference for step t with ONE batched Jev call. Returns the query_batch result."""
    if len(ledger["entries"]) < book["n_chunks"]:
        raise InferenceError(
            f"Ledger for {book['book_id']} has {len(ledger['entries'])}/{book['n_chunks']} entries. "
            f"Build it first: python -m detective_jev.cli ledger {book['book_id']}"
        )
    if ensure_rollups(ledger, book, roster, t):
        save_ledger(ledger)
    names = candidates(roster, candidate_set)
    suspects = None if candidate_set == "full_cast" else names
    sidecar = load_posthoc(book["book_id"], run_id) if posthoc else None
    state = build_state(book, ledger, roster, t, suspects=suspects, posthoc=sidecar)
    specs = resolve(questions, roster=roster, candidate_names=names)
    result = jev_client.query_batch(state, specs, mock=mock)
    if posthoc and "contradicts_prior" in result["answers"]:
        record_posthoc(book["book_id"], run_id, t, result["answers"]["contradicts_prior"]["prob"])
    return result


def result_row(t: int, result: dict[str, Any], *, book_id: str, run_id: str, condition: str,
               candidate_set: str) -> dict[str, Any]:
    """A results JSONL row: the existing fields (from the culprit question) plus
    the book-mode fields."""
    if "culprit" not in result["answers"]:
        raise InferenceError("inference_questions.yaml must define a 'culprit' question.")
    culprit = result["answers"]["culprit"]
    return {
        "t": t,
        "answer": culprit["value"],
        "probabilities": culprit["distribution"],
        "confidence": culprit["confidence"],
        "latency_ms": result["latency_ms"],
        "input_tokens": result["input_tokens"],
        "model": result["model"],
        "cost": result["cost"],
        "cached": result.get("cached", False),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        # Book-mode fields (added; existing fields above are unchanged).
        "book_id": book_id,
        "run_id": run_id,
        "condition": condition,
        "candidate_set": candidate_set,
        "inference_answers": {
            qid: {k: v for k, v in a.items() if k != "option_order"}
            for qid, a in result["answers"].items()
        },
        "option_order": result["option_order"],
    }
