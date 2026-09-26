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

# Jev 1.13 limits (docs.typesafe.ai/models, checked 2026-09-25): "64k tokens per
# request; 32k tokens for `state` plus the longest question". So
# MAX_INPUT_TOKENS bounds state + the single longest question, and
# MAX_REQUEST_TOKENS bounds state + ALL questions in a batched call.
MAX_INPUT_TOKENS: int = 32_000
MAX_REQUEST_TOKENS: int = 64_000
MAX_CHOICE_OPTIONS: int = 255
MAX_SCORE_LEVELS: int = 10

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
CACHE_DIR: Path = PROJECT_ROOT / ".cache" / "jev"  # legacy per-file cache dir (gitignored)
JEV_CACHE_DB: Path = PROJECT_ROOT / ".cache" / "jev.sqlite"  # every Jev call (gitignored)
EXTRACT_CACHE_DIR: Path = PROJECT_ROOT / ".cache" / "extract"  # Anthropic roster calls

# Ledger pipeline (ingestion -> chunks -> roster -> ledger). See CLAUDE.md.
HTML_CACHE_DIR: Path = RAW_DIR / "html"     # raw HTML keyed by URL hash (gitignored)
UPLOADS_DIR: Path = RAW_DIR / "uploads"     # uploaded/local book files by content hash (gitignored)
BOOKS_DIR: Path = DATA_DIR / "books"        # {book_id}.json.gz
LEDGERS_DIR: Path = DATA_DIR / "ledgers"    # {book_id}.json.gz + per-run post-hoc sidecars
ROSTERS_DIR: Path = DATA_DIR / "rosters"    # {book_id}.yaml, hand-editable, authoritative
# Answer key. ONLY detective_jev.scoring may read this; nothing that builds Jev
# state may touch it (enforced by tests/test_answer_isolation.py).
ANSWERS_DIR: Path = DATA_DIR / "answers"
MANIFEST_PATH: Path = DATA_DIR / "manifest.parquet"
PARQUET_DIR: Path = RESULTS_DIR / "parquet"  # derived, disposable (gitignored)
QUESTIONS_DIR: Path = PROJECT_ROOT / "config"
COMPRESSION_QUESTIONS_PATH: Path = QUESTIONS_DIR / "compression_questions.yaml"
INFERENCE_QUESTIONS_PATH: Path = QUESTIONS_DIR / "inference_questions.yaml"
MODEL_PRICES_PATH: Path = QUESTIONS_DIR / "model_prices.yaml"  # Cost Analysis tab
PROMPTS_DIR: Path = PROJECT_ROOT / "prompts"
EXTRACT_PROMPT_PATH: Path = PROMPTS_DIR / "extract_characters.txt"

# Bump whenever parsing/chunking/segmentation changes; stamped into every artifact.
PIPELINE_VERSION: str = "2"  # 2: drop page numbers/footnote markers, fix drop caps

# Words per chunk. Chunks accumulate whole paragraphs until they reach this.
CHUNK_SIZE_TARGET: int = int(os.getenv("CHUNK_SIZE_TARGET", "500"))

# Rendered inference state (ledger 1..t-1 + chunk t) above this many tokens
# triggers a rollup of the oldest entries. Leaves headroom under
# MAX_INPUT_TOKENS for the longest question.
LEDGER_TOKEN_BUDGET: int = int(os.getenv("LEDGER_TOKEN_BUDGET", "26000"))

# How many of the most recent chunks Jev sees as FULL TEXT at each inference
# step (including the current one). 1 = the original design (notes for
# 1..t-1, only chunk t in full). 3 = notes for 1..t-3, then chunks t-2..t in
# full, so a reveal stays readable for a few steps after it happens.
RAW_WINDOW: int = max(1, int(os.getenv("RAW_WINDOW", "3")))

# Write book/ledger JSON uncompressed (for debugging). Readers handle both.
WRITE_UNCOMPRESSED: bool = os.getenv("WRITE_UNCOMPRESSED", "0") == "1"

# Optional: after each inference step, write contradicts_prior for chunk t into
# a per-run sidecar so later steps' rendered state carries it. Off by default.
POSTHOC_CONTRADICTIONS: bool = os.getenv("POSTHOC_CONTRADICTIONS", "0") == "1"

# Roster validation: a capitalised token seen at least this often that matches
# no alias is flagged as a possibly missed character.
MISSED_NAME_MIN_COUNT: int = 8

# --- Character extraction (once per book) -----------------------------------

# Which backend runs the one-per-book roster extraction call:
#   "anthropic"  -> the Anthropic SDK (needs ANTHROPIC_API_KEY), EXTRACTION_MODEL
#   "openrouter" -> OpenRouter chat completions (reuses OPENROUTER_API_KEY),
#                   OPENROUTER_EXTRACTION_MODEL. Use this when you have no
#                   Anthropic key but do have an OpenRouter key.
EXTRACTION_PROVIDER: str = os.getenv("EXTRACTION_PROVIDER", "anthropic")

ANTHROPIC_KEY_ENV: str = "ANTHROPIC_API_KEY"
EXTRACTION_MODEL: str = os.getenv("EXTRACTION_MODEL", "claude-sonnet-5")
EXTRACTION_CONTEXT_TOKENS: int = 1_000_000  # claude-sonnet-5 context window
EXTRACTION_MAX_OUTPUT_TOKENS: int = 32_000

# OpenRouter extraction path. A large-context model is required because the
# whole novel goes in one request (a ~70k-word book is ~90k+ tokens).
OPENROUTER_CHAT_URL: str = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_EXTRACTION_MODEL: str = os.getenv(
    "OPENROUTER_EXTRACTION_MODEL", "deepseek/deepseek-v4-flash"
)
OPENROUTER_EXTRACTION_CONTEXT_TOKENS: int = int(
    os.getenv("OPENROUTER_EXTRACTION_CONTEXT_TOKENS", "1000000")
)
OPENROUTER_EXTRACTION_MAX_OUTPUT_TOKENS: int = 32_000
# Reasoning ("thinking") effort for the extraction model. On OpenRouter, hidden
# reasoning counts against max_tokens; with reasoning on, DeepSeek V4 Flash can
# spend the whole budget thinking and return an EMPTY answer. Listing
# characters needs no deep reasoning, so it is off by default.
# One of: none, minimal, low, medium, high.
OPENROUTER_EXTRACTION_REASONING: str = os.getenv("OPENROUTER_EXTRACTION_REASONING", "none")


def anthropic_api_key() -> str | None:
    """The Anthropic key from the environment. Never log or print the value."""
    return os.getenv(ANTHROPIC_KEY_ENV) or None

# Token counting is approximate: Jev's exact tokenizer is not published, so we
# use tiktoken's cl100k_base as a proxy for pre-run estimates. Real token
# counts always come back in the response's `usage.input_tokens`.
TOKENIZER_ENCODING: str = "cl100k_base"
