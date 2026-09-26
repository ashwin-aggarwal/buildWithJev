"""=============================================================================
 STUBS OWNED BY <FRIEND> — replace the placeholder bodies below.
=============================================================================

This module is the boundary between the plumbing (owned by the repo/env/client)
and the *decision design* (owned by you). The rest of the pipeline calls these
three functions and nothing else, so you can iterate here freely without
touching the client, cost estimator, or runner.

The runner (scripts/run_curve.py) does, for each paragraph index t = 1..N:

    choices  = build_choices(paragraphs)          # once, up front
    question = build_question()                    # once, up front
    context  = build_context(paragraphs, t)        # per step
    result   = jev_client.query(context, question, choices)

So whatever you return here flows straight into a real Jev Choice call. The
placeholder implementations are deliberately trivial and marked with TODO — they
let the whole pipeline run end-to-end in mock mode today, and produce a valid
(if naive) curve, but they are NOT the real design.
"""

from __future__ import annotations

from . import questions as _questions


def _culprit_question() -> dict:
    """The `culprit` question from config/inference_questions.yaml."""
    return _questions.find(_questions.load_inference_questions(), "culprit")


def build_choices(paragraphs: list[str]) -> dict[str, str | None]:
    """Build the suspect choice list for the whole story.

    This becomes Jev's Choice `criteria`: an {option: description} map where the
    key is a suspect label Jev will pick between, and the value is an optional
    natural-language description/disambiguation (or None).

    Args:
        paragraphs: the full ordered list of story paragraphs. You may scan the
            whole story to assemble the candidate suspects, since the choice set
            is fixed across all t (Jev needs a stable option set to produce a
            comparable distribution at every step).

    Returns:
        {suspect_label: description_or_None}. 2..255 options.

    TODO(<friend>): Replace this placeholder. Decide:
      - How suspects are identified (hand-curated? extracted from the text?).
      - Whether to include a catch-all like "unknown"/"no one yet".
      - What descriptions (if any) help Jev disambiguate similar names.
    """
    # Paragraph mode has no roster, so the suspects stay placeholders. Book mode
    # (run_curve.py --book) builds culprit options from the roster instead.
    # The culprit question's extra options (e.g. none_of_these) come from config.
    choices: dict[str, str | None] = {
        "suspect_a": "Placeholder suspect A",
        "suspect_b": "Placeholder suspect B",
        "suspect_c": "Placeholder suspect C",
    }
    extra = _culprit_question().get("extra_options", [])
    choices.update(dict(extra) if isinstance(extra, dict) else {str(e): None for e in extra})
    return choices


def build_question() -> str:
    """Return the question text passed to Jev at every step.

    Returns:
        A single natural-language Choice question, e.g. "Who is the killer?".

    TODO(<friend>): Confirm the exact wording. This is prompt content and is
    yours to own; the placeholder is just a sensible default.
    """
    # The culprit question text lives in config/inference_questions.yaml.
    return _culprit_question()["text"]


def build_context(paragraphs: list[str], t: int) -> str:
    """Build the `state` string Jev sees at step t.

    Args:
        paragraphs: full ordered list of story paragraphs (0-indexed).
        t: number of paragraphs revealed so far, 1..len(paragraphs). The context
            should reflect paragraphs 1..t (i.e. paragraphs[:t]).

    Returns:
        The state text to send as Jev's `state` for this step.

    TODO(<friend>): Replace this placeholder. Decide:
      - Any framing/preamble around the raw text.
      - Whether to trim/summarize early paragraphs once the prefix approaches
        the 32k-token limit (see scripts/estimate_cost.py, which flags this).
      - Exactly how paragraphs are joined.
    """
    # --- placeholder: naive concatenation of paragraphs 1..t ----------------
    return "\n\n".join(paragraphs[:t])
