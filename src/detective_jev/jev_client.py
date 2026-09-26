"""Thin client around the Jev Decisions API.

Public entry points:
  - `query_batch(state, questions)`: ask several typed questions (Choice,
    Score, Noul) against one state in ONE request. Jev evaluates them in
    parallel and independently.
  - `query(context, question, choices)`: one Choice question. A thin wrapper
    over `query_batch` with a single-element list, so there is one code path.

Shared behaviour (all in `query_batch` / `_post`):
  - OpenRouter (default) or TypeSafe-native transport, selected in config.
  - Retries with exponential backoff + jitter for 429 / 5xx / transient errors.
  - A request timeout.
  - A SQLite cache keyed by a hash of (model, provider, state, questions,
    options IN THE ORDER SENT), so reruns don't re-bill and crashed runs
    resume for free.
  - Choice option order is shuffled per call with a seed derived from
    (state, question id, option set), so the order is randomised across calls
    but reproducible for the same call (keeps caching and ledger purity). The
    order sent is returned so position sensitivity can be analysed later.
    Score levels are ordinal and are never shuffled.
  - A pre-send token guard: state + all questions <= MAX_REQUEST_TOKENS and
    state + longest question <= MAX_INPUT_TOKENS, else JevBudgetError.
  - A `mock=True` dry-run mode that returns fake distributions (no network, no
    key, no spend).

Schema references (verified 2026-09-25, docs.typesafe.ai):
  Request:  {model, state, questions: {qid: {type, instructions, criteria}}}
  Choice:   answers[qid] = {choice, probabilities: {opt: p}, confidence}
  Score:    answers[qid] = {score, probabilities: {"0": p, ...}, legend, confidence}
            (0-indexed levels; we convert to 1-indexed)
  Noul:     answers[qid] = {noul: p}
  Usage:    usage = {input_tokens, output_tokens, cost}
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import sqlite3
import time
from typing import Any

import requests

from . import config

logger = logging.getLogger("detective_jev.client")

# The question id `query()` uses. Callers pass the human-readable question
# text; this is just the key in the questions/answers maps.
_QUESTION_ID = "killer"

# HTTP statuses worth retrying. All 5xx are retried too (see _is_retryable),
# which covers Cloudflare's 52x codes (e.g. 520) that OpenRouter can return.
_RETRYABLE_STATUS = {408, 409, 425, 429}


def _is_retryable(status: int) -> bool:
    return status in _RETRYABLE_STATUS or 500 <= status < 600

_QUESTION_TYPES = {"choice", "score", "noul"}


class JevError(RuntimeError):
    """Raised when a Jev call fails after exhausting retries."""


class JevBudgetError(JevError):
    """Raised before sending when a request would exceed Jev's token limits."""


# --------------------------------------------------------------------------- #
# Question specs
# --------------------------------------------------------------------------- #
#
# A question spec is a dict:
#   {"id": str, "type": "choice"|"score"|"noul", "instructions": str,
#    "criteria": {option: desc|None}   (choice; insertion order is kept)
#              | [level_desc, ...]      (score; low -> high)
#              | None                   (noul)}
# Any other keys (e.g. "meta") are ignored by the client.


def _normalize_choices(choices: list[str] | dict[str, str | None]) -> dict[str, str | None]:
    """Accept a plain list of options or an {option: description} map."""
    if isinstance(choices, dict):
        return dict(choices)
    return {str(opt): None for opt in choices}


def _validate_spec(q: dict[str, Any]) -> None:
    qtype = q.get("type")
    if qtype not in _QUESTION_TYPES:
        raise JevError(f"Question {q.get('id')!r}: unknown type {qtype!r}.")
    if qtype == "choice":
        n = len(q.get("criteria") or {})
        if not 1 <= n <= config.MAX_CHOICE_OPTIONS:
            raise JevError(
                f"Choice {q['id']!r} has {n} options; Jev accepts 1..{config.MAX_CHOICE_OPTIONS}."
            )
    if qtype == "score":
        n = len(q.get("criteria") or [])
        if not 2 <= n <= config.MAX_SCORE_LEVELS:
            raise JevError(
                f"Score {q['id']!r} has {n} levels; Jev accepts 2..{config.MAX_SCORE_LEVELS}."
            )


def _shuffle_seed(state: str, qid: str, options: list[str]) -> int:
    payload = json.dumps([state, qid, sorted(options)], ensure_ascii=False)
    return int(hashlib.sha256(payload.encode("utf-8")).hexdigest(), 16) % (2**32)


def _prepare(state: str, questions: list[dict[str, Any]], shuffle: bool) -> list[dict[str, Any]]:
    """Validate specs and apply the per-call Choice option shuffle."""
    seen: set[str] = set()
    prepared = []
    for q in questions:
        _validate_spec(q)
        if q["id"] in seen:
            raise JevError(f"Duplicate question id {q['id']!r} in one batch.")
        seen.add(q["id"])
        criteria = q.get("criteria")
        if q["type"] == "choice":
            criteria = dict(criteria)
            if shuffle:
                order = list(criteria)
                random.Random(_shuffle_seed(state, q["id"], order)).shuffle(order)
                criteria = {opt: criteria[opt] for opt in order}
        elif q["type"] == "score":
            criteria = list(criteria)
        else:
            criteria = None
        prepared.append(
            {"id": q["id"], "type": q["type"], "instructions": q["instructions"], "criteria": criteria}
        )
    return prepared


def _wire_question(q: dict[str, Any]) -> dict[str, Any]:
    wire: dict[str, Any] = {"type": q["type"], "instructions": q["instructions"]}
    if q["criteria"] is not None:
        wire["criteria"] = q["criteria"]
    return wire


def _cache_key(state: str, prepared: list[dict[str, Any]]) -> str:
    # Choice criteria become ordered [option, desc] pairs so that option ORDER
    # is part of the key (a dict under sort_keys would hide it).
    ordered = [
        {
            "id": q["id"],
            "type": q["type"],
            "instructions": q["instructions"],
            "criteria": list(q["criteria"].items()) if isinstance(q["criteria"], dict) else q["criteria"],
        }
        for q in prepared
    ]
    payload = json.dumps(
        {"model": config.MODEL_ID, "provider": config.PROVIDER, "state": state, "questions": ordered},
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _check_budget(state: str, prepared: list[dict[str, Any]]) -> None:
    from .tokens import count_tokens

    state_tokens = count_tokens(state)
    q_tokens = [count_tokens(json.dumps(_wire_question(q), ensure_ascii=False)) for q in prepared]
    total = state_tokens + sum(q_tokens)
    longest = state_tokens + max(q_tokens, default=0)
    if total > config.MAX_REQUEST_TOKENS or longest > config.MAX_INPUT_TOKENS:
        raise JevBudgetError(
            f"Request too large for Jev: state+all questions ~{total:,} tokens "
            f"(limit {config.MAX_REQUEST_TOKENS:,}); state+longest question ~{longest:,} "
            f"(limit {config.MAX_INPUT_TOKENS:,}). Refusing rather than truncating."
        )


# --------------------------------------------------------------------------- #
# SQLite cache
# --------------------------------------------------------------------------- #

def _db() -> sqlite3.Connection:
    config.JEV_CACHE_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.JEV_CACHE_DB)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS jev_cache ("
        " key TEXT PRIMARY KEY, result TEXT NOT NULL, created_at REAL NOT NULL)"
    )
    return conn


def _read_cache(key: str) -> dict[str, Any] | None:
    try:
        with _db() as conn:
            row = conn.execute("SELECT result FROM jev_cache WHERE key = ?", (key,)).fetchone()
    except sqlite3.Error as exc:
        logger.warning("Could not read Jev cache: %s", exc)
        return None
    return json.loads(row[0]) if row else None


def _write_cache(key: str, result: dict[str, Any]) -> None:
    try:
        with _db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO jev_cache (key, result, created_at) VALUES (?, ?, ?)",
                (key, json.dumps(result, ensure_ascii=False), time.time()),
            )
    except sqlite3.Error as exc:  # cache is a nicety, never fatal
        logger.warning("Could not write Jev cache %s: %s", key, exc)


# --------------------------------------------------------------------------- #
# Transport
# --------------------------------------------------------------------------- #

def _endpoint_and_headers() -> tuple[str, dict[str, str]]:
    import os

    if config.PROVIDER == "openrouter":
        key = os.getenv(config.OPENROUTER_KEY_ENV)
        if not key:
            raise JevError(
                f"{config.OPENROUTER_KEY_ENV} is not set. Add it to .env "
                "(see SETUP.md) or run with mock=True."
            )
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            # Optional OpenRouter attribution headers (harmless if unused):
            "HTTP-Referer": "https://github.com/  (whodunit-curve)",
            "X-Title": "Whodunit Curve",
        }
        return config.OPENROUTER_DECISIONS_URL, headers

    if config.PROVIDER == "typesafe":
        key = os.getenv(config.TYPESAFE_KEY_ENV)
        if not key:
            raise JevError(
                f"{config.TYPESAFE_KEY_ENV} is not set. Add it to .env "
                "(see SETUP.md) or run with mock=True."
            )
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }
        return config.TYPESAFE_SYSTEMONE_URL, headers

    raise JevError(f"Unknown JEV_PROVIDER={config.PROVIDER!r} (expected 'openrouter' or 'typesafe').")


def _post(body: dict[str, Any]) -> tuple[dict[str, Any], float]:
    """POST one request with retries/backoff. Returns (raw JSON, latency_ms)."""
    url, headers = _endpoint_and_headers()
    last_exc: Exception | None = None
    for attempt in range(config.MAX_RETRIES + 1):
        try:
            start = time.perf_counter()
            resp = requests.post(
                url, headers=headers, json=body, timeout=config.REQUEST_TIMEOUT_S
            )
            latency_ms = (time.perf_counter() - start) * 1000.0

            if _is_retryable(resp.status_code):
                raise _RetryableHTTP(resp.status_code, resp.text)
            resp.raise_for_status()
            return resp.json(), latency_ms

        except (_RetryableHTTP, requests.Timeout, requests.ConnectionError) as exc:
            last_exc = exc
            if attempt >= config.MAX_RETRIES:
                break
            delay = min(
                config.BACKOFF_BASE_S * (2 ** attempt), config.BACKOFF_MAX_S
            ) + random.uniform(0, config.BACKOFF_BASE_S)
            logger.warning(
                "Jev call failed (%s), retry %d/%d in %.1fs",
                exc, attempt + 1, config.MAX_RETRIES, delay,
            )
            time.sleep(delay)
        except requests.HTTPError as exc:
            # Non-retryable HTTP error (e.g. 400/401/403): fail fast.
            raise JevError(f"Jev call failed: {exc} :: {getattr(exc.response, 'text', '')}") from exc

    raise JevError(f"Jev call failed after {config.MAX_RETRIES} retries: {last_exc}")


class _RetryableHTTP(Exception):
    def __init__(self, status: int, text: str) -> None:
        super().__init__(f"HTTP {status}: {text[:200]}")
        self.status = status


# --------------------------------------------------------------------------- #
# Answer normalisation
# --------------------------------------------------------------------------- #
#
# Every answer is normalised to:
#   {"type", "value", "prob", "distribution", "confidence", "option_order"}
#   choice: value = winning option, distribution = {option: p}
#   score:  value = most probable level (1-indexed int), distribution =
#           {"1": p, ..., "N": p}, plus "expected" = probability-weighted level
#   noul:   value = p >= 0.5, prob = p, distribution = {"true": p, "false": 1-p}

def _normalize_answer(q: dict[str, Any], ans: dict[str, Any]) -> dict[str, Any]:
    if q["type"] == "choice":
        probs = {opt: float(p) for opt, p in (ans.get("probabilities") or {}).items()}
        value = ans.get("choice")
        return {
            "type": "choice",
            "value": value,
            "prob": probs.get(value),
            "distribution": probs,
            "confidence": ans.get("confidence"),
            "option_order": list(q["criteria"]),
        }
    if q["type"] == "score":
        raw = ans.get("probabilities") or {}
        dist = {str(int(level) + 1): float(p) for level, p in raw.items()}
        top = max(dist, key=lambda k: (dist[k], -int(k))) if dist else None
        score = ans.get("score")
        return {
            "type": "score",
            "value": int(top) if top is not None else None,
            "expected": round(float(score) + 1, 4) if score is not None else None,
            "prob": dist.get(top) if top is not None else None,
            "distribution": dist,
            "confidence": ans.get("confidence"),
            "option_order": None,
        }
    p = float(ans.get("noul"))
    return {
        "type": "noul",
        "value": p >= 0.5,
        "prob": p,
        "distribution": {"true": p, "false": round(1.0 - p, 6)},
        "confidence": None,
        "option_order": None,
    }


def _mock_raw(state: str, prepared: list[dict[str, Any]], key: str) -> dict[str, Any]:
    """A deterministic fake API response shaped like the real one.

    Deterministic in (state, questions) so repeated dry-runs are stable, but it
    shifts as the state changes. No network, no key, no spend.
    """
    rng = random.Random(int(key, 16) % (2**32))
    answers: dict[str, Any] = {}
    for q in prepared:
        if q["type"] == "noul":
            answers[q["id"]] = {"type": "noul", "noul": round(rng.random(), 4)}
            continue
        options = list(q["criteria"]) if q["type"] == "choice" else [str(i) for i in range(len(q["criteria"]))]
        weights = [rng.random() ** 2 for _ in options]  # squared -> spikier, more realistic
        total = sum(weights) or 1.0
        probs = {opt: round(w / total, 4) for opt, w in zip(options, weights)}
        winner = max(probs, key=probs.get)
        probs[winner] = round(probs[winner] + (1.0 - sum(probs.values())), 4)
        sorted_p = sorted(probs.values(), reverse=True)
        confidence = round(sorted_p[0] - (sorted_p[1] if len(sorted_p) > 1 else 0.0), 4)
        if q["type"] == "choice":
            answers[q["id"]] = {"type": "choice", "choice": winner, "probabilities": probs,
                                "confidence": confidence}
        else:
            score = sum(int(k) * p for k, p in probs.items())
            answers[q["id"]] = {"type": "score", "score": round(score, 4), "probabilities": probs,
                                "legend": dict(zip(options, q["criteria"])), "confidence": confidence}

    from .tokens import count_tokens

    input_tokens = count_tokens(state) + sum(
        count_tokens(json.dumps(_wire_question(q), ensure_ascii=False)) for q in prepared
    )
    return {
        "answers": answers,
        "model": f"{config.MODEL_ID} (MOCK)",
        "usage": {"input_tokens": input_tokens, "output_tokens": 0, "cost": 0.0},
        "mock": True,
        "_latency_ms": round(rng.uniform(70, 500), 1),
    }


def _build_result(raw: dict[str, Any], prepared: list[dict[str, Any]], latency_ms: float,
                  mock: bool) -> dict[str, Any]:
    answers_raw = raw.get("answers", {}) or {}
    answers = {}
    for q in prepared:
        ans = answers_raw.get(q["id"])
        if ans is None:
            raise JevError(f"Response missing answer for question id {q['id']!r}: {raw!r}")
        answers[q["id"]] = _normalize_answer(q, ans)
    usage = raw.get("usage", {}) or {}
    return {
        "answers": answers,
        "option_order": {qid: a["option_order"] for qid, a in answers.items() if a["option_order"]},
        "raw_response": {"mock": True} if mock else raw,
        "latency_ms": round(latency_ms, 1),
        "input_tokens": usage.get("input_tokens"),
        # Resolved, dated model id echoed by the API (falls back to what we sent).
        "model": raw.get("model", config.MODEL_ID),
        "cost": usage.get("cost"),
        "cached": False,
        "mock": mock,
    }


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

def query_batch(
    state: str,
    questions: list[dict[str, Any]],
    *,
    mock: bool = False,
    use_cache: bool = True,
    shuffle: bool = True,
) -> dict[str, Any]:
    """Ask Jev several typed questions about one `state` in a single request.

    Args:
        state: The text Jev evaluates.
        questions: Question specs (see the module docstring).
        mock: If True, return fake distributions without hitting the network.
        use_cache: If True, read/write the SQLite response cache.
        shuffle: If True, shuffle each Choice's option order (seeded, see above).

    Returns:
        dict with keys:
          answers        -> {qid: {type, value, prob, distribution, confidence,
                                   option_order[, expected]}}
          option_order   -> {qid: [options in the order sent]} (Choice only)
          raw_response   -> the full JSON Jev returned
          latency_ms, input_tokens, model, cost, cached, mock

    Raises:
        JevBudgetError: if the request would exceed Jev's token limits.
        JevError: on missing key, bad config, or failure after all retries.
    """
    if not questions:
        raise JevError("query_batch needs at least one question.")
    prepared = _prepare(state, questions, shuffle)
    _check_budget(state, prepared)
    key = _cache_key(state, prepared)

    if mock:
        raw = _mock_raw(state, prepared, key)
        return _build_result(raw, prepared, raw.pop("_latency_ms"), mock=True)

    if use_cache:
        cached = _read_cache(key)
        if cached is not None:
            cached["cached"] = True
            return cached

    body = {
        "model": config.MODEL_ID,
        "state": state,
        "questions": {q["id"]: _wire_question(q) for q in prepared},
    }
    raw, latency_ms = _post(body)
    result = _build_result(raw, prepared, latency_ms, mock=False)
    logger.info(
        "Jev call ok: model=%s questions=%d tokens=%s latency=%.0fms cost=%s",
        result["model"], len(prepared), result["input_tokens"],
        result["latency_ms"], result["cost"],
    )
    if use_cache:
        _write_cache(key, result)
    return result


def query(
    context_text: str,
    question: str,
    choices: list[str] | dict[str, str | None],
    *,
    mock: bool = False,
    use_cache: bool = True,
) -> dict[str, Any]:
    """Ask Jev a single Choice question about `context_text`.

    A thin wrapper over `query_batch` with one question.

    Args:
        context_text: The state to evaluate (e.g. paragraphs 1..t of the story).
        question: The human-readable question, e.g. "Who is the killer?".
        choices: Either a list of option strings, or an {option: description}
            map. (Built via friend_stubs.build_choices.)
        mock: If True, return a fake distribution without hitting the network.
        use_cache: If True, read/write the response cache.

    Returns:
        dict with keys:
          answer         -> winning choice (str)
          confidence     -> float 0..1
          probabilities  -> {choice: probability}
          option_order   -> [choices in the order sent to Jev]
          raw_response   -> the full JSON Jev returned
          latency_ms     -> wall-clock latency of the call
          input_tokens   -> billed input tokens (from usage; None if unknown)
          model          -> resolved dated model id (logged for provenance)
          cost           -> USD cost of the call if the provider reports it
          cached         -> True if served from the cache
          mock           -> True if this was a dry-run

    Raises:
        JevError: on missing key, bad config, or failure after all retries.
    """
    spec = {"id": _QUESTION_ID, "type": "choice", "instructions": question,
            "criteria": _normalize_choices(choices)}
    r = query_batch(context_text, [spec], mock=mock, use_cache=use_cache)
    ans = r["answers"][_QUESTION_ID]
    return {
        "answer": ans["value"],
        "confidence": ans["confidence"],
        "probabilities": ans["distribution"],
        "option_order": ans["option_order"],
        "raw_response": r["raw_response"],
        "latency_ms": r["latency_ms"],
        "input_tokens": r["input_tokens"],
        "model": r["model"],
        "cost": r["cost"],
        "cached": r["cached"],
        "mock": r["mock"],
    }
