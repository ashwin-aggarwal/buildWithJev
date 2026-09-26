import pytest

from detective_jev import config, jev_client, ledger as ledger_mod, storage
from detective_jev.questions import load_compression_questions
from detective_jev.roster import load_roster


@pytest.fixture
def count_calls(monkeypatch):
    calls = []
    real = jev_client.query_batch

    def wrapped(state, questions, **kw):
        calls.append(state)
        return real(state, questions, **kw)

    monkeypatch.setattr(jev_client, "query_batch", wrapped)
    return calls


def test_entry_reproducible_from_chunk_text_alone(roster_file, book):
    roster = load_roster("pg99999")
    qs = load_compression_questions()
    t = 3
    text, sents = storage.chunk_text(book, t), storage.chunk_sentences(book, t)
    a = ledger_mod.build_entry(text, sents, roster, qs, mock=True)
    b = ledger_mod.build_entry(text, sents, roster, qs, mock=True)
    assert a == b
    # same text in a different book/position -> same entry content
    full = ledger_mod.build_ledger("pg99999", mock=True)
    stored = {k: v for k, v in full["entries"][t - 1].items() if k not in ("chunk", "chapter", "word_span")}
    assert stored == a
    assert a["key_quote"] == sents[a["key_sentence_idx"]]["text"]
    assert set(a["suspicion"]) == {c["canonical"] for c in roster["characters"]}
    assert all(1 <= s["value"] <= 5 for s in a["suspicion"].values())
    assert "contradicts_prior" not in a["answers"] and "alibi_effect" not in a["answers"]


def test_exactly_one_call_per_chunk_and_idempotent(roster_file, book, count_calls):
    led = ledger_mod.build_ledger("pg99999", mock=True)
    n = book["n_chunks"]
    assert len(count_calls) == n == len(led["entries"]) == led["compression_calls"]
    assert count_calls == [storage.chunk_text(book, t) for t in range(1, n + 1)]  # state = chunk only
    assert [e["chunk"] for e in led["entries"]] == list(range(1, n + 1))
    before = storage.read_json(storage.ledger_path("pg99999", mock=True))
    ledger_mod.build_ledger("pg99999", mock=True)
    assert len(count_calls) == n                                # re-run: zero calls
    assert storage.read_json(storage.ledger_path("pg99999", mock=True)) == before


def test_duplicate_entry_raises(roster_file, book):
    led = ledger_mod.build_ledger("pg99999", mock=True)
    with pytest.raises(ledger_mod.DuplicateEntryError):
        ledger_mod.append_entry(led, dict(led["entries"][0]))
    with pytest.raises(ledger_mod.LedgerError):
        ledger_mod.append_entry(led, dict(led["entries"][0], chunk=led["n_chunks"] + 5))


def test_resume_is_append_only(roster_file, book, count_calls):
    led = ledger_mod.build_ledger("pg99999", mock=True)
    first = led["entries"][:2]
    led["entries"] = led["entries"][:2]
    led["compression_calls"] = 2
    ledger_mod.save_ledger(led)
    resumed = ledger_mod.build_ledger("pg99999", mock=True)
    assert resumed["entries"][:2] == first
    assert len(count_calls) == book["n_chunks"] + book["n_chunks"] - 2


def test_call_counter_fails_loudly(roster_file, book):
    led = ledger_mod.build_ledger("pg99999", mock=True)
    led["entries"] = led["entries"][:1]           # entries lost but calls were already made
    ledger_mod.save_ledger(led)
    with pytest.raises(ledger_mod.CompressionCallBudgetError):
        ledger_mod.build_ledger("pg99999", mock=True)


def test_roster_or_config_change_refused(roster_file, book, monkeypatch):
    ledger_mod.build_ledger("pg99999", mock=True)
    text = roster_file.read_text().replace("- Crane", "- Crane\n  - Inspector")
    roster_file.write_text(text)
    with pytest.raises(ledger_mod.LedgerMismatchError, match="roster"):
        ledger_mod.build_ledger("pg99999", mock=True)
    ledger_mod.build_ledger("pg99999", mock=True, force=True)   # explicit rebuild works


def test_is_suspect_edit_does_not_invalidate(roster_file, book):
    ledger_mod.build_ledger("pg99999", mock=True)
    roster_file.write_text(roster_file.read_text().replace("is_suspect: false", "is_suspect: true", 1))
    ledger_mod.build_ledger("pg99999", mock=True)


def test_mock_and_real_ledgers_are_separate(roster_file, book):
    ledger_mod.build_ledger("pg99999", mock=True)
    assert storage.json_exists(storage.ledger_path("pg99999", mock=True))
    assert not storage.json_exists(storage.ledger_path("pg99999", mock=False))
