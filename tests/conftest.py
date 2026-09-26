"""Shared fixtures. Every test runs against temporary data/cache directories and
in mock mode: no network, no API keys, no spend."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from detective_jev import config  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
BOOK_URL = "https://www.gutenberg.org/cache/epub/99999/pg99999-images.html"

_CHAPTERS = {
    "CHAPTER I. THE HOUSE": [
        "Arthur Penrose arrived at Vane Court on a grey afternoon in October. Mr. Penrose had not seen the house for twenty years, and he thought it smaller than he remembered.",
        "Lady Margaret Vane received him in the library. She was pale, and her hands moved restlessly over the papers on her desk while she spoke of the old days.",
        "“You will stay for dinner, of course,” said Lady Vane. “Dr. Hollis is coming, and I should like you to meet him before the will is read.”",
        "Simms, the butler, showed Penrose to his room. The window looked out over the lawn toward the boathouse, where a light was burning although no one should have been there.",
        "At dinner Hollis talked about the weather and the price of coal. Margaret said very little. Penrose noticed that the doctor watched her whenever she lifted her glass.",
    ],
    "CHAPTER II. THE DEATH": [
        "At half past eleven a scream came from the library. Penrose ran down the stairs and found Simms standing in the doorway, white as a sheet.",
        "Lady Margaret Vane lay beside the fire. Dr. Hollis knelt over her and said at once that she was dead, and that it looked to him like poison.",
        "Inspector Crane arrived from the village an hour later. He was a slow, careful man who wrote everything down in a small black notebook.",
        "“Where were you at eleven o’clock?” Crane asked the doctor. Hollis replied that he had been walking in the garden alone, smoking a cigar.",
        "Simms told the Inspector that he had locked the boathouse at ten. Yet Penrose had seen the light there with his own eyes long after that hour.",
        "On the desk Crane found a glass with a faint bitter smell and a torn page from the will. He placed both carefully in an envelope.",
    ],
    "CHAPTER III. THE ANSWER": [
        "In the morning Penrose walked to the boathouse. Inside he found cigar ash on the floor and a bottle hidden beneath a coil of rope.",
        "He brought the bottle to Inspector Crane. The label had been scraped away, but the smell was the same bitter smell as the glass.",
        "“The doctor told us he was in the garden,” said Crane slowly. “But the garden does not lead to the boathouse unless one crosses the lawn.”",
        "Hollis was asked to explain the ash. He said nothing for a long moment, and then he asked whether he might send for his solicitor.",
        "Penrose left Vane Court that afternoon. He did not look back at the house, and he did not speak of that night again for many years.",
    ],
}


def make_html(*, with_boilerplate: bool = True) -> str:
    body = []
    if with_boilerplate:
        body.append('<section id="pg-header"><p>The Project Gutenberg eBook of The Vane Court Affair</p>'
                    '<p>Title: The Vane Court Affair</p><p>Author: A. N. Author</p>'
                    '<p>*** START OF THE PROJECT GUTENBERG EBOOK THE VANE COURT AFFAIR ***</p></section>')
    body.append('<h1>THE VANE COURT AFFAIR</h1>')
    body.append('<div class="transnote"><p>Transcriber’s Note: obvious typos have been corrected silently.</p></div>')
    body.append('<h2>CONTENTS</h2>')
    for i, title in enumerate(_CHAPTERS, start=1):
        body.append(f'<p class="toc"><a href="#ch{i}">{title}</a></p>')
    for i, (title, paras) in enumerate(_CHAPTERS.items(), start=1):
        body.append(f'<h2 id="ch{i}">{title}</h2>')
        body.extend(f"<p>{p}</p>" for p in paras)
        body.append('<p class="center">* * *</p>')
    if with_boilerplate:
        body.append('<section id="pg-footer"><p>*** END OF THE PROJECT GUTENBERG EBOOK THE VANE COURT AFFAIR ***</p>'
                    '<p>Updated editions will replace the previous one. Project Gutenberg License text.</p></section>')
    return ("<html><head><title>The Vane Court Affair | Project Gutenberg</title></head><body>"
            + "\n".join(body) + "</body></html>")


ROSTER = {
    "book_id": "pg99999",
    "pipeline_version": "1",
    "source": "hand",
    "characters": [
        {"canonical": "Arthur Penrose", "aliases": ["Penrose", "Mr. Penrose"], "is_suspect": False,
         "uncertain": False, "evidence": {}},
        {"canonical": "Lady Margaret Vane", "aliases": ["Lady Vane", "Margaret"], "is_suspect": False,
         "uncertain": False, "evidence": {}},
        {"canonical": "Dr. Hollis", "aliases": ["Hollis"], "is_suspect": True, "uncertain": False, "evidence": {}},
        {"canonical": "Inspector Crane", "aliases": ["Crane"], "is_suspect": False, "uncertain": False, "evidence": {}},
        {"canonical": "Simms", "aliases": [], "is_suspect": True, "uncertain": False, "evidence": {}},
    ],
}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Point every data/cache path at tmp_path, keep the real question configs."""
    data = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "RAW_DIR", data / "raw")
    monkeypatch.setattr(config, "HTML_CACHE_DIR", data / "raw" / "html")
    monkeypatch.setattr(config, "BOOKS_DIR", data / "books")
    monkeypatch.setattr(config, "LEDGERS_DIR", data / "ledgers")
    monkeypatch.setattr(config, "ROSTERS_DIR", data / "rosters")
    monkeypatch.setattr(config, "ANSWERS_DIR", data / "answers")
    monkeypatch.setattr(config, "RESULTS_DIR", data / "results")
    monkeypatch.setattr(config, "PARQUET_DIR", data / "results" / "parquet")
    monkeypatch.setattr(config, "MANIFEST_PATH", data / "manifest.parquet")
    monkeypatch.setattr(config, "JEV_CACHE_DB", tmp_path / "cache" / "jev.sqlite")
    monkeypatch.setattr(config, "EXTRACT_CACHE_DIR", tmp_path / "cache" / "extract")
    monkeypatch.setattr(config, "WRITE_UNCOMPRESSED", False)
    monkeypatch.setattr(config, "LEDGER_TOKEN_BUDGET", 26_000)
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    return tmp_path


@pytest.fixture
def fake_fetch(monkeypatch):
    """Serve make_html() instead of the network; counts fetches."""
    import requests

    calls = []

    class Resp:
        status_code = 200
        content = make_html().encode("utf-8")

        def raise_for_status(self):
            pass

    def get(url, timeout=None):
        calls.append(url)
        return Resp()

    monkeypatch.setattr(requests, "get", get)
    return calls


@pytest.fixture
def book(fake_fetch):
    from detective_jev.ingest import ingest

    return ingest(BOOK_URL, chunk_size=60)


@pytest.fixture
def roster_file(book):
    path = config.ROSTERS_DIR / "pg99999.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(ROSTER, sort_keys=False), encoding="utf-8")
    return path
