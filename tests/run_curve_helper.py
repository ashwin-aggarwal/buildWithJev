"""Call scripts/run_curve.py's main() in-process (book mode, mock)."""

import importlib.util
import sys
from pathlib import Path

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "run_curve.py"


def _module():
    spec = importlib.util.spec_from_file_location("run_curve", _PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_book(book_id, *, candidate_set="full_cast", condition="test", posthoc=False, limit=None):
    argv = ["run_curve.py", "--book", book_id, "--mock", "--candidate-set", candidate_set,
            "--condition", condition]
    if posthoc:
        argv.append("--posthoc")
    if limit is not None:
        argv += ["--limit", str(limit)]
    old = sys.argv
    sys.argv = argv
    try:
        _module().main()
    finally:
        sys.argv = old
