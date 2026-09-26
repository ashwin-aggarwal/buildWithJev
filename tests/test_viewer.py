"""The run viewer's API: shapes, path safety, spoiler-free ordering, cost inputs."""

import yaml

from detective_jev import config, ledger as ledger_mod
from detective_jev.webapp import app
from run_curve_helper import run_book


def _setup(roster_file, book):
    ledger_mod.build_ledger("pg99999", mock=True)
    run_book("pg99999", condition="viewer")
    return app.test_client()


def test_runs_and_run_payload(roster_file, book):
    c = _setup(roster_file, book)
    runs = c.get("/api/runs").json["runs"]
    assert len(runs) == 1 and runs[0]["mock"] is True and runs[0]["n_steps"] == book["n_chunks"]
    run = c.get(f"/api/run/{runs[0]['file']}").json
    names = [x["name"] for x in run["candidates"]]
    # neutral order: first appearance in the text, none_of_these last
    firsts = [x["first_chunk"] for x in run["candidates"] if not x["is_none"]]
    assert firsts == sorted(firsts) and names[-1] == "none_of_these"
    assert len(run["steps"]) == book["n_chunks"] and all(len(s["p"]) == len(names) for s in run["steps"])
    assert len(run["chunks"]) == book["n_chunks"] and run["chunks"][0]["text"].startswith("Arthur Penrose")
    assert "Mr. Penrose" in next(x for x in run["candidates"] if x["name"] == "Arthur Penrose")["names"]


def test_viewer_never_exposes_the_answer_key(roster_file, book):
    c = _setup(roster_file, book)
    config.ANSWERS_DIR.mkdir(parents=True, exist_ok=True)
    (config.ANSWERS_DIR / "pg99999.yaml").write_text(
        yaml.safe_dump({"book_id": "pg99999", "is_culprit": {"Dr. Hollis": True}}))
    f = c.get("/api/runs").json["runs"][0]["file"]
    for url in (f"/api/run/{f}", f"/api/cost/{f}"):
        body = c.get(url).get_data(as_text=True)
        assert "culprit" not in body and "is_culprit" not in body


def test_run_names_are_validated(roster_file, book):
    c = _setup(roster_file, book)
    for bad in ("..%2F..%2Fsecrets", "x.jsonl", "pg99999__zz.jsonl"):
        assert c.get(f"/api/run/{bad}").status_code == 404
    assert c.get("/static/webapp.py").status_code == 404
    assert c.get("/static/app.js").status_code == 200 and c.get("/live").status_code == 200


def test_cost_inputs(roster_file, book):
    c = _setup(roster_file, book)
    f = c.get("/api/runs").json["runs"][0]["file"]
    cost = c.get(f"/api/cost/{f}").json
    inf, comp = cost["inference"], cost["compression"]
    assert inf["calls"] == comp["calls"] == book["n_chunks"]
    assert inf["measured"] is True and comp["measured"] is False
    assert inf["llm_output_full_per_call"] > inf["llm_output_top_per_call"] > 0
    assert comp["input_tokens"] > 0 and cost["jev_price_per_m"] == config.PRICE_PER_1M_INPUT_TOKENS
    assert {m["id"] for m in cost["prices"]["models"]} >= {"anthropic/claude-sonnet-5", "deepseek/deepseek-v4-flash"}


def test_run_payload_carries_reveal_and_window(roster_file, book):
    c = _setup(roster_file, book)
    run = c.get(f"/api/run/{c.get('/api/runs').json['runs'][0]['file']}").json
    assert run["raw_window"] == config.RAW_WINDOW
    assert all(0.0 <= s["revealed"] <= 1.0 for s in run["steps"])   # Jev's judgement, per step
