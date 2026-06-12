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
import json
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
) -> list[str]:
    """Paginate root spans and collect unique trace IDs."""
    trace_ids: list[str] = []
    seen: set[str] = set()
    cursor: str | None = None

    while True:
        after_clause = f', after: "{cursor}"' if cursor is not None else ""
        query = (
            f'{{ node(id: "{project_id}") {{ ... on Project {{ '
            f'spans(first: 100{after_clause}, rootSpansOnly: true, '
            f'timeRange: {{start: "{start_iso}", end: "{end_iso}"}}) {{ '
            f'pageInfo {{ hasNextPage endCursor }} '
            f'edges {{ node {{ context {{ traceId }} }} }} '
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

        page_info = spans_data.get("pageInfo", {})
        if not page_info.get("hasNextPage") or not page_info.get("endCursor"):
            break
        cursor = page_info["endCursor"]

    return trace_ids


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


# ── Markdown table ────────────────────────────────────────────────────────────


def _phoenix_trace_url(trace_id: str, endpoint: str, project: str) -> str:
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
    return f"| [{short_id}]({url}) | {primary_workflow} | {verdict} | {fr} | {ev} | {ss} | {last_tools} |  |"


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

    for attempt_hours in [args.hours, args.hours * 2, args.hours * 4]:
        now = datetime.now(tz=UTC)
        start = now - timedelta(hours=attempt_hours)
        start_iso = start.isoformat().replace("+00:00", "Z")
        end_iso = now.isoformat().replace("+00:00", "Z")

        print(
            f"Fetching root trace IDs: last {attempt_hours}h ...", file=sys.stderr
        )
        trace_ids = _fetch_root_trace_ids(args.endpoint, project_id, start_iso, end_iso)
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
            # For unmapped traces, pick the first op for outcome evaluation purposes.
            op = operations[0] if operations else None
        else:
            op = next((o for o in operations if o.name == primary), operations[0] if operations else None)

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
        "| Trace | Primary workflow | Verdict | Failure reason | Evidence step | Status source | Last tools | AGREE? |"
    )
    lines.append(
        "|-------|-----------------|---------|---------------|--------------|--------------|-----------|--------|"
    )

    for (trace_id, primary, verdict, failure_reason, evidence_step, status_source, last_tools) in all_sampled:
        url = _phoenix_trace_url(trace_id, args.endpoint, args.project)
        lines.append(
            _md_row(trace_id, url, primary, verdict, failure_reason, evidence_step, status_source, last_tools)
        )

    lines.append(f"")
    lines.append(f"---")
    lines.append(f"")
    lines.append(f"*Generated by `scripts/export_spotcheck.py`. Re-run to refresh.*")

    out_path.write_text("\n".join(lines) + "\n")
    print(f"Wrote {len(all_sampled)} rows to {out_path}", file=sys.stderr)
    print(str(out_path))


if __name__ == "__main__":
    main()
