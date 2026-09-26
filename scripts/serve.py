"""Launch the live Detective JEV demo website.

Usage:
    python scripts/serve.py            # http://127.0.0.1:8000
    python scripts/serve.py --port 8080

Then open the printed URL, paste a Project Gutenberg "Plain Text UTF-8" link,
and watch JEV solve the mystery. Mock mode is ON by default in the UI (free);
untick it to make real (paid) calls once your key is set (see SETUP.md).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from detective_jev.webapp import app  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()

    print(f"Detective JEV demo running at http://{args.host}:{args.port}")
    print("Press Ctrl+C to stop.")
    app.run(host=args.host, port=args.port, threaded=True, debug=False)


if __name__ == "__main__":
    main()
