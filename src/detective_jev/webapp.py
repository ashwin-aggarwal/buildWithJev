"""Flask backend for the live "Detective JEV" demo.

Serves the single-page frontend and a Server-Sent Events (SSE) endpoint that:
  - downloads + parses a Project Gutenberg text,
  - queries Jev paragraph by paragraph,
  - streams each result to the browser as it arrives, so the page shows JEV
    "solving" the mystery live.

The API key stays server-side (never sent to the browser). Real calls only
happen when the client asks for mock=0 AND a key is configured; the UI defaults
to mock mode so the demo is free by default.

Run it with:  python scripts/serve.py
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Iterator

from flask import Flask, Response, request, stream_with_context

from . import config, friend_stubs
from .jev_client import JevError, query
from .story import parse_paragraphs
from .tokens import count_tokens

import requests

_VIZ_DIR = config.PROJECT_ROOT / "viz"

# Safety cap so a giant book can't run away with cost/time. The UI also sends a
# limit; this is the hard ceiling regardless of what the client asks for.
_MAX_PARAGRAPHS = 200

# Book mode: which characters are culprit options (a run condition).
_CANDIDATE_SETS = ("full_cast", "suspects_only")

app = Flask(__name__)


def _sse(event: str, payload: dict[str, Any]) -> str:
    """Format one Server-Sent Event."""
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _fetch_text(url: str) -> str:
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError("URL must start with http:// or https://")
    resp = requests.get(url, timeout=config.REQUEST_TIMEOUT_S)
    resp.raise_for_status()
    return resp.text


def _resolve_choices(paragraphs: list[str], suspects_arg: str | None):
    """Use suspects typed in the UI if given, else the friend's build_choices().

    A comma-separated `suspects` field is a convenience for demoing today; the
    default path defers to friend_stubs.build_choices (the friend's domain).
    """
    if suspects_arg:
        names = [s.strip() for s in suspects_arg.split(",") if s.strip()]
        if names:
            return {name: None for name in names}, "manual"
    return friend_stubs.build_choices(paragraphs), "build_choices()"


def solve_stream(url: str, limit: int, mock: bool, suspects_arg: str | None) -> Iterator[str]:
    try:
        text = _fetch_text(url)
        paragraphs = parse_paragraphs(text)
    except Exception as exc:  # noqa: BLE001 - surface any fetch/parse error to the UI
        yield _sse("error", {"message": f"Could not load/parse story: {exc}"})
        return

    if not paragraphs:
        yield _sse("error", {"message": "No paragraphs found. Try the 'Plain Text UTF-8' Gutenberg link."})
        return

    limit = max(1, min(limit, _MAX_PARAGRAPHS))
    paragraphs = paragraphs[:limit]
    n = len(paragraphs)

    choices, choices_source = _resolve_choices(paragraphs, suspects_arg)
    question = friend_stubs.build_question()
    suspects = list(choices.keys() if isinstance(choices, dict) else choices)

    yield _sse("meta", {
        "model": config.MODEL_ID,
        "mock": mock,
        "n_paragraphs": n,
        "suspects": suspects,
        "question": question,
        "choices_source": choices_source,
        "price_per_1m": config.PRICE_PER_1M_INPUT_TOKENS,
    })

    cumulative_cost = 0.0
    final_answer = None
    for t in range(1, n + 1):
        # Tell the browser we're about to query this paragraph (the "live" beat).
        yield _sse("querying", {"t": t, "paragraph": paragraphs[t - 1]})

        context = friend_stubs.build_context(paragraphs, t)

        # Guard the 32k input limit before spending on a call that would fail.
        approx_tokens = count_tokens(context) + count_tokens(question)
        if approx_tokens > config.MAX_INPUT_TOKENS:
            yield _sse("warn", {
                "t": t,
                "message": (
                    f"Prefix at paragraph {t} is ~{approx_tokens:,} tokens, over the "
                    f"{config.MAX_INPUT_TOKENS:,} limit. Stopping. "
                    "build_context() would need to trim/summarize earlier paragraphs."
                ),
            })
            break

        try:
            result = query(context, question, choices, mock=mock)
        except JevError as exc:
            yield _sse("error", {"message": f"Jev call failed at paragraph {t}: {exc}"})
            return

        # In mock mode, pace the stream so it *feels* live (real calls pace themselves).
        if mock:
            time.sleep(min((result.get("latency_ms") or 200) / 1000.0, 0.6))

        cumulative_cost += result.get("cost") or 0.0
        final_answer = result["answer"]
        yield _sse("step", {
            "t": t,
            "paragraph": paragraphs[t - 1],
            "answer": result["answer"],
            "probabilities": result["probabilities"],
            "confidence": result["confidence"],
            "latency_ms": result["latency_ms"],
            "input_tokens": result["input_tokens"],
            "cost": result.get("cost"),
            "cumulative_cost": round(cumulative_cost, 6),
            "cached": result.get("cached", False),
            "model": result["model"],
        })

    yield _sse("done", {
        "final_answer": final_answer,
        "cumulative_cost": round(cumulative_cost, 6),
        "n_processed": t,
    })


# --------------------------------------------------------------------------- #
# Book mode: step through chunks with the ledger as history (the real pipeline).
# State at step t = rendered ledger 1..t-1 + chunk t; one batched Jev call per
# step. Reuses the SAME SSE event shape as paragraph mode so the frontend's
# charts render either without change ("paragraph" carries the chunk text; the
# suspect set is the roster's candidate names plus none_of_these).
# --------------------------------------------------------------------------- #

def _book_catalog() -> list[dict[str, Any]]:
    """Ingested books plus whether a mock/real ledger exists for each."""
    from . import storage
    from .ledger import ledger_exists

    df = storage.read_manifest()
    out: list[dict[str, Any]] = []
    for _, r in df.iterrows():
        bid = str(r["book_id"])
        n_chunks = r.get("n_chunks")
        out.append({
            "book_id": bid,
            "title": r.get("title"),
            "n_chunks": int(n_chunks) if n_chunks is not None else None,
            "has_mock_ledger": ledger_exists(bid, mock=True),
            "has_real_ledger": ledger_exists(bid, mock=False),
        })
    return out


def solve_book_stream(book_id: str, limit: int | None, mock: bool,
                      candidate_set: str, condition: str) -> Iterator[str]:
    from . import inference, storage
    from .ledger import ledger_exists, load_ledger
    from .questions import find, load_inference_questions
    from .roster import candidates, load_roster

    if candidate_set not in _CANDIDATE_SETS:
        candidate_set = "full_cast"

    try:
        book = storage.load_book(book_id)
    except Exception as exc:  # noqa: BLE001
        yield _sse("error", {"message": f"Book {book_id!r} is not ingested: {exc}"})
        return
    if not ledger_exists(book_id, mock=mock):
        which = "mock" if mock else "real"
        cmd = f"python -m detective_jev.cli ledger {book_id}" + ("" if mock else " --real")
        yield _sse("error", {"message": f"No {which} ledger for {book_id}. Build one first:  {cmd}"})
        return
    try:
        roster = load_roster(book_id, book)
        ledger = load_ledger(book_id, mock=mock)
        questions = load_inference_questions()
        names = candidates(roster, candidate_set)
    except Exception as exc:  # noqa: BLE001
        yield _sse("error", {"message": f"Could not set up book mode: {exc}"})
        return

    run_id = inference.compute_run_id(ledger, questions, candidate_set=candidate_set,
                                      condition=condition, mock=mock, posthoc=False)
    culprit = find(questions, "culprit")
    extra = culprit.get("extra_options", []) or []
    extra_labels = list(extra) if isinstance(extra, list) else list(extra.keys())
    suspects = list(names) + [str(e) for e in extra_labels]

    n = book["n_chunks"] if limit is None else max(1, min(limit, book["n_chunks"]))
    n = min(n, _MAX_PARAGRAPHS)

    yield _sse("meta", {
        "model": config.MODEL_ID,
        "mock": mock,
        "n_paragraphs": n,          # "paragraph" == chunk index in book mode
        "suspects": suspects,
        "question": culprit["text"],
        "choices_source": f"roster · {candidate_set}",
        "price_per_1m": config.PRICE_PER_1M_INPUT_TOKENS,
        "book_id": book_id,
        "title": book.get("title"),
        "unit": "passage",
    })

    cumulative_cost = 0.0
    final_answer = None
    t = 0
    for t in range(1, n + 1):
        chunk = storage.chunk_text(book, t)
        yield _sse("querying", {"t": t, "paragraph": chunk})
        try:
            result = inference.run_step(book, ledger, roster, questions, t,
                                        candidate_set=candidate_set, run_id=run_id, mock=mock)
        except JevError as exc:
            yield _sse("error", {"message": f"Jev call failed at passage {t}: {exc}"})
            return
        except Exception as exc:  # noqa: BLE001
            yield _sse("error", {"message": f"Inference failed at passage {t}: {exc}"})
            return

        row = inference.result_row(t, result, book_id=book_id, run_id=run_id,
                                   condition=condition, candidate_set=candidate_set)
        if mock:
            time.sleep(min((result.get("latency_ms") or 200) / 1000.0, 0.5))
        cumulative_cost += result.get("cost") or 0.0
        final_answer = row["answer"]
        yield _sse("step", {
            "t": t,
            "paragraph": chunk,
            "answer": row["answer"],
            "probabilities": row["probabilities"],
            "confidence": row["confidence"],
            "latency_ms": row["latency_ms"],
            "input_tokens": row["input_tokens"],
            "cost": row["cost"],
            "cumulative_cost": round(cumulative_cost, 6),
            "cached": row["cached"],
            "model": row["model"],
        })

    yield _sse("done", {
        "final_answer": final_answer,
        "cumulative_cost": round(cumulative_cost, 6),
        "n_processed": t,
    })


_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}


@app.get("/")
def index() -> Response:
    """The run viewer: replays saved book-mode runs (no API calls)."""
    html = (_VIZ_DIR / "index.html").read_text(encoding="utf-8")
    return Response(html, mimetype="text/html")


@app.get("/live")
def live() -> Response:
    """The original live-solve page (streams real or mock Jev calls)."""
    html = (_VIZ_DIR / "live.html").read_text(encoding="utf-8")
    return Response(html, mimetype="text/html")


_STATIC = {"app.js": "text/javascript", "app.css": "text/css"}
_IMAGES = {"favicon.png", "apple-touch-icon.png", "logo.png"}


@app.get("/static/<name>")
def static_file(name: str) -> Response:
    if name not in _STATIC:
        return Response("not found", status=404)
    return Response((_VIZ_DIR / name).read_text(encoding="utf-8"), mimetype=_STATIC[name])


@app.get("/static/img/<name>")
def static_image(name: str) -> Response:
    if name not in _IMAGES:
        return Response("not found", status=404)
    return Response((_VIZ_DIR / "img" / name).read_bytes(), mimetype="image/png",
                    headers={"Cache-Control": "max-age=86400"})


@app.get("/favicon.ico")
def favicon() -> Response:
    return static_image("favicon.png")


def _json(payload: Any, status: int = 200) -> Response:
    return Response(json.dumps(payload, ensure_ascii=False), status=status, mimetype="application/json")


# --- Add a book: upload/URL -> one background job running the whole pipeline ---------

app.config["MAX_CONTENT_LENGTH"] = 80 * 1024 * 1024   # uploads up to 80 MB

_JOBS: dict[str, dict[str, Any]] = {}
_JOBS_LOCK = threading.Lock()


@app.post("/api/books/add")
def api_add_book() -> Response:
    """Read a book (free) from an uploaded file or a URL; return it plus a cost estimate."""
    from .ingest import IngestError, ingest_source
    from .pipeline import estimate
    from .roster import load_roster

    try:
        upload = request.files.get("file")
        if upload and upload.filename:
            book = ingest_source(data=upload.read(), filename=upload.filename)
        else:
            url = (request.form.get("url") or (request.get_json(silent=True) or {}).get("url") or "").strip()
            if not url.lower().startswith(("http://", "https://")):
                return _json({"error": "Choose a file, or paste a link starting with http:// or https://"}, 400)
            book = ingest_source(url)
    except IngestError as exc:
        return _json({"error": str(exc)}, 400)
    except Exception as exc:  # noqa: BLE001 - network errors etc. go back to the page
        return _json({"error": f"Could not read that book: {exc}"}, 400)

    from . import storage

    n_chars = None
    if storage.roster_path(book["book_id"]).exists():
        n_chars = len(load_roster(book["book_id"])["characters"])
    return _json({
        "book_id": book["book_id"], "title": book.get("title"), "author": book.get("author"),
        "n_words": book["n_words"], "n_chunks": book["n_chunks"], "kind": book.get("source_kind"),
        "estimate": estimate(book, n_chars),
    })


def _run_job(job: dict[str, Any]) -> None:
    from .pipeline import STAGE_LABELS, Cancelled, run_all

    def on_progress(ev: dict[str, Any]) -> None:
        with _JOBS_LOCK:
            job["stages"][ev["stage"]] = {"label": STAGE_LABELS[ev["stage"]], "state": ev["state"],
                                          "done": ev["done"], "total": ev["total"],
                                          "message": ev["message"] or job["stages"][ev["stage"]].get("message", "")}
    try:
        result = run_all(book_id=job["book_id"], mock=job["mock"], progress=on_progress,
                         should_stop=lambda: job["stop"])
        with _JOBS_LOCK:
            job.update(state="done", result=result)
    except Cancelled:
        with _JOBS_LOCK:
            job.update(state="cancelled")
    except Exception as exc:  # noqa: BLE001 - report any failure to the page
        with _JOBS_LOCK:
            job.update(state="error", error=str(exc))


@app.post("/api/jobs")
def api_start_job() -> Response:
    """Run the whole pipeline for an already-read book in the background."""
    import os
    import uuid

    from .pipeline import STAGE_LABELS, STAGES

    body = request.get_json(silent=True) or {}
    book_id = str(body.get("book_id") or "")
    mock = bool(body.get("mock", True))
    from . import storage

    if not storage.book_exists(book_id):
        return _json({"error": f"Unknown book {book_id!r}; add it first."}, 400)
    if not mock and config.PROVIDER == "openrouter" and not os.getenv(config.OPENROUTER_KEY_ENV):
        return _json({"error": f"{config.OPENROUTER_KEY_ENV} is not set on the server, so only a dry run is possible."}, 400)
    with _JOBS_LOCK:
        if any(j["state"] == "running" for j in _JOBS.values()):
            return _json({"error": "Another book is already being processed. Wait for it to finish."}, 409)
        job = {"id": uuid.uuid4().hex[:12], "book_id": book_id, "mock": mock, "state": "running",
               "stop": False, "error": None, "result": None,
               "stages": {s: {"label": STAGE_LABELS[s], "state": "pending", "done": 0, "total": 0, "message": ""}
                          for s in STAGES}}
        _JOBS[job["id"]] = job
    threading.Thread(target=_run_job, args=(job,), daemon=True).start()
    return _json({"job_id": job["id"]})


@app.get("/api/jobs/<job_id>")
def api_job(job_id: str) -> Response:
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            return _json({"error": "No such job."}, 404)
        return _json({k: v for k, v in job.items() if k != "stop"})


@app.post("/api/jobs/<job_id>/stop")
def api_stop_job(job_id: str) -> Response:
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            return _json({"error": "No such job."}, 404)
        job["stop"] = True
    return _json({"ok": True})


@app.get("/api/runs")
def api_runs() -> Response:
    from .viewer import list_runs

    return _json({"runs": list_runs()})


@app.get("/api/run/<name>")
def api_run(name: str) -> Response:
    from .viewer import ViewerError, load_run

    try:
        return _json(load_run(name))
    except ViewerError as exc:
        return _json({"error": str(exc)}, 404)


@app.get("/api/cost/<name>")
def api_cost(name: str) -> Response:
    from .viewer import ViewerError, cost_inputs

    try:
        return _json(cost_inputs(name))
    except ViewerError as exc:
        return _json({"error": str(exc)}, 404)


@app.get("/api/books")
def api_books() -> Response:
    """List ingested books (with ledger availability) for the book-mode picker."""
    try:
        catalog = _book_catalog()
    except Exception as exc:  # noqa: BLE001
        return Response(json.dumps({"error": str(exc), "books": []}), mimetype="application/json")
    return Response(json.dumps({"books": catalog}), mimetype="application/json")


@app.get("/api/solve_book")
def solve_book() -> Response:
    book_id = request.args.get("book", "").strip()
    if not book_id:
        return Response(_sse("error", {"message": "Missing 'book'."}), mimetype="text/event-stream")
    try:
        limit: int | None = int(request.args.get("limit"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        limit = None
    mock = request.args.get("mock", "1") != "0"
    candidate_set = request.args.get("candidate_set", "full_cast")
    condition = request.args.get("condition", "demo")

    stream = stream_with_context(
        solve_book_stream(book_id, limit, mock, candidate_set, condition)
    )
    return Response(stream, mimetype="text/event-stream", headers=_SSE_HEADERS)


@app.get("/api/solve")
def solve() -> Response:
    url = request.args.get("url", "").strip()
    if not url:
        return Response(_sse("error", {"message": "Missing 'url'."}), mimetype="text/event-stream")
    try:
        limit = int(request.args.get("limit", 40))
    except ValueError:
        limit = 40
    mock = request.args.get("mock", "1") != "0"
    suspects_arg = request.args.get("suspects")

    stream = stream_with_context(solve_stream(url, limit, mock, suspects_arg))
    return Response(
        stream,
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable proxy buffering if present
            "Connection": "keep-alive",
        },
    )


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8000, threaded=True, debug=False)
