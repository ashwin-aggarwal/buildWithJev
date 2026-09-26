from conftest import BOOK_URL, make_html

from detective_jev import config, storage
from detective_jev.ingest import ingest, make_book_id, parse_html


def test_boilerplate_stripped_and_headings_are_metadata():
    parsed = parse_html(make_html())
    body = "\n".join(parsed["paragraphs"])
    assert parsed["title"] == "The Vane Court Affair"
    assert parsed["author"] == "A. N. Author"
    for junk in ("PROJECT GUTENBERG", "Transcriber", "License", "* * *", "CHAPTER I."):
        assert junk not in body
    titles = [c["title"] for c in parsed["chapters"]]
    assert "CHAPTER II. THE DEATH" in titles
    assert parsed["paragraphs"][0].startswith("Arthur Penrose arrived")
    ch2 = next(c for c in parsed["chapters"] if c["title"] == "CHAPTER II. THE DEATH")
    assert parsed["paragraphs"][ch2["paragraph_offset"]].startswith("At half past eleven")


def test_old_style_markers():
    html = ("<html><body><p>Header junk about the licence.</p>"
            "<p>*** START OF THE PROJECT GUTENBERG EBOOK X ***</p>"
            "<h2>CHAPTER I</h2><p>Real text here.</p>"
            "<p>*** END OF THE PROJECT GUTENBERG EBOOK X ***</p><p>Footer junk.</p></body></html>")
    parsed = parse_html(html)
    assert parsed["paragraphs"] == ["Real text here."]
    assert parsed["chapters"] == [{"title": "CHAPTER I", "paragraph_offset": 0}]


def test_book_ids():
    paras = parse_html(make_html())["paragraphs"]
    assert make_book_id(BOOK_URL, "T", paras) == "pg99999"
    a = make_book_id("https://example.org/a.html", "The Vane Court Affair", paras)
    # same text from another source (different quotes/whitespace) -> same id
    variant = [p.replace("“", '"').replace("”", '"') + "  " for p in paras]
    b = make_book_id("https://mirror.example.net/b.htm", "The Vane Court Affair", variant)
    assert a == b and a.startswith("the-vane-court-affair-")


def test_ingest_caches_html_and_is_idempotent(book, fake_fetch):
    assert book["book_id"] == "pg99999"
    assert book["pipeline_version"] == config.PIPELINE_VERSION
    assert (config.BOOKS_DIR / "pg99999.json.gz").exists()
    assert len(fake_fetch) == 1
    again = ingest(BOOK_URL, chunk_size=60)
    assert again == book and len(fake_fetch) == 1          # no refetch, no rewrite
    ingest(BOOK_URL, chunk_size=60, force=True)
    assert len(fake_fetch) == 1                            # force re-parses the cache
    df = storage.read_manifest()
    assert list(df["book_id"]) == ["pg99999"]


def test_uncompressed_debug_flag(book, monkeypatch):
    monkeypatch.setattr(config, "WRITE_UNCOMPRESSED", True)
    storage.save_book(book)
    assert (config.BOOKS_DIR / "pg99999.json").exists()
    assert not (config.BOOKS_DIR / "pg99999.json.gz").exists()
    assert storage.load_book("pg99999") == book
