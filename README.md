# The Whodunit Curve

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

## Layout

```
src/detective_jev/
  config.py        model id, endpoints, 32k limit, pricing, paths
  jev_client.py    query(): retries, timeout, on-disk cache, mock mode
  tokens.py        approximate token counting (tiktoken proxy)
  story.py         download + parse Gutenberg text -> paragraphs JSON
  friend_stubs.py  build_choices / build_question / build_context  <-- FRIEND owns
scripts/
  smoke_test.py    ONE real call to verify key + billing
  estimate_cost.py pre-run token + $ estimate; flags 32k overflow
  run_curve.py     orchestrator: story + stubs + client -> results JSONL
data/
  raw/ (gitignored)  parsed/  results/
```

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
# 5. the real run
python scripts/run_curve.py data/parsed/holmes.json
```

> Ownership: the Jev client, environment, billing/keys, and repo structure are
> maintained separately from the **decision design** (suspect choices, question
> wording, and per-paragraph context), which lives entirely in
> [`friend_stubs.py`](src/detective_jev/friend_stubs.py).
