"""Command line for the ledger pipeline.

    detective-jev run <file-or-url> [--dry-run] [--yes]   # EVERYTHING in one step
    detective-jev serve                                   # open the viewer in a browser

`run` accepts a URL or a local .html/.txt/.pdf/.epub file. It reads the book
(free), shows the estimated cost, asks before spending, then finds the
characters, builds the ledger, and runs Jev over every chunk. --dry-run does
the whole thing for free with random numbers. (`detective-jev` is short for
`python -m detective_jev.cli`; run it with `uv run detective-jev ...`.)

The individual stages, for when you want control:

    python -m detective_jev.cli ingest <url> [--chunk-size N] [--force]
    python -m detective_jev.cli roster <book_id> [--force]
    python -m detective_jev.cli validate <book_id>
    python -m detective_jev.cli ledger <book_id> [--real] [--force]
    python -m detective_jev.cli list
    python -m detective_jev.cli inspect <book_id> [--chunk N | --entry N] [--real]

Every stage is idempotent; --force re-runs it:
  ingest --force   re-parse the cached HTML (never refetches)
  roster --force   re-run the Anthropic extraction (ONE paid call) and overwrite
                   the roster YAML, keeping a .bak of the old one
  ledger --force   start a fresh ledger (recompresses every chunk)

Ledger builds are MOCK by default (free, written to <book_id>.mock.json.gz).
Pass --real to make paid Jev calls.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from . import storage


def _cmd_ingest(args) -> None:
    from .ingest import ingest_source

    book = ingest_source(args.url, chunk_size=args.chunk_size, force=args.force)
    print(f"{book['book_id']}: {book.get('title')!s} by {book.get('author')!s} — "
          f"{book['n_words']:,} words, {book['n_chunks']} chunks")


def _cmd_roster(args) -> None:
    from .roster import get_roster

    roster = get_roster(args.book_id, force=args.force)
    for c in roster["characters"]:
        flag = " [suspect]" if c.get("is_suspect") else ""
        print(f"  {c['canonical']}{flag}  first@{c.get('first_mention_chunk')}  aliases={c['aliases']}")
    print(f"{len(roster['characters'])} characters -> {storage.roster_path(args.book_id)}")


def _cmd_validate(args) -> None:
    from .roster import load_roster, validate_roster

    book = storage.load_book(args.book_id)
    warnings = validate_roster(load_roster(args.book_id, book), book)
    print("\n".join(f"WARNING: {w}" for w in warnings) or "No roster warnings.")


def _cmd_ledger(args) -> None:
    from .ledger import build_ledger

    ledger = build_ledger(args.book_id, mock=not args.real, force=args.force)
    print(f"{args.book_id}: {len(ledger['entries'])}/{ledger['n_chunks']} entries, "
          f"{ledger['compression_calls']} compression calls, "
          f"{'MOCK' if ledger['mock'] else 'real'} -> {storage.ledger_path(args.book_id, mock=ledger['mock'])}")


def _bar(done: int, total: int, width: int = 28) -> str:
    filled = int(width * done / total) if total else width
    return "█" * filled + "░" * (width - filled)


def _cmd_run(args) -> None:
    from .ingest import IngestError, ingest_source
    from .pipeline import estimate, run_all
    from .roster import load_roster

    try:
        book = ingest_source(args.source, chunk_size=args.chunk_size)
    except IngestError as exc:
        raise SystemExit(f"Could not read the book: {exc}")
    n_chars = None
    if storage.roster_path(book["book_id"]).exists():
        n_chars = len(load_roster(book["book_id"])["characters"])
    est = estimate(book, n_chars)
    print(f"\n  {book.get('title') or book['book_id']}"
          f"{' by ' + book['author'] if book.get('author') else ''}")
    print(f"  {book['n_words']:,} words · {book['n_chunks']} chunks · book id {book['book_id']}")
    if args.dry_run:
        print("  Dry run: random numbers, no API calls, free.\n")
    else:
        extra = f" + about ${est['characters_cost']:.3f} for the character list" if est["characters_cost"] else ""
        print(f"  Estimated cost: about ${est['jev_cost']:.2f} for Jev{extra}; about {est['minutes']} min.\n")
        if not args.yes:
            answer = input("  Go ahead? [y/N] ").strip().lower()
            if answer not in ("y", "yes"):
                raise SystemExit("  Cancelled. Nothing was spent.")

    def show(ev):
        if ev["state"] == "done":
            print(f"\r  ✓ {ev['label']:<22} {ev['message']:<60}")
        elif ev["total"]:
            print(f"\r  … {ev['label']:<22} {_bar(ev['done'], ev['total'])} {ev['done']}/{ev['total']}",
                  end="", flush=True)
        else:
            print(f"\r  … {ev['label']:<22} {ev['message']}", end="", flush=True)

    logging.getLogger().setLevel(logging.WARNING)   # keep the progress display clean
    result = run_all(book_id=book["book_id"], mock=args.dry_run, condition=args.condition, progress=show)
    print(f"\n  Done. Watch it: uv run detective-jev serve   (run file {result['run_file']})\n")


def _cmd_serve(args) -> None:
    import threading
    import webbrowser

    from .webapp import app

    url = f"http://127.0.0.1:{args.port}"
    print(f"Detective Jev viewer at {url}  (Ctrl+C to stop)")
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=args.port, threaded=True, debug=False)


def _cmd_list(args) -> None:
    df = storage.read_manifest()
    if df.empty:
        print("No books ingested yet.")
        return
    for _, r in df.iterrows():
        print(f"{r['book_id']:24s} {r['n_chunks']:4d} chunks {r['n_words']:8,} words  {r['title']!s}")


def _cmd_inspect(args) -> None:
    book = storage.load_book(args.book_id)
    if args.entry is not None:
        from .ledger import load_ledger

        ledger = load_ledger(args.book_id, mock=not args.real)
        entry = next((e for e in ledger["entries"] if e["chunk"] == args.entry), None)
        if entry is None:
            raise SystemExit(f"No entry {args.entry} (ledger has {len(ledger['entries'])}).")
        print(json.dumps(entry, indent=2, ensure_ascii=False))
        return
    if args.chunk is not None:
        t = args.chunk
        print(json.dumps(book["chunk_meta"][t - 1], indent=2, ensure_ascii=False))
        print()
        for i, s in enumerate(storage.chunk_sentences(book, t), start=1):
            print(f"s{i:<3d} {s['text']}")
        return
    meta = {k: v for k, v in book.items() if k not in ("chunks", "sentences", "chunk_meta")}
    print(json.dumps(meta, indent=2, ensure_ascii=False))


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="detective-jev", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run", help="ONE STEP: read a book, find characters, build the ledger, run Jev")
    p.add_argument("source", help="URL, or a .html/.txt/.pdf/.epub file")
    p.add_argument("--dry-run", action="store_true", help="free: random numbers, no API calls")
    p.add_argument("--yes", "-y", action="store_true", help="don't ask before spending")
    p.add_argument("--chunk-size", type=int, default=None)
    p.add_argument("--condition", default=None, help="label for this run (default v1, or dry-run)")
    p.set_defaults(fn=_cmd_run)

    p = sub.add_parser("serve", help="start the viewer and open it in your browser")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--no-browser", action="store_true")
    p.set_defaults(fn=_cmd_serve)

    p = sub.add_parser("ingest", help="read + chunk a book (URL or .html/.txt/.pdf/.epub file)")
    p.add_argument("url")
    p.add_argument("--chunk-size", type=int, default=None)
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=_cmd_ingest)

    p = sub.add_parser("roster", help="load or extract the character roster")
    p.add_argument("book_id")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=_cmd_roster)

    p = sub.add_parser("validate", help="print roster validation warnings")
    p.add_argument("book_id")
    p.set_defaults(fn=_cmd_validate)

    p = sub.add_parser("ledger", help="build the ledger (mock unless --real)")
    p.add_argument("book_id")
    p.add_argument("--real", action="store_true", help="make real (paid) Jev calls")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=_cmd_ledger)

    p = sub.add_parser("list", help="list ingested books")
    p.set_defaults(fn=_cmd_list)

    p = sub.add_parser("inspect", help="show a book, a chunk, or a ledger entry")
    p.add_argument("book_id")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--chunk", type=int)
    g.add_argument("--entry", type=int)
    p.add_argument("--real", action="store_true", help="inspect the real ledger, not the mock one")
    p.set_defaults(fn=_cmd_inspect)

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING if args.cmd in ("run", "serve") else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    args.fn(args)


if __name__ == "__main__":
    main(sys.argv[1:])
