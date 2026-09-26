"""Question configs for the two Jev call sites.

  config/compression_questions.yaml  scope: chunk_local
      Asked once per chunk with state = that chunk's raw text only. Answers
      become the chunk's ledger entry.
  config/inference_questions.yaml    scope: history_aware
      Asked once per timestep with state = rendered ledger (1..t-1) + chunk t.
      Answers go to results rows.

Every question carries a `scope`, and each loader rejects the other scope, so
the boundary can't be crossed by a config edit alone.

Question schema:
  id:             [a-z][a-z0-9_]*, unique in its file
  scope:          chunk_local | history_aware
  type:           choice | score | noul
  text:           the instructions sent to Jev ("{name}" is filled in for
                  per_character questions)
  options:        (choice) roster | candidates | sentences | [literal, ...]
                    roster     = full cast, names as options
                    candidates = the run's candidate set (history_aware only;
                                 the ledger must not depend on a run condition)
                    sentences  = the chunk's numbered sentences (chunk_local only)
  extra_options:  (choice, optional) list of extra option labels, or a map
                  {label: description}
  levels:         (score) 2..10 level descriptions, low -> high; reported 1..N
  per_character:  (score/noul, optional) expand into one question per
                  character: the full roster in chunk_local files, the
                  candidate set in history_aware files
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import yaml

from . import config

SCOPES = ("chunk_local", "history_aware")
_TYPES = ("choice", "score", "noul")
_ID = re.compile(r"^[a-z][a-z0-9_]*$")


class QuestionConfigError(ValueError):
    pass


def _check(q: dict[str, Any], expected_scope: str, where: str) -> None:
    qid = q.get("id")
    if not isinstance(qid, str) or not _ID.match(qid) or "__" in qid:
        raise QuestionConfigError(f"{where}: bad question id {qid!r} (use [a-z][a-z0-9_]*, no '__').")
    scope = q.get("scope")
    if scope not in SCOPES:
        raise QuestionConfigError(f"{where}: question {qid!r} needs scope: one of {SCOPES}.")
    if scope != expected_scope:
        raise QuestionConfigError(
            f"{where}: question {qid!r} has scope {scope!r} but this file only accepts "
            f"{expected_scope!r}. chunk_local questions see one chunk; history_aware "
            "questions need the ledger. Move it to the other file."
        )
    qtype = q.get("type")
    if qtype not in _TYPES:
        raise QuestionConfigError(f"{where}: question {qid!r} has unknown type {qtype!r}.")
    if not isinstance(q.get("text"), str) or not q["text"].strip():
        raise QuestionConfigError(f"{where}: question {qid!r} needs non-empty text.")

    if qtype == "choice":
        opts = q.get("options")
        if opts == "sentences" and scope != "chunk_local":
            raise QuestionConfigError(f"{where}: {qid!r}: options: sentences only makes sense for chunk_local.")
        if opts == "candidates" and scope != "history_aware":
            raise QuestionConfigError(
                f"{where}: {qid!r}: options: candidates is history_aware only; the ledger "
                "must stay independent of the run's candidate set (use roster)."
            )
        if not (opts in ("roster", "candidates", "sentences") or
                (isinstance(opts, list) and opts and all(isinstance(o, str) for o in opts))):
            raise QuestionConfigError(f"{where}: {qid!r}: options must be roster/candidates/sentences or a list.")
        extra = q.get("extra_options", [])
        if not isinstance(extra, (list, dict)):
            raise QuestionConfigError(f"{where}: {qid!r}: extra_options must be a list or a map.")
        if q.get("per_character"):
            raise QuestionConfigError(f"{where}: {qid!r}: per_character is for score/noul questions.")
    else:
        if "options" in q or "extra_options" in q:
            raise QuestionConfigError(f"{where}: {qid!r}: only choice questions take options.")
    if qtype == "score":
        levels = q.get("levels")
        if not isinstance(levels, list) or not 2 <= len(levels) <= config.MAX_SCORE_LEVELS:
            raise QuestionConfigError(f"{where}: {qid!r}: score needs 2..{config.MAX_SCORE_LEVELS} levels.")
    elif "levels" in q:
        raise QuestionConfigError(f"{where}: {qid!r}: only score questions take levels.")
    if q.get("per_character") and "{name}" not in q["text"]:
        raise QuestionConfigError(f"{where}: {qid!r}: per_character text must contain {{name}}.")


def load_questions(path: Path, expected_scope: str) -> list[dict[str, Any]]:
    """Load and validate a question file whose questions must all be `expected_scope`."""
    if expected_scope not in SCOPES:
        raise QuestionConfigError(f"expected_scope must be one of {SCOPES}")
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    file_scope = data.get("scope")
    if file_scope is not None and file_scope != expected_scope:
        raise QuestionConfigError(f"{path}: file scope {file_scope!r} != expected {expected_scope!r}.")
    questions = data.get("questions")
    if not isinstance(questions, list) or not questions:
        raise QuestionConfigError(f"{path}: needs a non-empty 'questions' list.")
    seen: set[str] = set()
    for q in questions:
        _check(q, expected_scope, str(path))
        if q["id"] in seen:
            raise QuestionConfigError(f"{path}: duplicate question id {q['id']!r}.")
        seen.add(q["id"])
    return questions


def load_compression_questions(path: Path | None = None) -> list[dict[str, Any]]:
    return load_questions(path or config.COMPRESSION_QUESTIONS_PATH, "chunk_local")


def load_inference_questions(path: Path | None = None) -> list[dict[str, Any]]:
    return load_questions(path or config.INFERENCE_QUESTIONS_PATH, "history_aware")


def questions_hash(questions: list[dict[str, Any]]) -> str:
    payload = json.dumps(questions, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def find(questions: list[dict[str, Any]], qid: str) -> dict[str, Any]:
    for q in questions:
        if q["id"] == qid:
            return q
    raise KeyError(qid)


# --- Resolution into Jev question specs ----------------------------------------

def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "x"


def per_character_ids(template_id: str, names: list[str]) -> dict[str, str]:
    """{name: question id}, deterministic and collision-free for this list."""
    out: dict[str, str] = {}
    used: set[str] = set()
    for name in names:
        base = f"{template_id}__{_slug(name)}"
        qid, n = base, 2
        while qid in used:
            qid, n = f"{base}_{n}", n + 1
        used.add(qid)
        out[name] = qid
    return out


def _extra(q: dict[str, Any]) -> dict[str, str | None]:
    extra = q.get("extra_options", [])
    return dict(extra) if isinstance(extra, dict) else {str(e): None for e in extra}


def resolve(
    questions: list[dict[str, Any]],
    *,
    roster: dict[str, Any],
    candidate_names: list[str] | None = None,
    sentences: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Turn config questions into Jev question specs for one call.

    Each spec is {"id", "type", "instructions", "criteria", "meta"}; "meta"
    carries {"template", "character"} for per_character questions and
    {"labels": [...]} for sentence choices, and is ignored by the client.
    """
    from .roster import names_for

    by_name = {c["canonical"]: c for c in roster["characters"]}

    def describe(name: str) -> str | None:
        c = by_name.get(name)
        others = [n for n in names_for(c) if n != name] if c else []
        return f"also called: {', '.join(others)}" if others else None

    specs: list[dict[str, Any]] = []
    for q in questions:
        if q.get("per_character"):
            names = candidate_names if q["scope"] == "history_aware" else list(by_name)
            if names is None:
                raise QuestionConfigError(f"{q['id']!r} needs candidate_names.")
            for name, qid in per_character_ids(q["id"], names).items():
                specs.append({
                    "id": qid, "type": q["type"], "instructions": q["text"].replace("{name}", name),
                    "criteria": list(q["levels"]) if q["type"] == "score" else None,
                    "meta": {"template": q["id"], "character": name},
                })
            continue

        if q["type"] == "score":
            specs.append({"id": q["id"], "type": "score", "instructions": q["text"],
                          "criteria": list(q["levels"]), "meta": {"template": q["id"]}})
            continue
        if q["type"] == "noul":
            specs.append({"id": q["id"], "type": "noul", "instructions": q["text"],
                          "criteria": None, "meta": {"template": q["id"]}})
            continue

        opts = q["options"]
        meta: dict[str, Any] = {"template": q["id"]}
        if opts == "roster":
            criteria = {name: describe(name) for name in by_name}
        elif opts == "candidates":
            if candidate_names is None:
                raise QuestionConfigError(f"{q['id']!r} needs candidate_names.")
            criteria = {name: describe(name) for name in candidate_names}
        elif opts == "sentences":
            if sentences is None:
                raise QuestionConfigError(f"{q['id']!r} needs the chunk's sentences.")
            labels = [f"s{i}" for i in range(1, len(sentences) + 1)]
            criteria = {label: s["text"] for label, s in zip(labels, sentences)}
            meta["labels"] = labels
        else:
            criteria = {str(o): None for o in opts}
        for label, desc in _extra(q).items():
            if label in criteria:
                raise QuestionConfigError(f"{q['id']!r}: extra option {label!r} clashes with an option.")
            criteria[label] = desc
        if len(criteria) > config.MAX_CHOICE_OPTIONS:
            raise QuestionConfigError(
                f"{q['id']!r} would have {len(criteria)} options; Jev's limit is "
                f"{config.MAX_CHOICE_OPTIONS}. Refusing rather than truncating or splitting."
            )
        specs.append({"id": q["id"], "type": "choice", "instructions": q["text"],
                      "criteria": criteria, "meta": meta})
    return specs
