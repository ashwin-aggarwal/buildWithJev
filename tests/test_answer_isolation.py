"""The answer key must never reach Jev state.

Static: no state-building module imports detective_jev.scoring or mentions the
answers directory. Runtime: a full mock ingest -> roster -> ledger -> inference
run (both candidate sets) never opens anything under data/answers/.
"""

import ast
import builtins
import gzip
import io
import sys
from pathlib import Path

import pytest
import yaml

from detective_jev import config

SRC = Path(__file__).resolve().parents[1] / "src" / "detective_jev"
REPO = SRC.parents[1]
STATE_BUILDING = [
    SRC / f for f in ("ingest.py", "chunking.py", "roster.py", "questions.py", "ledger.py",
                      "render.py", "inference.py", "jev_client.py", "storage.py",
                      "friend_stubs.py", "webapp.py", "story.py", "tokens.py", "cli.py",
                      "viewer.py")
] + [REPO / "scripts" / "run_curve.py"]


@pytest.mark.parametrize("path", STATE_BUILDING, ids=lambda p: p.name)
def test_state_building_modules_never_reference_answers(path):
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    docstrings = {
        id(n.body[0].value) for n in ast.walk(tree)
        if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef)) and n.body
        and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            names = [a.name for a in node.names]
            assert "scoring" not in mod.split(".") and "scoring" not in names, f"{path.name} imports scoring"
        if isinstance(node, ast.Import):
            assert not any("scoring" in a.name.split(".") for a in node.names), f"{path.name} imports scoring"
        if isinstance(node, ast.Attribute):
            assert node.attr != "ANSWERS_DIR", f"{path.name} touches config.ANSWERS_DIR"
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            assert "data/answers" not in node.value and "answers/" not in node.value, \
                f"{path.name} mentions the answers directory"


def test_full_mock_pipeline_never_opens_answers(roster_file, book, monkeypatch, tmp_path):
    answers = config.ANSWERS_DIR
    answers.mkdir(parents=True, exist_ok=True)
    (answers / "pg99999.yaml").write_text(yaml.safe_dump({"book_id": "pg99999",
                                                          "is_culprit": {"Dr. Hollis": True}}))
    touched = []

    def guard(fn):
        def wrapper(file, *a, **k):
            if isinstance(file, (str, Path)) and Path(file).resolve().is_relative_to(answers.resolve()):
                touched.append(str(file))
                raise AssertionError(f"state-building code opened {file}")
            return fn(file, *a, **k)
        return wrapper

    monkeypatch.setattr(builtins, "open", guard(builtins.open))
    monkeypatch.setattr(io, "open", guard(io.open))
    monkeypatch.setattr(gzip, "open", guard(gzip.open))
    real_read_text, real_read_bytes = Path.read_text, Path.read_bytes
    monkeypatch.setattr(Path, "read_text", lambda self, *a, **k: guard(lambda f, *a, **k: real_read_text(f, *a, **k))(self, *a, **k))
    monkeypatch.setattr(Path, "read_bytes", lambda self: guard(lambda f: real_read_bytes(f))(self))

    sys.modules.pop("detective_jev.scoring", None)
    from detective_jev.ledger import build_ledger
    from run_curve_helper import run_book

    build_ledger("pg99999", mock=True)
    for cs in ("full_cast", "suspects_only"):
        run_book("pg99999", candidate_set=cs, condition="iso", posthoc=True)
    assert touched == []
    assert "detective_jev.scoring" not in sys.modules
