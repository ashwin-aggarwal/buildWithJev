"""Readers for non-HTML book formats: plain text, PDF, and EPUB.

Each returns the same shape as ingest.parse_html:
    {"title", "author", "paragraphs": [str], "chapters": [{"title", "paragraph_offset"}]}
so everything downstream (chunking, roster, ledger) is format-independent.

  - Plain text: paragraphs are separated by blank lines; hard-wrapped lines
    are joined. Project Gutenberg headers/footers are stripped.
  - PDF: best effort. PDFs store lines, not paragraphs, so paragraphs are
    rebuilt from blank lines, indentation, and short sentence-ending lines;
    running headers, page numbers, and end-of-line hyphenation are removed.
    Scanned PDFs (images, no text layer) are refused.
  - EPUB: the chapter files are read in reading order with the HTML parser.

Chapter headings are recognised in text and PDF by shape: a short line with
no sentence ending that starts with CHAPTER/BOOK/PART/..., is a Roman
numeral, or is in capitals.
"""

from __future__ import annotations

import io
import posixpath
import re
import statistics
import zipfile
from collections import Counter
from typing import Any

_START = re.compile(r"\*\*\*\s*START OF (THE|THIS) PROJECT GUTENBERG[^\n]*", re.I)
_END = re.compile(r"\*\*\*\s*END OF (THE|THIS) PROJECT GUTENBERG", re.I)
_NUMBER_WORD = (r"(?:[ivxlc]+|\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
                r"thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty\w*|thirty\w*|"
                r"forty\w*|fifty\w*|the\s+\w+)")
# "CHAPTER IV", "Book Two", "Part the First"; but not prose like "Part of me..."
_HEADING_WORD = re.compile(rf"^(?:chapter|book|part|volume)\s+{_NUMBER_WORD}\b", re.I)
_SECTION_WORD = re.compile(r"^(?:prologue|epilogue|preface|contents|introduction|the end)\W*$", re.I)
# "IV", "IV.", or "IV. THE BODY"; but not prose like "I went home."
_ROMAN = re.compile(r"^[IVXLC]+\.?$|^[IVXLC]+\.\s+\S")
_SENTENCE_END = (".", "!", "?", '"', "”", "’", ":", ";", ",")


class SourceError(ValueError):
    pass


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def is_heading(text: str) -> bool:
    """Does this short block look like a chapter heading rather than prose?"""
    words = text.split()
    if not words or len(words) > 12:
        return False
    if _HEADING_WORD.match(text) or _SECTION_WORD.match(text) or _ROMAN.match(text):
        return not text.endswith(("!", "?", '"', "\u201d"))
    if text.endswith(_SENTENCE_END[:4]):
        return False
    letters = [c for c in text if c.isalpha()]
    return len(letters) >= 3 and all(c.isupper() for c in letters)


def _metadata(head: str) -> tuple[str | None, str | None]:
    title = re.search(r"^\s*Title:\s*(.+)$", head, re.M)
    author = re.search(r"^\s*Author:\s*(.+)$", head, re.M)
    return (_clean(title.group(1)) if title else None, _clean(author.group(1)) if author else None)


def _is_chapter_start(text: str) -> bool:
    return bool(_HEADING_WORD.match(text) or _ROMAN.match(text))


def _assemble(blocks: list[str]) -> tuple[list[str], list[dict[str, Any]]]:
    """Blocks -> (paragraphs, chapters).

    - A heading followed directly by a subtitle merges into one title
      ("CHAPTER I · THE BREAKFAST TABLE"); a new CHAPTER line always starts a
      new chapter, so a table of contents can't swallow chapter one.
    - Transcriber's notes (a note paragraph, or everything under a
      "TRANSCRIBER'S NOTES" heading) are dropped.
    - Front matter (title page, dedication, contents) before the first real
      chapter is dropped when the book clearly has chapters and that front
      matter is short.
    """
    paragraphs: list[str] = []
    chapters: list[dict[str, Any]] = []
    skipping = False
    for block in blocks:
        text = _clean(block)
        if not re.search(r"[A-Za-z]", text):
            continue
        if text.lower().startswith(("transcriber's note", "transcriber’s note", "produced by")):
            skipping = is_heading(text) or len(text.split()) <= 5  # a notes section header
            continue
        if is_heading(text):
            skipping = False
            last = chapters[-1] if chapters else None
            if (last and last["paragraph_offset"] == len(paragraphs) and not _is_chapter_start(text)
                    and last["title"].count(" · ") < 2):
                last["title"] += f" · {text}"
            else:
                chapters.append({"title": text, "paragraph_offset": len(paragraphs)})
            continue
        if not skipping:
            paragraphs.append(text)

    starts = [c for c in chapters if _is_chapter_start(c["title"])]
    if len(starts) >= 3:
        # The first chapter heading followed by real text (contents entries
        # are headings with nothing after them).
        first = None
        for c in starts:
            i = chapters.index(c)
            next_offset = chapters[i + 1]["paragraph_offset"] if i + 1 < len(chapters) else len(paragraphs)
            if next_offset > c["paragraph_offset"]:
                first = c
                break
        total = sum(len(p.split()) for p in paragraphs) or 1
        cut = first["paragraph_offset"] if first else 0
        front = sum(len(p.split()) for p in paragraphs[:cut])
        if first and front < 0.03 * total:
            keep = chapters.index(first)
            paragraphs = paragraphs[cut:]
            chapters = [{"title": c["title"], "paragraph_offset": c["paragraph_offset"] - cut}
                        for c in chapters[keep:]]
    return paragraphs, chapters


# --- Plain text -------------------------------------------------------------------

def decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def parse_text(text: str) -> dict[str, Any]:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    title, author = _metadata(text[:6000])
    start, end = _START.search(text), _END.search(text)
    body = text[start.end():] if start else text
    if end:
        cut = _END.search(body)
        if cut:
            body = body[: cut.start()]
    body = re.sub(r"(?<![\w_])_(\S(?:[^_]*?\S)?)_(?![\w_])", r"\1", body)  # _italics_ -> italics
    blocks = [" ".join(line.strip() for line in b.split("\n")) for b in re.split(r"\n\s*\n", body)]
    paragraphs, chapters = _assemble(blocks)
    return {"title": title, "author": author, "paragraphs": paragraphs, "chapters": chapters}


# --- PDF ----------------------------------------------------------------------------

def _norm_line(line: str) -> str:
    return re.sub(r"\d+", "#", line.strip().lower())


def parse_pdf(data: bytes) -> dict[str, Any]:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(io.BytesIO(data))
        pages = [(p.extract_text() or "") for p in reader.pages]
    except (PdfReadError, ValueError, KeyError) as exc:
        raise SourceError(f"Could not read this PDF: {exc}") from exc
    if sum(len(p.strip()) for p in pages) < 200:
        raise SourceError("This PDF has no text layer (probably scanned images); it can't be read.")

    meta = reader.metadata or {}
    title = _clean(str(meta.get("/Title") or "")) or None
    author = _clean(str(meta.get("/Author") or "")) or None

    page_lines = [[ln.rstrip() for ln in p.split("\n")] for p in pages]
    # Running headers/footers: the same line (digits ignored) at the top or
    # bottom of many pages.
    edge = Counter()
    for lines in page_lines:
        body = [ln for ln in lines if ln.strip()]
        for ln in set(body[:2] + body[-2:]):
            edge[_norm_line(ln)] += 1
    repeated = {k for k, n in edge.items() if n >= max(3, len(page_lines) * 0.3)}

    # A PDF printed from a Project Gutenberg page carries its banners too.
    flat = [ln for page in page_lines for ln in page]
    start_i = next((i for i, ln in enumerate(flat) if _START.search(ln)), None)
    if start_i is not None:
        end_i = next((i for i, ln in enumerate(flat) if i > start_i and _END.search(ln)), len(flat))
        page_lines = [flat[start_i + 1:end_i]]

    lines: list[str] = []
    for page in page_lines:
        for ln in page:
            s = ln.strip()
            if s and (_norm_line(s) in repeated or re.fullmatch(r"[\divxlcIVXLC\s\-–—.]+", s)):
                continue  # running header/footer or a bare page number
            lines.append(ln)

    lengths = [len(ln.strip()) for ln in lines if len(ln.strip()) > 20]
    typical = statistics.median(lengths) if lengths else 60
    blocks: list[str] = []
    current = ""

    def flush():
        nonlocal current
        if current.strip():
            blocks.append(current)
        current = ""

    for ln in lines:
        s = ln.strip()
        if not s:
            flush()
            continue
        if is_heading(s) and len(s) < typical * 0.7:
            flush()
            blocks.append(s)
            continue
        if ln.startswith(("  ", "\t")) and current:
            flush()  # an indented first line starts a new paragraph
        if current.endswith("-") and s[:1].islower():
            current = current[:-1] + s  # re-join a hyphenated word
        else:
            current = f"{current} {s}" if current else s
        if len(s) < typical * 0.75 and s.endswith(_SENTENCE_END[:5]):
            flush()  # a short line ending a sentence ends the paragraph
    flush()
    blocks = [_fix_small_caps(b) for b in blocks]
    paragraphs, chapters = _assemble(blocks)
    return {"title": title, "author": author, "paragraphs": paragraphs, "chapters": chapters}


def _fix_small_caps(text: str) -> str:
    """PDF text extraction splits small capitals: "M RS . F ERRARS" -> "MRS. FERRARS"."""
    text = re.sub(r"\b([A-Z]) (?=[A-Z]{2,}\b)", r"\1", text)
    return re.sub(r"(?<=[A-Z]{2}) (?=[.,;:])", "", text)


# --- EPUB ---------------------------------------------------------------------------

def parse_epub(data: bytes) -> dict[str, Any]:
    from bs4 import BeautifulSoup

    import warnings

    from bs4 import XMLParsedAsHTMLWarning

    from .ingest import parse_html

    warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)  # the OPF is XML; html.parser copes
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
        container = BeautifulSoup(z.read("META-INF/container.xml"), "html.parser")
        opf_path = container.find("rootfile")["full-path"]
        opf = BeautifulSoup(z.read(opf_path), "html.parser")
    except (zipfile.BadZipFile, KeyError, TypeError) as exc:
        raise SourceError(f"Could not read this EPUB: {exc}") from exc

    base = posixpath.dirname(opf_path)
    manifest = {item.get("id"): item for item in opf.find_all("item")}
    title_tag, author_tag = opf.find("dc:title"), opf.find("dc:creator")
    paragraphs: list[str] = []
    chapters: list[dict[str, Any]] = []
    for ref in opf.find_all("itemref"):
        item = manifest.get(ref.get("idref"))
        if item is None or "html" not in (item.get("media-type") or ""):
            continue
        try:
            part = parse_html(z.read(posixpath.normpath(posixpath.join(base, item.get("href")))))
        except KeyError:
            continue
        offset = len(paragraphs)
        chapters += [{"title": c["title"], "paragraph_offset": c["paragraph_offset"] + offset}
                     for c in part["chapters"]]
        paragraphs += part["paragraphs"]
    return {
        "title": _clean(title_tag.get_text()) if title_tag else None,
        "author": _clean(author_tag.get_text()) if author_tag else None,
        "paragraphs": paragraphs,
        "chapters": chapters,
    }
