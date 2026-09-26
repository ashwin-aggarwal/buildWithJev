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
  config.py        model id, endpoints, Jev limits, price, paths, ledger settings
  jev_client.py    query_batch() + query() wrapper: retries, SQLite cache, mock, shuffle
  tokens.py        approximate token counting (tiktoken cl100k_base proxy)
  story.py         download + parse Gutenberg text -> ordered paragraphs
  friend_stubs.py  build_choices / build_question / build_context   <-- ACTIVE WORK
  webapp.py        Flask: serves the page + /api/solve SSE live stream
  --- ledger pipeline (see "Ledger pipeline" below) ---
  ingest.py        HTML URL -> paragraphs + chapter headings -> data/books/
  chunking.py      ~500-word whole-paragraph chunks + sentence segmentation
  roster.py        character roster (one Anthropic call per book) + validation
  questions.py     loads config/*_questions.yaml, enforces scope, resolves options
  ledger.py        compression: one batched Jev call per chunk -> append-only ledger
  render.py        render_ledger(): pure ledger -> state text; compose_state()
  inference.py     per-step state (ledger 1..t-1 + chunk t) + one batched call
  storage.py       gzip JSON helpers, paths, manifest.parquet
  cli.py           python -m detective_jev.cli ingest|roster|validate|ledger|list|inspect
  scoring/         ONLY place that reads data/answers/ (answer key) + Parquet converter
config/            compression_questions.yaml (chunk_local), inference_questions.yaml (history_aware)
prompts/           extract_characters.txt (roster extraction prompt)
tests/             pytest suite (all mock; `uv run pytest`)
viz/index.html     live demo frontend (dark UI, SVG bar race + Whodunit Curve)
scripts/
  serve.py         launch the live demo website (http://127.0.0.1:8000)
  smoke_test.py    ONE real call to verify key + billing
  estimate_cost.py pre-run token + $ estimate; flags 32k prefix overflow
  run_curve.py     batch orchestrator -> results JSONL (resumable); --book = ledger mode
  results_to_parquet.py  book-mode JSONL -> long Parquet (derived, disposable)
data/
  raw/ (gitignored)  parsed/<name>.json   results/<name>.jsonl
  books/ ledgers/ rosters/ answers/ manifest.parquet   (ledger pipeline)
.cache/jev.sqlite  every Jev call, keyed by exact request (gitignored) — reruns are free
notes.md project_ideas skill.md TestingJev.ipynb   # original research/notes
```

## Conventions

- **Tooling:** `uv` + `pyproject.toml`, pinned deps. Run things with
  `uv run python scripts/...`. Package lives under `src/` (src layout).
- **Parsed story:** `data/parsed/<name>.json` = `[{"index": i, "text": "..."}]`.
- **Results row (JSONL):** `{t, answer, probabilities, confidence, latency_ms,
  input_tokens, model, cost, cached, timestamp}` — one per paragraph.
- **Cache:** SQLite at `.cache/jev.sqlite`, key = SHA-256 of (model, provider,
  state, questions, Choice options IN THE ORDER SENT). Interrupted runs resume;
  identical calls never re-bill. (The old per-file `.cache/jev/*.json` cache is
  no longer read.)
- **Model is pinned** (`typesafe/jev-1.13`) and the resolved dated build id is
  logged in every result's `model` field.

## Verified Jev schema (don't re-guess — confirmed against live docs 2026-09-25)

- **Transports:** OpenRouter Decisions (default) `POST https://openrouter.ai/api/alpha/decisions`
  with `Authorization: Bearer $OPENROUTER_API_KEY`; TypeSafe native
  `POST https://api.typesafe.ai/v1/systemone`. Switch via `JEV_PROVIDER` env.
  (The original `TestingJev.ipynb` uses a third path: the `langchain_typesafe`
  SDK with a `TYPESAFE_API_KEY`.)
- **Model id:** `typesafe/jev-1.13` (responses echo e.g. `typesafe/jev-1.13-20260917`).
- **Pricing:** $0.042 / 1M input tokens; output free.
- **Limits (docs.typesafe.ai/models):** "64k tokens per request; 32k tokens for
  `state` plus the longest question." So `MAX_INPUT_TOKENS=32000` bounds state +
  the single longest question and `MAX_REQUEST_TOKENS=64000` bounds state + ALL
  questions. No documented cap on the number of questions per request; ≤255
  options per Choice; 2–10 levels per Score. Rate limit 1,200 req/min.
  (These are TypeSafe-native numbers; OpenRouter publishes none of its own.)
- **Score is 0-indexed** in responses (`probabilities: {"0": p, ...}`); the client
  converts to 1-indexed.
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
# r -> {answer, confidence, probabilities, option_order, raw_response,
#       latency_ms, input_tokens, model, cost, cached, mock}

from detective_jev.jev_client import query_batch
r = query_batch(state, [
    {"id": "who", "type": "choice", "instructions": "...", "criteria": {"a": None, "b": "desc"}},
    {"id": "sev", "type": "score",  "instructions": "...", "criteria": ["low", "mid", "high"]},
    {"id": "yes", "type": "noul",   "instructions": "...", "criteria": None},
], mock=False, use_cache=True)
# r["answers"][qid] -> {type, value, prob, distribution, confidence, option_order[, expected]}
#   choice: value = winner, distribution = {option: p}
#   score:  value = most probable level 1..N, expected = weighted level, distribution {"1": p, ...}
#   noul:   value = p >= 0.5, prob = p, distribution {"true": p, "false": 1-p}
# r also has option_order {qid: [...]}, latency_ms, input_tokens, model, cost, cached, mock
```

`query()` is a thin wrapper over `query_batch()` (one code path; retries live in
`_post()`). Both **shuffle Choice option order per call** with a seed from
(state, question id, option set): randomised across calls, reproducible for the
same call, and returned as `option_order`. Score levels are never shuffled.
Both refuse (`JevBudgetError`) instead of truncating when a request would exceed
the limits above.

## The stubs you implement (`friend_stubs.py`)

- `build_choices(paragraphs) -> {suspect: desc|None}` — the fixed suspect set
  (stable across all `t` so distributions are comparable). Paragraph mode still
  returns placeholder suspects, **plus the culprit question's `extra_options`
  (`none_of_these`) from config.** Book mode builds options from the roster.
- `build_question() -> str` — the Choice question text. **Now reads the `culprit`
  question's `text` from `config/inference_questions.yaml`.**
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
uv run pytest                                                     # test suite (mock only)
```

See `SETUP.md` for account/key/billing steps (waitlist, OpenRouter key, credits,
spending limit).

## Ledger pipeline (added 2026-09-25 by the friend's Claude session)

Why: Jev's 32K limit means growing prefixes of a whole novel can't fit. Each
~500-word chunk is compressed ONCE into a structured **ledger entry** built only
from Jev's typed answers plus one verbatim sentence Jev selects. No LLM writes
prose anywhere in the reading loop.

### Two Jev call sites (the core invariant)

1. **Compression** (`ledger.py`): state = the raw text of ONE chunk, nothing
   else. Exactly one batched `query_batch()` call per chunk with every question
   in `config/compression_questions.yaml` (`scope: chunk_local`). A pure function
   of (chunk text, its sentences, roster, questions). One entry per chunk index;
   appending an existing index raises `DuplicateEntryError`; a persisted
   per-book counter raises `CompressionCallBudgetError` if calls would exceed
   the chunk count.
2. **Inference** (`inference.py`): state at step t = `CASE NOTES` (rendered
   entries **1..t-1**) + `CURRENT PASSAGE` (chunk t raw). Chunk t is never in the
   notes at step t. One batched call per step with every question in
   `config/inference_questions.yaml` (`scope: history_aware`). Answers go to
   **results rows only**, never into ledger entries.

Each loader rejects the other scope, and `options: candidates` is rejected in
compression (the ledger must not depend on the run condition). Current split:
- compression: `concerns`, `secondary` (roster + none/multiple), `event_type`,
  `new_evidence`, `time_reference`, `location_stated` (Noul), per-character
  `suspicion` Score 1–5 over the FULL roster (level 1 = "absent, or nothing
  suspicious"), `key_sentence` (Choice over the chunk's numbered sentences;
  code copies the sentence verbatim into `key_quote`).
- inference: `culprit` (Choice over candidates + `none_of_these`; required),
  `contradicts_prior` (Noul), `alibi_effect` (moved from compression).
- Known, deliberately kept caveats are commented in the YAML (`event_type`'s
  `alibi_broken`/`misdirection` options and `new_evidence` arguably need
  history). Moving further questions is the friend's decision.

### Entry shape (`data/ledgers/{book_id}[.mock].json.gz`)

```
{"chunk", "chapter", "word_span",                      # positional
 "answers": {qid: {type, value, prob, distribution, confidence[, expected]}},
 "suspicion": {canonical: {value 1-5, expected, prob, distribution}},
 "mentioned": {canonical: bool},                       # alias match in code
 "key_quote", "key_sentence_idx" (0-based), "option_order": {qid: [...]},
 "text_sha256"}
```
The ledger file also records `roster_hash` (names + aliases only),
`questions_hash`, `book_text_sha256`, `pipeline_version`, `compression_calls`,
`rollups`. A mismatch refuses to append until `cli ledger <id> --force`.
Editing `is_suspect` never invalidates a ledger. **Mock ledgers are separate
files** (`<id>.mock.json.gz`) so a dry run can never stand in for a real one.

### Rendering and overflow

`render.render_ledger(entries, roster, rollups=, suspects=, posthoc=)` is pure
and lives alone so the format can change without touching extraction. Only
suspicion values ≥ 2 and true Nouls are printed, to stay compact. Before each
inference call, `ledger.ensure_rollups` counts the tokens of the full-cast state.
Over `LEDGER_TOKEN_BUDGET` (26K), it rolls up the oldest chapter's entries into
per-chapter event counts plus the key quotes where `new_evidence` was true. Each rollup
stores `triggered_at_t` and is shown only from that step on (no lookahead),
covers each entry at most once, and is logged. Arithmetic: at ~70–115
tokens/entry a 90K-word book (180 chunks) renders to ~12–21K tokens, so rollups
never fire. They start at about 114K–194K words (*The Moonstone*, *The Woman in
White*). Rollups use only `new_evidence`, not `contradicts_prior`, which is now
run-specific.

### Candidate sets, run ids, post-hoc

- `candidate_set` is a run condition: `full_cast` | `suspects_only` (roster
  `is_suspect: true`). The ledger is built once for the full cast;
  `suspects_only` narrows the culprit options and filters the rendered
  suspicion lines. That is exact, because Jev evaluates each Score independently.
- `run_id` = deterministic hash of (book, model, provider, ledger entries,
  inference questions, candidate_set, condition, mock, posthoc). Book-mode
  results go to `data/results/{book_id}__{run_id}.jsonl` so conditions never
  collide on resume.
- `--posthoc` (default off, `POSTHOC_CONTRADICTIONS=1`) writes each step's
  `contradicts_prior` into a **per-run sidecar**
  `data/ledgers/{book_id}.posthoc.{run_id}.json.gz`. It is rendered in later
  steps of that run only. The shared ledger is never modified.

### Results rows (book mode) — existing fields unchanged, fields ADDED

`{t, answer, probabilities, confidence, latency_ms, input_tokens, model, cost,
cached, timestamp}` (from the `culprit` answer) **+** `book_id, run_id, condition,
candidate_set, inference_answers {qid: {...full distribution...}}, option_order
{qid: [...]}`. Paragraph-mode rows are unchanged. `scripts/results_to_parquet.py`
derives `data/results/parquet/{run_id}.parquet` (gitignored): one row per
(t, candidate) with prob, is_culprit, introduced_by_t, option_position,
q_<id>_value/prob. The file is regenerated whole each time and never edited in place.

### Roster and answer key (kept separate on purpose)

- `data/rosters/{id}.yaml`: hand-editable and **authoritative once it exists**;
  after that the Anthropic API is never called for that book. It is extracted
  once per book by `EXTRACTION_MODEL` (default `claude-sonnet-5`, key from
  `ANTHROPIC_API_KEY` via `config.anthropic_api_key()`, never logged), using the
  prompt in `prompts/extract_characters.txt`. The response is cached by
  normalised-text hash. Books too long for one call are refused (hand-write the
  roster). `first_mention_chunk` is derived in code. Validation warnings cover
  duplicate aliases, over-merges, and missed names.
- `data/answers/{id}.yaml`: `is_culprit` only. **Only `detective_jev.scoring`
  may read it.** `tests/test_answer_isolation.py` statically scans every
  state-building module (incl. `webapp.py`, `friend_stubs.py`, `run_curve.py`)
  and runs the whole mock pipeline with reads under `data/answers/` trapped.

### What changed in existing files (minimal, shape-compatible)

- `jev_client.py`: `query_batch()` added; `query()` rewritten as a wrapper
  (same return keys + `option_order`); retries moved into `_post()`; SQLite cache
  replaces the per-file cache; Choice order shuffling; token guard; mock covers
  choice/score/noul.
- `scripts/run_curve.py`: `parsed_path` is now optional; new `--book`,
  `--candidate-set`, `--condition`, `--posthoc` book mode (`run_book()`). The
  paragraph path is unchanged.
- `friend_stubs.py`: `build_question()` / `build_choices()` read the culprit
  question from config (see above). `build_context()` is unchanged.
- `config.py`: limits clarified (`MAX_REQUEST_TOKENS` added), new paths,
  `PIPELINE_VERSION`, `CHUNK_SIZE_TARGET`, `LEDGER_TOKEN_BUDGET`,
  `WRITE_UNCOMPRESSED`, `POSTHOC_CONTRADICTIONS`, extraction settings.
- `pyproject.toml`: pinned `beautifulsoup4`, `pyyaml`, `pandas`, `pyarrow`,
  `anthropic`; `pytest` in the dev group. `.env.example`: `ANTHROPIC_API_KEY`.
- `webapp.py`, `viz/index.html`, `estimate_cost.py`, `smoke_test.py`: **not
  modified**. The webapp still runs paragraph mode; whether the frontend moves
  to chunk/ledger steps is the frontend owner's call.

### Ledger workflow

```bash
uv run python -m detective_jev.cli ingest <gutenberg_html_url>   # -> pg<N>
uv run python -m detective_jev.cli roster pg<N>      # ONE paid Anthropic call unless the YAML exists
#   hand-edit data/rosters/pg<N>.yaml: fix aliases, set is_suspect
uv run python -m detective_jev.cli ledger pg<N>      # MOCK; add --real for paid Jev calls
uv run python scripts/run_curve.py --book pg<N> --mock --candidate-set full_cast --condition v1
uv run python scripts/results_to_parquet.py data/results/pg<N>__*.jsonl
```
Nothing in the ledger pipeline has made a real Jev or Anthropic call yet. All
of it is tested in mock mode only (`uv run pytest`, 66 tests).
