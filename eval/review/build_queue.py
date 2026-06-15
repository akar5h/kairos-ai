"""build_queue.py — Build eval/review/queue.json for the trace-review app.

Fetches traces from the Kairos DB (spans table), runs the Kairos engine, and
emits a structured queue of trace entries with digested steps, token totals,
and a pre-generated question per verdict.

F1.5: Trace discovery and envelope fetch now read from the DB (spans table)
via list_trace_ids + fetch_envelope_from_db. Phoenix is no longer required.

Usage:
    uv run eval/review/build_queue.py [--hours N] [--trace-ids id1,id2,...]

Options:
    --hours N           Look-back window in hours (default: 168)
    --trace-ids LIST    Comma-separated explicit trace IDs (skips DB listing)
    --dsn DSN           Postgres DSN (default: KAIROS_PG_DSN env var)
    --context PATH      Path to context.yaml (default: config/context.yaml)
    --out PATH          Output JSON path (default: eval/review/queue.json)

Output: eval/review/queue.json (regenerable, not committed).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

# ── path bootstrap ────────────────────────────────────────────────────────────
_HERE = Path(__file__).parent
_REPO = _HERE.parent.parent
sys.path.insert(0, str(_REPO / "src"))  # noqa: E402

# ruff: noqa: E402 — sys.path must be mutated before kairos imports
from kairos.analysis.outcome_metric import OutcomeResult, evaluate_outcome  # noqa: E402
from kairos.analysis.workflow_membership import MembershipKind  # noqa: E402
from kairos.engine.pipeline import classify_membership  # noqa: E402
from kairos.models.enums import StepStatus, StepType  # noqa: E402
from kairos.readers.db import fetch_envelope_from_db, list_trace_ids  # noqa: E402
from kairos.taxonomy.business_context import BusinessContext  # noqa: E402

# Transcript aligner (same directory) — also the single source of redaction.
sys.path.insert(0, str(_HERE))
from transcript_align import (  # noqa: E402
    NO_MATCH,
    TranscriptCall,
    align_trace_to_transcript,
    attach_reactions,
    build_conversation,
    call_args_digest,
    call_full_input,
    call_full_output,
    call_output_digest,
    entry_frame_fields,
    load_windowed_frame,
    redact,
)

if TYPE_CHECKING:
    from kairos.models.trace import Step, TraceEnvelope

DEFAULT_DSN = ""  # falls back to KAIROS_PG_DSN env var
DEFAULT_CONTEXT = str(_REPO / "config" / "context.yaml")
DEFAULT_OUT = str(_HERE / "queue.json")
DEFAULT_HOURS = 168

# Collapse consecutive same-tool runs of this length or more into one entry.
_COLLAPSE_RUN_MIN = 4

# Digest length limits
_ARGS_DIGEST_CHARS = 160
_OUTPUT_DIGEST_CHARS = 240


def _one_line(text: str, limit: int) -> str:
    """Collapse whitespace/newlines and truncate to limit chars."""
    flat = " ".join(text.split())
    return flat[:limit] + "…" if len(flat) > limit else flat


# ── DB-backed helpers (F1.5 — replaces Phoenix GraphQL) ──────────────────────


def _fetch_db_trace_ids(dsn: str, since_iso: str) -> list[str]:
    """List trace_ids from the DB that started at or after ``since_iso``."""
    return list_trace_ids(dsn, since=since_iso)


def _empty_meta() -> dict[str, str | None]:
    """Default per-trace meta when root-span metadata is unavailable (no Phoenix)."""
    return {"session_id": None, "issue": None, "agent": None}


# ── Workflow helpers (reused from export_spotcheck) ───────────────────────────


def _primary_workflow(envelope: TraceEnvelope, context: BusinessContext) -> str:
    """Return the primary workflow name or 'unmapped'."""
    best_name: str | None = None
    best_recall: float = -1.0
    best_full: bool = False

    for op in context.operations:
        m = classify_membership(envelope, op)
        if m.kind == MembershipKind.NONE:
            continue
        is_full = m.kind == MembershipKind.FULL
        if best_name is None:
            best_name = op.name
            best_recall = m.recall
            best_full = is_full
            continue
        if is_full and not best_full:
            best_name = op.name
            best_recall = m.recall
            best_full = True
        elif is_full == best_full:
            if m.recall > best_recall or (m.recall == best_recall and op.name < (best_name or "")):
                best_name = op.name
                best_recall = m.recall

    return best_name or "unmapped"


def _membership_kind_str(envelope: TraceEnvelope, context: BusinessContext) -> str:
    """Return 'full', 'attempted', or 'unmapped'."""
    for op in context.operations:
        m = classify_membership(envelope, op)
        if m.kind == MembershipKind.FULL:
            return "full"
        if m.kind == MembershipKind.ATTEMPTED:
            return "attempted"
    return "unmapped"


# ── Step digest helpers ───────────────────────────────────────────────────────


def _summarize_args(step: Step) -> str:
    """Redacted ≤160ch summary of span-carried tool args (usually empty — F10)."""
    if step.step_type != StepType.TOOL_CALL:
        if step.llm_input:
            return redact(_one_line(step.llm_input, _ARGS_DIGEST_CHARS))
        return ""
    tool_args = step.tool_args or step.tool_args_normalized
    if not tool_args:
        return ""
    for key in ("command", "file_path", "skill", "prompt", "pattern", "query"):
        if tool_args.get(key):
            return redact(_one_line(str(tool_args[key]), _ARGS_DIGEST_CHARS))
    return redact(_one_line(json.dumps(tool_args, default=str), _ARGS_DIGEST_CHARS))


def _summarize_output(step: Step) -> str:
    """Redacted ≤240ch summary of span-carried tool output (usually empty — F10)."""
    raw: str | None = None
    if step.step_type == StepType.TOOL_CALL:
        raw = step.tool_output
    elif step.step_type == StepType.LLM:
        raw = step.llm_output
    if not raw:
        return ""
    return redact(_one_line(str(raw), _OUTPUT_DIGEST_CHARS))


def _step_args_digest(step: Step, transcript_map: dict[int, TranscriptCall | None]) -> str:
    """args_digest for a step: transcript call first, span attrs second, NO_MATCH last.

    Tool steps without a transcript match get the explicit ``(no transcript
    match)`` marker — never a guess across tool names.
    """
    if step.step_type == StepType.TOOL_CALL:
        call = transcript_map.get(step.step_index)
        if call is not None:
            return call_args_digest(call, _ARGS_DIGEST_CHARS)
        return _summarize_args(step) or NO_MATCH
    return _summarize_args(step)


def _step_output_digest(step: Step, transcript_map: dict[int, TranscriptCall | None]) -> str:
    """output_digest for a step: transcript tool_result first, span output second."""
    if step.step_type == StepType.TOOL_CALL:
        call = transcript_map.get(step.step_index)
        if call is not None:
            return call_output_digest(call, _OUTPUT_DIGEST_CHARS)
    return _summarize_output(step)


def _tool_label(step: Step) -> str:
    """Return the display name for a step's tool/call type."""
    if step.step_type == StepType.TOOL_CALL and step.tool_name:
        return step.tool_name
    if step.step_type == StepType.LLM:
        return f"LLM({step.llm_model or 'unknown'})"
    if step.step_type == StepType.RETRIEVAL:
        return "Retrieval"
    return step.step_type.value


# ── Secret-grep gate (mirrors build_haywire_queue) ────────────────────────────
# Last-line defence: even after per-field redaction, grep the final JSON for
# credential shapes. Any hit aborts the write — never silently leak.

_SECRET_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"sk-[A-Za-z0-9-]{20,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/-]{8,}=*"),
    re.compile(r"\bghp_[A-Za-z0-9]{36,}"),
    re.compile(r"\bAKIA[A-Z0-9]{16}"),
    re.compile(r"-----BEGIN\s+[A-Z ]+PRIVATE KEY-----"),
    re.compile(r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}"),
]


def _secret_grep_json(text: str) -> list[str]:
    """Return matched secret-pattern strings found in *text*."""
    return [pat.pattern for pat in _SECRET_PATTERNS if pat.search(text)]


# ── Collapsed-run logic ───────────────────────────────────────────────────────


def build_step_list(
    steps: list[Step],
    evidence_step_index: int | None,
    transcript_map: dict[int, TranscriptCall | None] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Build the step list and collapsed_runs list from a TraceEnvelope's steps.

    ``transcript_map`` (step_index → TranscriptCall | None) supplies the real
    args/outputs from the session transcript; Phoenix spans carry none (F10).
    Tool steps with no transcript match get args_digest = "(no transcript match)".

    Run detection treats LLM steps as TRANSPARENT: live claude_code traces
    interleave an llm_request span between every tool call, so a strictly
    consecutive rule would never fire on real data (the owner's 47× Bash case).
    An LLM step between two same-tool collapsible steps joins the run; count
    counts tool steps only.

    Returns (step_entries, collapsed_runs).

    step_entries: one dict per step with keys:
        index, tool, status, status_source, args_digest, output_digest, is_evidence, collapsed

    collapsed_runs: entries for same-tool runs >= _COLLAPSE_RUN_MIN:
        {tool, count, first_index, last_index, first_args_digest, last_args_digest}
    """
    if transcript_map is None:
        transcript_map = {}
    collapsed_indices: set[int] = set()
    collapsed_runs: list[dict[str, Any]] = []

    def _collapsible(s: Step, tool: str) -> bool:
        return (
            s.step_type == StepType.TOOL_CALL
            and s.status == StepStatus.OK
            and s.step_index != evidence_step_index
            and _tool_label(s) == tool
        )

    i = 0
    while i < len(steps):
        step = steps[i]
        tool = _tool_label(step)
        if _collapsible(step, tool):
            run_positions = [i]  # positions of tool steps in the run
            j = i + 1
            while j < len(steps):
                if _collapsible(steps[j], tool):
                    run_positions.append(j)
                    j += 1
                    continue
                if steps[j].step_type == StepType.LLM:
                    # LLM steps are transparent IF a same-tool collapsible
                    # step follows the LLM block; otherwise the run ends here.
                    k = j + 1
                    while k < len(steps) and steps[k].step_type == StepType.LLM:
                        k += 1
                    if k < len(steps) and _collapsible(steps[k], tool):
                        j = k
                        continue
                break
            if len(run_positions) >= _COLLAPSE_RUN_MIN:
                first_step = steps[run_positions[0]]
                last_step = steps[run_positions[-1]]
                collapsed_runs.append(
                    {
                        "tool": tool,
                        "count": len(run_positions),
                        "first_index": first_step.step_index,
                        "last_index": last_step.step_index,
                        "first_args_digest": _step_args_digest(first_step, transcript_map),
                        "last_args_digest": _step_args_digest(last_step, transcript_map),
                    }
                )
                # Collapse everything from first to last tool step, including
                # interleaved LLM steps (skeleton spans, no content to show).
                for k in range(run_positions[0], run_positions[-1] + 1):
                    collapsed_indices.add(steps[k].step_index)
                i = run_positions[-1] + 1
                continue
        i += 1

    step_entries: list[dict[str, Any]] = []
    prev_ts: datetime | None = None
    for step in steps:
        is_evidence = step.step_index == evidence_step_index
        call = transcript_map.get(step.step_index) if step.step_type == StepType.TOOL_CALL else None
        # Time gap between this step and the previous timestamped step — a stall
        # (long gap) or a burst (near-zero) is itself a signal.
        time_gap_s: float | None = None
        if call is not None and call.ts is not None:
            if prev_ts is not None:
                time_gap_s = round((call.ts - prev_ts).total_seconds(), 1)
            prev_ts = call.ts
        step_entries.append(
            {
                "index": step.step_index,
                "tool": _tool_label(step),
                "status": step.status.value,
                "status_source": step.status_source.value,
                "args_digest": _step_args_digest(step, transcript_map),
                "output_digest": _step_output_digest(step, transcript_map),
                # Full redacted transcript content (labeling view) — "" when no match.
                "input_full": call_full_input(call) if call is not None else "",
                "output_full": call_full_output(call) if call is not None else "",
                # Structured failure flag (the tool LITERALLY returned is_error),
                # distinct from the inferred ``status``.
                "is_error_struct": bool(call.is_error) if call is not None else False,
                "time_gap_s": time_gap_s,
                "is_evidence": is_evidence,
                "collapsed": step.step_index in collapsed_indices,
            }
        )

    return step_entries, collapsed_runs


# ── Question generation ───────────────────────────────────────────────────────

_FAILURE_REASON_PLAIN: dict[str, str] = {
    "missing_side_effect": "the required write/side-effect tool was never called or every call failed",
    "side_effect_output_failed": "the side-effect tool ran but its output text signals failure",
    "critical_tool_error": "a key tool errored and never successfully recovered",
    "terminal_error": "the session ended with an error or timeout",
    "terminal_unknown": "the session terminal status could not be determined",
    "partial_trace": "spans are missing — the trace is incomplete",
}


def generate_question(verdict: str, failure_reason: str | None) -> str:
    """Generate the reviewer question for a trace entry."""
    if verdict == "fail":
        fr = failure_reason or "unknown"
        plain = _FAILURE_REASON_PLAIN.get(fr, fr)
        return (
            f"Engine says FAIL ({fr}): {plain}. "
            f"Do you agree? What actually happened here?"
        )
    if verdict == "pass":
        return (
            "Engine says PASS (contract completed). "
            "Anything bad here the engine can't see (loops, haywire restarts, wasted work)?"
        )
    # non_computable or escalated
    reason_str = failure_reason or "insufficient evidence"
    return f"Engine abstained ({reason_str}). What's your read?"


# ── Token aggregation ─────────────────────────────────────────────────────────


def _aggregate_tokens(envelope: TraceEnvelope) -> dict[str, int]:
    cache_read = sum(s.cache_read_tokens for s in envelope.steps)
    return {
        "input": envelope.total_input_tokens,
        "output": envelope.total_output_tokens,
        "cache_read": cache_read,
    }


# ── Session id + trace window helpers ─────────────────────────────────────────


def _session_id_from_steps(envelope: TraceEnvelope) -> str | None:
    """Extract ``session.id`` from tool-span attrs (it lives on tool spans, not roots)."""
    for step in envelope.steps:
        if step.attrs and step.attrs.get("session.id"):
            return str(step.attrs["session.id"])
    return None


def _trace_window(envelope: TraceEnvelope) -> tuple[datetime | None, datetime | None]:
    """Trace start/end from the envelope, falling back to step timestamps."""
    start = envelope.started_at
    end = envelope.ended_at
    if start is None or end is None:
        starts: list[datetime] = [s.started_at for s in envelope.steps if s.started_at is not None]
        ends: list[datetime] = []
        for s in envelope.steps:
            candidate = s.ended_at or s.started_at
            if candidate is not None:
                ends.append(candidate)
        if start is None and starts:
            start = min(starts)
        if end is None and ends:
            end = max(ends)
    return start, end


# ── Main queue-entry builder ──────────────────────────────────────────────────


def build_entry(
    trace_id: str,
    envelope: TraceEnvelope,
    result: OutcomeResult,
    primary_workflow: str,
    membership_kind: str,
    meta: dict[str, str | None],
) -> dict[str, Any]:
    """Build one queue.json entry for a trace."""
    verdict = "non_computable"
    if result.computable:
        verdict = "pass" if result.outcome_pass else "fail"

    failure_reason = result.failure_reason.value if result.failure_reason else None
    evidence_step_index = result.evidence.step_index if result.evidence else None

    # Per-step transcript alignment: real args/outputs come from the session
    # JSONL. Ordinal per-tool-name matching inside the trace's ±60s time window.
    session_id = _session_id_from_steps(envelope) or meta.get("session_id")
    start, end = _trace_window(envelope)
    transcript_map = align_trace_to_transcript(envelope.steps, session_id, start, end)

    step_entries, collapsed_runs = build_step_list(envelope.steps, evidence_step_index, transcript_map)

    # Conversation frame: task / surface / user interjections + per-step reaction.
    frame, windowed_events = load_windowed_frame(session_id, start, end)
    frame_fields = entry_frame_fields(frame, windowed_events, start)
    attach_reactions(step_entries, transcript_map, windowed_events)

    # F1.5: Phoenix is retired; no UI deep-link available. phoenix_url kept
    # as empty string so app.py (which renders it only when truthy) is unaffected.
    phoenix_url = ""

    return {
        "trace_id": trace_id,
        "phoenix_url": phoenix_url,
        "agent": redact(meta.get("agent") or "unknown"),
        "primary_workflow": primary_workflow,
        "membership_kind": membership_kind,
        "verdict": verdict,
        "failure_reason": failure_reason,
        "evidence_step_index": evidence_step_index,
        "steps": step_entries,
        "collapsed_runs": collapsed_runs,
        "tokens": _aggregate_tokens(envelope),
        "question": generate_question(verdict, failure_reason),
        # Conversation frame (labeling view).
        "task": frame_fields["task"],
        "surface": frame_fields["surface"],
        "user_events": frame_fields["user_events"],
        # Merged chronological conversation (primary reading surface in app.py).
        "conversation": build_conversation(session_id, start, end),
    }


# ── Main ──────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--hours",
        type=int,
        default=DEFAULT_HOURS,
        help="Look-back window in hours (default: %(default)s)",
    )
    parser.add_argument(
        "--trace-ids",
        default="",
        help="Comma-separated explicit trace IDs (skips DB listing)",
    )
    parser.add_argument("--dsn", default=DEFAULT_DSN, help="Postgres DSN (default: KAIROS_PG_DSN env var)")
    parser.add_argument("--context", default=DEFAULT_CONTEXT, help="Path to context.yaml (default: %(default)s)")
    parser.add_argument("--out", default=DEFAULT_OUT, help="Output JSON path (default: %(default)s)")
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

    # ── Resolve trace IDs from DB ─────────────────────────────────────────────
    explicit_ids = [t.strip() for t in args.trace_ids.split(",") if t.strip()] if args.trace_ids else []

    if explicit_ids:
        trace_ids = explicit_ids
        print(f"Using {len(trace_ids)} explicit trace IDs.", file=sys.stderr)
    else:
        now = datetime.now(tz=UTC)
        start = now - timedelta(hours=args.hours)
        start_iso = start.isoformat().replace("+00:00", "Z")
        print(f"Fetching trace IDs from DB for last {args.hours}h ...", file=sys.stderr)
        trace_ids = _fetch_db_trace_ids(dsn, start_iso)
        print(f"  {len(trace_ids)} trace IDs found.", file=sys.stderr)

    if not trace_ids:
        print("ERROR: no trace IDs to process.", file=sys.stderr)
        sys.exit(1)

    # ── Fetch envelopes from DB and build queue entries ───────────────────────
    operations = list(context.operations)

    entries: list[dict[str, Any]] = []
    errors = 0

    for i, trace_id in enumerate(trace_ids):
        if i % 25 == 0 and i > 0:
            print(f"  processed {i}/{len(trace_ids)} ...", file=sys.stderr)
        try:
            envelope = fetch_envelope_from_db(trace_id, dsn, enrich_hooks=False)
        except Exception as exc:  # noqa: BLE001
            print(f"  SKIP {trace_id[:16]}: fetch error: {exc}", file=sys.stderr)
            errors += 1
            continue

        if not envelope.is_valid:
            print(f"  SKIP {trace_id[:16]}: invalid envelope", file=sys.stderr)
            continue

        primary = _primary_workflow(envelope, context)
        membership_kind = _membership_kind_str(envelope, context)

        # Find the primary operation for outcome evaluation
        op = next((o for o in operations if o.name == primary), None)
        if op is None:
            # unmapped — no verdict
            result: OutcomeResult = OutcomeResult(
                trace_id=trace_id,
                outcome_pass=False,
                computable=False,
                reason="unmapped",
            )
        else:
            result = evaluate_outcome(envelope, op)

        # F1.5: no Phoenix root-span meta; agent defaults to "unknown".
        meta = _empty_meta()

        entry = build_entry(
            trace_id=trace_id,
            envelope=envelope,
            result=result,
            primary_workflow=primary,
            membership_kind=membership_kind,
            meta=meta,
        )
        entries.append(entry)

    print(f"Built {len(entries)} queue entries ({errors} skipped).", file=sys.stderr)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    output_json = json.dumps(entries, indent=2, default=str) + "\n"

    # Secret-grep gate — never write if a credential shape survived redaction.
    hits = _secret_grep_json(output_json)
    if hits:
        print(f"ERROR: secret-grep found {len(hits)} hit(s) in output: {hits}", file=sys.stderr)
        print("  Output NOT written. Fix redaction before retrying.", file=sys.stderr)
        sys.exit(2)
    print("secret-grep: 0 hits (clean).", file=sys.stderr)

    out_path.write_text(output_json)
    print(f"Wrote {out_path}", file=sys.stderr)
    print(str(out_path))


if __name__ == "__main__":
    main()
