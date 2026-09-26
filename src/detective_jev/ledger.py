"""Stage 4: the ledger. One compressed entry per chunk, built from Jev's answers.

Invariants (enforced here, tested in tests/test_ledger.py):
  - Compression state = ONE chunk's raw text, nothing else. `build_entry` is a
    pure function of (chunk text, its sentences, roster, questions): same
    inputs, same entry, independent of every other chunk.
  - Exactly ONE batched Jev call per chunk (the whole chunk_local battery),
    and exactly ONE entry per chunk index. Appending an index that exists is
    a DuplicateEntryError, never an overwrite. A persisted per-book counter
    raises CompressionCallBudgetError if calls would exceed the chunk count.
  - The ledger is independent of any run condition: suspicion is scored for
    the full cast; candidate_set=suspects_only is a filtered VIEW at render time.
  - Entries are never recompressed. A ledger built with a different book text,
    roster (names/aliases), or question set is refused unless force=True
    (which starts a fresh ledger).

Overflow (rollups): before each inference call the rendered state for step t
(entries 1..t-1 + chunk t) is token-counted against LEDGER_TOKEN_BUDGET. If it
is over, the OLDEST un-rolled entries (one chapter at a time) are replaced by
a coarser rollup: event_type counts per chapter, plus the key quotes of
entries whose new_evidence answer was true. Every rollup records the step it
was triggered at and is shown only at steps >= that step, so an early step
never sees summaries shaped by later chunks. Rollups are computed step by
step from the first unchecked step, so the result is the same regardless of
the order steps are run in. An entry is rolled up at most once.

Arithmetic: a rendered entry is ~70-115 tokens, so a 90,000-word book at
500-word chunks (180 chunks) renders to ~12-21K tokens and never triggers a
rollup under the 26K default. Rollups start around 114K-194K words (e.g. The
Moonstone, The Woman in White).
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import Any

from . import config, jev_client, storage
from .questions import load_compression_questions, questions_hash, resolve
from .render import compose_state, render_ledger
from .roster import load_roster, mentions, roster_hash

logger = logging.getLogger("detective_jev.ledger")


class LedgerError(RuntimeError):
    pass


class DuplicateEntryError(LedgerError):
    pass


class CompressionCallBudgetError(LedgerError):
    pass


class LedgerMismatchError(LedgerError):
    pass


# --- One entry (pure) -----------------------------------------------------------

def _strip(ans: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in ans.items() if k != "option_order"}


def build_entry(
    chunk_text: str,
    sentences: list[dict[str, Any]],
    roster: dict[str, Any],
    questions: list[dict[str, Any]],
    *,
    mock: bool = False,
) -> dict[str, Any]:
    """Compress one chunk with ONE batched Jev call. Pure in its inputs.

    Returns the entry's content (no positional fields):
      answers           {qid: {type, value, prob, distribution, confidence[, expected]}}
      suspicion         {canonical: {value, expected, prob, distribution, ...}}
      per_character     {template: {canonical: answer}} for other per-character questions
      mentioned         {canonical: bool}, from alias matches in code
      key_quote         the exact sentence Jev selected (verbatim, copied by code)
      key_sentence_idx  0-based index into this chunk's sentence list
      option_order      {qid: [options in the order sent]} for Choice questions
      text_sha256       hash of the chunk text
    """
    import hashlib

    specs = resolve(questions, roster=roster, sentences=sentences)
    result = jev_client.query_batch(chunk_text, specs, mock=mock)

    answers: dict[str, Any] = {}
    per_character: dict[str, dict[str, Any]] = {}
    key_quote, key_idx = None, None
    for spec in specs:
        ans = _strip(result["answers"][spec["id"]])
        meta = spec["meta"]
        if "character" in meta:
            per_character.setdefault(meta["template"], {})[meta["character"]] = ans
            continue
        answers[spec["id"]] = ans
        if "labels" in meta:
            key_idx = meta["labels"].index(ans["value"])
            key_quote = sentences[key_idx]["text"]

    entry = {
        "answers": answers,
        "suspicion": per_character.pop("suspicion", {}),
        "mentioned": mentions(chunk_text, roster),
        "key_quote": key_quote,
        "key_sentence_idx": key_idx,
        "option_order": result["option_order"],
        "text_sha256": hashlib.sha256(chunk_text.encode("utf-8")).hexdigest(),
    }
    if per_character:
        entry["per_character"] = per_character
    return entry


# --- Ledger file --------------------------------------------------------------------

def new_ledger(book: dict[str, Any], roster: dict[str, Any], questions: list[dict[str, Any]],
               *, mock: bool) -> dict[str, Any]:
    return {
        "book_id": book["book_id"],
        "pipeline_version": config.PIPELINE_VERSION,
        "mock": mock,
        "model": config.MODEL_ID,
        "book_text_sha256": book["text_sha256"],
        "roster_hash": roster_hash(roster),
        "questions_hash": questions_hash(questions),
        "n_chunks": book["n_chunks"],
        "compression_calls": 0,
        "entries": [],
        "token_budget": config.LEDGER_TOKEN_BUDGET,
        "rollups": [],
        "rollups_checked_through": 0,
    }


def load_ledger(book_id: str, *, mock: bool = False) -> dict[str, Any]:
    return storage.read_json(storage.ledger_path(book_id, mock=mock))


def save_ledger(ledger: dict[str, Any]) -> None:
    storage.write_json(storage.ledger_path(ledger["book_id"], mock=ledger["mock"]), ledger)


def ledger_exists(book_id: str, *, mock: bool = False) -> bool:
    return storage.json_exists(storage.ledger_path(book_id, mock=mock))


def check_compatible(ledger: dict[str, Any], book: dict[str, Any], roster: dict[str, Any],
                     questions: list[dict[str, Any]]) -> None:
    problems = []
    if ledger["book_text_sha256"] != book["text_sha256"]:
        problems.append("book text changed (re-ingested?)")
    if ledger["roster_hash"] != roster_hash(roster):
        problems.append("roster names/aliases changed")
    if ledger["questions_hash"] != questions_hash(questions):
        problems.append("compression_questions.yaml changed")
    if ledger["pipeline_version"] != config.PIPELINE_VERSION:
        problems.append(f"pipeline_version {ledger['pipeline_version']} != {config.PIPELINE_VERSION}")
    if problems:
        raise LedgerMismatchError(
            f"Ledger for {book['book_id']} was built with different inputs: "
            + "; ".join(problems)
            + ". Entries are never recompressed in place; rebuild with --force."
        )


def append_entry(ledger: dict[str, Any], entry: dict[str, Any]) -> None:
    """Append-only. A duplicate chunk index is an error, never an overwrite."""
    idx = entry["chunk"]
    if any(e["chunk"] == idx for e in ledger["entries"]):
        raise DuplicateEntryError(f"Ledger {ledger['book_id']} already has an entry for chunk {idx}.")
    expected = len(ledger["entries"]) + 1
    if idx != expected:
        raise LedgerError(f"Entries must be appended in order: expected chunk {expected}, got {idx}.")
    ledger["entries"].append(entry)


def build_ledger(book_id: str, *, mock: bool = False, force: bool = False,
                 progress=None) -> dict[str, Any]:
    """Stage entry point: compress every chunk not yet in the ledger.

    Idempotent: a complete ledger is a no-op. Saved after every entry, so a
    crash resumes where it stopped (and the SQLite Jev cache makes any
    repeated call free).
    """
    book = storage.load_book(book_id)
    roster = load_roster(book_id)
    questions = load_compression_questions()

    if ledger_exists(book_id, mock=mock) and not force:
        ledger = load_ledger(book_id, mock=mock)
        check_compatible(ledger, book, roster, questions)
    else:
        ledger = new_ledger(book, roster, questions, mock=mock)

    n = book["n_chunks"]
    for t in range(len(ledger["entries"]) + 1, n + 1):
        if ledger["compression_calls"] >= n:
            raise CompressionCallBudgetError(
                f"{book_id}: {ledger['compression_calls']} compression calls already made for "
                f"{n} chunks; refusing another. The one-call-per-chunk invariant is broken."
            )
        ledger["compression_calls"] += 1
        meta = book["chunk_meta"][t - 1]
        content = build_entry(storage.chunk_text(book, t), storage.chunk_sentences(book, t),
                              roster, questions, mock=mock)
        append_entry(ledger, {"chunk": t, "chapter": meta["chapter"], "word_span": meta["word_span"],
                              **content})
        save_ledger(ledger)
        logger.info("%s: compressed chunk %d/%d", book_id, t, n)
        if progress:
            progress(t, n)
    return ledger


# --- Rollups ------------------------------------------------------------------

def _next_rollup(visible: list[dict[str, Any]], covered: set[int]) -> list[dict[str, Any]]:
    """The oldest un-rolled entries to roll next: the rest of the oldest chapter,
    or the older half if that chapter is everything still un-rolled."""
    remaining = [e for e in visible if e["chunk"] not in covered]
    if not remaining:
        return []
    chapter = remaining[0].get("chapter")
    group = []
    for e in remaining:
        if e.get("chapter") != chapter:
            break
        group.append(e)
    if len(group) == len(remaining):
        group = group[: max(1, len(group) // 2)]
    return group


def make_rollup(group: list[dict[str, Any]], rollup_id: int, triggered_at_t: int) -> dict[str, Any]:
    chapters: list[dict[str, Any]] = []
    for e in group:
        if not chapters or chapters[-1]["chapter"] != e.get("chapter"):
            chapters.append({"chapter": e.get("chapter"), "chunks": [e["chunk"], e["chunk"]],
                             "_events": Counter(), "quotes": []})
        ch = chapters[-1]
        ch["chunks"][1] = e["chunk"]
        event = e["answers"].get("event_type", {}).get("value")
        if event is not None:
            ch["_events"][event] += 1
        if e["answers"].get("new_evidence", {}).get("value") and e.get("key_quote"):
            ch["quotes"].append({"chunk": e["chunk"], "quote": e["key_quote"]})
    for ch in chapters:
        events = ch.pop("_events")
        ch["event_counts"] = dict(sorted(events.items(), key=lambda kv: (-kv[1], kv[0])))
    return {"id": rollup_id, "chunks": [group[0]["chunk"], group[-1]["chunk"]],
            "triggered_at_t": triggered_at_t, "chapters": chapters}


def active_rollups(ledger: dict[str, Any], t: int) -> list[dict[str, Any]]:
    return [r for r in ledger["rollups"] if r["triggered_at_t"] <= t]


def state_text(book: dict[str, Any], ledger: dict[str, Any], roster: dict[str, Any], t: int, *,
               rollups: list[dict[str, Any]], suspects: list[str] | None = None,
               posthoc: dict[str, Any] | None = None, window: int | None = None) -> str:
    """The exact inference state at step t (shared by inference and the
    overflow check): compressed notes for 1..t-W, then chunks t-W+1..t in full,
    where W = config.RAW_WINDOW. Pure in its arguments."""
    w = window or config.RAW_WINDOW
    first_raw = max(1, t - w + 1)
    notes = render_ledger(ledger["entries"][: first_raw - 1], roster, rollups=rollups,
                          suspects=suspects, posthoc=posthoc)
    recent = [(k, storage.chunk_text(book, k)) for k in range(first_raw, t)]
    return compose_state(notes, t, storage.chunk_text(book, t), recent=recent, notes_upto=first_raw - 1)


def ensure_rollups(ledger: dict[str, Any], book: dict[str, Any], roster: dict[str, Any], t: int) -> bool:
    """Make sure rollups are computed for every step up to t. Returns True if
    any new rollup was added (the caller should then save the ledger).

    The budget check renders the FULL-CAST view with no post-hoc values, so
    rollups are a property of the ledger, not of any run.
    """
    from .tokens import count_tokens

    # Rollups depend on the budget and the full-text window. They are derived
    # (no API calls) and deterministic, so if either setting changed they are
    # simply recomputed; the ledger entries themselves are never touched.
    params = {"token_budget": config.LEDGER_TOKEN_BUDGET, "raw_window": config.RAW_WINDOW}
    changed = False
    if ledger.get("token_budget") != params["token_budget"] or ledger.get("raw_window", 1) != params["raw_window"]:
        if ledger["rollups"] or ledger["rollups_checked_through"]:
            logger.info("%s: rollup settings changed %s; recomputing rollups.", ledger["book_id"], params)
            changed = True
        ledger.update(params, rollups=[], rollups_checked_through=0)
    first_raw = lambda s: max(1, s - config.RAW_WINDOW + 1)  # noqa: E731
    for s in range(ledger["rollups_checked_through"] + 1, t + 1):
        visible = ledger["entries"][: first_raw(s) - 1]
        while True:
            tokens = count_tokens(state_text(book, ledger, roster, s, rollups=ledger["rollups"]))
            if tokens <= config.LEDGER_TOKEN_BUDGET:
                break
            covered = {i for r in ledger["rollups"] for i in range(r["chunks"][0], r["chunks"][1] + 1)}
            group = _next_rollup(visible, covered)
            if not group:
                raise LedgerError(
                    f"{ledger['book_id']} step {s}: state is {tokens:,} tokens even with every "
                    f"earlier entry rolled up (budget {config.LEDGER_TOKEN_BUDGET:,})."
                )
            rollup = make_rollup(group, len(ledger["rollups"]) + 1, s)
            ledger["rollups"].append(rollup)
            changed = True
            logger.warning(
                "%s: rollup #%d of chunks %d-%d at step %d (state was %s tokens > budget %s)",
                ledger["book_id"], rollup["id"], rollup["chunks"][0], rollup["chunks"][1], s,
                f"{tokens:,}", f"{config.LEDGER_TOKEN_BUDGET:,}",
            )
        ledger["rollups_checked_through"] = s
    return changed


# --- Per-run post-hoc sidecar -----------------------------------------------------

def load_posthoc(book_id: str, run_id: str) -> dict[str, Any]:
    path = storage.posthoc_path(book_id, run_id)
    return storage.read_json(path) if storage.json_exists(path) else {}


def record_posthoc(book_id: str, run_id: str, chunk: int, contradicts_prior: float) -> None:
    """Store the inference contradiction result for `chunk`, for this run only.
    A local write, never another Jev call; the shared ledger is untouched."""
    data = load_posthoc(book_id, run_id)
    data[str(chunk)] = {"contradicts_prior": contradicts_prior}
    storage.write_json(storage.posthoc_path(book_id, run_id), data)
