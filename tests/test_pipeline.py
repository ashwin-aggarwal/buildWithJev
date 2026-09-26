"""One-step pipeline: CLI `run`, web upload + job, placeholder roster, estimate."""

import time

import pytest
import yaml

from detective_jev import config, pipeline, roster as roster_mod, storage
from test_sources import gutenberg_txt


@pytest.fixture
def txt_book(tmp_path):
    path = tmp_path / "vane_court.txt"
    # repeat the story so there are several chunks
    path.write_text(gutenberg_txt().replace("Simms found the body by the fire.",
                                            " ".join(["Simms found the body by the fire."] * 60)), encoding="utf-8")
    return path


def test_run_all_dry_run_every_stage(txt_book):
    events = []
    result = pipeline.run_all(str(txt_book), mock=True, chunk_size=60, progress=events.append)
    stages_done = [e["stage"] for e in events if e["state"] == "done"]
    assert stages_done == list(pipeline.STAGES)
    rows = (config.RESULTS_DIR / result["run_file"]).read_text().splitlines()
    book = storage.load_book(result["book_id"])
    assert len(rows) == book["n_chunks"] and result["mock"] is True
    ros = yaml.safe_load(storage.roster_path(result["book_id"]).read_text())
    assert roster_mod.is_heuristic(ros)                       # free placeholder, no model called
    again = pipeline.run_all(book_id=result["book_id"], mock=True)
    assert again["run_file"] == result["run_file"]            # resumes, same run


def test_real_run_replaces_placeholder_roster(txt_book, monkeypatch):
    result = pipeline.run_all(str(txt_book), mock=True, chunk_size=60)
    calls = []
    fake = {"characters": [{"canonical": "Arthur Penrose", "aliases": ["Penrose"], "uncertain": False},
                           {"canonical": "Simms", "aliases": [], "uncertain": False}]}
    monkeypatch.setattr(roster_mod, "_call_extraction_api", lambda b: calls.append(1) or fake)
    r = roster_mod.get_roster(result["book_id"], mock=False)
    assert calls == [1] and not roster_mod.is_heuristic(r)
    assert roster_mod.get_roster(result["book_id"], mock=False)["characters"][0]["canonical"] == "Arthur Penrose"
    assert calls == [1]                                        # authoritative from now on


def test_extraction_falls_back_to_openrouter(monkeypatch, book):
    monkeypatch.setattr(config, "EXTRACTION_PROVIDER", "anthropic")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    used = []
    monkeypatch.setattr(roster_mod, "_call_extraction_openrouter", lambda b: used.append("or") or {"characters": []})
    roster_mod._call_extraction_api(book)
    assert used == ["or"]


def test_estimate_scales(txt_book):
    small = pipeline.run_all(str(txt_book), mock=True, chunk_size=200)
    book = storage.load_book(small["book_id"])
    est = pipeline.estimate(book)
    assert est["jev_cost"] > 0 and est["jev_calls"] == 2 * book["n_chunks"] and est["minutes"] > 0


def test_cancel_stops_between_steps(txt_book):
    with pytest.raises(pipeline.Cancelled):
        pipeline.run_all(str(txt_book), mock=True, chunk_size=60, should_stop=lambda: True)


def test_web_upload_and_job(txt_book):
    from detective_jev.webapp import app

    c = app.test_client()
    with open(txt_book, "rb") as fh:
        r = c.post("/api/books/add", data={"file": (fh, "vane_court.txt")}, content_type="multipart/form-data")
    assert r.status_code == 200, r.json
    info = r.json
    assert info["kind"] == "txt" and info["estimate"]["jev_cost"] > 0
    bad = c.post("/api/books/add", data={"file": (open(txt_book, "rb"), "x.docx")},
                 content_type="multipart/form-data")
    assert bad.status_code == 400 and "Unsupported" in bad.json["error"]
    assert c.post("/api/jobs", json={"book_id": info["book_id"], "mock": False}).status_code == 400  # no key
    job = c.post("/api/jobs", json={"book_id": info["book_id"], "mock": True}).json["job_id"]
    for _ in range(200):
        state = c.get(f"/api/jobs/{job}").json
        if state["state"] != "running":
            break
        time.sleep(0.05)
    assert state["state"] == "done", state
    assert all(s["state"] == "done" for s in state["stages"].values())
    assert state["result"]["run_file"] in [r["file"] for r in c.get("/api/runs").json["runs"]]
