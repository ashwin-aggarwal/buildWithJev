from detective_jev.chunking import (PARAGRAPH_SEP, build_chunks, chunk_paragraphs,
                                    segment_sentences, word_count)

PARAS = [" ".join(f"w{i}_{j}" for j in range(n)) + "." for i, n in
         enumerate([30, 10, 80, 5, 5, 5, 40, 12, 200, 3, 7])]


def test_paragraphs_never_split_and_cover_everything():
    for target in (1, 20, 50, 100, 500, 10_000):
        ranges = chunk_paragraphs(PARAS, target)
        # contiguous, whole paragraphs, nothing dropped or repeated
        assert ranges[0][0] == 0 and ranges[-1][1] == len(PARAS)
        for (a, b), (c, _) in zip(ranges, ranges[1:]):
            assert b == c and a < b
        out = build_chunks(PARAS, [], target)
        rebuilt = [p for i in range(1, len(out["chunks"]) + 1)
                   for p in out["chunks"][f"chunk_{i}"].split(PARAGRAPH_SEP)]
        assert rebuilt == PARAS


def test_chunks_reach_target_except_last():
    target = 50
    ranges = chunk_paragraphs(PARAS, target)
    for a, b in ranges[:-1]:
        words = sum(word_count(p) for p in PARAS[a:b])
        assert words >= target
        # emitted as soon as the target was reached
        assert sum(word_count(p) for p in PARAS[a:b - 1]) < target


def test_indices_contiguous_from_one_and_deterministic():
    a = build_chunks(PARAS, [{"title": "One", "paragraph_offset": 0},
                             {"title": "Two", "paragraph_offset": 6}], 50)
    b = build_chunks(PARAS, [{"title": "One", "paragraph_offset": 0},
                             {"title": "Two", "paragraph_offset": 6}], 50)
    assert a == b
    n = len(a["chunks"])
    assert list(a["chunks"]) == [f"chunk_{i}" for i in range(1, n + 1)]
    assert [m["index"] for m in a["chunk_meta"]] == list(range(1, n + 1))
    assert a["chunk_meta"][-1]["cumulative_fraction"] == 1.0
    assert a["chunk_meta"][0]["chapter"] == "One"
    assert any(m["chapter"] == "Two" for m in a["chunk_meta"])


def test_sentence_offsets_and_abbreviations():
    paras = ['Mr. Penrose met Dr. J. Hollis at St. Mary’s. They spoke briefly!',
             '“Yes!” he cried. “No,” said Lady Vane. Then silence fell.']
    text = PARAGRAPH_SEP.join(paras)
    sents = segment_sentences(paras)
    assert [s["text"] for s in sents] == [
        "Mr. Penrose met Dr. J. Hollis at St. Mary’s.",
        "They spoke briefly!",
        "“Yes!” he cried.",
        "“No,” said Lady Vane.",
        "Then silence fell.",
    ]
    for s in sents:
        assert text[s["start"]:s["end"]] == s["text"]


def test_sentence_segmentation_stable_and_stored_once(book):
    from detective_jev import storage

    stored = storage.load_book(book["book_id"])
    for t in range(1, stored["n_chunks"] + 1):
        text = storage.chunk_text(stored, t)
        again = segment_sentences(text.split(PARAGRAPH_SEP))
        assert again == storage.chunk_sentences(stored, t)
        assert again == segment_sentences(text.split(PARAGRAPH_SEP))
        assert stored["chunk_meta"][t - 1]["sentence_count"] == len(again)
