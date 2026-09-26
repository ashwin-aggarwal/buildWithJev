# SETUP

This guide takes you from a fresh clone to watching Jev read a detective novel
and guess the killer. Part A is one-time setup. Part B runs the book that is
already included. Part C adds a new book.

Steps marked **[YOU]** need a person (accounts, payment). Everything else is a
command you copy and paste. All commands are run from the repo root.

---

## Before you start: two ways to feed Jev a story

| | **Book mode** (use this) | Paragraph mode (old) |
|---|---|---|
| What Jev sees at step *t* | A compact **ledger** of passages 1..t-1, plus passage *t* in full | The full text of paragraphs 1..t, every time |
| Works for | Whole novels (tested: 70,000 words) | Short stories only (roughly 25,000 words max) |
| Cost for a 70k-word novel | about **$0.08** total | Fails partway |
| Commands | `detective_jev.cli` + `run_curve.py --book` | `detective_jev.story` + `run_curve.py <file>` |

**Why paragraph mode fails on long texts.** It resends *everything so far* at
every step, so the input keeps growing. Jev accepts at most 32,000 tokens.
*The Adventures of Sherlock Holmes* splits into 1,973 paragraphs and passes that
limit around paragraph 470, after spending about $0.30. Book mode stays small
because each passage is compressed once into a short ledger entry. The largest
input for the included 70k-word novel is about 14,000 tokens.

---

## Part A — One-time setup

### A1. Install Python tools

You need Python 3.10+ (the repo pins 3.12) and [uv](https://docs.astral.sh/uv/):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh   # installs uv (skip if you have it)
uv sync                                            # installs this project's dependencies
```

Run every command below with `uv run ...`. It uses the project's environment
automatically.

### A2. Get an OpenRouter account with Jev access  **[YOU]**

1. Sign up at <https://openrouter.ai>.
2. Open the Jev model page, <https://openrouter.ai/typesafe/jev-1.13>. If it
   asks you to request access, do so and wait for approval.

### A3. Create an API key, add credit, set a limit  **[YOU]**

1. Create a key at <https://openrouter.ai/keys> and copy it now. You won't see
   it again.
2. Add a few dollars of credit. A whole novel costs well under $1.
3. Set a **spending limit** so a bug can't drain your balance. You can set it on
   the key itself (<https://openrouter.ai/keys>) or for the whole account
   (<https://openrouter.ai/settings/credits>).

### A4. Put the key in `.env`

```bash
cp .env.example .env
```

Open `.env` and fill in:

```bash
OPENROUTER_API_KEY=sk-or-...        # required
EXTRACTION_PROVIDER=openrouter      # lets Part C's character list use the same key
```

The second line matters only when you add new books (Part C). Without it, the
character-list step expects an Anthropic key (`ANTHROPIC_API_KEY`) instead.

`.env` is gitignored. **Never commit it, and never paste a key into code,
notes, or a commit message.**

### A5. Check that everything works

```bash
uv run python scripts/smoke_test.py --mock   # free: checks the code runs
uv run python scripts/smoke_test.py          # ONE real Jev call, a tiny fraction of a cent
uv run pytest -q                             # optional: the test suite (free, no network)
```

If the real smoke test prints probabilities for three suspects, your key and
billing work.

---

## Part B — Run the included book

The repo already has *The Murder of Roger Ackroyd* (Agatha Christie, book id
`pg69087`) ingested, with its character list and ledger built. Only the final
"guess the killer" pass is left to run.

### B1. Free dry run

```bash
uv run python scripts/run_curve.py --book pg69087 --mock --limit 5
```

This uses fake random probabilities to check the plumbing. It spends nothing.
The mock curve means nothing, so don't read into it.

### B2. The real run (about $0.04, a minute or two)

```bash
uv run python scripts/run_curve.py --book pg69087
```

- Makes 133 Jev calls, one per passage.
- Writes one line per passage to `data/results/pg69087__<run_id>.jsonl`.
- If it stops halfway, run the same command again. It resumes where it
  stopped, and repeated calls are served free from the cache
  (`.cache/jev.sqlite`).

Options:
- `--candidate-set suspects_only` limits the options to characters marked
  `is_suspect: true` in `data/rosters/pg69087.yaml`. Nobody is marked yet, so
  set at least two first.
- `--condition <label>` is a free-text tag for comparing runs.

### B3. Watch it in the browser

```bash
uv run python scripts/serve.py      # then open http://127.0.0.1:8000
```

The page replays any saved run from B2. It makes no API calls and costs
nothing.

- **Run tab.** Suspect bars on the left, the probability curve in the middle,
  and the text Jev is reading on the right. Click or drag anywhere on the
  curve to jump to that chunk. Press **Space** to play or pause, and **← →**
  to step (hold Shift to move 10 at a time). Hover a line to highlight that
  character everywhere, and click legend entries to hide lines.
- **Cost analysis tab.** What the run cost with Jev, and what the same calls
  would cost on OpenAI, Anthropic, and DeepSeek models. This is list-price
  arithmetic only, and prices are in `config/model_prices.yaml`.

The old live-solve page (runs Jev as you watch; untick "Mock mode" for real
results) is still at <http://127.0.0.1:8000/live>. The key stays on the server
and is never sent to the browser.

---

## Part C — Add a new book

Budget about **$0.10 per 70,000 words**, plus a few cents for the character
list. Each step can be re-run safely: finished work is kept and not
re-bought.

### C1. Find the book's HTML page

On Project Gutenberg, open the book and copy the link to **"Read online
(web)"**. It looks like
`https://www.gutenberg.org/ebooks/69087.html.images` or
`https://www.gutenberg.org/files/69087/69087-h/69087-h.htm`.
**Plain-text (.txt) links do not work here.**

Other websites work too if the whole book is on one page. See
[Which websites work?](#which-websites-work) below.

### C2. Ingest: download and split into passages (free)

```bash
uv run python -m detective_jev.cli ingest <html-url>
```

This prints a **book id**, such as `pg1661`. Use it in the next steps. The
book is split into ~500-word passages that never cut a paragraph in half. To
check what was kept, run
`uv run python -m detective_jev.cli inspect <book_id> --chunk 1`.

### C3. Build the character list (one LLM call, a few cents)

```bash
uv run python -m detective_jev.cli roster <book_id>
```

This sends the whole book to a language model once and writes
`data/rosters/<book_id>.yaml`. **Open that file and check it**:
- Fix any wrong nicknames (`aliases`).
- Set `is_suspect: true` for the characters you'd call suspects.
- Read the warnings the command printed. They flag possible missed or
  merged characters.

Once this file exists, it is the source of truth. The model is never asked
again for this book. You can re-check your edits with
`uv run python -m detective_jev.cli validate <book_id>`.

### C4. Build the ledger (about $0.035 per 70k words)

```bash
uv run python -m detective_jev.cli ledger <book_id>          # free mock version first
uv run python -m detective_jev.cli ledger <book_id> --real   # the real one: one Jev call per passage
```

Each passage is compressed once, into Jev's answers plus one sentence it
picks. If you later edit the character names or aliases, the command refuses
to mix old and new entries. Rebuild with `--real --force` when that happens.

### C5. Run it

Same as Part B, with your book id:

```bash
uv run python scripts/run_curve.py --book <book_id>
```

---

## Which websites work?

**Any HTML page where the book's text is in normal paragraph tags (`<p>`) on a
single page.** Project Gutenberg is the tested case. Other sites often work
too, with these limits:

- **Gutenberg extras are removed automatically.** This covers the license
  header and footer, transcriber's notes, and the table of contents. On
  other sites, stray menus, captions, or comments inside `<p>` tags will end
  up in the text. Check with `cli inspect`.
- **Chapter headings** are read from `<h1>`–`<h6>` tags. Sites that style
  headings some other way still work, but chapters come out unnamed.
- **One page only.** A site that puts each chapter on its own page is only
  partly ingested: you get the one page you linked.
- **No JavaScript-rendered sites.** The page is downloaded as-is, without
  running scripts.
- **Text with no `<p>` tags** (only `<br>` line breaks, or plain `.txt`)
  finds no paragraphs and stops with "No body paragraphs found".
- **Book ids.** Gutenberg books get `pg<number>`. Anything else gets a name
  made from the title plus a short code based on the text. The same book
  from two websites gets the same id.

Downloaded pages are cached in `data/raw/html/`, so each URL is fetched only
once.

---

## Paragraph mode (short stories only)

Use this only for a single short story. For anything longer, use book mode.

```bash
uv run python -m detective_jev.story <gutenberg-plain-text-url> <name>   # parse
uv run python scripts/estimate_cost.py data/parsed/<name>.json           # ALWAYS check first
uv run python scripts/run_curve.py data/parsed/<name>.json --mock --limit 10
uv run python scripts/run_curve.py data/parsed/<name>.json               # real run
```

If `estimate_cost.py` prints a WARNING about the 32,000-token limit, the text
is too long for paragraph mode. `run_curve.py` now also refuses to start in
that case, instead of failing partway after spending money.

---

## Troubleshooting

| Message | What it means / what to do |
|---|---|
| `Paragraph mode can't finish this text` | Too long for paragraph mode. Use book mode (Part C). |
| `Request too large for Jev` | One call would pass Jev's limit. In book mode this should not happen for normal novels; report it. |
| `No body paragraphs found` | The URL isn't an HTML page with `<p>` paragraphs. Use the HTML ("Read online") link, not `.txt`. |
| `OPENROUTER_API_KEY is not set` | Step A4. |
| `ANTHROPIC_API_KEY is not set` | Add `EXTRACTION_PROVIDER=openrouter` to `.env` (step A4), or add an Anthropic key. |
| `No roster at ...` | Run step C3 for that book. |
| `Ledger for ... was built with different inputs` | The character list or questions changed since the ledger was built. Rebuild with `cli ledger <book_id> --real --force`. |
| `suspects_only needs at least 2 characters` | Mark suspects with `is_suspect: true` in the roster YAML. |
| The curve in the browser is flat or random | You are viewing a mock run (yellow banner), or "Mock mode" is ticked on `/live`. |
| The viewer says "No saved runs yet" | Run step B2 (or C5) first; the viewer only replays saved results. |

---

## Where a person is required

| Step | Why |
|---|---|
| A2 | Jev access (possible waitlist) |
| A3 | Creating the key, paying, setting a spending limit |
| A4 | Pasting the key into `.env` |
| C3 | Checking the character list and marking suspects |

Everything else is automated by the commands above.
