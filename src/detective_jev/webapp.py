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


@app.get("/")
def index() -> Response:
    html = (_VIZ_DIR / "index.html").read_text(encoding="utf-8")
    return Response(html, mimetype="text/html")


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
