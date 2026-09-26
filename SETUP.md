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
uv run detective-jev serve          # opens http://127.0.0.1:8000 in your browser
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

## Part C — Add a new book (one step)

A book can be a **PDF, EPUB, plain-text (.txt), or HTML** file, or a link to
one. Budget about **$0.08 per 70,000 words** for Jev, plus under a cent for the
character list. You'll always see the estimate before anything is spent.

### Easiest: drag and drop in the browser

```bash
uv run detective-jev serve          # opens http://127.0.0.1:8000 in your browser
```

1. Open the **+ Add a book** tab and drop the file on the page, or paste a
   link.
2. It reads the book (free) and shows its length and estimated cost.
3. Choose **Free dry run** (random numbers, costs nothing) or **Run it for
   real**.
4. Watch the four steps tick off. When they finish, press **Watch it →**.

Port 8000 busy? Use `uv run detective-jev serve --port 8080`.

### Or: one command

```bash
uv run detective-jev run path/to/book.pdf              # asks before spending
uv run detective-jev run path/to/book.epub --dry-run   # free rehearsal, random numbers
uv run detective-jev run https://www.gutenberg.org/ebooks/69087.html.images --yes
```

Both do the same four steps:

1. **Read the book.** Split it into ~500-word chunks without cutting a
   paragraph.
2. **Find the characters.** One language-model call through your OpenRouter
   key. A dry run uses a free placeholder list instead.
3. **Build the case notes.** One Jev call per chunk.
4. **Jev reads the book.** One Jev call per chunk. Results are saved to
   `data/results/`.

Every step is saved as it goes. Running the same book again is free and
picks up where it stopped. A real run after a dry run replaces the
placeholder character list automatically.

### Optional: tidy the character list

The character list is made automatically and saved to
`data/rosters/<book_id>.yaml`. It is usually fine. For a careful experiment:
- Open the file and fix any wrong nicknames (`aliases`).
- Mark the suspects with `is_suspect: true`.
- Check the list with `uv run detective-jev validate <book_id>`.

If you change names or aliases after the case notes were built, rebuild
them with `uv run detective-jev ledger <book_id> --real --force` (about
$0.03). Marking suspects needs no rebuild.

### Doing the steps one at a time

The individual stages are still there:

```bash
uv run detective-jev ingest <file-or-url>
uv run detective-jev roster <book_id>
uv run detective-jev ledger <book_id> --real
uv run python scripts/run_curve.py --book <book_id>
```

---

## Which files and websites work?

| Format | How well | Notes |
|---|---|---|
| **EPUB** | Best | Chapters are read in reading order, with chapter titles. |
| **HTML** (file or link) | Very good | The whole book must be on one page, in normal `<p>` paragraphs. Pages that load their text with JavaScript don't work, and a site with one page per chapter only gives you the page you linked. |
| **Plain text (.txt)** | Very good | Paragraphs must be separated by blank lines, as in Project Gutenberg files. |
| **PDF** | Good, best effort | PDFs store lines, not paragraphs, so paragraph breaks are rebuilt. Running headers, page numbers, and hyphenated line-ends are removed. Scanned PDFs (pictures of pages, no selectable text) can't be read. |

- **Project Gutenberg extras are removed automatically in every format.**
  This covers the license, transcriber's notes, and the title page and
  table of contents before chapter one.
- **Advertisements for other books** at the very end are *not* removed yet.
- **To check what was read,** run `uv run detective-jev inspect <book_id> --chunk 1`.
- **Book ids.** Gutenberg links get `pg<number>`. Anything else gets a name
  made from the title plus a short code based on the text.
- **Caching.** Links are downloaded once and cached in `data/raw/html/`;
  uploaded and local files are kept in `data/raw/uploads/`.

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
| `No story text found` | The file or page has no readable paragraphs. For web pages, use the HTML edition; for PDFs, it may be a scanned (image-only) file. |
| `Unsupported file type` | Use a PDF, EPUB, TXT, or HTML file. Word files (.docx) aren't supported; save them as PDF or TXT first. |
| `This PDF has no text layer` | It's a scanned PDF. Find an EPUB or text edition instead. |
| `OPENROUTER_API_KEY is not set` | Step A4. |
| `ANTHROPIC_API_KEY is not set` | Add `EXTRACTION_PROVIDER=openrouter` to `.env` (step A4), or add an Anthropic key. |
| `No roster at ...` | Run step C3 for that book. |
| `Ledger for ... was built with different inputs` | The character list or questions changed since the ledger was built. Rebuild with `cli ledger <book_id> --real --force`. |
| `suspects_only needs at least 2 characters` | Mark suspects with `is_suspect: true` in the roster YAML. |
| The curve in the browser is flat or random | You are viewing a mock run (yellow banner), or "Mock mode" is ticked on `/live`. |
| The viewer says "No saved runs yet" | Add a book (Part C) or run step B2 first. |

---

## Where a person is required

| Step | Why |
|---|---|
| A2 | Jev access (possible waitlist) |
| A3 | Creating the key, paying, setting a spending limit |
| A4 | Pasting the key into `.env` |
| C (optional) | Checking the character list and marking suspects |

Everything else is automated by the commands above.
