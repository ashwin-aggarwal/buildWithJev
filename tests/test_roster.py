import pytest
import yaml

from conftest import ROSTER
from detective_jev import config, roster as roster_mod, storage


def test_first_mention_derived_in_code(roster_file, book):
    r = roster_mod.load_roster("pg99999", book)
    firsts = {c["canonical"]: c["first_mention_chunk"] for c in r["characters"]}
    assert firsts["Arthur Penrose"] == 1
    crane_chunk = next(t for t in range(1, book["n_chunks"] + 1)
                       if "Crane" in storage.chunk_text(book, t))
    assert firsts["Inspector Crane"] == crane_chunk > 1


def test_duplicate_alias_warning(book):
    r = {"book_id": "x", "characters": [
        {"canonical": "A", "aliases": ["Sam"]}, {"canonical": "B", "aliases": ["sam"]}]}
    warnings = roster_mod.validate_roster(r)
    assert any("'sam'" in w and "several characters" in w for w in warnings)


def test_overmerge_warning(book):
    # "Penrose" and "Simms" wrongly merged: they co-occur in one sentence.
    r = {"book_id": "x", "characters": [
        {"canonical": "Arthur Penrose", "aliases": ["Penrose", "Simms"]}]}
    warnings = roster_mod.validate_roster(r, book)
    assert any("over-merge" in w for w in warnings)
    # longest-match: "Mr. Penrose" alone is ONE name, not two
    r2 = {"book_id": "x", "characters": [{"canonical": "Arthur Penrose", "aliases": ["Mr. Penrose", "Penrose"]}]}
    assert not any("over-merge" in w for w in roster_mod.validate_roster(r2, book))


def test_missed_character_warning(book, monkeypatch):
    monkeypatch.setattr(config, "MISSED_NAME_MIN_COUNT", 3)
    r = {"book_id": "x", "characters": [c for c in ROSTER["characters"] if c["canonical"] != "Dr. Hollis"]}
    warnings = roster_mod.validate_roster(r, book)
    assert any("missed" in w and "Hollis" in w for w in warnings)
    full = roster_mod.validate_roster(ROSTER, book)
    assert not any("Hollis" in w for w in full)


def test_validation_warns_never_raises(book, caplog):
    r = {"book_id": "x", "characters": [{"canonical": "A", "aliases": ["A2"]},
                                        {"canonical": "B", "aliases": ["A2"]}]}
    roster_mod.validate_roster(r, book)
    assert any(rec.levelname == "WARNING" for rec in caplog.records)


def test_existing_yaml_is_authoritative(roster_file, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("API must not be called when a roster file exists")
    monkeypatch.setattr(roster_mod, "_call_extraction_api", boom)
    r = roster_mod.get_roster("pg99999")
    assert [c["canonical"] for c in r["characters"]][0] == "Arthur Penrose"


def test_extraction_once_then_cached(book, monkeypatch):
    calls = []
    fake = {"characters": [{"canonical": "Arthur Penrose", "aliases": ["Penrose", "Arthur Penrose"],
                            "evidence": {"Penrose": "..."}, "uncertain": False}]}
    monkeypatch.setattr(roster_mod, "_call_extraction_api", lambda b: calls.append(1) or fake)
    r = roster_mod.get_roster("pg99999")
    assert calls == [1]
    assert r["characters"][0]["aliases"] == ["Penrose"]           # canonical removed from aliases
    assert r["characters"][0]["is_suspect"] is False
    saved = yaml.safe_load(storage.roster_path("pg99999").read_text())
    assert saved["characters"][0]["first_mention_chunk"] == 1
    storage.roster_path("pg99999").unlink()
    roster_mod.get_roster("pg99999")                               # YAML gone, cache hit
    assert calls == [1]


def test_over_length_book_refused(book, monkeypatch):
    import anthropic

    class Count:
        input_tokens = 2_000_000

    class Messages:
        def count_tokens(self, **kw):
            return Count()

        def stream(self, **kw):
            raise AssertionError("must not call the model for an over-length book")

    class Client:
        def __init__(self, **kw):
            self.messages = Messages()

    monkeypatch.setattr(anthropic, "Anthropic", Client)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    with pytest.raises(roster_mod.RosterError, match="hand-write"):
        roster_mod.get_roster("pg99999")


def test_candidates(roster_file):
    r = roster_mod.load_roster("pg99999")
    assert len(roster_mod.candidates(r, "full_cast")) == 5
    assert roster_mod.candidates(r, "suspects_only") == ["Dr. Hollis", "Simms"]
    with pytest.raises(roster_mod.RosterError):
        roster_mod.candidates(r, "everyone")
