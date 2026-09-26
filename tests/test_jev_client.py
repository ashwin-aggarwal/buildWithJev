import pytest

from detective_jev import jev_client
from detective_jev.jev_client import JevBudgetError, query, query_batch

CHOICE = {"id": "who", "type": "choice", "instructions": "Who?",
          "criteria": {"alice": None, "bob": "the gardener", "carol": None, "dave": None}}
SCORE = {"id": "sev", "type": "score", "instructions": "How bad?", "criteria": ["low", "mid", "high"]}
NOUL = {"id": "yes", "type": "noul", "instructions": "Is it?", "criteria": None}


@pytest.fixture
def fake_post(monkeypatch):
    """Replace the network layer; answers every question in the body."""
    calls = []

    def post(body):
        calls.append(body)
        answers = {}
        for qid, q in body["questions"].items():
            if q["type"] == "choice":
                opts = list(q["criteria"])
                probs = {o: (0.7 if i == 0 else 0.3 / (len(opts) - 1)) for i, o in enumerate(opts)}
                answers[qid] = {"type": "choice", "choice": opts[0], "probabilities": probs, "confidence": 0.5}
            elif q["type"] == "score":
                answers[qid] = {"type": "score", "score": 1.6, "confidence": 0.4,
                                "probabilities": {"0": 0.1, "1": 0.2, "2": 0.7}, "legend": {}}
            else:
                answers[qid] = {"type": "noul", "noul": 0.8}
        return {"answers": answers, "model": "typesafe/jev-1.13-test",
                "usage": {"input_tokens": 42, "cost": 0.0}}, 12.0

    monkeypatch.setattr(jev_client, "_post", post)
    return calls


def test_query_and_query_batch_share_one_code_path(fake_post, monkeypatch):
    seen = []
    real = jev_client.query_batch
    monkeypatch.setattr(jev_client, "query_batch", lambda *a, **k: seen.append(1) or real(*a, **k))
    r = query("state", "Who?", ["alice", "bob"])
    assert seen == [1] and len(fake_post) == 1
    assert set(r) >= {"answer", "confidence", "probabilities", "option_order", "raw_response",
                      "latency_ms", "input_tokens", "model", "cost", "cached", "mock"}
    assert r["answer"] == r["option_order"][0]
    assert list(fake_post[0]["questions"]) == ["killer"]


def test_batch_parses_all_types_and_score_is_one_indexed(fake_post):
    r = query_batch("state", [CHOICE, SCORE, NOUL])
    assert len(fake_post) == 1                         # one request for all three
    s = r["answers"]["sev"]
    assert s["distribution"] == {"1": 0.1, "2": 0.2, "3": 0.7}
    assert s["value"] == 3 and s["expected"] == 2.6
    n = r["answers"]["yes"]
    assert n["value"] is True and n["prob"] == 0.8 and n["distribution"]["true"] == 0.8
    assert fake_post[0]["questions"]["sev"]["criteria"] == ["low", "mid", "high"]  # ordinal: never shuffled
    assert "criteria" not in fake_post[0]["questions"]["yes"]


def test_sqlite_cache_resumes_free(fake_post):
    a = query_batch("state", [CHOICE, NOUL])
    b = query_batch("state", [CHOICE, NOUL])
    assert len(fake_post) == 1 and b["cached"] is True
    assert a["answers"] == b["answers"]


def test_option_order_shuffled_recorded_and_in_cache_key(fake_post):
    r1 = query_batch("state one", [CHOICE])
    r2 = query_batch("state one", [CHOICE])
    assert r1["option_order"] == r2["option_order"]            # reproducible per call
    orders = {tuple(query_batch(f"state {i}", [CHOICE])["option_order"]["who"]) for i in range(12)}
    assert len(orders) > 1                                       # varies across calls
    assert all(sorted(o) == sorted(CHOICE["criteria"]) for o in orders)
    sent = [list(b["questions"]["who"]["criteria"]) for b in fake_post]
    assert sent[0] == r1["option_order"]["who"]
    # the same question with a different option order is a different cache entry
    p1 = jev_client._prepare("s", [CHOICE], shuffle=False)
    p2 = jev_client._prepare("s", [dict(CHOICE, criteria=dict(reversed(list(CHOICE["criteria"].items()))))],
                             shuffle=False)
    assert jev_client._cache_key("s", p1) != jev_client._cache_key("s", p2)


def test_budget_guard_refuses_instead_of_truncating(monkeypatch):
    monkeypatch.setattr(jev_client.config, "MAX_INPUT_TOKENS", 50)
    with pytest.raises(JevBudgetError):
        query_batch("word " * 100, [NOUL], mock=True)


def test_mock_is_deterministic_and_complete():
    a = query_batch("s", [CHOICE, SCORE, NOUL], mock=True)
    b = query_batch("s", [CHOICE, SCORE, NOUL], mock=True)
    assert a["answers"] == b["answers"] and a["mock"] is True
    assert abs(sum(a["answers"]["who"]["distribution"].values()) - 1) < 1e-6
    assert set(a["answers"]["sev"]["distribution"]) == {"1", "2", "3"}
