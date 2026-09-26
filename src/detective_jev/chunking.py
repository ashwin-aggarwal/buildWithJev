"""Paragraph-preserving chunking and sentence segmentation.

Chunks accumulate WHOLE paragraphs until the running word count reaches the
target, then emit. A single paragraph longer than the target becomes its own
(oversized) chunk; the trailing remainder becomes the last chunk. Chunks are
1-indexed. Chunk text is its paragraphs joined by a blank line.

Sentences are segmented once per chunk here and stored with character offsets
into the chunk text. The ledger uses them as numbered Jev options; nothing
downstream re-segments. Paragraph breaks are always sentence breaks.

Everything here is a pure function: same input, same output, always.
"""

from __future__ import annotations

import re
from typing import Any

PARAGRAPH_SEP = "\n\n"

# Tokens that end with "." without ending a sentence.
_ABBREVIATIONS = {
    "mr", "mrs", "ms", "messrs", "mme", "mlle", "dr", "st", "capt", "col",
    "gen", "lt", "sgt", "rev", "prof", "hon", "jr", "sr", "no", "vs", "etc",
    "esq", "inst", "ult", "viz", "cf", "vol", "ch", "fig", "p", "pp", "sq",
}

# A sentence terminator run, plus any closing quotes/brackets glued to it.
_TERMINATOR = re.compile(r"[.!?]+[\"'”’)\]]*")


def word_count(text: str) -> int:
    return len(text.split())


def chunk_paragraphs(paragraphs: list[str], target: int) -> list[tuple[int, int]]:
    """Group paragraphs into chunks. Returns [(start, end_exclusive), ...]."""
    if target < 1:
        raise ValueError("chunk target must be >= 1 word")
    ranges: list[tuple[int, int]] = []
    start, running = 0, 0
    for i, para in enumerate(paragraphs):
        running += word_count(para)
        if running >= target:
            ranges.append((start, i + 1))
            start, running = i + 1, 0
    if start < len(paragraphs):
        ranges.append((start, len(paragraphs)))
    return ranges


def _is_abbreviation(text: str, dot_pos: int) -> bool:
    """True if the '.' at dot_pos closes an abbreviation or an initial."""
    m = re.search(r"([A-Za-z]+)$", text[:dot_pos])
    if not m:
        return False
    token = m.group(1)
    if token.lower() in _ABBREVIATIONS:
        return True
    # A single capital letter ("J. Smith") is an initial.
    return len(token) == 1 and token.isupper()


def _split_paragraph(text: str, base: int) -> list[dict[str, Any]]:
    sentences: list[dict[str, Any]] = []
    start = 0
    for m in _TERMINATOR.finditer(text):
        end = m.end()
        rest = text[end:]
        # Only split before whitespace followed by something that can open a
        # sentence (not a lowercase continuation like '"Yes!" he cried').
        nxt = re.match(r"\s+(\S)", rest)
        if not nxt or nxt.group(1).islower():
            continue
        if m.group(0).startswith(".") and len(m.group(0).rstrip("\"'”’)]")) == 1 \
                and _is_abbreviation(text, m.start()):
            continue
        piece = text[start:end]
        lead = len(piece) - len(piece.lstrip())
        if piece.strip():
            sentences.append({"text": piece.strip(), "start": base + start + lead,
                              "end": base + start + lead + len(piece.strip())})
        start = end
    piece = text[start:]
    lead = len(piece) - len(piece.lstrip())
    if piece.strip():
        sentences.append({"text": piece.strip(), "start": base + start + lead,
                          "end": base + start + lead + len(piece.strip())})
    return sentences


def segment_sentences(paragraphs: list[str]) -> list[dict[str, Any]]:
    """Sentences of the chunk formed by joining `paragraphs` with PARAGRAPH_SEP.

    Each sentence is {"text", "start", "end"} with offsets into the chunk text,
    so chunk_text[start:end] == text.
    """
    out: list[dict[str, Any]] = []
    base = 0
    for para in paragraphs:
        out.extend(_split_paragraph(para, base))
        base += len(para) + len(PARAGRAPH_SEP)
    return out


def build_chunks(paragraphs: list[str], chapters: list[dict[str, Any]], target: int) -> dict[str, Any]:
    """Chunk a book. Returns {"chunks", "sentences", "chunk_meta"}.

    `chapters` is [{"title", "paragraph_offset"}] sorted by offset; a chunk's
    chapter is the last heading at or before its first paragraph.
    """
    ranges = chunk_paragraphs(paragraphs, target)
    total_words = sum(word_count(p) for p in paragraphs) or 1
    chunks: dict[str, str] = {}
    sentences: dict[str, list[dict[str, Any]]] = {}
    meta: list[dict[str, Any]] = []
    cumulative = 0
    for idx, (a, b) in enumerate(ranges, start=1):
        paras = paragraphs[a:b]
        text = PARAGRAPH_SEP.join(paras)
        sents = segment_sentences(paras)
        words = sum(word_count(p) for p in paras)
        cumulative += words
        chapter = None
        for ch in chapters:
            if ch["paragraph_offset"] <= a:
                chapter = ch["title"]
            else:
                break
        chunks[f"chunk_{idx}"] = text
        sentences[f"chunk_{idx}"] = sents
        meta.append({
            "index": idx,
            "word_count": words,
            "sentence_count": len(sents),
            "paragraph_range": [a, b],
            "word_span": [cumulative - words, cumulative],
            "chapter": chapter,
            "cumulative_fraction": round(cumulative / total_words, 6),
        })
    return {"chunks": chunks, "sentences": sentences, "chunk_meta": meta}
