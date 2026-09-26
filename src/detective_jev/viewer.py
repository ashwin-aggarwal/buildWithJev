"""Read-only data for the run viewer (viz/index.html).

Loads a saved book-mode results JSONL (data/results/<book_id>__<run_id>.jsonl),
the book's chunk text, and the roster's names, and computes the inputs for the
Cost Analysis tab. Nothing here calls an API or changes a file.

Spoiler rule: this module never reads the answer key (data/answers/). The UI
must not reveal which character is the culprit.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml

from . import config, storage

_RUN_FILE = re.compile(r"^[A-Za-z0-9_.-]+__[0-9a-f]+\.jsonl$")
NONE_OPTION = "none_of_these"


class ViewerError(RuntimeError):
    pass


def _run_path(name: str) -> Path:
    if not _RUN_FILE.match(name):
        raise ViewerError(f"Not a run file name: {name!r}")
    path = config.RESULTS_DIR / name
    if not path.exists():
        raise ViewerError(f"No such run: {name}")
    return path


def _read_rows(path: Path) -> list[dict[str, Any]]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    rows.sort(key=lambda r: r["t"])
    return rows


def list_runs() -> list[dict[str, Any]]:
    """Every book-mode results file, newest first."""
    runs = []
    for path in config.RESULTS_DIR.glob("*__*.jsonl"):
        if not _RUN_FILE.match(path.name):
            continue
        rows = _read_rows(path)
        if not rows or "book_id" not in rows[0]:
            continue
        first = rows[0]
        title = None
        n_chunks = None
        if storage.book_exists(first["book_id"]):
            book = storage.load_book(first["book_id"])
            title, n_chunks = book.get("title"), book["n_chunks"]
        runs.append({
            "file": path.name,
            "book_id": first["book_id"],
            "title": title,
            "run_id": first.get("run_id"),
            "candidate_set": first.get("candidate_set"),
            "condition": first.get("condition"),
            "mock": "(MOCK)" in str(first.get("model", "")),
            "n_steps": len(rows),
            "n_chunks": n_chunks,
            "mtime": path.stat().st_mtime,
        })
    runs.sort(key=lambda r: (r["mock"], -r["mtime"]))
    return runs


def _roster_names(book_id: str, book: dict[str, Any]) -> dict[str, Any]:
    """{canonical: {"names": [...], "first": chunk|None}} from the roster YAML,
    without importing anything that could touch the answer key."""
    from .roster import first_mention_chunks, names_for

    path = storage.roster_path(book_id)
    if not path.exists():
        return {}
    roster = yaml.safe_load(path.read_text(encoding="utf-8"))
    firsts = first_mention_chunks(book, roster)
    return {c["canonical"]: {"names": names_for(c), "first": firsts.get(c["canonical"])}
            for c in roster.get("characters", [])}


def load_run(name: str) -> dict[str, Any]:
    """Everything the Run tab needs, in one payload."""
    rows = _read_rows(_run_path(name))
    if not rows or "book_id" not in rows[0]:
        raise ViewerError(f"{name} is not a book-mode run.")
    book_id = rows[0]["book_id"]
    book = storage.load_book(book_id)
    names = _roster_names(book_id, book)

    options = list(rows[0]["probabilities"])
    # Neutral order: first appearance in the text (never by probability or truth),
    # then any option without a roster entry, with none_of_these last.
    def order_key(opt: str):
        info = names.get(opt)
        first = info["first"] if info and info["first"] is not None else 10**6
        return (opt == NONE_OPTION, first, opt)
    candidates = sorted(options, key=order_key)

    return {
        "file": name,
        "book_id": book_id,
        "title": book.get("title"),
        "author": book.get("author"),
        "n_chunks": book["n_chunks"],
        "mock": "(MOCK)" in str(rows[0].get("model", "")),
        "candidate_set": rows[0].get("candidate_set"),
        "condition": rows[0].get("condition"),
        "model": rows[0].get("model"),
        "candidates": [
            {"name": c,
             "names": (names.get(c) or {}).get("names", [c] if c != NONE_OPTION else []),
             "first_chunk": (names.get(c) or {}).get("first"),
             "is_none": c == NONE_OPTION}
            for c in candidates
        ],
        "steps": [{"t": r["t"], "p": [r["probabilities"].get(c, 0.0) for c in candidates],
                   # Jev's own judgement of whether the culprit has been revealed yet (None on
                   # runs made before the question existed). Not the answer key.
                   "revealed": ((r.get("inference_answers") or {}).get("culprit_revealed") or {}).get("prob")}
                  for r in rows],
        "raw_window": rows[0].get("raw_window", 1),
        "chunks": [
            {"t": m["index"], "chapter": m.get("chapter"), "words": m["word_count"],
             "fraction": m["cumulative_fraction"], "text": storage.chunk_text(book, m["index"])}
            for m in book["chunk_meta"]
        ],
    }


# --- Cost analysis ----------------------------------------------------------------

def _llm_output_tokens(specs: list[dict[str, Any]]) -> tuple[int, int]:
    """Tokens a text model would have to WRITE to return the same answers.

    Returns (full, top): `full` carries the same information Jev returns (a
    probability for every option of every question, 2 decimals); `top` carries
    only the chosen option per question. Counted with the repo's tokenizer proxy.
    """
    from .tokens import count_tokens

    full: dict[str, Any] = {}
    top: dict[str, Any] = {}
    for s in specs:
        if s["type"] == "noul":
            full[s["id"]] = 0.42
            top[s["id"]] = True
            continue
        opts = list(s["criteria"]) if s["type"] == "choice" else [str(i + 1) for i in range(len(s["criteria"]))]
        full[s["id"]] = {o: 0.12 for o in opts}
        top[s["id"]] = opts[0]
    dump = lambda o: json.dumps(o, ensure_ascii=False, separators=(",", ":"))  # noqa: E731
    return count_tokens(dump(full)), count_tokens(dump(top))


def load_prices() -> dict[str, Any]:
    return yaml.safe_load(config.MODEL_PRICES_PATH.read_text(encoding="utf-8"))


def cost_inputs(name: str) -> dict[str, Any]:
    """Measured Jev usage for the run, plus token totals for pricing other models."""
    from .questions import load_compression_questions, load_inference_questions, resolve
    from .roster import candidates as roster_candidates, load_roster
    from .tokens import count_tokens

    rows = _read_rows(_run_path(name))
    book_id = rows[0]["book_id"]
    book = storage.load_book(book_id)
    roster = load_roster(book_id, book)

    # Inference: measured per call.
    inf_specs = resolve(load_inference_questions(), roster=roster,
                        candidate_names=roster_candidates(roster, rows[0]["candidate_set"]))
    inf_full, inf_top = _llm_output_tokens(inf_specs)
    latencies = sorted(r["latency_ms"] for r in rows if r.get("latency_ms") is not None)

    # Compression: not recorded in the ledger, so estimated from the chunk text
    # plus the question battery, per chunk.
    comp_q = load_compression_questions()
    comp_in = comp_full = comp_top = 0
    for t in range(1, book["n_chunks"] + 1):
        specs = resolve(comp_q, roster=roster, sentences=storage.chunk_sentences(book, t))
        comp_in += count_tokens(storage.chunk_text(book, t)) + sum(
            count_tokens(json.dumps({"type": s["type"], "instructions": s["instructions"],
                                     "criteria": s["criteria"]}, ensure_ascii=False)) for s in specs)
        f, tp = _llm_output_tokens(specs)
        comp_full += f
        comp_top += tp

    return {
        "file": name,
        "book_id": book_id,
        "title": book.get("title"),
        "n_words": book.get("n_words"),
        "mock": "(MOCK)" in str(rows[0].get("model", "")),
        "jev_price_per_m": config.PRICE_PER_1M_INPUT_TOKENS,
        "inference": {
            "calls": len(rows),
            "input_tokens": sum(r.get("input_tokens") or 0 for r in rows),
            "cost": sum(r.get("cost") or 0.0 for r in rows),
            "latency_total_ms": sum(latencies),
            "latency_median_ms": latencies[len(latencies) // 2] if latencies else None,
            "cached_calls": sum(1 for r in rows if r.get("cached")),
            "llm_output_full_per_call": inf_full,
            "llm_output_top_per_call": inf_top,
            "measured": True,
        },
        "compression": {
            "calls": book["n_chunks"],
            "input_tokens": comp_in,
            "cost": comp_in / 1e6 * config.PRICE_PER_1M_INPUT_TOKENS,
            "llm_output_full_total": comp_full,
            "llm_output_top_total": comp_top,
            "measured": False,
        },
        "prices": load_prices(),
    }
