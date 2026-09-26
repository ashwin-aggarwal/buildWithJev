import json

import pandas as pd
import yaml

from detective_jev import config, ledger as ledger_mod
from detective_jev.scoring.convert import convert, read_jsonl, to_jsonl_fields
from run_curve_helper import run_book


def test_converter_idempotent_and_round_trips(roster_file, book):
    ledger_mod.build_ledger("pg99999", mock=True)
    config.ANSWERS_DIR.mkdir(parents=True, exist_ok=True)
    (config.ANSWERS_DIR / "pg99999.yaml").write_text(
        yaml.safe_dump({"book_id": "pg99999", "is_culprit": {"Dr. Hollis": True}}))
    run_book("pg99999", candidate_set="suspects_only", condition="c1")
    jsonl = next(config.RESULTS_DIR.glob("pg99999__*.jsonl"))

    [path] = convert(jsonl)
    first = path.read_bytes()
    [again] = convert(jsonl)
    assert again == path and path.read_bytes() == first          # idempotent, regenerated whole

    df = pd.read_parquet(path)
    rows = read_jsonl(jsonl)
    assert len(df) == sum(len(r["probabilities"]) for r in rows)
    assert set(df.columns) >= {"book_id", "t", "candidate", "prob", "is_culprit", "introduced_by_t",
                               "candidate_set", "run_id", "condition", "option_position",
                               "q_contradicts_prior_prob", "q_alibi_effect_value"}
    assert df.loc[df.candidate == "Dr. Hollis", "is_culprit"].all()
    assert not df.loc[df.candidate != "Dr. Hollis", "is_culprit"].any()
    assert df.loc[df.candidate == "none_of_these", "introduced_by_t"].isna().all()

    back = {r["t"]: r for r in to_jsonl_fields(df)}
    for r in rows:
        b = back[r["t"]]
        for k in ("answer", "probabilities", "run_id", "condition", "candidate_set", "book_id",
                  "option_order", "inference_answers", "model", "confidence"):
            assert b[k] == r[k], k
