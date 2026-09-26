"""Central configuration for the Whodunit Curve project.

Every tunable constant lives here so there is exactly one place to change the
model version, the price, or the context limit. All values are verified against
the live docs (see SETUP.md) as of 2026-09-25.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Load a local .env if present. Never commit .env (see .gitignore).
load_dotenv()

# --- Model + provider -------------------------------------------------------

# Pinned model version. Requests send this; responses echo a dated build id
# (e.g. "typesafe/jev-1.13-20260917") which we log with every result.
MODEL_ID: str = os.getenv("JEV_MODEL_ID", "typesafe/jev-1.13")

# "openrouter" (default) or "typesafe". Chosen via env so no code change needed.
PROVIDER: str = os.getenv("JEV_PROVIDER", "openrouter")

# Decisions endpoints (verified against docs on 2026-09-25).
OPENROUTER_DECISIONS_URL: str = "https://openrouter.ai/api/alpha/decisions"
TYPESAFE_SYSTEMONE_URL: str = "https://api.typesafe.ai/v1/systemone"

# Env var names holding the API keys (the values live in .env, never in git).
OPENROUTER_KEY_ENV: str = "OPENROUTER_API_KEY"
TYPESAFE_KEY_ENV: str = "TYPESAFE_API_KEY"

# --- Pricing + limits (verified on the OpenRouter Jev 1.13 model page) -------

# USD per 1,000,000 input tokens. Output tokens are free.
PRICE_PER_1M_INPUT_TOKENS: float = 0.042

# Max input length in tokens. If the longest prefix (paragraphs 1..N + the
# question + choices) exceeds this, the run will fail on later paragraphs.
MAX_INPUT_TOKENS: int = 32_000

# --- Client behaviour -------------------------------------------------------

REQUEST_TIMEOUT_S: float = 30.0
MAX_RETRIES: int = 5
BACKOFF_BASE_S: float = 1.0  # exponential: BACKOFF_BASE_S * 2**attempt (+ jitter)
BACKOFF_MAX_S: float = 30.0

# --- Paths ------------------------------------------------------------------

PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]
DATA_DIR: Path = PROJECT_ROOT / "data"
RAW_DIR: Path = DATA_DIR / "raw"            # downloaded texts (gitignored)
PARSED_DIR: Path = DATA_DIR / "parsed"      # paragraphs JSON (tracked)
RESULTS_DIR: Path = DATA_DIR / "results"    # per-run JSONL (tracked)
CACHE_DIR: Path = PROJECT_ROOT / ".cache" / "jev"  # hashed responses (gitignored)

# Token counting is approximate: Jev's exact tokenizer is not published, so we
# use tiktoken's cl100k_base as a proxy for pre-run estimates. Real token
# counts always come back in the response's `usage.input_tokens`.
TOKENIZER_ENCODING: str = "cl100k_base"
