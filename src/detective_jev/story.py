"""Download and parse a public-domain story into ordered paragraphs.

This is plumbing (I/O + text cleanup), not prompt/choice design. It turns a raw
Project Gutenberg text file into a clean, ordered list of paragraphs that the
rest of the pipeline consumes.

Parsed format on disk (data/parsed/<name>.json):
    [{"index": 0, "text": "..."}, {"index": 1, "text": "..."}, ...]
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import requests

from . import config

# Gutenberg wraps the real text between these markers.
_GUTENBERG_START = re.compile(r"\*\*\*\s*START OF (THE|THIS) PROJECT GUTENBERG.*?\*\*\*", re.I)
_GUTENBERG_END = re.compile(r"\*\*\*\s*END OF (THE|THIS) PROJECT GUTENBERG.*?\*\*\*", re.I)


def download_story(url: str, name: str) -> Path:
    """Download a raw text file to data/raw/<name>.txt and return the path."""
    config.RAW_DIR.mkdir(parents=True, exist_ok=True)
    dest = config.RAW_DIR / f"{name}.txt"
    resp = requests.get(url, timeout=config.REQUEST_TIMEOUT_S)
    resp.raise_for_status()
    dest.write_text(resp.text, encoding="utf-8")
    return dest


def strip_gutenberg_boilerplate(text: str) -> str:
    """Remove Gutenberg header/footer if the markers are present."""
    start = _GUTENBERG_START.search(text)
    end = _GUTENBERG_END.search(text)
    body = text[start.end():] if start else text
    if end:
        # Search for the end marker in the (already trimmed) body.
        end_in_body = _GUTENBERG_END.search(body)
        if end_in_body:
            body = body[: end_in_body.start()]
    return body


def parse_paragraphs(text: str, *, min_chars: int = 40) -> list[str]:
    """Split cleaned text into paragraphs.

    Paragraphs are separated by blank lines. Internal single newlines (hard-
    wrapped lines) are joined into one line, whitespace is collapsed, and very
    short fragments (chapter numbers, "* * *" separators) are dropped.

    Args:
        text: raw story text (Gutenberg boilerplate is stripped automatically).
        min_chars: drop paragraphs shorter than this after cleaning.
    """
    body = strip_gutenberg_boilerplate(text)
    raw_paras = re.split(r"\n\s*\n", body)

    paragraphs: list[str] = []
    for para in raw_paras:
        collapsed = re.sub(r"\s+", " ", para).strip()
        if len(collapsed) >= min_chars:
            paragraphs.append(collapsed)
    return paragraphs


def save_paragraphs(paragraphs: list[str], name: str) -> Path:
    """Write paragraphs to data/parsed/<name>.json as indexed objects."""
    config.PARSED_DIR.mkdir(parents=True, exist_ok=True)
    dest = config.PARSED_DIR / f"{name}.json"
    payload = [{"index": i, "text": p} for i, p in enumerate(paragraphs)]
    dest.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return dest


def load_paragraphs(path: str | Path) -> list[str]:
    """Load a parsed paragraphs JSON file into an ordered list of strings."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    data.sort(key=lambda row: row["index"])
    return [row["text"] for row in data]


if __name__ == "__main__":
    # Convenience CLI: python -m detective_jev.story <gutenberg_url> <name>
    import sys

    if len(sys.argv) != 3:
        print("usage: python -m detective_jev.story <gutenberg_txt_url> <name>")
        raise SystemExit(1)

    url, name = sys.argv[1], sys.argv[2]
    raw_path = download_story(url, name)
    paras = parse_paragraphs(raw_path.read_text(encoding="utf-8"))
    out = save_paragraphs(paras, name)
    print(f"Downloaded {raw_path}\nParsed {len(paras)} paragraphs -> {out}")
