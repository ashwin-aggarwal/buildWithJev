"""The answer key: data/answers/{book_id}.yaml, holding is_culprit ONLY.

    book_id: pg863
    is_culprit:
      Alfred Inglethorp: true
      Evelyn Howard: true

Names are roster canonical names; any name not listed is not a culprit.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from .. import config


class AnswerKeyError(RuntimeError):
    pass


def answers_path(book_id: str) -> Path:
    return config.ANSWERS_DIR / f"{book_id}.yaml"


def load_answer_key(book_id: str) -> dict[str, bool]:
    """{canonical: is_culprit} for the names listed in the answer key."""
    path = answers_path(book_id)
    if not path.exists():
        raise AnswerKeyError(f"No answer key at {path}.")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    extra = set(data) - {"book_id", "is_culprit"}
    if extra:
        raise AnswerKeyError(f"{path}: only 'book_id' and 'is_culprit' are allowed, found {sorted(extra)}.")
    key = data.get("is_culprit") or {}
    if not isinstance(key, dict) or not all(isinstance(v, bool) for v in key.values()):
        raise AnswerKeyError(f"{path}: is_culprit must map canonical names to true/false.")
    return dict(key)
