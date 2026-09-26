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
    html = (_VIZ_DIR / "index.html").read_text(encoding="utf-8")
    return Response(html, mimetype="text/html")


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
