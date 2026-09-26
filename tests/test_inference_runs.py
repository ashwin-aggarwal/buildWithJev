import json

import pytest

from detective_jev import config, jev_client, ledger as ledger_mod, storage
from detective_jev.roster import load_roster
from run_curve_helper import run_book


@pytest.fixture
def ledger_built(roster_file, book):
    return ledger_mod.build_ledger("pg99999", mock=True)


def _rows(candidate_set=None):
    rows = []
    for p in sorted(config.RESULTS_DIR.glob("pg99999__*.jsonl")):
        rows += [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
    return [r for r in rows if candidate_set is None or r["candidate_set"] == candidate_set]


def test_both_candidate_sets_run_and_are_recorded(ledger_built, book):
    run_book("pg99999", candidate_set="full_cast")
    run_book("pg99999", candidate_set="suspects_only")
    full, sus = _rows("full_cast"), _rows("suspects_only")
    n = book["n_chunks"]
    assert [r["t"] for r in full] == list(range(1, n + 1)) == [r["t"] for r in sus]
    assert len({r["run_id"] for r in full}) == 1 and len({r["run_id"] for r in sus}) == 1
    assert full[0]["run_id"] != sus[0]["run_id"]
    assert len(list(config.RESULTS_DIR.glob("pg99999__*.jsonl"))) == 2    # separate files
    assert set(full[0]["probabilities"]) == {c["canonical"] for c in load_roster("pg99999")["characters"]} | {"none_of_these"}
    assert set(sus[0]["probabilities"]) == {"Dr. Hollis", "Simms", "none_of_these"}
    for r in full + sus:
        # existing fields unchanged, new fields added
        assert {"t", "answer", "probabilities", "confidence", "latency_ms", "input_tokens", "model",
                "cost", "cached", "timestamp"} <= set(r)
        assert r["condition"] == "test" and r["book_id"] == "pg99999"
        assert set(r["inference_answers"]) == {"culprit", "contradicts_prior", "alibi_effect"}
        assert r["answer"] == r["inference_answers"]["culprit"]["value"]
        assert sorted(r["option_order"]["culprit"]) == sorted(r["probabilities"])


def test_one_inference_call_per_step_and_resume(ledger_built, book, monkeypatch):
    calls = []
    real = jev_client.query_batch
    monkeypatch.setattr(jev_client, "query_batch", lambda s, q, **k: calls.append(s) or real(s, q, **k))
    run_book("pg99999", limit=3)
    assert len(calls) == 3
    run_book("pg99999")
    assert len(calls) == book["n_chunks"]                   # resumed: only the missing steps
    assert all("CURRENT PASSAGE" in s for s in calls)


def test_inference_answers_never_enter_ledger(ledger_built):
    before = storage.read_json(storage.ledger_path("pg99999", mock=True))
    run_book("pg99999", posthoc=True)
    after = storage.read_json(storage.ledger_path("pg99999", mock=True))
    assert after["entries"] == before["entries"]
    for e in after["entries"]:
        assert "contradicts_prior" not in e["answers"] and "post_hoc" not in e


def test_posthoc_sidecar_is_per_run_and_reaches_later_state(ledger_built, book, monkeypatch):
    states = []
    real = jev_client.query_batch
    monkeypatch.setattr(jev_client, "query_batch", lambda s, q, **k: states.append(s) or real(s, q, **k))
    run_book("pg99999", posthoc=True, condition="a")
    sidecars = list(config.LEDGERS_DIR.glob("pg99999.posthoc.*.json*"))
    assert len(sidecars) == 1
    data = storage.read_json(config.LEDGERS_DIR / sidecars[0].name.removesuffix(".gz"))
    assert set(data) == {str(t) for t in range(1, book["n_chunks"] + 1)}
    flagged = [int(k) for k, v in data.items() if v["contradicts_prior"] >= 0.5]
    if flagged and flagged[0] < book["n_chunks"]:
        assert "post-hoc: contradicts" in states[flagged[0]]          # shown from step t+1 on
    assert all("post-hoc" not in s for s in states[:1])
    run_book("pg99999", posthoc=False, condition="b")
    assert len(list(config.LEDGERS_DIR.glob("pg99999.posthoc.*.json*"))) == 1   # off by default
