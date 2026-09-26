"""Stage 1: read a book (HTML page, plain text, PDF, or EPUB; from a URL, a local
file, or an upload), strip boilerplate, and store the chunked book.

Non-HTML formats are read by sources.py; this module holds the HTML reader.

  - Raw HTML is cached under data/raw/html/ keyed by a hash of the URL and is
    never refetched.
  - Project Gutenberg boilerplate is removed: the pg-header / pg-footer
    sections of modern HTML editions, everything outside the
    "*** START/END OF THE PROJECT GUTENBERG EBOOK ***" markers of older ones,
    transcriber's notes, and link-only tables of contents.
  - Inline page numbers (.pagenum) and footnote markers are removed, and
    inline markup never splits a word (drop caps: <span>T</span>o -> "To").
  - Paragraph boundaries are preserved (one <p> = one paragraph). Headings
    (<h1>-<h6>) are recorded as chapter metadata with paragraph offsets and
    excluded from the body text.
  - book_id is pg{N} for Gutenberg URLs, else a slug plus a short hash of the
    NORMALISED text, so the same book from two sources is one book.

Idempotent: if data/books/{book_id}.json.gz exists, ingest is a no-op unless
force=True (which re-parses the cached HTML; it still never refetches).
"""

from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup, NavigableString, Tag

from . import config, storage
from .chunking import build_chunks, word_count

logger = logging.getLogger("detective_jev.ingest")

_START = re.compile(r"\*\*\*\s*START OF (THE|THIS) PROJECT GUTENBERG", re.I)
_END = re.compile(r"\*\*\*\s*END OF (THE|THIS) PROJECT GUTENBERG", re.I)
_GUTENBERG_ID = re.compile(r"gutenberg\.org/(?:cache/epub|ebooks|files|epub)/(\d+)", re.I)
# Project Gutenberg Australia: gutenberg.net.au/ebooks13/1302201h.html -> pgau1302201
_GUTENBERG_AU_ID = re.compile(r"gutenberg\.net\.au/ebooks\d*/(\d+)h?\.(?:html?|txt)", re.I)
# Site navigation lines ("GO TO Project Gutenberg Australia HOME PAGE") are not story text.
_SITE_BOILERPLATE = re.compile(r"project gutenberg|gutenberg\.(org|net\.au)", re.I)
_HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
_SKIP_CONTAINER = re.compile(r"transnote|transcriber|tnote|\btoc\b|pg-header|pg-footer|pgheader|pgfooter", re.I)


class IngestError(RuntimeError):
    pass


# --- Fetch + cache -----------------------------------------------------------

def html_cache_path(url: str) -> Path:
    return config.HTML_CACHE_DIR / f"{hashlib.sha256(url.encode('utf-8')).hexdigest()[:24]}.html"


def fetch_html(url: str) -> bytes:
    """Return the URL's raw bytes (any format), fetching only if not already cached."""
    path = html_cache_path(url)
    if path.exists():
        return path.read_bytes()
    if not url.lower().startswith(("http://", "https://")):
        raise IngestError("URL must start with http:// or https://")
    resp = requests.get(url, timeout=config.REQUEST_TIMEOUT_S)
    resp.raise_for_status()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(resp.content)
    logger.info("Fetched %s -> %s", url, path)
    return resp.content


# --- Parsing -----------------------------------------------------------------

def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _text(el: Tag) -> str:
    """Visible text of an element. Inline tags are joined WITHOUT a separator so
    markup inside a word (a drop-cap <span>T</span>o) stays one word; <br> is
    turned into a space before parsing (see parse_html)."""
    return _clean(el.get_text(""))


def _in_skipped_container(el: Tag) -> bool:
    for parent in [el, *el.parents]:
        if not isinstance(parent, Tag):
            continue
        ident = " ".join([parent.get("id") or "", *(parent.get("class") or [])])
        if ident and _SKIP_CONTAINER.search(ident):
            return True
    return False


def _is_link_list(p: Tag) -> bool:
    """A paragraph made (almost) entirely of internal links is a TOC line."""
    text = _text(p)
    links = p.find_all("a", href=re.compile(r"^#"))
    if not text or not links:
        return False
    link_text = sum(len(_text(a)) for a in links)
    return link_text / len(text) > 0.6


def _metadata(soup: BeautifulSoup) -> tuple[str | None, str | None]:
    title = author = None
    header_text = ""
    for sel in ("#pg-header", ".pg-header", "#pg-machine-header"):
        el = soup.select_one(sel)
        if el:
            header_text = el.get_text("\n")
            break
    if not header_text:
        header_text = soup.get_text("\n")[:5000]
    m = re.search(r"^\s*Title:\s*(.+)$", header_text, re.M)
    if m:
        title = _clean(m.group(1))
    m = re.search(r"^\s*Author:\s*(.+)$", header_text, re.M)
    if m:
        author = _clean(m.group(1))
    if not title:
        meta = soup.find("meta", attrs={"name": re.compile(r"dc\.title", re.I)})
        if meta and meta.get("content"):
            title = _clean(meta["content"])
        elif soup.title and soup.title.string:
            title = _clean(soup.title.string.split("|")[0])
    if not author:
        meta = soup.find("meta", attrs={"name": re.compile(r"dc\.creator", re.I)})
        if meta and meta.get("content"):
            author = _clean(meta["content"])
    return title, author


def parse_html(html: bytes | str) -> dict[str, Any]:
    """Extract {title, author, paragraphs, chapters} from a book's HTML page."""
    soup = BeautifulSoup(html, "html.parser")
    title, author = _metadata(soup)

    for el in soup.find_all(["script", "style", "nav"]):
        el.decompose()
    for br in soup.find_all("br"):
        br.replace_with(" ")
    # Printed page numbers and footnote markers sit inline in the prose
    # ("One<span class="pagenum">2</span> might") and are not part of the story.
    for el in soup.select(".pagenum, .pageno, .fnanchor, .footnote-ref"):
        el.decompose()
    for sel in ("#pg-header", "#pg-footer", ".pg-header", ".pg-footer", "#pg-machine-header"):
        for el in soup.select(sel):
            el.decompose()

    root = soup.body or soup
    has_start = root.find(string=_START) is not None

    paragraphs: list[str] = []
    chapters: list[dict[str, Any]] = []
    started = not has_start
    for node in root.descendants:
        if isinstance(node, NavigableString):
            if _START.search(node):
                # Old-style edition: drop everything collected before the marker.
                paragraphs, chapters, started = [], [], True
            elif started and _END.search(node):
                break
            continue
        if not started or not isinstance(node, Tag):
            continue
        if node.name not in _HEADINGS and node.name != "p":
            continue
        if node.find_parent(["p", *_HEADINGS]) is not None:
            continue  # nested inside a block we already take whole
        if _in_skipped_container(node):
            continue
        text = _text(node)
        if not text or _START.search(text) or _END.search(text):
            continue
        if node.name in _HEADINGS:
            chapters.append({"title": text, "paragraph_offset": len(paragraphs)})
            continue
        if text.lower().startswith(("transcriber's note", "transcriber’s note")):
            continue
        if _is_link_list(node):
            continue
        if not re.search(r"[A-Za-z]", text):
            continue  # "* * *" separators and the like
        if len(text.split()) <= 15 and _SITE_BOILERPLATE.search(text):
            continue  # short site-navigation / credit lines
        paragraphs.append(text)

    return {"title": title, "author": author, "paragraphs": paragraphs, "chapters": chapters}


# --- Identity ------------------------------------------------------------------

def normalize_text(paragraphs: list[str]) -> str:
    """Source-independent form of the text, used for hashing and book ids."""
    text = unicodedata.normalize("NFKC", "\n".join(paragraphs))
    text = text.translate({0x2018: "'", 0x2019: "'", 0x201C: '"', 0x201D: '"',
                           0x2014: "-", 0x2013: "-"})
    return re.sub(r"\s+", " ", text).strip().lower()


def text_hash(paragraphs: list[str]) -> str:
    return hashlib.sha256(normalize_text(paragraphs).encode("utf-8")).hexdigest()


def _slug(text: str | None) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "book").lower()).strip("-")
    return s[:40].rstrip("-") or "book"


def make_book_id(url: str, title: str | None, paragraphs: list[str]) -> str:
    m = _GUTENBERG_ID.search(url)
    if m:
        return f"pg{m.group(1)}"
    m = _GUTENBERG_AU_ID.search(url)
    if m:
        return f"pgau{m.group(1)}"
    return f"{_slug(title)}-{text_hash(paragraphs)[:8]}"


# --- Any source: URL, local file, or uploaded bytes ---------------------------------

SUPPORTED_EXTENSIONS = (".html", ".htm", ".xhtml", ".txt", ".pdf", ".epub")


def detect_kind(data: bytes, name: str = "") -> str:
    """"html" | "txt" | "pdf" | "epub", from the content first, then the name."""
    if data[:5] == b"%PDF-":
        return "pdf"
    if data[:2] == b"PK":
        return "epub"
    ext = Path(urlparse(name).path).suffix.lower()
    if ext in (".html", ".htm", ".xhtml") or re.search(rb"<(html|body|p|div)[\s>]", data[:20000], re.I):
        return "html"
    return "txt"


def parse_any(data: bytes, name: str = "") -> tuple[str, dict[str, Any]]:
    from . import sources

    kind = detect_kind(data, name)
    try:
        if kind == "html":
            return kind, parse_html(data)
        if kind == "pdf":
            return kind, sources.parse_pdf(data)
        if kind == "epub":
            return kind, sources.parse_epub(data)
        return kind, sources.parse_text(sources.decode(data))
    except sources.SourceError as exc:
        raise IngestError(str(exc)) from exc


def _title_from_name(name: str) -> str | None:
    stem = Path(urlparse(name).path).stem
    stem = re.sub(r"[-_]+", " ", stem).strip()
    return stem.title() if re.search(r"[A-Za-z]{3}", stem) else None


def _save_upload(data: bytes, filename: str) -> Path:
    ext = Path(filename).suffix.lower() or ".bin"
    path = config.UPLOADS_DIR / f"{hashlib.sha256(data).hexdigest()[:24]}{ext}"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return path


def read_source(source: str | Path | None = None, *, data: bytes | None = None,
                filename: str | None = None) -> tuple[bytes, str, str]:
    """(bytes, name, source_url) for a URL, a local path, or uploaded bytes."""
    if data is not None:
        name = Path(filename or "upload.txt").name
        if Path(name).suffix.lower() not in SUPPORTED_EXTENSIONS:
            raise IngestError(f"Unsupported file type {Path(name).suffix!r}; use one of {', '.join(SUPPORTED_EXTENSIONS)}.")
        _save_upload(data, name)
        return data, name, f"upload:{name}"
    src = str(source)
    if src.lower().startswith(("http://", "https://")):
        return fetch_html(src), src, src
    path = Path(src).expanduser()
    if not path.is_file():
        raise IngestError(f"No such file or URL: {src}")
    return path.read_bytes(), path.name, f"file:{path.name}"


# --- Stage entry point ---------------------------------------------------------

def ingest(url: str, *, chunk_size: int | None = None, force: bool = False) -> dict[str, Any]:
    """Fetch (cached), parse, chunk, and store a book from a URL or local path."""
    return ingest_source(url, chunk_size=chunk_size, force=force)


def ingest_source(source: str | Path | None = None, *, data: bytes | None = None,
                  filename: str | None = None, chunk_size: int | None = None,
                  force: bool = False) -> dict[str, Any]:
    """Parse, chunk, and store a book from a URL, a local file, or uploaded bytes.

    Accepts HTML, plain text, PDF, and EPUB. Returns the book dict.
    """
    target = chunk_size or config.CHUNK_SIZE_TARGET
    raw, name, source_url = read_source(source, data=data, filename=filename)
    kind, parsed = parse_any(raw, name)
    if not parsed["paragraphs"]:
        raise IngestError(f"No story text found in {name}. For web pages, use the HTML edition.")
    title = re.sub(r"\s*\|\s*Project Gutenberg.*$", "", parsed["title"] or "").strip() or _title_from_name(name)
    book_id = make_book_id(source_url, title, parsed["paragraphs"])

    if storage.book_exists(book_id) and not force:
        logger.info("Book %s already ingested; skipping (use force to re-parse).", book_id)
        return storage.load_book(book_id)

    chunked = build_chunks(parsed["paragraphs"], parsed["chapters"], target)
    book = {
        "book_id": book_id,
        "source_url": source_url,
        "source_kind": kind,
        "title": title,
        "author": parsed["author"],
        "n_chunks": len(chunked["chunks"]),
        "n_words": sum(word_count(p) for p in parsed["paragraphs"]),
        "chunk_size_target": target,
        "text_sha256": text_hash(parsed["paragraphs"]),
        "chapters": parsed["chapters"],
        "chunks": chunked["chunks"],
        "sentences": chunked["sentences"],
        "chunk_meta": chunked["chunk_meta"],
        "ingested_at": datetime.now(timezone.utc).isoformat(),
        "pipeline_version": config.PIPELINE_VERSION,
    }
    path = storage.save_book(book)
    storage.update_manifest(book)
    logger.info("Ingested %s (%s): %d words, %d chunks -> %s", book_id, kind, book["n_words"],
                book["n_chunks"], path)
    return book
