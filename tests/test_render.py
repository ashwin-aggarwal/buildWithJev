import copy

import pytest

from detective_jev import config, ledger as ledger_mod, storage
from detective_jev.render import compose_state, render_ledger
from detective_jev.roster import load_roster


@pytest.fixture
def built(roster_file, book):
    return ledger_mod.build_ledger("pg99999", mock=True), load_roster("pg99999"), book


def test_render_is_deterministic_and_pure(built):
    led, roster, _ = built
    entries = led["entries"]
    a = render_ledger(entries, roster)
    snapshot = copy.deepcopy(entries)
    assert render_ledger(list(reversed(entries)), roster) == a     # order-independent input
    assert render_ledger(copy.deepcopy(entries), roster) == a
    assert entries == snapshot                                      # no mutation
    # a prefix renders identically at every timestep
    for t in range(2, len(entries) + 1):
        assert render_ledger(entries[:t], roster).startswith(render_ledger(entries[: t - 1], roster))
    assert render_ledger([], roster) == "(no earlier passages)"
    for e in entries:
        if e["key_quote"]:
            assert e["key_quote"] in a


def test_suspects_filter_only_narrows_suspicion(built):
    led, roster, _ = built
    for e in led["entries"]:
        for name in e["suspicion"]:
            e["suspicion"][name]["value"] = 4
    full = render_ledger(led["entries"], roster)
    narrow = render_ledger(led["entries"], roster, suspects=["Simms"])
    assert "Arthur Penrose 4" in full and "Arthur Penrose 4" not in narrow
    assert "suspicion: Simms 4" in narrow


def test_state_excludes_chunk_t_from_notes(built):
    from detective_jev.inference import build_state

    led, roster, book = built
    t = 4
    state = build_state(book, led, roster, t)
    notes, current = state.split("\n\nCURRENT PASSAGE (#4):\n")
    assert current == storage.chunk_text(book, t)
    assert "#3 " in notes and "#4 " not in notes
    assert build_state(book, led, roster, 1).startswith("CASE NOTES (none yet)")
    assert state == compose_state(render_ledger(led["entries"][:3], roster), t, current)


def test_rollup_triggers_logs_and_hides_future(built, monkeypatch, caplog):
    led, roster, book = built
    monkeypatch.setattr(config, "LEDGER_TOKEN_BUDGET", 400)
    n = book["n_chunks"]
    ledger_mod.ensure_rollups(led, book, roster, n)
    assert led["rollups"], "a tiny budget must trigger rollups"
    assert any("rollup #1 of chunks" in r.message for r in caplog.records)
    covered = [i for r in led["rollups"] for i in range(r["chunks"][0], r["chunks"][1] + 1)]
    assert len(covered) == len(set(covered))                           # rolled up at most once
    for r in led["rollups"]:
        assert r["chunks"][1] < r["triggered_at_t"]                    # only entries before t
        assert r["chunks"][0] >= 1
    first = led["rollups"][0]
    assert not ledger_mod.active_rollups(led, first["triggered_at_t"] - 1) or \
        first not in ledger_mod.active_rollups(led, first["triggered_at_t"] - 1)
    notes = render_ledger(led["entries"][: n - 1], roster, rollups=ledger_mod.active_rollups(led, n))
    assert "SUMMARY of passages" in notes


def test_rollups_idempotent_and_order_independent(built, monkeypatch):
    led, roster, book = built
    monkeypatch.setattr(config, "LEDGER_TOKEN_BUDGET", 400)
    n = book["n_chunks"]
    a = copy.deepcopy(led)
    ledger_mod.ensure_rollups(a, book, roster, n)
    snapshot = copy.deepcopy(a["rollups"])
    assert ledger_mod.ensure_rollups(a, book, roster, n) is False        # second pass: no-op
    assert a["rollups"] == snapshot
    b = copy.deepcopy(led)
    for t in (3, n, 2, n - 1):                                          # steps out of order
        ledger_mod.ensure_rollups(b, book, roster, t)
    assert b["rollups"] == snapshot


def test_no_rollup_under_default_budget(built):
    led, roster, book = built
    assert ledger_mod.ensure_rollups(led, book, roster, book["n_chunks"]) is False
    assert led["rollups"] == []
