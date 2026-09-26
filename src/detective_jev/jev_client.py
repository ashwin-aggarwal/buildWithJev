"""Thin client around the Jev Decisions API.

Public entry point is `query(...)`. It handles:
  - OpenRouter (default) or TypeSafe-native transport, selected in config.
  - Retries with exponential backoff + jitter for 429 / 5xx / transient errors.
  - A request timeout.
  - An on-disk cache keyed by a hash of (model, context, question, choices) so
    reruns don't re-bill us and interrupted runs resume for free.
  - A `mock=True` dry-run mode that returns fake distributions (no network, no
    key, no spend) so the rest of the pipeline can be built cheaply.
  - Logging the resolved (dated) model version with every result.

Schema references (verified 2026-09-25, see SETUP.md):
  Request:  {model, state, questions: {qid: {type, instructions, criteria}}}
  Choice response: answers[qid] = {choice, probabilities: {opt: p}, confidence}
  Usage:    usage = {input_tokens, output_tokens, cost}
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import time
from pathlib import Path
from typing import Any

import requests

from . import config

logger = logging.getLogger("detective_jev.client")

# The single question id we use for every call. Callers pass the human-readable
# question text; this is just the key in the questions/answers maps.
_QUESTION_ID = "killer"

# HTTP statuses worth retrying.
_RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


class JevError(RuntimeError):
    """Raised when a Jev call fails after exhausting retries."""


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _normalize_choices(choices: list[str] | dict[str, str | None]) -> dict[str, str | None]:
    """Accept a plain list of options or an {option: description} map.

    Jev's Choice primitive takes `criteria` as an {option: description} map. If
    the caller passes a bare list, we send each option with a null description.
    (Your friend owns the *content* of choices/descriptions; this only handles
    serialization.)
    """
    if isinstance(choices, dict):
        return dict(choices)
    return {str(opt): None for opt in choices}


def _cache_key(context_text: str, question: str, criteria: dict[str, Any]) -> str:
    payload = json.dumps(
        {
            "model": config.MODEL_ID,
            "provider": config.PROVIDER,
            "context": context_text,
            "question": question,
            "criteria": criteria,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _cache_path(key: str) -> Path:
    return config.CACHE_DIR / f"{key}.json"


def _read_cache(key: str) -> dict[str, Any] | None:
    path = _cache_path(key)
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def _write_cache(key: str, result: dict[str, Any]) -> None:
    config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _cache_path(key).with_suffix(".json.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False)
        tmp.replace(_cache_path(key))
    except OSError as exc:  # cache is a nicety, never fatal
        logger.warning("Could not write cache %s: %s", key, exc)


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


def _mock_result(
    context_text: str,
    question: str,
    criteria: dict[str, Any],
) -> dict[str, Any]:
    """Return a deterministic fake distribution over the choices.

    Deterministic in (context, choices) so repeated dry-runs are stable, but it
    shifts as the context grows, which makes the animated curve look alive while
    developing. No network, no key, no spend.
    """
    options = list(criteria.keys())
    if not options:
        raise JevError("mock query needs at least one choice.")

    seed = int(_cache_key(context_text, question, criteria), 16) % (2**32)
    rng = random.Random(seed)
    weights = [rng.random() ** 2 for _ in options]  # squared -> spikier, more realistic
    total = sum(weights) or 1.0
    probs = {opt: round(w / total, 4) for opt, w in zip(options, weights)}

    # Renormalize rounding drift onto the top option.
    winner = max(probs, key=probs.get)
    probs[winner] = round(probs[winner] + (1.0 - sum(probs.values())), 4)

    sorted_p = sorted(probs.values(), reverse=True)
    confidence = round(sorted_p[0] - (sorted_p[1] if len(sorted_p) > 1 else 0.0), 4)

    # Rough token estimate so mock runs still exercise token/cost plumbing.
    from .tokens import count_tokens

    input_tokens = count_tokens(context_text) + count_tokens(question) + len(options) * 4

    return {
        "answer": winner,
        "confidence": confidence,
        "probabilities": probs,
        "raw_response": {"mock": True},
        "latency_ms": round(rng.uniform(70, 500), 1),
        "input_tokens": input_tokens,
        "model": f"{config.MODEL_ID} (MOCK)",
        "cost": 0.0,
        "cached": False,
        "mock": True,
    }


def _parse_response(raw: dict[str, Any], latency_ms: float) -> dict[str, Any]:
    """Map the raw Jev JSON onto our normalized result dict."""
    answers = raw.get("answers", {})
    ans = answers.get(_QUESTION_ID)
    if ans is None:
        raise JevError(f"Response missing answer for question id {_QUESTION_ID!r}: {raw!r}")

    usage = raw.get("usage", {}) or {}
    return {
        "answer": ans.get("choice"),
        "confidence": ans.get("confidence"),
        "probabilities": ans.get("probabilities", {}),
        "raw_response": raw,
        "latency_ms": round(latency_ms, 1),
        "input_tokens": usage.get("input_tokens"),
        # Resolved, dated model id echoed by the API (falls back to what we sent).
        "model": raw.get("model", config.MODEL_ID),
        "cost": usage.get("cost"),
        "cached": False,
        "mock": False,
    }


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

def query(
    context_text: str,
    question: str,
    choices: list[str] | dict[str, str | None],
    *,
    mock: bool = False,
    use_cache: bool = True,
) -> dict[str, Any]:
    """Ask Jev a single Choice question about `context_text`.

    Args:
        context_text: The state to evaluate (e.g. paragraphs 1..t of the story).
        question: The human-readable question, e.g. "Who is the killer?".
        choices: Either a list of option strings, or an {option: description}
            map. (Your friend builds this via friend_stubs.build_choices.)
        mock: If True, return a fake distribution without hitting the network.
        use_cache: If True, read/write the on-disk response cache.

    Returns:
        dict with keys:
          answer         -> winning choice (str)
          confidence     -> float 0..1
          probabilities  -> {choice: probability}
          raw_response   -> the full JSON Jev returned
          latency_ms     -> wall-clock latency of the call
          input_tokens   -> billed input tokens (from usage; None if unknown)
          model          -> resolved dated model id (logged for provenance)
          cost           -> USD cost of the call if the provider reports it
          cached         -> True if served from the on-disk cache
          mock           -> True if this was a dry-run

    Raises:
        JevError: on missing key, bad config, or failure after all retries.
    """
    criteria = _normalize_choices(choices)

    if mock:
        return _mock_result(context_text, question, criteria)

    key = _cache_key(context_text, question, criteria)
    if use_cache:
        cached = _read_cache(key)
        if cached is not None:
            cached["cached"] = True
            return cached

    url, headers = _endpoint_and_headers()
    body = {
        "model": config.MODEL_ID,
        "state": context_text,
        "questions": {
            _QUESTION_ID: {
                "type": "choice",
                "instructions": question,
                "criteria": criteria,
            }
        },
    }

    last_exc: Exception | None = None
    for attempt in range(config.MAX_RETRIES + 1):
        try:
            start = time.perf_counter()
            resp = requests.post(
                url, headers=headers, json=body, timeout=config.REQUEST_TIMEOUT_S
            )
            latency_ms = (time.perf_counter() - start) * 1000.0

            if resp.status_code in _RETRYABLE_STATUS:
                raise _RetryableHTTP(resp.status_code, resp.text)
            resp.raise_for_status()

            result = _parse_response(resp.json(), latency_ms)
            logger.info(
                "Jev call ok: model=%s tokens=%s latency=%.0fms cost=%s",
                result["model"], result["input_tokens"],
                result["latency_ms"], result["cost"],
            )
            if use_cache:
                _write_cache(key, result)
            return result

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
