"""JSONL results -> long-format Parquet (derived, disposable).

One output file per run: data/results/parquet/{run_id}.parquet, with one row
per (t, candidate):

  book_id, t, candidate, prob, is_culprit, introduced_by_t, candidate_set,
  run_id, condition, model, answer, confidence, option_position,
  option_order (JSON), inference_answers (JSON), plus flattened
  q_<id>_value / q_<id>_prob columns for every non-culprit inference question.

The Parquet is regenerated from the JSONL on every run and replaced atomically;
it is never edited in place, so the conversion is idempotent. Paragraph-mode
rows (no run_id) are skipped.
"""

from __future__ import annotations

import json
import logging
import os
from collections import defaultdict
from pathlib import Path
from typing import Any

from .. import config
from ..roster import load_roster
from .. import storage
from .answers import AnswerKeyError, load_answer_key

logger = logging.getLogger("detective_jev.scoring.convert")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    with Path(path).open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def long_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Explode JSONL rows (one per t) into one row per (t, candidate)."""
    out: list[dict[str, Any]] = []
    cache: dict[str, tuple[dict[str, bool], dict[str, int | None]]] = {}
    for row in sorted(rows, key=lambda r: (r.get("run_id") or "", r["t"])):
        if not row.get("run_id"):
            continue
        book_id = row["book_id"]
        if book_id not in cache:
            try:
                key = load_answer_key(book_id)
            except AnswerKeyError as exc:
                logger.warning("%s; is_culprit will be null", exc)
                key = None
            book = storage.load_book(book_id)
            roster = load_roster(book_id, book)
            firsts = {c["canonical"]: c.get("first_mention_chunk") for c in roster["characters"]}
            cache[book_id] = (key, firsts)
        key, firsts = cache[book_id]
        answers = row.get("inference_answers") or {}
        order = (row.get("option_order") or {}).get("culprit") or []
        extra = {}
        for qid, a in sorted(answers.items()):
            if qid == "culprit":
                continue
            value = a.get("value")
            extra[f"q_{qid}_value"] = value if isinstance(value, (bool, int, float)) or value is None else str(value)
            extra[f"q_{qid}_prob"] = a.get("prob")
        for candidate, prob in row["probabilities"].items():
            first = firsts.get(candidate)
            out.append({
                "book_id": book_id,
                "t": row["t"],
                "candidate": candidate,
                "prob": prob,
                "is_culprit": (key.get(candidate, False) if candidate in firsts else False) if key is not None else None,
                "introduced_by_t": (first is not None and first <= row["t"]) if candidate in firsts else None,
                "candidate_set": row["candidate_set"],
                "run_id": row["run_id"],
                "condition": row["condition"],
                "model": row.get("model"),
                "answer": row.get("answer"),
                "confidence": row.get("confidence"),
                "option_position": order.index(candidate) if candidate in order else None,
                "option_order": json.dumps(row.get("option_order") or {}, ensure_ascii=False, sort_keys=True),
                "inference_answers": json.dumps(answers, ensure_ascii=False, sort_keys=True),
                **extra,
            })
    return out


def convert(jsonl_path: Path, out_dir: Path | None = None) -> list[Path]:
    """Write one Parquet per run_id found in `jsonl_path`. Returns the paths."""
    import pandas as pd

    out_dir = Path(out_dir or config.PARQUET_DIR)
    by_run: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in long_rows(read_jsonl(jsonl_path)):
        by_run[r["run_id"]].append(r)
    written = []
    for run_id, rows in sorted(by_run.items()):
        df = pd.DataFrame(rows).sort_values(["t", "candidate"]).reset_index(drop=True)
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{run_id}.parquet"
        tmp = path.with_name(path.name + ".tmp")
        df.to_parquet(tmp, index=False)
        os.replace(tmp, path)
        written.append(path)
        logger.info("Wrote %s (%d rows)", path, len(df))
    return written


def to_jsonl_fields(df) -> list[dict[str, Any]]:
    """Reconstruct the per-t JSONL fields from a converted Parquet (round-trip check)."""
    rows = []
    for t, g in df.groupby("t", sort=True):
        first = g.iloc[0]
        rows.append({
            "t": int(t),
            "answer": first["answer"],
            "confidence": first["confidence"],
            "probabilities": dict(zip(g["candidate"], g["prob"])),
            "run_id": first["run_id"],
            "condition": first["condition"],
            "candidate_set": first["candidate_set"],
            "book_id": first["book_id"],
            "model": first["model"],
            "option_order": json.loads(first["option_order"]),
            "inference_answers": json.loads(first["inference_answers"]),
        })
    return rows
