"""Deterministic serialisation of ledger entries into Jev state text.

`render_ledger` is a PURE function of its arguments: the same entries, rollups,
filter, and post-hoc values always give the same string, at every timestep.
It is kept separate from extraction so the format can change freely.

Format (one block per entry, chapter lines only when the chapter changes):

    == CHAPTER III ==
    #12 concerns=Alfred Inglethorp(.81) secondary=none(.40) event_type=alibi_claim(.62)
      flags: new_evidence .77, time_reference .64
      suspicion: Alfred Inglethorp 4, Evelyn Howard 2
      "I was at the chemist's in Tadminster at six o'clock," said he.

Rolled-up entries are replaced by their rollup summary (see ledger.py).
"""

from __future__ import annotations

from typing import Any, Iterable

# Answers not shown as key=value (key_sentence is shown as the quote).
_HIDDEN = {"key_sentence"}


def _p(x: float | None) -> str:
    if x is None:
        return "?"
    return f"{x:.2f}".lstrip("0") if x < 1 else "1.0"


def _suspicion_line(entry: dict[str, Any], order: dict[str, int], suspects: set[str] | None) -> str | None:
    items = [
        (name, s["value"]) for name, s in entry.get("suspicion", {}).items()
        if s.get("value") is not None and s["value"] >= 2 and (suspects is None or name in suspects)
    ]
    if not items:
        return None
    items.sort(key=lambda kv: (-kv[1], order.get(kv[0], 10**6), kv[0]))
    return "  suspicion: " + ", ".join(f"{n} {v}" for n, v in items)


def _render_entry(entry: dict[str, Any], order: dict[str, int], suspects: set[str] | None,
                  posthoc: dict[str, Any] | None) -> list[str]:
    parts, flags = [], []
    for qid, ans in entry["answers"].items():
        if qid in _HIDDEN:
            continue
        if ans["type"] == "noul":
            if ans["value"]:
                flags.append(f"{qid} {_p(ans['prob'])}")
        elif ans["type"] == "choice":
            parts.append(f"{qid}={ans['value']}({_p(ans['prob'])})")
        else:
            parts.append(f"{qid}={ans['value']}")
    lines = [f"#{entry['chunk']} " + " ".join(parts)]
    if flags:
        lines.append("  flags: " + ", ".join(flags))
    sus = _suspicion_line(entry, order, suspects)
    if sus:
        lines.append(sus)
    ph = (posthoc or {}).get(str(entry["chunk"]))
    if ph and ph.get("contradicts_prior", 0) >= 0.5:
        lines.append(f"  post-hoc: contradicts earlier notes {_p(ph['contradicts_prior'])}")
    if entry.get("key_quote"):
        lines.append(f"  \"{entry['key_quote']}\"")
    return lines


def _render_rollup(rollup: dict[str, Any]) -> list[str]:
    a, b = rollup["chunks"]
    lines = [f"== SUMMARY of passages #{a}-#{b} =="]
    for ch in rollup["chapters"]:
        ca, cb = ch["chunks"]
        counts = ", ".join(f"{k} x{v}" for k, v in ch["event_counts"].items())
        lines.append(f"[{ch['chapter'] or 'untitled'}] #{ca}-#{cb}: {counts}")
        for q in ch["quotes"]:
            lines.append(f"  #{q['chunk']} \"{q['quote']}\"")
    return lines


def render_ledger(
    entries: Iterable[dict[str, Any]],
    roster: dict[str, Any],
    *,
    rollups: Iterable[dict[str, Any]] = (),
    suspects: Iterable[str] | None = None,
    posthoc: dict[str, Any] | None = None,
) -> str:
    """Render ledger entries (and any rollups covering them) as compact text.

    Args:
        entries: ledger entries to show (the caller slices 1..t-1).
        roster: the book's roster (fixes name ordering for ties).
        rollups: rollups active at this timestep; entries they cover are shown
            only through the rollup summary.
        suspects: if given, suspicion lines list only these names (the
            candidate_set=suspects_only view of the full-cast ledger).
        posthoc: {str(chunk): {"contradicts_prior": p}} from this run's sidecar.
    """
    entries = sorted(entries, key=lambda e: e["chunk"])
    rollups = sorted(rollups, key=lambda r: r["chunks"][0])
    if not entries:
        return "(no earlier passages)"
    order = {c["canonical"]: i for i, c in enumerate(roster["characters"])}
    suspect_set = set(suspects) if suspects is not None else None
    covered = {i for r in rollups for i in range(r["chunks"][0], r["chunks"][1] + 1)}

    items: list[tuple[int, str, dict[str, Any]]] = [(r["chunks"][0], "rollup", r) for r in rollups]
    items += [(e["chunk"], "entry", e) for e in entries if e["chunk"] not in covered]
    items.sort(key=lambda it: (it[0], it[1] == "entry"))

    lines: list[str] = []
    chapter: object = object()  # sentinel: first entry always prints its chapter
    for _, kind, item in items:
        if kind == "rollup":
            lines.extend(_render_rollup(item))
            chapter = object()
            continue
        if item.get("chapter") != chapter:
            chapter = item.get("chapter")
            lines.append(f"== {chapter or 'untitled'} ==")
        lines.extend(_render_entry(item, order, suspect_set, posthoc))
    return "\n".join(lines)


def compose_state(notes: str, t: int, chunk_text: str) -> str:
    """The inference state at step t: case notes for 1..t-1, then chunk t raw."""
    header = "CASE NOTES (none yet)" if t <= 1 else f"CASE NOTES (compressed record of passages 1-{t - 1})"
    return f"{header}:\n{notes}\n\nCURRENT PASSAGE (#{t}):\n{chunk_text}"
