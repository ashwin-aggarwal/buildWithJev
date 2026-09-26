"""On-disk artifacts for the ledger pipeline.

  data/books/{book_id}.json.gz     parsed book: chunks, sentences, chunk_meta
  data/ledgers/{book_id}.json.gz   append-only ledger entries + rollups
  data/rosters/{book_id}.yaml      hand-editable roster (see roster.py)
  data/manifest.parquet            one row per book, scalar fields only

Book and ledger JSON are gzipped by default; set WRITE_UNCOMPRESSED=1 to write
plain .json for debugging. Readers accept either transparently. The manifest
and rosters are never compressed.

This module never touches data/answers/ (see detective_jev.scoring).
"""

from __future__ import annotations

import gzip
import json
import os
from pathlib import Path
from typing import Any

from . import config


def _gz(path: Path) -> Path:
    return path.with_name(path.name + ".gz")


def write_json(path: Path, obj: Any, *, compress: bool | None = None) -> Path:
    """Atomically write `obj` to `path` (a *.json path), gzipped unless disabled.

    Returns the path actually written. A stale copy in the other format is
    removed so readers never see two versions.
    """
    if compress is None:
        compress = not config.WRITE_UNCOMPRESSED
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(obj, ensure_ascii=False, indent=None if compress else 2).encode("utf-8")
    target, other = (_gz(path), path) if compress else (path, _gz(path))
    tmp = target.with_name(target.name + ".tmp")
    if compress:
        # mtime=0 keeps the bytes deterministic for identical content.
        with open(tmp, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as fh:
            fh.write(data)
    else:
        tmp.write_bytes(data)
    os.replace(tmp, target)
    if other.exists():
        other.unlink()
    return target


def read_json(path: Path) -> Any:
    """Read `path` (*.json) from either its .json.gz or plain .json form."""
    if _gz(path).exists():
        with gzip.open(_gz(path), "rt", encoding="utf-8") as fh:
            return json.load(fh)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    raise FileNotFoundError(f"{path} (or {_gz(path).name}) not found")


def json_exists(path: Path) -> bool:
    return _gz(path).exists() or path.exists()


# --- Paths ------------------------------------------------------------------

def book_path(book_id: str) -> Path:
    return config.BOOKS_DIR / f"{book_id}.json"


def ledger_path(book_id: str, *, mock: bool = False) -> Path:
    # Mock ledgers live beside real ones so a dry run can never be mistaken
    # for (or overwrite) a real ledger.
    suffix = ".mock" if mock else ""
    return config.LEDGERS_DIR / f"{book_id}{suffix}.json"


def posthoc_path(book_id: str, run_id: str) -> Path:
    return config.LEDGERS_DIR / f"{book_id}.posthoc.{run_id}.json"


def roster_path(book_id: str) -> Path:
    return config.ROSTERS_DIR / f"{book_id}.yaml"


# --- Books ------------------------------------------------------------------

def save_book(book: dict[str, Any]) -> Path:
    return write_json(book_path(book["book_id"]), book)


def load_book(book_id: str) -> dict[str, Any]:
    return read_json(book_path(book_id))


def book_exists(book_id: str) -> bool:
    return json_exists(book_path(book_id))


def chunk_text(book: dict[str, Any], t: int) -> str:
    return book["chunks"][f"chunk_{t}"]


def chunk_sentences(book: dict[str, Any], t: int) -> list[dict[str, Any]]:
    return book["sentences"][f"chunk_{t}"]


# --- Manifest ---------------------------------------------------------------

_MANIFEST_FIELDS = (
    "book_id", "title", "author", "source_url", "n_chunks", "n_words",
    "chunk_size_target", "text_sha256", "ingested_at", "pipeline_version",
)


def update_manifest(book: dict[str, Any]) -> None:
    """Insert or replace this book's row in data/manifest.parquet."""
    import pandas as pd

    row = {k: book.get(k) for k in _MANIFEST_FIELDS}
    path = config.MANIFEST_PATH
    if path.exists():
        df = pd.read_parquet(path)
        df = df[df["book_id"] != row["book_id"]]
        df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
    else:
        df = pd.DataFrame([row])
    df = df.sort_values("book_id").reset_index(drop=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    df.to_parquet(tmp, index=False)
    os.replace(tmp, path)


def read_manifest():
    import pandas as pd

    if not config.MANIFEST_PATH.exists():
        return pd.DataFrame(columns=list(_MANIFEST_FIELDS))
    return pd.read_parquet(config.MANIFEST_PATH)
