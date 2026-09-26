# SETUP

Everything needed to run the Whodunit Curve. Steps marked **[YOU — manual]** are
account/billing steps that can't be automated for you; do those yourself and the
rest of the pipeline follows.

---

## 0. Prerequisites

- Python 3.10+ (this repo pins 3.12 via `.python-version`).
- [uv](https://docs.astral.sh/uv/) for dependency management. Install:
  ```bash
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ```

## 1. Install dependencies

```bash
uv sync
```

This creates a `.venv/` and installs the pinned deps from `pyproject.toml`.
Run scripts either with `uv run python scripts/...` or after `source .venv/bin/activate`.

## 2. Get Jev access  **[YOU — manual]**

Jev is reached through **OpenRouter's Decisions API** (our default transport).

1. Create an account at <https://openrouter.ai>.
2. Jev may be behind early-access/a waitlist. Check the model page
   <https://openrouter.ai/typesafe/jev-1.13>. If it says "request access" or
   similar, join the waitlist and wait for approval before continuing.
   - Alternative transport: TypeSafe's own API (<https://api.typesafe.ai>), which
     your friend's Colab notebook already uses via a `TYPESAFE_API_KEY`. To use
     it instead, set `JEV_PROVIDER=typesafe` and `TYPESAFE_API_KEY=...` in `.env`.

## 3. Create an OpenRouter API key  **[YOU — manual]**

1. Go to <https://openrouter.ai/keys>.
2. Create a new key. Copy it now (you won't see it again).

## 4. Add credits and set a spending limit  **[YOU — manual]**

1. Add a small amount of credit (a few dollars is plenty — a full short-story run
   is typically well under a dollar; run the estimator in step 7 first).
2. **Set a spending limit** so a bug can't drain the balance:
   - Per-key limit: on the key's settings at <https://openrouter.ai/keys>.
   - Account limit: in <https://openrouter.ai/settings/credits>.

## 5. Fill in `.env`

```bash
cp .env.example .env
```

Open `.env` and paste your key:

```
OPENROUTER_API_KEY=sk-or-...
```

`.env` is gitignored — **never commit it, and never paste a key into code, notes,
or a commit message.**

## 6. Parse a story

```bash
uv run python -m detective_jev.story https://www.gutenberg.org/files/1661/1661-0.txt holmes
```

That downloads to `data/raw/holmes.txt` (gitignored) and writes
`data/parsed/holmes.json`. Open the JSON and sanity-check the paragraph split.

> Note: `1661-0.txt` is *The Adventures of Sherlock Holmes* (a whole collection).
> For a single short story you'll want to trim `data/parsed/holmes.json` to just
> the story's paragraphs, or point at a single-story text. Choosing the story and
> its boundaries is a content decision — coordinate with your friend.

## 7. Estimate cost BEFORE any real run

```bash
uv run python scripts/estimate_cost.py data/parsed/holmes.json
```

This prints estimated total input tokens and dollar cost (input tokens grow
~quadratically because we resend paragraphs `1..t` every step), and **warns if
the longest prefix exceeds Jev's 32,000-token input limit**. If it warns, the
per-paragraph context needs trimming/summarizing (friend's `build_context`).

## 8. Free dry run (no spend)

```bash
uv run python scripts/run_curve.py data/parsed/holmes.json --mock --limit 10
```

Confirms the whole pipeline works end-to-end using fake distributions.

## 9. Smoke test — ONE real call  **[needs step 5 done]**

```bash
uv run python scripts/smoke_test.py
```

Makes a single real Jev call with a toy locked-room scenario and prints the
probabilities, confidence, latency, input tokens, resolved model id, and cost.
If this prints sensible probabilities, your key and billing work.

## 10. The real run

```bash
uv run python scripts/run_curve.py data/parsed/holmes.json
```

Writes one JSON row per paragraph to `data/results/holmes.jsonl`. Responses are
cached on disk (`.cache/jev/`), so reruns are free and an interrupted run
resumes where it stopped.

---

## Where a human is required

| Step | Why it needs you |
|------|------------------|
| 2 | Jev access / waitlist approval |
| 3 | Creating the OpenRouter API key |
| 4 | Adding credits + setting a spending limit |
| 5 | Pasting the key into `.env` |
| 9 | Confirming you're OK to make the one real (paid) call |

Everything else is automated by the scripts above.
