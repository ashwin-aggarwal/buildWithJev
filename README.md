# Predicting the Murder Mystery

Feed a murder mystery to [Jev](https://openrouter.ai/typesafe/jev-1.13) one
paragraph at a time and watch how its belief about *who the killer is* shifts
over the course of the story.

Jev is TypeSafe's "System One" decision model: you send it a `state` plus a typed
question (here, a **Choice** over suspects) and it returns a typed answer, a
confidence, and a **probability for every option** — fast (~70–500 ms) and cheap
($0.042 / 1M input tokens, output free).

## Pipeline

1. **Parse** a public-domain story (start with a Project Gutenberg Sherlock
   Holmes short story) into ordered paragraphs → `data/parsed/<name>.json`.
2. **Query** Jev at each paragraph `t`, sending paragraphs `1..t`, the question,
   and the suspect choices.
3. **Store** the full probability distribution at every `t` →
   `data/results/<name>.jsonl`.
4. **Visualize** later as an animated histogram / line chart. *(separate step)*

## Two ways to run it

- **Live demo website** (`scripts/serve.py`) — paste a Gutenberg link and watch
  Detective JEV read the story and stream its guesses in real time.
- **Batch run** (`scripts/run_curve.py`) — process a parsed story to a results
  JSONL for offline analysis.

Both share the same Jev client, cache, and stubs.

## Layout

```
src/detective_jev/
  config.py        model id, endpoints, 32k limit, pricing, paths
  jev_client.py    query(): retries, timeout, on-disk cache, mock mode
  tokens.py        approximate token counting (tiktoken proxy)
  story.py         download + parse Gutenberg text -> paragraphs JSON
  friend_stubs.py  build_choices / build_question / build_context  <-- FRIEND owns
  webapp.py        Flask server: serves the page + SSE live-solve endpoint
viz/
  index.html       the live demo frontend (dark UI, SVG bar race + Whodunit Curve)
scripts/
  serve.py         launch the live demo website
  smoke_test.py    ONE real call to verify key + billing
  estimate_cost.py pre-run token + $ estimate; flags 32k overflow
  run_curve.py     batch orchestrator: story + stubs + client -> results JSONL
data/
  raw/ (gitignored)  parsed/  results/
```

## The live demo

```bash
uv run python scripts/serve.py      # then open http://127.0.0.1:8000
```

Paste a Gutenberg **Plain Text UTF-8** link (a few murder-mystery presets are in
the box), optionally type suspects (otherwise `build_choices()` supplies them),
pick a paragraph cap, and press **Solve**. The page shows:

- a **"JEV is reading paragraph t…"** live indicator as each query streams back,
- a **racing bar chart** of current suspicion,
- the **Whodunit Curve** (each suspect's probability over the story) with a
  scrubber to trace what JEV believed at any point,
- running **cost** and the resolved model id.

**Mock mode is on by default (free).** Untick it to make real calls once your key
is set — the API key stays server-side and is never sent to the browser. A hard
cap of 200 paragraphs and the 32k-token prefix guard protect against runaway cost.

## Quick start

See [SETUP.md](SETUP.md) for accounts, keys, credits, and spending limits.
Once set up:

```bash
uv sync
# 1. parse a story
python -m detective_jev.story https://www.gutenberg.org/files/1661/1661-0.txt holmes
# 2. estimate cost BEFORE spending anything
python scripts/estimate_cost.py data/parsed/holmes.json
# 3. free dry run to check the pipeline end-to-end
python scripts/run_curve.py data/parsed/holmes.json --mock --limit 10
# 4. one real call to confirm billing
python scripts/smoke_test.py
# 5a. the live demo website (mock by default)
python scripts/serve.py            # open http://127.0.0.1:8000
# 5b. or a batch run to JSONL
python scripts/run_curve.py data/parsed/holmes.json
```

> Ownership: the Jev client, environment, billing/keys, and repo structure are
> maintained separately from the **decision design** (suspect choices, question
> wording, and per-paragraph context), which lives entirely in
> [`friend_stubs.py`](src/detective_jev/friend_stubs.py).
