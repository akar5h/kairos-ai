"""build_disagreement_queue.py — Build the disagreement re-label queue.

Loads labeled traces from two sources:
  1. eval/review/answers.jsonl   (15 records from the in-app review)
  2. docs/spotcheck-day4.md      (20 trace rows with AGREE?/freetext)

For each labeled trace, fetches the Phoenix envelope, runs D1
(unrecovered_error) and D2 (struggle_ratio), maps the owner label to a
coarse sentiment (CLEAN | PROBLEM | UNKNOWN), and identifies
DISAGREEMENTS:

  Disagreement = (D1 or D2 fired AND label==CLEAN)
              OR (neither D1 nor D2 fired AND label==PROBLEM)

Emits eval/review/disagreement_queue.json with the same schema that
app.py reads, extended with:
  - `question`         detector-specific challenge text
  - `is_evidence=true` on every affected_step_indices step
  - `detector_note`    per-step string on flagged steps
  - `prior_label`      the raw original label text
  - `prior_comment`    freetext comment from the original review
  - `disagreement_kind` "fired_clean" | "silent_problem"

Cap: emit at most MAX_QUEUE (15) entries.  Priority order:
  1. D1 severity=="error" fires (required side-effect tool errored)
  2. Most total fires (D1+D2) on that trace
  3. D1 severity=="warning" fires
  4. D2-only fires

Dropped traces logged explicitly — no silent truncation.

Usage:
    uv run eval/review/build_disagreement_queue.py [options]

Options match build_queue.py:
    --endpoint URL   Phoenix base URL (default: http://localhost:6006)
    --project NAME   Phoenix project name (default: default)
    --context PATH   Path to context.yaml (default: config/context.yaml)
    --out PATH       Output path (default: eval/review/disagreement_queue.json)
    --answers PATH   Answers jsonl (default: eval/review/answers.jsonl)
    --spotcheck PATH Spotcheck markdown (default: docs/spotcheck-day4.md)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

# ── path bootstrap ─────────────────────────────────────────────────────────────
_HERE = Path(__file__).parent
_REPO = _HERE.parent.parent
sys.path.insert(0, str(_REPO / "src"))

from typing import TYPE_CHECKING  # noqa: E402

from kairos.analysis.workflow_membership import MembershipKind  # noqa: E402
from kairos.detection.session_quality import (  # noqa: E402
    detect_struggle_ratio,
    detect_unrecovered_error,
)
from kairos.engine.pipeline import classify_membership  # noqa: E402
from kairos.readers.db import fetch_envelope_from_db  # noqa: E402
from kairos.taxonomy.business_context import BusinessContext  # noqa: E402

# Reuse transcript alignment + redaction from sibling module
sys.path.insert(0, str(_HERE))
from build_queue import (  # noqa: E402
    _aggregate_tokens,
    _empty_meta,
    _session_id_from_steps,
    _trace_window,
    build_step_list,
)
from transcript_align import (  # noqa: E402
    TranscriptCall,
    align_trace_to_transcript,
    redact,
)

if TYPE_CHECKING:
    from kairos.detection.models import Finding
    from kairos.models.trace import TraceEnvelope
    from kairos.taxonomy.business_context import BusinessOperation

# ── Constants ─────────────────────────────────────────────────────────────────

DEFAULT_DSN = ""  # falls back to KAIROS_PG_DSN env var
DEFAULT_CONTEXT = str(_REPO / "config" / "context.yaml")
DEFAULT_OUT = str(_HERE / "disagreement_queue.json")
DEFAULT_ANSWERS = str(_HERE / "answers.jsonl")
DEFAULT_SPOTCHECK = str(_REPO / "docs" / "spotcheck-day4.md")
MAX_QUEUE = 15

# ── Coarse label mapping ───────────────────────────────────────────────────────
# Rule (applied in order):
#
# CLEAN  — owner says the trace is fine:
#   answers.jsonl: answer text matches "pass", "lgtm", "looks good",
#                  or the verdict_shown was "pass" and the answer is very short
#                  (owner just hit Save to confirm), or spotcheck AGREE==Y
#                  on a pass-verdict row.
#
# PROBLEM — owner identifies a real issue:
#   answers.jsonl: answer contains explicit error/struggle/haywire markers,
#                  or spotcheck AGREE==N (engine wrong on a pass) or
#                  AGREE==Y on a fail verdict.
#
# UNKNOWN — ambiguous / insufficient:
#   Everything else, including AGREE==?, missing comments, and answers that
#   describe inconclusive evidence ("can't comment", "I don't know").

_CLEAN_PATTERNS = re.compile(
    r"\b(pass|lgtm|looks good|good to me|fine|ok|okay|agree)\b",
    re.IGNORECASE,
)
_PROBLEM_PATTERNS = re.compile(
    r"\b(fail(ed|s|ure)?|errors?|haywire|loop(ed|s)?|struggle[ds]?|wrong|bad|issue|concern|problem"
    r"|restart|silent failure|never re.attempt|not a success|broke"
    r"|incorrect|stale|abort|exits with|exit code [1-9])\b",
    re.IGNORECASE,
)
_INCONCLUSIVE_PATTERNS = re.compile(
    r"\b(inconclusive|can.t comment|no data|i don.t know|no verdict|unclear)\b",
    re.IGNORECASE,
)


def map_label_to_coarse(answer: str, verdict_shown: str) -> str:
    """Map raw answer text + verdict to CLEAN | PROBLEM | UNKNOWN.

    Rule (in order):
      1. Explicit inconclusive markers → UNKNOWN.
      2. If answer matches PROBLEM patterns → PROBLEM.
      3. If answer matches CLEAN patterns OR (verdict_shown=="pass" AND
         answer is very short, i.e. the owner just confirmed) → CLEAN.
      4. Fallback → UNKNOWN.
    """
    if _INCONCLUSIVE_PATTERNS.search(answer):
        return "UNKNOWN"
    if _PROBLEM_PATTERNS.search(answer):
        return "PROBLEM"
    if _CLEAN_PATTERNS.search(answer):
        return "CLEAN"
    # Short answers on passing verdicts are confirmations
    if verdict_shown == "pass" and len(answer.strip()) <= 30:
        return "CLEAN"
    return "UNKNOWN"


# ── Load sources ──────────────────────────────────────────────────────────────


def load_answers_jsonl(path: Path) -> list[dict[str, Any]]:
    """Load answers.jsonl; last record per trace_id wins."""
    by_trace: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec: dict[str, Any] = json.loads(line)
                if rec.get("trace_id"):
                    by_trace[rec["trace_id"]] = rec
            except (json.JSONDecodeError, KeyError):
                continue
    return list(by_trace.values())


def parse_spotcheck_md(path: Path) -> list[dict[str, Any]]:
    """Parse the AGREE?/comment columns from docs/spotcheck-day4.md.

    Returns a list of dicts:
      trace_id, verdict, agree (Y/N/?), comment, full_trace_id

    The markdown table rows look like:
      | [8f0780364da8cbff…](http://..../traces/<full_id>) | workflow | verdict |
        fr | ev | src | last_tools | [↓ digest](...) | Y | comment text |

    Columns (1-indexed):
      1  trace link (contains full id in URL)
      2  primary workflow
      3  verdict
      4  failure reason
      5  evidence step
      6  status source
      7  last tools
      8  digest link
      9  AGREE? (Y / N / ?)
      10 comment (may be empty or absent if row has 9 cols)
    """
    records: list[dict[str, Any]] = []
    if not path.exists():
        return records

    with path.open(encoding="utf-8") as f:
        content = f.read()

    # Find the table rows (lines with | at start, skip header/separator)
    table_pattern = re.compile(
        r"^\|\s*\[([0-9a-f]{16})…\]\(.*?/traces/([0-9a-f]{32})\)\s*\|(.+)$",
        re.MULTILINE,
    )
    for m in table_pattern.finditer(content):
        short_id = m.group(1)  # noqa: F841
        full_id = m.group(2)
        rest = m.group(3)
        cols = [c.strip() for c in rest.split("|")]
        # cols: workflow, verdict, failure_reason, ev_step, src, last_tools, digest, AGREE?, [comment...]
        if len(cols) < 8:
            continue
        agree_raw = cols[7].strip() if len(cols) > 7 else ""
        comment = " | ".join(cols[8:]).strip() if len(cols) > 8 else ""
        verdict = cols[1].strip().lower() if len(cols) > 1 else ""
        agree = agree_raw.upper() if agree_raw.upper() in ("Y", "N", "?") else "?"
        records.append(
            {
                "trace_id": full_id,
                "verdict": verdict,
                "agree": agree,
                "comment": comment,
            }
        )
    return records


def spotcheck_to_label(row: dict[str, Any]) -> tuple[str, str, str]:
    """Convert a spotcheck row to (coarse_label, prior_label, prior_comment).

    Mapping rule:
      AGREE==Y + verdict==pass  → CLEAN
      AGREE==Y + verdict==fail  → PROBLEM (engine correctly identified failure)
      AGREE==N + verdict==pass  → PROBLEM (owner says it's actually bad)
      AGREE==N + verdict==fail  → CLEAN   (owner says engine was wrong; trace is OK)
      AGREE==?                  → UNKNOWN
      AGREE==Y + verdict other  → UNKNOWN
    """
    agree = row.get("agree", "?").upper()
    verdict = row.get("verdict", "").lower()
    comment = row.get("comment", "")

    if agree == "Y":
        if verdict == "pass":
            return ("CLEAN", "AGREE=Y (verdict=pass)", comment)
        if verdict == "fail":
            return ("PROBLEM", "AGREE=Y (verdict=fail)", comment)
        return ("UNKNOWN", f"AGREE=Y (verdict={verdict})", comment)

    if agree == "N":
        if verdict == "pass":
            # Owner disagreed with pass → it was actually problematic
            return ("PROBLEM", "AGREE=N (verdict=pass, owner says bad)", comment)
        if verdict == "fail":
            # Owner disagreed with fail → trace was actually OK
            return ("CLEAN", "AGREE=N (verdict=fail, owner says OK)", comment)
        return ("UNKNOWN", f"AGREE=N (verdict={verdict})", comment)

    return ("UNKNOWN", f"AGREE=? (verdict={verdict})", comment)


# (Phoenix GraphQL helpers removed in F1.5; DB is the source.)


# ── Detector note builders ─────────────────────────────────────────────────────


def _d1_note_for_step(
    step_index: int,
    findings_d1: list[Finding],
    operation: BusinessOperation | None,
) -> str | None:
    """Build a detector_note string for a D1-flagged step, or None."""
    for f in findings_d1:
        if step_index in f.affected_step_indices:
            ev = f.evidence
            tool = ev.get("tool", "?")
            window = ev.get("recovery_window", 10)
            required = ev.get("in_required_side_effects", False)
            req_note = " (required side-effect tool)" if required else ""
            return (
                f"D1: {tool}{req_note} errored, no same-command retry within "
                f"{window} steps."
            )
    return None


def _d2_note(findings_d2: list[Finding]) -> str | None:
    """Build a single D2 detector_note string for all D2 error steps, or None."""
    if not findings_d2:
        return None
    ev = findings_d2[0].evidence
    ratio = ev.get("struggle_ratio", "?")
    threshold = ev.get("threshold", "?")
    errors = ev.get("error_steps", 0)
    redundant = ev.get("redundant_steps", 0)
    rejected = ev.get("rejected_tool_calls", 0)
    side = ev.get("side_effect_successes", 0)
    return (
        f"D2: struggle_ratio={ratio} >= threshold={threshold} "
        f"(errors={errors}, redundant={redundant}, rejected={rejected}, "
        f"side_effect_successes={side})."
    )


# ── Question builders ─────────────────────────────────────────────────────────


def _step_list_str(indices: list[int]) -> str:
    if not indices:
        return "(none)"
    return ", ".join(str(i) for i in sorted(set(indices)))


def generate_disagreement_question(
    findings_d1: list[Finding],
    findings_d2: list[Finding],
    prior_label: str,
    prior_comment: str,
    operation_name: str,
) -> str:
    """Compose a detector-specific challenge question.

    For D1: names the erroring tool, affected steps, no-retry window.
    For D2: names the struggle ratio and breakdown.
    Combined if both fired.
    """
    parts: list[str] = []

    if findings_d1:
        d1_steps = sorted({i for f in findings_d1 for i in f.affected_step_indices})
        step_str = _step_list_str(d1_steps)
        n = len(d1_steps)
        parts.append(
            f"Kairos D1 flags {n} tool call(s) as errors that were never retried "
            f"with the same command (steps {step_str})."
        )

    if findings_d2:
        ev = findings_d2[0].evidence
        ratio = ev.get("struggle_ratio", "?")
        threshold = ev.get("threshold", "?")
        d2_steps = sorted({i for f in findings_d2 for i in f.affected_step_indices})
        step_str = _step_list_str(d2_steps) if d2_steps else "none individually flagged"
        parts.append(
            f"Kairos D2 flags this session with struggle_ratio={ratio} "
            f"(threshold={threshold}), error steps at {step_str}."
        )

    detector_text = " ".join(parts)
    prior_text = f"'{prior_label}'"
    if prior_comment.strip():
        prior_text += f" — \"{prior_comment.strip()[:120]}\""

    return (
        f"{detector_text} "
        f"You earlier labeled this trace {prior_text}. "
        f"For the flagged steps: was each a REAL unhandled failure, or did the "
        f"agent recover another way? Your call decides if the detector is right "
        f"or noisy."
    )


# ── Step list builder (disagreement-aware) ────────────────────────────────────


def build_disagreement_step_list(
    envelope: TraceEnvelope,
    findings_d1: list[Finding],
    findings_d2: list[Finding],
    operation: BusinessOperation | None,
    transcript_map: dict[int, TranscriptCall | None],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Build step list with is_evidence and detector_note on flagged steps.

    Reuses build_step_list for collapse logic, then overlays:
      - is_evidence=True on all D1+D2 affected steps
      - detector_note on those steps
    """
    # Collect all affected indices
    d1_indices: set[int] = {i for f in findings_d1 for i in f.affected_step_indices}
    d2_indices: set[int] = {i for f in findings_d2 for i in f.affected_step_indices}
    all_affected = d1_indices | d2_indices

    # Use the first affected step as the "evidence" anchor for collapse logic
    evidence_anchor = min(all_affected) if all_affected else None
    step_entries, collapsed_runs = build_step_list(
        envelope.steps, evidence_anchor, transcript_map
    )

    # Now overlay is_evidence and detector_note on all flagged steps
    for entry in step_entries:
        idx = entry["index"]
        if idx in all_affected:
            entry["is_evidence"] = True
            # Build note: D1 first, then D2 if also applicable
            notes: list[str] = []
            if idx in d1_indices:
                note = _d1_note_for_step(idx, findings_d1, operation)
                if note:
                    notes.append(note)
            if idx in d2_indices:
                note = _d2_note(findings_d2)
                if note:
                    notes.append(note)
            if notes:
                entry["detector_note"] = " | ".join(notes)

    return step_entries, collapsed_runs


# ── Disagreement detection ────────────────────────────────────────────────────


def find_matching_operation(
    envelope: TraceEnvelope,
    context: BusinessContext,
) -> BusinessOperation | None:
    """Return the primary operation for this trace (highest recall FULL > ATTEMPTED)."""
    best_op = None
    best_recall = -1.0
    best_full = False

    for op in context.operations:
        m = classify_membership(envelope, op)
        if m.kind == MembershipKind.NONE:
            continue
        is_full = m.kind == MembershipKind.FULL
        if best_op is None:
            best_op, best_recall, best_full = op, m.recall, is_full
            continue
        if is_full and not best_full:
            best_op, best_recall, best_full = op, m.recall, True
        elif is_full == best_full and m.recall > best_recall:
            best_op, best_recall = op, m.recall

    return best_op


# ── Priority scorer ───────────────────────────────────────────────────────────


def _priority_score(
    findings_d1: list[Finding],
    findings_d2: list[Finding],
) -> tuple[int, int, int]:
    """Return (has_d1_error, total_fires, has_d1_warning) for cap-priority sort.

    Higher tuples sort first (Python sorts ascending; caller inverts).
    """
    has_d1_error = int(any(f.severity == "error" for f in findings_d1))
    total_fires = len(findings_d1) + len(findings_d2)
    has_d1_warning = int(bool(findings_d1))
    return (has_d1_error, total_fires, has_d1_warning)


# ── Main ──────────────────────────────────────────────────────────────────────


def main() -> None:  # noqa: C901
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--dsn", default=DEFAULT_DSN, help="Postgres DSN (default: KAIROS_PG_DSN env var)")
    parser.add_argument("--context", default=DEFAULT_CONTEXT)
    parser.add_argument("--out", default=DEFAULT_OUT)
    parser.add_argument("--answers", default=DEFAULT_ANSWERS)
    parser.add_argument("--spotcheck", default=DEFAULT_SPOTCHECK)
    args = parser.parse_args()

    import os  # noqa: PLC0415

    dsn = args.dsn or os.environ.get("KAIROS_PG_DSN", "")
    if not dsn:
        print("ERROR: --dsn or KAIROS_PG_DSN env var required.", file=sys.stderr)
        sys.exit(1)

    context_path = Path(args.context)
    if not context_path.exists():
        print(f"ERROR: context file not found: {context_path}", file=sys.stderr)
        sys.exit(1)

    print(f"Loading context from {context_path} ...", file=sys.stderr)
    context = BusinessContext.from_yaml(str(context_path))

    # ── Collect labeled traces ────────────────────────────────────────────────
    # (a) answers.jsonl
    answers_path = Path(args.answers)
    answers = load_answers_jsonl(answers_path)
    print(f"Loaded {len(answers)} answers from {answers_path}", file=sys.stderr)

    # (b) spotcheck-day4.md
    spotcheck_path = Path(args.spotcheck)
    spotcheck_rows = parse_spotcheck_md(spotcheck_path)
    print(f"Loaded {len(spotcheck_rows)} rows from {spotcheck_path}", file=sys.stderr)

    # Build unified label map: trace_id → (coarse, prior_label, prior_comment)
    # answers.jsonl takes precedence if a trace appears in both sources.
    label_map: dict[str, tuple[str, str, str]] = {}

    for row in spotcheck_rows:
        tid = row["trace_id"]
        coarse, prior_label, comment = spotcheck_to_label(row)
        label_map[tid] = (coarse, prior_label, comment)

    for rec in answers:
        tid = rec["trace_id"]
        answer_text = rec.get("answer", "")
        verdict_shown = rec.get("verdict_shown", "")
        coarse = map_label_to_coarse(answer_text, verdict_shown)
        prior_label = f"answer={answer_text[:60]!r} (verdict_shown={verdict_shown})"
        comment = answer_text
        label_map[tid] = (coarse, prior_label, comment)

    all_trace_ids = list(label_map.keys())
    print(f"Total unique labeled traces: {len(all_trace_ids)}", file=sys.stderr)

    # ── Fetch envelopes + run detectors ──────────────────────────────────────
    candidates: list[dict[str, Any]] = []  # raw disagreement candidates before cap

    for trace_id in all_trace_ids:
        coarse, prior_label, prior_comment = label_map[trace_id]
        if coarse == "UNKNOWN":
            print(f"  SKIP {trace_id[:16]}: UNKNOWN label", file=sys.stderr)
            continue

        try:
            envelope = fetch_envelope_from_db(trace_id, dsn, enrich_hooks=False)
        except Exception as exc:  # noqa: BLE001
            print(f"  SKIP {trace_id[:16]}: fetch error: {exc}", file=sys.stderr)
            continue

        if not envelope.is_valid:
            print(f"  SKIP {trace_id[:16]}: invalid envelope", file=sys.stderr)
            continue

        operation = find_matching_operation(envelope, context)
        findings_d1 = detect_unrecovered_error(envelope, operation)
        findings_d2 = detect_struggle_ratio(envelope, operation)

        d1_fired = bool(findings_d1)
        d2_fired = bool(findings_d2)
        any_fired = d1_fired or d2_fired

        # Disagreement check
        if coarse == "CLEAN" and any_fired:
            kind = "fired_clean"
        elif coarse == "PROBLEM" and not any_fired:
            kind = "silent_problem"
        else:
            # Agreement — skip
            continue

        print(
            f"  DISAGREE {trace_id[:16]}: coarse={coarse}, "
            f"D1={d1_fired}({len(findings_d1)}), D2={d2_fired}({len(findings_d2)}), "
            f"kind={kind}",
            file=sys.stderr,
        )

        priority = _priority_score(findings_d1, findings_d2)
        candidates.append(
            {
                "trace_id": trace_id,
                "envelope": envelope,
                "operation": operation,
                "findings_d1": findings_d1,
                "findings_d2": findings_d2,
                "coarse": coarse,
                "prior_label": prior_label,
                "prior_comment": prior_comment,
                "kind": kind,
                "priority": priority,
            }
        )

    print(f"Total disagreements found: {len(candidates)}", file=sys.stderr)

    # ── Apply cap with priority sort ─────────────────────────────────────────
    candidates.sort(key=lambda c: c["priority"], reverse=True)

    if len(candidates) > MAX_QUEUE:
        dropped = candidates[MAX_QUEUE:]
        print(
            f"  CAP: keeping {MAX_QUEUE}, dropping {len(dropped)} lower-priority "
            f"disagreements: {[d['trace_id'][:16] for d in dropped]}",
            file=sys.stderr,
        )
        candidates = candidates[:MAX_QUEUE]

    # ── Build queue entries ───────────────────────────────────────────────────
    entries: list[dict[str, Any]] = []

    for cand in candidates:
        c_trace_id: str = cand["trace_id"]
        c_envelope = cand["envelope"]
        c_operation = cand["operation"]
        c_findings_d1 = cand["findings_d1"]
        c_findings_d2 = cand["findings_d2"]
        c_prior_label: str = cand["prior_label"]
        c_prior_comment: str = cand["prior_comment"]
        c_kind: str = cand["kind"]

        # Transcript alignment (for rich step digests)
        # F1.5: no Phoenix root-span meta; agent defaults to "unknown".
        meta = _empty_meta()
        session_id = _session_id_from_steps(c_envelope) or meta.get("session_id")
        start, end = _trace_window(c_envelope)
        transcript_map = align_trace_to_transcript(c_envelope.steps, session_id, start, end)

        step_entries, collapsed_runs = build_disagreement_step_list(
            c_envelope, c_findings_d1, c_findings_d2, c_operation, transcript_map
        )

        operation_name = c_operation.name if c_operation else "unmapped"
        question = generate_disagreement_question(
            c_findings_d1, c_findings_d2, c_prior_label, c_prior_comment, operation_name
        )

        # F1.5: Phoenix retired; no UI deep-link available.
        phoenix_url = ""

        entry: dict[str, Any] = {
            "trace_id": c_trace_id,
            "phoenix_url": phoenix_url,
            "agent": redact(meta.get("agent") or "unknown"),
            "primary_workflow": operation_name,
            "membership_kind": "full" if c_operation else "unmapped",
            "verdict": "non_computable",  # relabel queue — no verdict used
            "failure_reason": None,
            "evidence_step_index": None,
            "steps": step_entries,
            "collapsed_runs": collapsed_runs,
            "tokens": _aggregate_tokens(c_envelope),
            "question": question,
            # Disagreement-specific fields
            "prior_label": redact(c_prior_label),
            "prior_comment": redact(c_prior_comment[:500]),
            "disagreement_kind": c_kind,
            # Detectors that fired
            "d1_fired": bool(c_findings_d1),
            "d2_fired": bool(c_findings_d2),
        }
        entries.append(entry)

    print(f"Emitting {len(entries)} disagreement queue entries.", file=sys.stderr)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(entries, indent=2, default=str) + "\n")
    print(f"Wrote {out_path}", file=sys.stderr)
    print(str(out_path))


if __name__ == "__main__":
    main()
