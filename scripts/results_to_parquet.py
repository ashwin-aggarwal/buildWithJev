"""Convert a book-mode results JSONL into long-format Parquet, one file per run.

The Parquet is derived and disposable: rerunning regenerates it from the JSONL.

Usage:
    python scripts/results_to_parquet.py data/results/<book_id>__<run_id>.jsonl
    python scripts/results_to_parquet.py data/results/*.jsonl --out data/results/parquet
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from detective_jev.scoring.convert import convert  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("jsonl", type=Path, nargs="+", help="Results JSONL file(s)")
    ap.add_argument("--out", type=Path, default=None, help="Output dir (default data/results/parquet)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    for path in args.jsonl:
        for out in convert(path, args.out):
            print(out)


if __name__ == "__main__":
    main()
