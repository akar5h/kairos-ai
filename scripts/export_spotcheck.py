"""export_spotcheck.py — Day 4 exit gate: stratified spot-check of live trace verdicts.

Fetches recent live traces from Phoenix, runs the kairos engine (PhoenixReader path)
with the supplied context.yaml, computes per-trace outcome, then stratified-samples:
  - 10 outcome-fail traces
  -  5 pass traces
  -  5 escalated traces

If a stratum is short (fewer than requested), fills from other strata and notes it
in the output header.

Writes docs/spotcheck-day4.md: one row per trace — phoenix link, primary workflow,
verdict, failure_reason, evidence step + status_source, last-tool summary, and an
empty "AGREE? (Y/N/?)" column.

Usage:
    uv run scripts/export_spotcheck.py [--endpoint URL] [--project NAME] [--hours N] \\
        [--context PATH] [--out PATH]

Default context: /Users/akarshgajbhiye/kairos-ai/config/context.yaml
"""

from __future__ import annotations

import argparse
import glob
import json
import re
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

# ── path bootstrap so the script works from any cwd ──────────────────────────
_HERE = Path(__file__).parent
_REPO = _HERE.parent
sys.path.insert(0, str(_REPO / "src"))

from kairos.analysis.outcome_metric import OutcomeResult, evaluate_outcome
from kairos.analysis.workflow_membership import MembershipKind
from kairos.engine.pipeline import classify_membership
from kairos.models.enums import FailureReason, TerminalStatus
from kairos.models.trace import TraceEnvelope
from kairos.readers.phoenix import PhoenixReader
from kairos.taxonomy.business_context import BusinessContext

DEFAULT_ENDPOINT = "http://localhost:6006"
DEFAULT_PROJECT = "default"
DEFAULT_CONTEXT = str(_REPO / "config" / "context.yaml")
DEFAULT_OUT = str(_REPO / "docs" / "spotcheck-day4.md")
DEFAULT_HOURS = 168  # 7 days

TARGET_FAIL = 10
TARGET_PASS = 5
TARGET_ESCALATED = 5


# ── Phoenix GraphQL helpers (reuse pattern from observed_tools.py) ───────────


def _gql(endpoint: str, query: str) -> Any:
    url = endpoint.rstrip("/") + "/graphql"
    body = json.dumps({"query": query}).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
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
            return node["id"]
    available = [e["node"]["name"] for e in edges]
    print(
        f"ERROR: project '{project_name}' not found. Available: {', '.join(available)}",
        file=sys.stderr,
    )
    sys.exit(1)


def _fetch_root_trace_ids(
    endpoint: str, project_id: str, start_iso: str, end_iso: str
) -> tuple[list[str], dict[str, dict[str, str | None]]]:
    """Paginate root spans; collect unique trace IDs + per-trace root-span meta.

    Root ``claude_code.interaction`` spans carry top-level ``session.id``,
    ``paperclip.{issue,agent_id}``, and ``service.name`` attributes — exactly
    what the transcript digest needs. One pass, no extra queries.

    Returns (trace_ids, meta_by_trace) where meta values are
    {"session_id", "issue", "agent"} (any may be None when absent).
    """
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
    """Extract session_id / paperclip issue / agent service name from a root span."""
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


# ── Membership + primary workflow ─────────────────────────────────────────────


def _primary_workflow(envelope: TraceEnvelope, context: BusinessContext) -> str:
    """Return the primary workflow name for this envelope, or 'unmapped'."""
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
        # FULL > ATTEMPTED; tie-break by recall then name
        if is_full and not best_full:
            best_name = op.name
            best_recall = m.recall
            best_full = True
        elif is_full == best_full:
            if m.recall > best_recall or (m.recall == best_recall and op.name < best_name):
                best_name = op.name
                best_recall = m.recall

    return best_name or "unmapped"


# ── Outcome → verdict label ───────────────────────────────────────────────────


def _verdict_label(result: OutcomeResult, envelope: TraceEnvelope) -> str:
    if not result.computable:
        return "non_computable"
    if result.outcome_pass:
        if envelope.terminal_status == TerminalStatus.HUMAN_ESCALATION:
            return "escalated"
        return "pass"
    return "fail"


# ── Last-tool summary ─────────────────────────────────────────────────────────


def _last_tool_summary(envelope: TraceEnvelope, n: int = 3) -> str:
    """Return the last N tool names in the trace as a comma-separated string."""
    seq = envelope.tool_sequence
    if not seq:
        return "(none)"
    tail = seq[-n:]
    return ", ".join(tail)


# ── Transcript digest (F10 workaround) ────────────────────────────────────────
# Phoenix spans are skeletons (tool_name + timing + success only — no args or
# outputs). A human cannot judge verdicts from the Phoenix UI alone, so the
# spotcheck doc carries a redacted transcript digest per sampled trace.

_TRANSCRIPT_GLOB = "/Users/akarshgajbhiye/.claude/projects/*/{session_id}.jsonl"
_DIGEST_TOOL_COUNT = 8
_DIGEST_ARG_CHARS = 110
_DIGEST_FINAL_TEXT_CHARS = 250

# Applied in order to ALL digest text before it is written (the doc gets
# committed). Aggressive by design: a false redaction is cheap, a leaked
# credential is not.
_REDACTION_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)(api[_-]?key|token|secret|password|authorization|bearer)\s*[=:]\s*\S+"), "[REDACTED]"),
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/-]{8,}=*"), "[REDACTED]"),  # "Bearer <tok>" without separator
    (re.compile(r"sk-[A-Za-z0-9-]{20,}"), "[REDACTED]"),
    (re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b"), "[REDACTED]"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[REDACTED]"),
    (re.compile(r"postgres(ql)?://\S+"), "[REDACTED]"),
]


def _redact(text: str) -> str:
    """Apply all redaction patterns. Idempotent; safe on already-clean text."""
    for pattern, replacement in _REDACTION_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _find_transcript(session_id: str) -> Path | None:
    """Locate the Claude Code session transcript for a session id, or None."""
    matches = glob.glob(_TRANSCRIPT_GLOB.format(session_id=glob.escape(session_id)))
    return Path(matches[0]) if matches else None


def _one_line(text: str, limit: int) -> str:
    """Collapse whitespace/newlines to single spaces and truncate."""
    flat = " ".join(text.split())
    return flat[:limit] + "…" if len(flat) > limit else flat


def _summarize_tool_input(tool_input: Any) -> str:
    """One-line arg summary: command > file_path > compact JSON of the input."""
    if not isinstance(tool_input, dict):
        return _one_line(str(tool_input), _DIGEST_ARG_CHARS)
    for key in ("command", "file_path", "skill", "prompt", "pattern", "query"):
        if tool_input.get(key):
            return _one_line(str(tool_input[key]), _DIGEST_ARG_CHARS)
    return _one_line(json.dumps(tool_input, default=str), _DIGEST_ARG_CHARS)


def _digest_transcript(path: Path) -> dict[str, Any]:
    """Parse a session jsonl into digest material.

    Same parse pattern as insight-report-0: each line carries
    ``message.content[]`` with {type: "tool_use", name, input} items,
    {type: "tool_result", is_error} items, and assistant {type: "text"} items.

    Returns {"tool_uses": [(name, arg_summary)], "error_count": int,
             "error_samples": [str], "final_text": str | None}.
    """
    tool_uses: list[tuple[str, str]] = []
    error_count = 0
    error_samples: list[str] = []
    final_text: str | None = None

    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict):
                continue
            msg = rec.get("message")
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for item in content:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "tool_use":
                    tool_uses.append((str(item.get("name", "?")), _summarize_tool_input(item.get("input"))))
                elif item.get("type") == "tool_result" and item.get("is_error"):
                    error_count += 1
                    if len(error_samples) < 2:
                        error_samples.append(_one_line(str(item.get("content", "")), _DIGEST_ARG_CHARS))
                elif item.get("type") == "text" and rec.get("type") == "assistant":
                    text = item.get("text", "")
                    if text.strip():
                        final_text = text

    return {
        "tool_uses": tool_uses[-_DIGEST_TOOL_COUNT:],
        "error_count": error_count,
        "error_samples": error_samples,
        "final_text": final_text,
    }


def _digest_block(
    trace_id: str,
    meta: dict[str, str | None],
) -> list[str]:
    """Render one '### digest-<short12>' block (≤25 lines) for a sampled trace.

    All free text passes through _redact before it lands in the doc.
    """
    short = trace_id[:12]
    lines: list[str] = [f"### digest-{short}", ""]

    issue = meta.get("issue") or "unknown"
    agent = meta.get("agent") or "unknown"
    session_id = meta.get("session_id")
    lines.append(f"**Issue:** {_redact(issue)} · **Agent:** {_redact(agent)}")

    if not session_id:
        lines.append("")
        lines.append("_No session.id on the root span — transcript not resolvable._")
        lines.append("")
        return lines

    transcript = _find_transcript(session_id)
    if transcript is None:
        lines.append(f"**Session:** `{session_id}`")
        lines.append("")
        lines.append("_Transcript not found in ~/.claude/projects/._")
        lines.append("")
        return lines

    digest = _digest_transcript(transcript)
    lines.append(f"**Session:** `{session_id}` (session-level digest — trace is one interaction within it)")
    lines.append("")
    lines.append(f"Last {len(digest['tool_uses'])} tool calls:")
    lines.append("")
    for name, arg in digest["tool_uses"]:
        lines.append(f"- `{name}`: {_redact(arg)}")
    lines.append("")
    if digest["error_count"]:
        first_err = _redact(digest["error_samples"][0]) if digest["error_samples"] else ""
        lines.append(f"Tool errors in session: {digest['error_count']}. First: {first_err}")
        lines.append("")
    if digest["final_text"]:
        lines.append(f"Final assistant message: {_redact(_one_line(digest['final_text'], _DIGEST_FINAL_TEXT_CHARS))}")
    else:
        lines.append("Final assistant message: _(none found)_")
    lines.append("")
    return lines


# ── Markdown table ────────────────────────────────────────────────────────────


def _phoenix_trace_url(trace_id: str, endpoint: str, project: str) -> str:
    """Build a Phoenix UI deep-link.

    ``project`` must be the project NODE id (base64 relay id, e.g.
    ``UHJvamVjdDox``), not the project name — Phoenix 15.x UI routes resolve
    the node id; name-based URLs make the projectLoaderQuery fail.
    """
    from urllib.parse import quote

    return f"{endpoint.rstrip('/')}/projects/{quote(project, safe='')}/traces/{quote(trace_id, safe='')}"


def _md_row(
    trace_id: str,
    url: str,
    primary_workflow: str,
    verdict: str,
    failure_reason: str | None,
    evidence_step: int | None,
    status_source: str | None,
    last_tools: str,
) -> str:
    short_id = trace_id[:16] + "…" if len(trace_id) > 16 else trace_id
    fr = failure_reason or ""
    ev = str(evidence_step) if evidence_step is not None else ""
    ss = status_source or ""
    digest_link = f"[↓ digest](#digest-{trace_id[:12]})"
    return (
        f"| [{short_id}]({url}) | {primary_workflow} | {verdict} "
        f"| {fr} | {ev} | {ss} | {last_tools} | {digest_link} |  |"
    )


# ── Main ──────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--endpoint", default=DEFAULT_ENDPOINT, help="Phoenix base URL (default: %(default)s)"
    )
    parser.add_argument(
        "--project", default=DEFAULT_PROJECT, help="Phoenix project name (default: %(default)s)"
    )
    parser.add_argument(
        "--hours",
        type=int,
        default=DEFAULT_HOURS,
        help="Initial look-back window in hours (default: %(default)s). Widened if < 20 qualifying traces.",
    )
    parser.add_argument(
        "--context", default=DEFAULT_CONTEXT, help="Path to context.yaml (default: %(default)s)"
    )
    parser.add_argument(
        "--out", default=DEFAULT_OUT, help="Output markdown path (default: %(default)s)"
    )
    args = parser.parse_args()

    context_path = Path(args.context)
    if not context_path.exists():
        print(f"ERROR: context file not found: {context_path}", file=sys.stderr)
        sys.exit(1)

    print(f"Loading context from {context_path} ...", file=sys.stderr)
    context = BusinessContext.from_yaml(str(context_path))

    # ── Fetch trace IDs from Phoenix ──────────────────────────────────────────
    print(f"Resolving Phoenix project '{args.project}' at {args.endpoint} ...", file=sys.stderr)
    project_id = _resolve_project_id(args.endpoint, args.project)

    hours_used = args.hours
    widened = False
    trace_ids: list[str] = []
    meta_by_trace: dict[str, dict[str, str | None]] = {}

    for attempt_hours in [args.hours, args.hours * 2, args.hours * 4]:
        now = datetime.now(tz=UTC)
        start = now - timedelta(hours=attempt_hours)
        start_iso = start.isoformat().replace("+00:00", "Z")
        end_iso = now.isoformat().replace("+00:00", "Z")

        print(
            f"Fetching root trace IDs: last {attempt_hours}h ...", file=sys.stderr
        )
        trace_ids, meta_by_trace = _fetch_root_trace_ids(args.endpoint, project_id, start_iso, end_iso)
        print(f"  {len(trace_ids)} trace IDs found.", file=sys.stderr)

        if len(trace_ids) >= 20:
            hours_used = attempt_hours
            widened = attempt_hours != args.hours
            break
        if attempt_hours == args.hours * 4:
            hours_used = attempt_hours
            widened = attempt_hours != args.hours
            print(
                f"WARNING: only {len(trace_ids)} traces found even in {attempt_hours}h window.",
                file=sys.stderr,
            )

    if not trace_ids:
        print("ERROR: no traces found in Phoenix. Is Phoenix running?", file=sys.stderr)
        sys.exit(1)

    # ── Fetch envelopes and evaluate outcomes ─────────────────────────────────
    reader = PhoenixReader(endpoint=args.endpoint, project=args.project)

    print(
        f"Fetching envelopes and evaluating outcomes for {len(trace_ids)} traces ...",
        file=sys.stderr,
    )

    # We need an operation to evaluate against — pick primary op per trace.
    operations = list(context.operations)

    rows_by_verdict: dict[str, list[tuple[str, str, str, str | None, int | None, str | None, str]]] = defaultdict(list)
    # Each value: (trace_id, primary_workflow, verdict, failure_reason, evidence_step, status_source, last_tools)
    unmapped_traces: list[tuple[str, str]] = []  # (trace_id, last_tools) — no verdict by definition

    errors = 0
    for i, trace_id in enumerate(trace_ids):
        if i % 50 == 0 and i > 0:
            print(f"  processed {i}/{len(trace_ids)} ...", file=sys.stderr)
        try:
            envelope = reader.fetch_envelope(trace_id)
        except Exception as exc:  # noqa: BLE001
            print(f"  SKIP {trace_id[:16]}: fetch error: {exc}", file=sys.stderr)
            errors += 1
            continue

        if not envelope.is_valid:
            continue

        primary = _primary_workflow(envelope, context)
        if primary == "unmapped":
            # A trace matching no operation has no side-effect contract to pass
            # or fail — it gets no verdict. Listed separately, never sampled
            # into the verdict strata.
            unmapped_traces.append((trace_id, _last_tool_summary(envelope)))
            continue
        op = next((o for o in operations if o.name == primary), None)
        if op is None:
            continue

        result = evaluate_outcome(envelope, op)
        verdict = _verdict_label(result, envelope)

        # Build evidence status_source lookup
        status_source: str | None = None
        if result.evidence.step_index is not None:
            for step in envelope.steps:
                if step.step_index == result.evidence.step_index:
                    status_source = str(step.status_source) if step.status_source is not None else None
                    break

        failure_reason_str = result.failure_reason.value if result.failure_reason is not None else None
        last_tools = _last_tool_summary(envelope)

        rows_by_verdict[verdict].append(
            (trace_id, primary, verdict, failure_reason_str, result.evidence.step_index, status_source, last_tools)
        )

    print(
        f"Done. Verdict counts: "
        + ", ".join(f"{k}={len(v)}" for k, v in sorted(rows_by_verdict.items())),
        file=sys.stderr,
    )
    if errors:
        print(f"  ({errors} traces skipped due to fetch errors)", file=sys.stderr)

    # ── Stratified sampling ───────────────────────────────────────────────────
    fail_pool = rows_by_verdict.get("fail", [])
    pass_pool = rows_by_verdict.get("pass", [])
    escalated_pool = rows_by_verdict.get("escalated", [])

    sampled_fail = fail_pool[:TARGET_FAIL]
    sampled_pass = pass_pool[:TARGET_PASS]
    sampled_escalated = escalated_pool[:TARGET_ESCALATED]

    strata_notes: list[str] = []
    total_needed = TARGET_FAIL + TARGET_PASS + TARGET_ESCALATED

    def _shortfall(stratum: str, actual: int, target: int) -> int:
        if actual < target:
            return target - actual
        return 0

    fail_short = _shortfall("fail", len(sampled_fail), TARGET_FAIL)
    pass_short = _shortfall("pass", len(sampled_pass), TARGET_PASS)
    esc_short = _shortfall("escalated", len(sampled_escalated), TARGET_ESCALATED)

    # Fill shortfalls from other strata
    if fail_short > 0:
        strata_notes.append(
            f"fail stratum short by {fail_short} (only {len(fail_pool)} available); filling from pass/escalated."
        )
        fill = (pass_pool[TARGET_PASS:] + escalated_pool[TARGET_ESCALATED:])[:fail_short]
        sampled_fail = sampled_fail + fill

    if pass_short > 0:
        strata_notes.append(
            f"pass stratum short by {pass_short} (only {len(pass_pool)} available); filling from fail/escalated."
        )
        fill = (fail_pool[TARGET_FAIL:] + escalated_pool[TARGET_ESCALATED:])[:pass_short]
        sampled_pass = sampled_pass + fill

    if esc_short > 0:
        strata_notes.append(
            f"escalated stratum short by {esc_short} (only {len(escalated_pool)} available); filling from fail/pass."
        )
        fill = (fail_pool[TARGET_FAIL:] + pass_pool[TARGET_PASS:])[:esc_short]
        sampled_escalated = sampled_escalated + fill

    all_sampled = sampled_fail + sampled_pass + sampled_escalated

    # ── Write markdown ────────────────────────────────────────────────────────
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []
    lines.append(f"# Spot-check — Day 4 Exit Gate")
    lines.append(f"")
    lines.append(f"Generated: {datetime.now(tz=UTC).strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append(f"Window: last {hours_used}h{' (WIDENED from default)' if widened else ''}")
    lines.append(f"Phoenix: {args.endpoint} / project: {args.project}")
    lines.append(f"Context: {args.context}")
    lines.append(f"")
    lines.append(f"## Stratum counts")
    lines.append(f"")
    lines.append(f"| Stratum | Available | Sampled | Target |")
    lines.append(f"|---------|-----------|---------|--------|")
    lines.append(f"| fail | {len(fail_pool)} | {len(sampled_fail)} | {TARGET_FAIL} |")
    lines.append(f"| pass | {len(pass_pool)} | {len(sampled_pass)} | {TARGET_PASS} |")
    lines.append(f"| escalated | {len(escalated_pool)} | {len(sampled_escalated)} | {TARGET_ESCALATED} |")
    lines.append(f"| **total** | {len(fail_pool)+len(pass_pool)+len(escalated_pool)} | **{len(all_sampled)}** | {total_needed} |")
    lines.append(f"")
    lines.append(
        f"**Unmapped traces (no operation matched → no verdict by definition): "
        f"{len(unmapped_traces)}.** Not part of the verdict strata. "
        f"These are predominantly Bash-only coordination sessions (see insight-report-0.md); "
        f"they become mappable after the Day 13 intervention."
    )
    lines.append(f"")

    if strata_notes:
        lines.append(f"**Stratum notes:**")
        for note in strata_notes:
            lines.append(f"- {note}")
        lines.append(f"")

    lines.append(f"## Trace rows")
    lines.append(f"")
    lines.append(f"Owner: fill the **AGREE?** column (Y = engine correct, N = engine wrong, ? = unsure).")
    lines.append(f"")
    lines.append(
        f"**Judge from the digest** (linked per row, below the table); the Phoenix link is "
        f"supplementary — spans carry no args/outputs (F10 emitter limitation), so the "
        f"Phoenix UI alone cannot support a verdict judgment."
    )
    lines.append(f"")
    lines.append(
        "| Trace | Primary workflow | Verdict | Failure reason | Evidence step "
        "| Status source | Last tools | Digest | AGREE? |"
    )
    lines.append(
        "|-------|-----------------|---------|---------------|--------------"
        "|--------------|-----------|--------|--------|"
    )

    for (trace_id, primary, verdict, failure_reason, evidence_step, status_source, last_tools) in all_sampled:
        url = _phoenix_trace_url(trace_id, args.endpoint, project_id)
        lines.append(
            _md_row(trace_id, url, primary, verdict, failure_reason, evidence_step, status_source, last_tools)
        )

    # ── Transcript digests ────────────────────────────────────────────────────
    lines.append(f"")
    lines.append(f"## Transcript digests")
    lines.append(f"")
    lines.append(
        f"One block per sampled trace. Source: Claude Code session transcripts "
        f"(`~/.claude/projects/*/<session_id>.jsonl`), resolved via the root span's "
        f"`session.id` attribute. All text is redacted before writing."
    )
    lines.append(f"")

    transcripts_found = 0
    transcripts_missing = 0
    for (trace_id, _primary, _verdict, _fr, _ev, _ss, _lt) in all_sampled:
        meta = meta_by_trace.get(trace_id, {"session_id": None, "issue": None, "agent": None})
        block = _digest_block(trace_id, meta)
        if any("Transcript not found" in ln or "not resolvable" in ln for ln in block):
            transcripts_missing += 1
        else:
            transcripts_found += 1
        lines.extend(block)

    lines.append(f"---")
    lines.append(f"")
    lines.append(f"*Generated by `scripts/export_spotcheck.py`. Re-run to refresh.*")

    out_path.write_text("\n".join(lines) + "\n")
    print(
        f"Transcript digests: {transcripts_found} found, {transcripts_missing} missing "
        f"(of {len(all_sampled)} sampled).",
        file=sys.stderr,
    )
    print(f"Wrote {len(all_sampled)} rows to {out_path}", file=sys.stderr)
    print(str(out_path))


if __name__ == "__main__":
    main()
