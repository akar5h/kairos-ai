"""build_queue.py — Build eval/review/queue.json for the trace-review app.

Fetches traces from Phoenix, runs the Kairos engine, and emits a structured
queue of trace entries with digested steps, token totals, and a pre-generated
question per verdict.

Usage:
    uv run eval/review/build_queue.py [--hours N] [--trace-ids id1,id2,...]

Options:
    --hours N           Look-back window in hours (default: 168)
    --trace-ids LIST    Comma-separated explicit trace IDs (skips Phoenix listing)
    --endpoint URL      Phoenix base URL (default: http://localhost:6006)
    --project NAME      Phoenix project name (default: default)
    --context PATH      Path to context.yaml (default: config/context.yaml)
    --out PATH          Output JSON path (default: eval/review/queue.json)

Output: eval/review/queue.json (regenerable, not committed).
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
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
from kairos.readers.phoenix import PhoenixReader  # noqa: E402
from kairos.taxonomy.business_context import BusinessContext  # noqa: E402

# Transcript aligner (same directory) — also the single source of redaction.
sys.path.insert(0, str(_HERE))
from transcript_align import (  # noqa: E402
    NO_MATCH,
    TranscriptCall,
    align_trace_to_transcript,
    call_args_digest,
    call_output_digest,
    redact,
)

if TYPE_CHECKING:
    from kairos.models.trace import Step, TraceEnvelope

DEFAULT_ENDPOINT = "http://localhost:6006"
DEFAULT_PROJECT = "default"
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


# ── Phoenix helpers ───────────────────────────────────────────────────────────


def _gql(endpoint: str, query: str) -> Any:
    url = endpoint.rstrip("/") + "/graphql"
    body = json.dumps({"query": query}).encode()
    req = urllib.request.Request(  # noqa: S310
        url,
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
            raw = resp.read()
    except urllib.error.URLError as exc:
        print(f"ERROR: cannot reach Phoenix at {url}: {exc}", file=sys.stderr)
        sys.exit(1)
    parsed = json.loads(raw)
    if "errors" in parsed:
        print(f"GraphQL errors: {parsed['errors']}", file=sys.stderr)
        sys.exit(1)
    return parsed.get("data", {})


def _resolve_project_id(endpoint: str, project_name: str) -> str:
    data = _gql(endpoint, "{ projects(first: 100) { edges { node { id name } } } }")
    edges = data.get("projects", {}).get("edges", [])
    for edge in edges:
        node = edge.get("node", {})
        if node.get("name") == project_name:
            return str(node["id"])
    available = [e["node"]["name"] for e in edges]
    print(f"ERROR: project '{project_name}' not found. Available: {', '.join(available)}", file=sys.stderr)
    sys.exit(1)


def _fetch_root_trace_ids(
    endpoint: str, project_id: str, start_iso: str, end_iso: str
) -> tuple[list[str], dict[str, dict[str, str | None]]]:
    """Paginate root spans; collect trace IDs + per-trace root-span meta."""
    trace_ids: list[str] = []
    meta_by_trace: dict[str, dict[str, str | None]] = {}
    seen: set[str] = set()
    cursor: str | None = None

    while True:
        after_clause = f', after: "{cursor}"' if cursor is not None else ""
        query = (
            f'{{ node(id: "{project_id}") {{ ... on Project {{ '
            f'spans(first: 100{after_clause}, rootSpansOnly: true, '
            f'timeRange: {{start: "{start_iso}", end: "{end_iso}"}}) {{ '
            f'pageInfo {{ hasNextPage endCursor }} '
            f'edges {{ node {{ context {{ traceId }} attributes }} }} '
            f'}} }} }} }}'
        )
        data = _gql(endpoint, query)
        spans_data = data.get("node", {}).get("spans")
        if not spans_data:
            break

        for edge in spans_data.get("edges", []):
            node = edge.get("node", {})
            tid = node.get("context", {}).get("traceId", "")
            if tid and tid not in seen:
                seen.add(tid)
                trace_ids.append(tid)
                meta_by_trace[tid] = _root_span_meta(node.get("attributes"))

        page_info = spans_data.get("pageInfo", {})
        if not page_info.get("hasNextPage") or not page_info.get("endCursor"):
            break
        cursor = page_info["endCursor"]

    return trace_ids, meta_by_trace


def _root_span_meta(raw_attributes: str | None) -> dict[str, str | None]:
    """Extract session_id / paperclip issue / agent from a root span."""
    meta: dict[str, str | None] = {"session_id": None, "issue": None, "agent": None}
    if not raw_attributes:
        return meta
    try:
        attrs = json.loads(raw_attributes)
    except (json.JSONDecodeError, TypeError):
        return meta
    if not isinstance(attrs, dict):
        return meta
    session = attrs.get("session")
    if isinstance(session, dict) and session.get("id"):
        meta["session_id"] = str(session["id"])
    paperclip = attrs.get("paperclip")
    if isinstance(paperclip, dict) and paperclip.get("issue"):
        meta["issue"] = str(paperclip["issue"])
    service = attrs.get("service")
    if isinstance(service, dict) and service.get("name"):
        meta["agent"] = str(service["name"])
    return meta


def _fetch_meta_for_ids(
    endpoint: str, project_id: str, trace_ids: list[str]
) -> dict[str, dict[str, str | None]]:
    """Fetch root span metadata for explicit trace IDs."""
    meta_by_trace: dict[str, dict[str, str | None]] = {}
    for tid in trace_ids:
        query = (
            f'{{ node(id: "{project_id}") {{ ... on Project {{ '
            f'spans(first: 10, rootSpansOnly: true, filterCondition: "trace_id == \\"{tid}\\"") {{ '
            f'edges {{ node {{ context {{ traceId }} attributes }} }} '
            f'}} }} }} }}'
        )
        try:
            data = _gql(endpoint, query)
            edges = data.get("node", {}).get("spans", {}).get("edges", [])
            for edge in edges:
                node = edge.get("node", {})
                found_tid = node.get("context", {}).get("traceId", "")
                if found_tid == tid:
                    meta_by_trace[tid] = _root_span_meta(node.get("attributes"))
                    break
        except SystemExit:
            pass
        if tid not in meta_by_trace:
            meta_by_trace[tid] = {"session_id": None, "issue": None, "agent": None}
    return meta_by_trace


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
    for step in steps:
        is_evidence = step.step_index == evidence_step_index
        step_entries.append(
            {
                "index": step.step_index,
                "tool": _tool_label(step),
                "status": step.status.value,
                "status_source": step.status_source.value,
                "args_digest": _step_args_digest(step, transcript_map),
                "output_digest": _step_output_digest(step, transcript_map),
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
    endpoint: str,
    project_node_id: str,
) -> dict[str, Any]:
    """Build one queue.json entry for a trace."""
    verdict = "non_computable"
    if result.computable:
        verdict = "pass" if result.outcome_pass else "fail"

    failure_reason = result.failure_reason.value if result.failure_reason else None
    evidence_step_index = result.evidence.step_index if result.evidence else None

    # Per-step transcript alignment: real args/outputs come from the session
    # JSONL (Phoenix spans carry none — F10). Ordinal per-tool-name matching
    # inside the trace's ±60s time window.
    session_id = _session_id_from_steps(envelope) or meta.get("session_id")
    start, end = _trace_window(envelope)
    transcript_map = align_trace_to_transcript(envelope.steps, session_id, start, end)

    step_entries, collapsed_runs = build_step_list(envelope.steps, evidence_step_index, transcript_map)

    phoenix_url = f"{endpoint.rstrip('/')}/projects/{project_node_id}/traces/{trace_id}"

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
        help="Comma-separated explicit trace IDs (skips Phoenix listing)",
    )
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT, help="Phoenix base URL (default: %(default)s)")
    parser.add_argument("--project", default=DEFAULT_PROJECT, help="Phoenix project name (default: %(default)s)")
    parser.add_argument("--context", default=DEFAULT_CONTEXT, help="Path to context.yaml (default: %(default)s)")
    parser.add_argument("--out", default=DEFAULT_OUT, help="Output JSON path (default: %(default)s)")
    args = parser.parse_args()

    context_path = Path(args.context)
    if not context_path.exists():
        print(f"ERROR: context file not found: {context_path}", file=sys.stderr)
        sys.exit(1)

    print(f"Loading context from {context_path} ...", file=sys.stderr)
    context = BusinessContext.from_yaml(str(context_path))

    print(f"Resolving Phoenix project '{args.project}' at {args.endpoint} ...", file=sys.stderr)
    project_id = _resolve_project_id(args.endpoint, args.project)

    # ── Resolve trace IDs ─────────────────────────────────────────────────────
    explicit_ids = [t.strip() for t in args.trace_ids.split(",") if t.strip()] if args.trace_ids else []

    if explicit_ids:
        trace_ids = explicit_ids
        print(f"Using {len(trace_ids)} explicit trace IDs.", file=sys.stderr)
        meta_by_trace = _fetch_meta_for_ids(args.endpoint, project_id, trace_ids)
    else:
        now = datetime.now(tz=UTC)
        start = now - timedelta(hours=args.hours)
        start_iso = start.isoformat().replace("+00:00", "Z")
        end_iso = now.isoformat().replace("+00:00", "Z")
        print(f"Fetching trace IDs for last {args.hours}h ...", file=sys.stderr)
        trace_ids, meta_by_trace = _fetch_root_trace_ids(args.endpoint, project_id, start_iso, end_iso)
        print(f"  {len(trace_ids)} trace IDs found.", file=sys.stderr)

    if not trace_ids:
        print("ERROR: no trace IDs to process.", file=sys.stderr)
        sys.exit(1)

    # ── Fetch envelopes and build queue entries ───────────────────────────────
    reader = PhoenixReader(endpoint=args.endpoint, project=args.project)
    operations = list(context.operations)

    entries: list[dict[str, Any]] = []
    errors = 0

    for i, trace_id in enumerate(trace_ids):
        if i % 25 == 0 and i > 0:
            print(f"  processed {i}/{len(trace_ids)} ...", file=sys.stderr)
        try:
            envelope = reader.fetch_envelope(trace_id)
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

        meta = meta_by_trace.get(trace_id, {"session_id": None, "issue": None, "agent": None})

        entry = build_entry(
            trace_id=trace_id,
            envelope=envelope,
            result=result,
            primary_workflow=primary,
            membership_kind=membership_kind,
            meta=meta,
            endpoint=args.endpoint,
            project_node_id=project_id,
        )
        entries.append(entry)

    print(f"Built {len(entries)} queue entries ({errors} skipped).", file=sys.stderr)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(entries, indent=2, default=str) + "\n")
    print(f"Wrote {out_path}", file=sys.stderr)
    print(str(out_path))


if __name__ == "__main__":
    main()
