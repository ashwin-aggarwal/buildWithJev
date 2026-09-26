# CLAUDE.md — Detective JEV / The Whodunit Curve

Context for any Claude agent working in this repo. Read this first.

## What this project is

Feed a public-domain murder mystery to **Jev** one paragraph at a time and track
how its belief about *who the killer is* changes across the story. For each
paragraph `t`, we send Jev paragraphs `1..t` (the "state"), the question
"Who is the killer?", and a list of suspect **Choices**, and record the full
probability distribution it returns.

Two runners share one client:
- **Live demo site** — `scripts/serve.py` → Flask + SSE → `viz/index.html`.
- **Batch run** — `scripts/run_curve.py` → `data/results/<name>.jsonl`.

## Ownership (read before editing)

The **environment, Jev client, billing/keys, repo structure, and the demo
frontend** are already built and tested — treat them as stable ground you can
build on. The current active workstream is **data parsing, API querying, and
context-compression logic** (owned by the friend taking over now). That work
plugs into the three stubs in `src/detective_jev/friend_stubs.py` and,
increasingly, into `story.py` and the client.

All files are now fair game to modify, including the shared core
(`jev_client.py`, `config.py`, `story.py`) — the whole pipeline is his to shape.
The one obligation is **keep public shapes consistent**: if you change
`query(...)`'s return dict, the results JSONL row schema, or the `friend_stubs`
signatures, grep for every caller (`webapp.py`, `run_curve.py`, `viz/index.html`,
`estimate_cost.py`) and update them in the same change.

## Hard rules

- **Never print, commit, or hard-code API keys.** Keys live only in `.env`
  (gitignored). The web server keeps the key server-side; it is never sent to the
  browser.
- **Do not make real (paid) Jev calls** except the single `scripts/smoke_test.py`
  call, and only after the user confirms their key is set. Everything else runs in
  **mock mode** by default (`mock=True` / the UI checkbox). Real calls are
  quadratic in tokens — always run `scripts/estimate_cost.py` first.
- **Don't commit** unless the user asks.
- Use the **TypeSafe skill** when working with Jev (see `skill.md` for install).
- `config.py` is the single source of truth for the model id, endpoints, the
  32k input limit, pricing, and paths. Change tunables there, not inline.

## Repo map

```
src/detective_jev/
  config.py        model id, endpoints, MAX_INPUT_TOKENS=32000, price, paths
  jev_client.py    query(): retries+backoff, timeout, on-disk cache, mock mode
  tokens.py        approximate token counting (tiktoken cl100k_base proxy)
  story.py         download + parse Gutenberg text -> ordered paragraphs
  friend_stubs.py  build_choices / build_question / build_context   <-- ACTIVE WORK
  webapp.py        Flask: serves the page + /api/solve SSE live stream
viz/index.html     live demo frontend (dark UI, SVG bar race + Whodunit Curve)
scripts/
  serve.py         launch the live demo website (http://127.0.0.1:8000)
  smoke_test.py    ONE real call to verify key + billing
  estimate_cost.py pre-run token + $ estimate; flags 32k prefix overflow
  run_curve.py     batch orchestrator -> results JSONL (resumable via cache)
data/
  raw/ (gitignored)  parsed/<name>.json   results/<name>.jsonl
.cache/jev/        hashed response cache (gitignored) — reruns are free
notes.md project_ideas skill.md TestingJev.ipynb   # original research/notes
```

## Conventions

- **Tooling:** `uv` + `pyproject.toml`, pinned deps. Run things with
  `uv run python scripts/...`. Package lives under `src/` (src layout).
- **Parsed story:** `data/parsed/<name>.json` = `[{"index": i, "text": "..."}]`.
- **Results row (JSONL):** `{t, answer, probabilities, confidence, latency_ms,
  input_tokens, model, cost, cached, timestamp}` — one per paragraph.
- **Cache key:** SHA-256 of (model id, context, question, choices). Interrupted
  runs resume; identical calls never re-bill.
- **Model is pinned** (`typesafe/jev-1.13`) and the resolved dated build id is
  logged in every result's `model` field.

## Verified Jev schema (don't re-guess — confirmed against live docs 2026-09-25)

- **Transports:** OpenRouter Decisions (default) `POST https://openrouter.ai/api/alpha/decisions`
  with `Authorization: Bearer $OPENROUTER_API_KEY`; TypeSafe native
  `POST https://api.typesafe.ai/v1/systemone`. Switch via `JEV_PROVIDER` env.
  (The original `TestingJev.ipynb` uses a third path: the `langchain_typesafe`
  SDK with a `TYPESAFE_API_KEY`.)
- **Model id:** `typesafe/jev-1.13` (responses echo e.g. `typesafe/jev-1.13-20260917`).
- **Pricing:** $0.042 / 1M input tokens; output free. **Context limit: 32,000 tokens.**
- **Request:**
  ```json
  {"model": "...", "state": "<context>",
   "questions": {"killer": {"type": "choice", "instructions": "<question>",
                            "criteria": {"<option>": "<desc or null>"}}}}
  ```
- **Choice response:** `answers.killer = {"choice": "<winner>",
  "probabilities": {opt: p}, "confidence": 0..1}`; top-level
  `usage = {input_tokens, output_tokens, cost}`. **The full distribution is in
  `probabilities`.** Noul returns `noul` (0..1); Score returns `score` +
  `probabilities` + `legend` + `confidence`.

## The client interface you build on

```python
from detective_jev import query
r = query(context_text, question, choices, mock=False, use_cache=True)
# choices: list[str] OR {option: description|None}
# r -> {answer, confidence, probabilities, raw_response, latency_ms,
#       input_tokens, model, cost, cached, mock}
```

## The stubs you implement (`friend_stubs.py`)

- `build_choices(paragraphs) -> {suspect: desc|None}` — the fixed suspect set
  (stable across all `t` so distributions are comparable).
- `build_question() -> str` — the Choice question text.
- `build_context(paragraphs, t) -> str` — the `state` for step `t`. **This is
  where compression lives:** once the `1..t` prefix nears the 32k limit,
  trim/summarize earlier paragraphs here. `estimate_cost.py` flags the first `t`
  that would overflow.

## How to run

```bash
uv sync
uv run python -m detective_jev.story <gutenberg_txt_url> <name>   # parse
uv run python scripts/estimate_cost.py data/parsed/<name>.json    # cost first
uv run python scripts/serve.py                                    # live demo (mock default)
uv run python scripts/run_curve.py data/parsed/<name>.json --mock # batch dry run
uv run python scripts/smoke_test.py                              # ONE real call (needs key)
```

See `SETUP.md` for account/key/billing steps (waitlist, OpenRouter key, credits,
spending limit).
