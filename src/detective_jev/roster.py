"""Stage 3: the character roster.

One Anthropic call per book, ever:
  - If data/rosters/{book_id}.yaml exists it is AUTHORITATIVE: it is loaded and
    the API is never called for that book (unless explicitly forced).
  - Otherwise the full text goes to EXTRACTION_MODEL in one request with the
    prompt from prompts/extract_characters.txt. The response is cached by a
    hash of the normalised text, then written to the roster YAML for hand
    editing.
  - Books too long for one call are refused with a clear error (no
    chapter-split-and-merge: hand-write those rosters instead).

The roster YAML is hand-editable. Per character:
  canonical, aliases, is_suspect (hand-set; used by candidate_set=suspects_only),
  uncertain, evidence, first_mention_chunk (derived in code, never from the model).

Validation problems are logged as WARNINGS, never raised.

This module never reads data/answers/ (see detective_jev.scoring).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections import Counter
from typing import Any

import yaml

from . import config, storage

logger = logging.getLogger("detective_jev.roster")

CANDIDATE_SETS = ("full_cast", "suspects_only")

_ROSTER_HEADER = """\
# Character roster. HAND-EDITABLE and AUTHORITATIVE: once this file exists the
# extraction API is never called again for this book.
#   canonical            fullest name; used as the Jev option label
#   aliases              other exact strings the text uses for this person
#   is_suspect           set by hand; candidate_set=suspects_only uses these
#   uncertain            extractor was unsure about this merge/split
#   first_mention_chunk  DERIVED in code from alias matches; edits are overwritten
#   evidence             alias -> quote, for checking the extraction
# Do NOT record who the culprit is here; that belongs in the separate answer key.
"""

# Capitalised words that are commonly not names; excluded from the
# missed-character check.
_NOT_NAMES = {
    "The", "And", "But", "Then", "There", "This", "That", "What", "When", "Where",
    "Why", "How", "Who", "Yes", "Well", "Now", "Here", "Sir", "Madam", "Madame",
    "Monsieur", "Mademoiselle", "Miss", "Mrs", "Lady", "Lord", "God", "Heaven",
    "Inspector", "Captain", "Doctor", "Colonel", "Major", "Professor", "Mister",
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
    "January", "February", "March", "April", "May", "June", "July", "August",
    "September", "October", "November", "December", "English", "England", "French",
    "London", "Christmas", "Street", "Road", "House", "Hall", "Court", "Chapter",
}


class RosterError(RuntimeError):
    pass


# --- Aliases and mentions -------------------------------------------------------

def names_for(character: dict[str, Any]) -> list[str]:
    """Canonical name plus aliases, deduplicated, canonical first."""
    out: list[str] = []
    for name in [character["canonical"], *character.get("aliases", [])]:
        if name and name not in out:
            out.append(name)
    return out


def _alias_regex(names: list[str]) -> re.Pattern[str]:
    # Longest first so "Mr. Alfred Inglethorp" wins over "Alfred".
    alts = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
    return re.compile(rf"(?<!\w)(?:{alts})(?!\w)")


def mentions(text: str, roster: dict[str, Any]) -> dict[str, bool]:
    """{canonical: True if any of its names appears in text} (case-sensitive)."""
    return {
        c["canonical"]: bool(_alias_regex(names_for(c)).search(text))
        for c in roster["characters"]
    }


def first_mention_chunks(book: dict[str, Any], roster: dict[str, Any]) -> dict[str, int | None]:
    out: dict[str, int | None] = {}
    for c in roster["characters"]:
        rx = _alias_regex(names_for(c))
        out[c["canonical"]] = next(
            (t for t in range(1, book["n_chunks"] + 1) if rx.search(storage.chunk_text(book, t))),
            None,
        )
    return out


def roster_hash(roster: dict[str, Any]) -> str:
    """Hash of what compression depends on: canonical names and their aliases.

    Hand edits to is_suspect, uncertain, or evidence do not change it, so they
    never invalidate a ledger.
    """
    key = sorted((c["canonical"], sorted(names_for(c))) for c in roster["characters"])
    return hashlib.sha256(json.dumps(key, ensure_ascii=False).encode("utf-8")).hexdigest()


def candidates(roster: dict[str, Any], candidate_set: str) -> list[str]:
    """Canonical names in the given candidate set, in roster order."""
    if candidate_set == "full_cast":
        return [c["canonical"] for c in roster["characters"]]
    if candidate_set == "suspects_only":
        names = [c["canonical"] for c in roster["characters"] if c.get("is_suspect")]
        if len(names) < 2:
            raise RosterError(
                "candidate_set=suspects_only needs at least 2 characters with "
                "is_suspect: true in the roster YAML."
            )
        return names
    raise RosterError(f"Unknown candidate_set {candidate_set!r}; expected one of {CANDIDATE_SETS}.")


# --- Validation (warnings only) ----------------------------------------------

def _non_overlapping_hits(sentence: str, names: list[str]) -> set[str]:
    """Distinct names matched in `sentence`, longest match winning overlaps."""
    return {m.group(0) for m in _alias_regex(names).finditer(sentence)}


def validate_roster(roster: dict[str, Any], book: dict[str, Any] | None = None) -> list[str]:
    """Return (and log) warnings. Never raises on content problems."""
    warnings: list[str] = []

    owners: dict[str, list[str]] = {}
    for c in roster["characters"]:
        for name in names_for(c):
            owners.setdefault(name.casefold(), []).append(c["canonical"])
    for name, owner_list in sorted(owners.items()):
        if len(set(owner_list)) > 1:
            warnings.append(f"alias {name!r} appears under several characters: {sorted(set(owner_list))}")

    if book is not None:
        all_sentences = [
            s["text"] for t in range(1, book["n_chunks"] + 1) for s in storage.chunk_sentences(book, t)
        ]
        for c in roster["characters"]:
            names = names_for(c)
            if len(names) < 2:
                continue
            hits = [s for s in all_sentences if len(_non_overlapping_hits(s, names)) >= 2]
            if hits:
                warnings.append(
                    f"possible over-merge: {len(hits)} sentence(s) use two names of "
                    f"{c['canonical']!r}, e.g. {hits[0][:120]!r}"
                )

        alias_words = {w for c in roster["characters"] for n in names_for(c) for w in re.findall(r"\w+", n)}
        counts: Counter[str] = Counter()
        for s in all_sentences:
            words = re.findall(r"\b[A-Z][a-z]{2,}\b", s)
            first = re.match(r"[\W_]*(\w+)", s)
            for i, w in enumerate(words):
                if i == 0 and first and first.group(1) == w:
                    continue  # sentence-initial capitalisation says nothing
                if w not in alias_words and w not in _NOT_NAMES:
                    counts[w] += 1
        missed = [(w, n) for w, n in counts.most_common() if n >= config.MISSED_NAME_MIN_COUNT]
        if missed:
            warnings.append(
                "possible missed characters (frequent capitalised words matching no alias): "
                + ", ".join(f"{w} x{n}" for w, n in missed[:15])
            )

    for w in warnings:
        logger.warning("roster %s: %s", roster.get("book_id"), w)
    return warnings


# --- Extraction (one Anthropic call per book) --------------------------------

def _extract_cache_path(book: dict[str, Any]):
    return config.EXTRACT_CACHE_DIR / f"{book['text_sha256']}.json"


def _book_text(book: dict[str, Any]) -> str:
    return "\n\n".join(storage.chunk_text(book, t) for t in range(1, book["n_chunks"] + 1))


def _call_extraction_api(book: dict[str, Any]) -> dict[str, Any]:
    import anthropic

    key = config.anthropic_api_key()
    if not key:
        raise RosterError(
            f"{config.ANTHROPIC_KEY_ENV} is not set. Add it to .env, or hand-write "
            f"{storage.roster_path(book['book_id'])}."
        )
    client = anthropic.Anthropic(api_key=key)
    system = config.EXTRACT_PROMPT_PATH.read_text(encoding="utf-8")
    messages = [{
        "role": "user",
        "content": f"Title: {book.get('title') or 'unknown'}\n\n<novel>\n{_book_text(book)}\n</novel>",
    }]
    model = config.EXTRACTION_MODEL

    n_in = client.messages.count_tokens(model=model, system=system, messages=messages).input_tokens
    limit = config.EXTRACTION_CONTEXT_TOKENS - config.EXTRACTION_MAX_OUTPUT_TOKENS
    if n_in > limit:
        raise RosterError(
            f"{book['book_id']} is {n_in:,} input tokens; one {model} call fits at most "
            f"{limit:,}. Books this long are not split automatically: hand-write "
            f"{storage.roster_path(book['book_id'])} instead."
        )

    logger.info("Extracting roster for %s with %s (%s input tokens)", book["book_id"], model, f"{n_in:,}")
    with client.messages.stream(
        model=model,
        max_tokens=config.EXTRACTION_MAX_OUTPUT_TOKENS,
        system=system,
        messages=messages,
    ) as stream:
        response = stream.get_final_message()

    if response.stop_reason in ("refusal", "max_tokens"):
        raise RosterError(f"Extraction stopped early (stop_reason={response.stop_reason}).")
    text = "".join(b.text for b in response.content if b.type == "text")
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        raise RosterError("Extraction response contained no JSON object.")
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError as exc:
        raise RosterError(f"Extraction response was not valid JSON: {exc}") from exc


def extract_characters(book: dict[str, Any], *, use_cache: bool = True) -> dict[str, Any]:
    """The model's raw {"characters": [...]} for this book, cached by text hash."""
    path = _extract_cache_path(book)
    if use_cache and path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    data = _call_extraction_api(book)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


def roster_from_extraction(book: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    chars = []
    for raw in data.get("characters", []):
        canonical = str(raw.get("canonical") or "").strip()
        if not canonical:
            logger.warning("Dropping extracted character with no canonical name: %r", raw)
            continue
        aliases = [str(a).strip() for a in raw.get("aliases") or [] if str(a).strip() and str(a).strip() != canonical]
        chars.append({
            "canonical": canonical,
            "aliases": list(dict.fromkeys(aliases)),
            "is_suspect": False,
            "uncertain": bool(raw.get("uncertain", False)),
            "first_mention_chunk": None,
            "evidence": dict(raw.get("evidence") or {}),
        })
    return {
        "book_id": book["book_id"],
        "pipeline_version": config.PIPELINE_VERSION,
        "source": f"anthropic:{config.EXTRACTION_MODEL}",
        "characters": chars,
    }


# --- YAML I/O ----------------------------------------------------------------------

def save_roster(roster: dict[str, Any]) -> None:
    path = storage.roster_path(roster["book_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    body = yaml.safe_dump(roster, sort_keys=False, allow_unicode=True, width=100)
    path.write_text(_ROSTER_HEADER + body, encoding="utf-8")


def load_roster(book_id: str, book: dict[str, Any] | None = None) -> dict[str, Any]:
    """Load the authoritative roster YAML. If `book` is given, refresh the
    derived first_mention_chunk values in memory."""
    path = storage.roster_path(book_id)
    if not path.exists():
        raise RosterError(f"No roster at {path}. Run: python -m detective_jev.cli roster {book_id}")
    roster = yaml.safe_load(path.read_text(encoding="utf-8"))
    names = [c.get("canonical") for c in roster.get("characters", [])]
    if len(names) != len(set(names)) or not all(names):
        raise RosterError(f"{path}: every character needs a unique, non-empty canonical name.")
    for c in roster["characters"]:
        c.setdefault("aliases", [])
        c.setdefault("is_suspect", False)
    if book is not None:
        firsts = first_mention_chunks(book, roster)
        for c in roster["characters"]:
            c["first_mention_chunk"] = firsts[c["canonical"]]
    return roster


def get_roster(book_id: str, *, force: bool = False) -> dict[str, Any]:
    """Stage entry point: load the roster, extracting it first if none exists.

    force=True re-runs extraction (bypassing the response cache) and
    overwrites the YAML, keeping a .bak copy of the old file.
    """
    book = storage.load_book(book_id)
    path = storage.roster_path(book_id)
    if path.exists() and not force:
        roster = load_roster(book_id, book)
        validate_roster(roster, book)
        return roster
    if path.exists():
        path.with_name(path.name + ".bak").write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    data = extract_characters(book, use_cache=not force)
    roster = roster_from_extraction(book, data)
    firsts = first_mention_chunks(book, roster)
    for c in roster["characters"]:
        c["first_mention_chunk"] = firsts[c["canonical"]]
    validate_roster(roster, book)
    save_roster(roster)
    logger.info("Wrote %s (%d characters). Hand-edit is_suspect before suspects_only runs.",
                path, len(roster["characters"]))
    return roster
