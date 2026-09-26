"""Command line for the ledger pipeline.

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
    from .ingest import ingest

    book = ingest(args.url, chunk_size=args.chunk_size, force=args.force)
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
    ap = argparse.ArgumentParser(prog="python -m detective_jev.cli", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("ingest", help="fetch + parse + chunk a book from a URL")
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
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args.fn(args)


if __name__ == "__main__":
    main(sys.argv[1:])
