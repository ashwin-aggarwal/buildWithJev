import pytest
import yaml

from detective_jev import config
from detective_jev.questions import (QuestionConfigError, load_compression_questions,
                                     load_inference_questions, load_questions)


def _write(tmp_path, questions, scope=None):
    path = tmp_path / "q.yaml"
    data = {"questions": questions}
    if scope:
        data["scope"] = scope
    path.write_text(yaml.safe_dump(data))
    return path


def test_shipped_configs_load():
    comp = load_compression_questions()
    inf = load_inference_questions()
    assert {q["scope"] for q in comp} == {"chunk_local"}
    assert {q["scope"] for q in inf} == {"history_aware"}
    ids_c, ids_i = {q["id"] for q in comp}, {q["id"] for q in inf}
    assert {"contradicts_prior", "alibi_effect"} <= ids_i
    assert not {"contradicts_prior", "alibi_effect", "suspicion_delta"} & ids_c
    assert "culprit" in ids_i and "suspicion" in ids_c


def test_compression_loader_rejects_history_aware(tmp_path):
    path = _write(tmp_path, [{"id": "contradicts_prior", "scope": "history_aware", "type": "noul",
                              "text": "Contradicts?"}])
    with pytest.raises(QuestionConfigError, match="history_aware"):
        load_questions(path, "chunk_local")


def test_inference_loader_rejects_chunk_local(tmp_path):
    path = _write(tmp_path, [{"id": "concerns", "scope": "chunk_local", "type": "choice",
                              "text": "Who?", "options": "roster"}])
    with pytest.raises(QuestionConfigError, match="chunk_local"):
        load_questions(path, "history_aware")


def test_missing_scope_and_file_scope_mismatch(tmp_path):
    with pytest.raises(QuestionConfigError, match="scope"):
        load_questions(_write(tmp_path, [{"id": "a", "type": "noul", "text": "x"}]), "chunk_local")
    with pytest.raises(QuestionConfigError, match="file scope"):
        load_questions(_write(tmp_path, [{"id": "a", "scope": "chunk_local", "type": "noul", "text": "x"}],
                              scope="history_aware"), "chunk_local")


def test_options_bound_to_scope(tmp_path):
    # the ledger must not depend on the run's candidate set
    with pytest.raises(QuestionConfigError, match="candidates"):
        load_questions(_write(tmp_path, [{"id": "a", "scope": "chunk_local", "type": "choice",
                                          "text": "x", "options": "candidates"}]), "chunk_local")
    with pytest.raises(QuestionConfigError, match="sentences"):
        load_questions(_write(tmp_path, [{"id": "a", "scope": "history_aware", "type": "choice",
                                          "text": "x", "options": "sentences"}]), "history_aware")


def test_schema_errors(tmp_path):
    bad = [
        [{"id": "a", "scope": "chunk_local", "type": "score", "text": "x", "levels": ["only one"]}],
        [{"id": "a", "scope": "chunk_local", "type": "noul", "text": "x", "options": ["y"]}],
        [{"id": "a", "scope": "chunk_local", "type": "noul", "text": "x", "per_character": True}],
        [{"id": "a", "scope": "chunk_local", "type": "noul", "text": "x"},
         {"id": "a", "scope": "chunk_local", "type": "noul", "text": "y"}],
        [{"id": "Bad-Id", "scope": "chunk_local", "type": "noul", "text": "x"}],
    ]
    for qs in bad:
        with pytest.raises(QuestionConfigError):
            load_questions(_write(tmp_path, qs), "chunk_local")
