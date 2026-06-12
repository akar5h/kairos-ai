"""rerun_spotcheck.py — re-evaluate the EXACT 20 owner-labeled spot-check traces.

After the side_effect_match any/all fix, re-runs membership + outcome on the
fixed trace-id list from docs/spotcheck-day4.md and writes
docs/spotcheck-day4-rerun.md with old verdict vs new verdict per trace, so the
owner's handwritten labels can be re-tallied without a second review round.

NEVER overwrites docs/spotcheck-day4.md (owner's handwritten labels).

Usage:
    uv run scripts/rerun_spotcheck.py [--endpoint URL] [--project NAME]
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

_HERE = Path(__file__).parent
_REPO = _HERE.parent
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_HERE))

from export_spotcheck import _last_tool_summary, _primary_workflow, _verdict_label

from kairos.analysis.outcome_metric import evaluate_outcome
from kairos.readers.phoenix import PhoenixReader
from kairos.taxonomy.business_context import BusinessContext

SPOTCHECK_DOC = _REPO / "docs" / "spotcheck-day4.md"
OUT_DOC = _REPO / "docs" / "spotcheck-day4-rerun.md"
DEFAULT_CONTEXT = _REPO / "config" / "context.yaml"
PHOENIX_PROJECT_NODE_ID = "UHJvamVjdDox"

_ROW_RE = re.compile(
    r"^\| \[(?P<short>[0-9a-f]{16})…\]\(\S*?/traces/(?P<tid>[0-9a-f]{32})\) "
    r"\| (?P<workflow>[^|]+?) \| (?P<verdict>[^|]+?) \| (?P<reason>[^|]*?) \|.*"
    r"\| (?P<label>[YN?][^|]*?)\s*\|(?P<comment>.*)$"
)


def _parse_labeled_rows(doc: Path) -> list[dict[str, str]]:
    rows = []
    for line in doc.read_text().splitlines():
        m = _ROW_RE.match(line)
        if m:
            rows.append(
                {
                    "trace_id": m.group("tid"),
                    "old_workflow": m.group("workflow").strip(),
                    "old_verdict": m.group("verdict").strip(),
                    "old_reason": m.group("reason").strip(),
                    "label": m.group("label").strip(),
                    "comment": m.group("comment").strip(),
                }
            )
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default="http://localhost:6006")
    parser.add_argument("--project", default="default")
    parser.add_argument("--context", default=str(DEFAULT_CONTEXT))
    args = parser.parse_args()

    rows = _parse_labeled_rows(SPOTCHECK_DOC)
    if len(rows) != 20:
        print(f"ERROR: expected 20 labeled rows in {SPOTCHECK_DOC}, parsed {len(rows)}", file=sys.stderr)
        sys.exit(1)

    context = BusinessContext.from_yaml(args.context)
    reader = PhoenixReader(endpoint=args.endpoint, project=args.project)

    out_rows: list[str] = []
    flipped = 0
    for row in rows:
        tid = row["trace_id"]
        try:
            envelope = reader.fetch_envelope(tid)
        except Exception as exc:  # noqa: BLE001
            print(f"  FETCH ERROR {tid[:16]}: {exc}", file=sys.stderr)
            out_rows.append(
                f"| `{tid[:16]}…` | {row['old_workflow']} | {row['old_verdict']} | FETCH ERROR | | {row['label']} |"
            )
            continue

        primary = _primary_workflow(envelope, context)
        if primary == "unmapped":
            new_verdict, new_reason = "unmapped (no verdict)", ""
        else:
            op = next(o for o in context.operations if o.name == primary)
            result = evaluate_outcome(envelope, op)
            new_verdict = _verdict_label(result, envelope)
            new_reason = result.failure_reason.value if result.failure_reason else ""

        changed = new_verdict != row["old_verdict"] or primary != row["old_workflow"]
        if changed:
            flipped += 1
        link = f"[{tid[:16]}…]({args.endpoint}/projects/{PHOENIX_PROJECT_NODE_ID}/traces/{tid})"
        out_rows.append(
            f"| {link} | {row['old_workflow']} / {row['old_verdict']} | {primary} / {new_verdict} "
            f"| {new_reason} | {'**CHANGED**' if changed else 'same'} | {row['label']} |"
        )
        print(
            f"  {tid[:16]}  {row['old_verdict']:>5} -> {new_verdict:<22} "
            f"({primary})  label={row['label']}",
            file=sys.stderr,
        )

    OUT_DOC.write_text(
        "# Spot-check Day 4 — RERUN after side_effect_match any/all fix\n\n"
        "*Re-evaluation of the exact 20 traces the owner labeled in "
        "`spotcheck-day4.md` (that file is preserved untouched). Old verdicts "
        "from the labeled doc; new verdicts from the fixed engine "
        "(`side_effect_match: any` on Code Implementation).*\n\n"
        "| Trace | Old (workflow / verdict) | New (workflow / verdict) | New failure reason | Δ | Owner label |\n"
        "|-------|--------------------------|--------------------------|--------------------|---|-------------|\n"
        + "\n".join(out_rows)
        + f"\n\n**{flipped}/20 rows changed.**\n"
    )
    print(f"\nWrote {OUT_DOC} ({flipped}/20 changed)", file=sys.stderr)


if __name__ == "__main__":
    main()
