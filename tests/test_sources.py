"""Plain text, PDF, and EPUB readers, and the any-source ingest entry point."""

import io
import zipfile

import pytest

from detective_jev import config, storage
from detective_jev.ingest import IngestError, detect_kind, ingest_source, parse_any
from detective_jev.sources import is_heading, parse_epub, parse_pdf, parse_text

CHAPTERS = {
    "CHAPTER I": ["Arthur Penrose came to _Vane Court_ in the rain.", "Lady Vane was waiting for him in the hall."],
    "CHAPTER II": ["At midnight a scream rang out.", "Simms found the body by the fire."],
    "CHAPTER III": ["Inspector Crane arrived at dawn.", "He found the bottle in the boathouse."],
}


def gutenberg_txt() -> str:
    body = "\n\n".join(f"{title}\n\n" + "\n\n".join(paras) for title, paras in CHAPTERS.items())
    return ("The Project Gutenberg eBook of The Vane Court Affair\n\nTitle: The Vane Court Affair\n"
            "Author: A. N. Author\n\n*** START OF THE PROJECT GUTENBERG EBOOK THE VANE COURT AFFAIR ***\n\n"
            "THE VANE COURT AFFAIR\n\nBY\n\nA. N. AUTHOR\n\nCONTENTS\n\nI. THE HOUSE\n\nII. THE DEATH\n\n"
            "III. THE ANSWER\n\n" + body.replace("in the rain.", "in the\nrain.") +
            "\n\nTranscriber's Notes:\n\n  - Obvious typos have been fixed.\n  - Italics shown as _this_.\n\n"
            "*** END OF THE PROJECT GUTENBERG EBOOK THE VANE COURT AFFAIR ***\n\nLicense text here.\n")


def make_pdf(pages: list[list[str]]) -> bytes:
    """A minimal text PDF (Helvetica), one text line per entry."""
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", None,
            "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for lines in pages:
        ops = ["BT", "/F1 11 Tf", "14 TL", "50 780 Td"]
        for ln in lines:
            ops.append("(" + ln.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") + ") Tj T*")
        ops.append("ET")
        stream = "\n".join(ops)
        objs.append(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")
        content_id = len(objs)
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents {content_id} 0 R "
                    "/Resources << /Font << /F1 3 0 R >> >> >>")
        kids.append(f"{len(objs)} 0 R")
    objs[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>"
    out = io.BytesIO(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n{body}\nendobj\n".encode("latin-1"))
    xref = out.tell()
    out.write(f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode())
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()


def make_epub() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", '<?xml version="1.0"?><container><rootfiles>'
                   '<rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
                   '</rootfiles></container>')
        items, spine = [], []
        for i, (title, paras) in enumerate(CHAPTERS.items(), start=1):
            z.writestr(f"OEBPS/ch{i}.xhtml", f"<html><body><h2>{title}</h2>" +
                       "".join(f"<p>{p}</p>" for p in paras) + "</body></html>")
            items.append(f'<item id="c{i}" href="ch{i}.xhtml" media-type="application/xhtml+xml"/>')
            spine.append(f'<itemref idref="c{i}"/>')
        # spine order deliberately differs from file order
        spine = [spine[0], spine[1], spine[2]]
        z.writestr("OEBPS/content.opf", '<?xml version="1.0"?><package><metadata>'
                   '<dc:title>The Vane Court Affair</dc:title><dc:creator>A. N. Author</dc:creator></metadata>'
                   f'<manifest>{"".join(items)}</manifest><spine>{"".join(spine)}</spine></package>')
    return buf.getvalue()


EXPECTED = [p.replace("_Vane Court_", "Vane Court") for paras in CHAPTERS.values() for p in paras]


def test_plain_text():
    parsed = parse_text(gutenberg_txt())
    assert parsed["title"] == "The Vane Court Affair" and parsed["author"] == "A. N. Author"
    assert parsed["paragraphs"] == EXPECTED          # front matter, notes, license all gone
    assert [c["title"] for c in parsed["chapters"]] == list(CHAPTERS)
    assert [c["paragraph_offset"] for c in parsed["chapters"]] == [0, 2, 4]


def test_pdf():
    pages = [["The Vane Court Affair", "", "CHAPTER I", "", *EXPECTED[:2], "1"],
             ["The Vane Court Affair", "CHAPTER II", "", EXPECTED[2], "", EXPECTED[3], "2"],
             ["The Vane Court Affair", "CHAPTER III", "", EXPECTED[4], "", "He found the bottle in the boat-",
              "house.", "3"]]
    parsed = parse_pdf(make_pdf(pages))
    text = " ".join(parsed["paragraphs"])
    assert "boathouse." in text                      # hyphenation re-joined
    assert "The Vane Court Affair" not in text        # running header removed
    assert not any(p.strip().isdigit() for p in parsed["paragraphs"])   # page numbers removed
    assert [c["title"] for c in parsed["chapters"]] == list(CHAPTERS)
    for sentence in EXPECTED[:5]:
        assert sentence in text


def test_scanned_pdf_refused():
    with pytest.raises(Exception, match="no text layer"):
        parse_pdf(make_pdf([[""]]))


def test_epub():
    parsed = parse_epub(make_epub())
    assert parsed["title"] == "The Vane Court Affair" and parsed["author"] == "A. N. Author"
    assert parsed["paragraphs"] == [p for paras in CHAPTERS.values() for p in paras]
    assert [c["paragraph_offset"] for c in parsed["chapters"]] == [0, 2, 4]


def test_detect_kind():
    assert detect_kind(b"%PDF-1.4 ...") == "pdf"
    assert detect_kind(make_epub()) == "epub"
    assert detect_kind(b"<html><body><p>x</p>") == "html"
    assert detect_kind(b"Just some words.\n\nMore words.", "book.txt") == "txt"


def test_ingest_local_file_and_upload(tmp_path):
    path = tmp_path / "vane_court.txt"
    path.write_text(gutenberg_txt(), encoding="utf-8")
    a = ingest_source(str(path), chunk_size=20)
    assert a["source_kind"] == "txt" and a["source_url"] == "file:vane_court.txt"
    assert a["book_id"].startswith("the-vane-court-affair-") and storage.book_exists(a["book_id"])
    b = ingest_source(data=make_epub(), filename="vane.epub", chunk_size=20)
    assert b["source_kind"] == "epub" and b["source_url"] == "upload:vane.epub"
    assert any(config.UPLOADS_DIR.glob("*.epub"))
    with pytest.raises(IngestError, match="Unsupported"):
        ingest_source(data=b"x", filename="notes.docx")
    with pytest.raises(IngestError, match="No such file"):
        ingest_source(str(tmp_path / "missing.pdf"))


def test_heading_shapes():
    for t in ("CHAPTER IV", "Chapter the First", "IV.", "I. THE HOUSE", "THE END", "Book Two"):
        assert is_heading(t), t
    for t in ("I went home.", "Part of me wanted to stay.", "“Yes!” he cried.", "Mrs. Ferrars died."):
        assert not is_heading(t), t
