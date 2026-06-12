"""honest_snapshot.py — Day 5 Honest Snapshot + Day 14 delta tool.

Generates docs/honest-snapshot-{N}.md with baseline metrics from live Phoenix.
Rerunnable: invoke again on Day 14 to produce a delta-comparable snapshot.

Metrics per the spec template:
  - traces analyzed (7d window from live Phoenix), unmapped count+%
  - per workflow: outcome_rate, human_escalation_rate, failure_reason histogram,
    deduped finding count, token waste total, full/attempted counts,
    mean memberships per trace (global), top 5 costliest traces with links
  - config sha + engine version header

Usage:
    uv run scripts/honest_snapshot.py [--endpoint URL] [--project NAME]
        [--hours N] [--context PATH] [--out PATH] [--snapshot-num N]

Defaults:
    endpoint: http://localhost:6006
    project: default
    hours: 168 (7 days)
    context: config/context.yaml
    out: docs/honest-snapshot-1.md
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import sys
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

_HERE = Path(__file__).parent
_REPO = _HERE.parent
sys.path.insert(0, str(_REPO / "src"))

from kairos.analysis.outcome_metric import OutcomeResult, evaluate_outcome
from kairos.analysis.workflow_membership import MembershipKind
from kairos.engine.pipeline import classify_membership, map_envelope_multilabel
from kairos.models.enums import FailureReason, TerminalStatus
from kairos.models.trace import TraceEnvelope
from kairos.readers.phoenix import PhoenixReader
from kairos.taxonomy.business_context import BusinessContext

DEFAULT_ENDPOINT = "http://localhost:6006"
DEFAULT_PROJECT = "default"
DEFAULT_CONTEXT = str(_REPO / "config" / "context.yaml")
DEFAULT_OUT = str(_REPO / "docs" / "honest-snapshot-1.md")
DEFAULT_HOURS = 168


# ── Phoenix GraphQL helpers ───────────────────────────────────────────────────


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


# ── Primary workflow ──────────────────────────────────────────────────────────


def _primary_workflow_name(envelope: TraceEnvelope, context: BusinessContext) -> str | None:
    """Return primary workflow name using the same tiebreak as pipeline._primary_workflow."""
    memberships = map_envelope_multilabel(envelope, list(context.operations))
    if not memberships:
        return None

    full = [m for m in memberships if m.kind == MembershipKind.FULL]
    candidates = full if full else memberships

    _priority_rank: dict[str, int] = {"high": 2, "medium": 1, "low": 0}
    op_by_name = {op.name: op for op in context.operations}

    def _key(m: Any) -> tuple[float, int, str]:
        op = op_by_name.get(m.operation_name)
        rank = _priority_rank.get(op.priority, 1) if op else 1
        return (m.recall, rank, m.operation_name)

    sorted_c = sorted(candidates, key=lambda m: (-_key(m)[0], -_key(m)[1], _key(m)[2]))
    return sorted_c[0].operation_name


# ── Phoenix trace URL ─────────────────────────────────────────────────────────


def _phoenix_url(trace_id: str, endpoint: str, project: str) -> str:
    from urllib.parse import quote
    return f"{endpoint.rstrip('/')}/projects/{quote(project, safe='')}/traces/{quote(trace_id, safe='')}"


# ── Main ──────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--project", default=DEFAULT_PROJECT)
    parser.add_argument("--hours", type=int, default=DEFAULT_HOURS)
    parser.add_argument("--context", default=DEFAULT_CONTEXT)
    parser.add_argument("--out", default=DEFAULT_OUT)
    parser.add_argument("--snapshot-num", type=int, default=1, help="Snapshot number for filename/header")
    args = parser.parse_args()

    context_path = Path(args.context)
    if not context_path.exists():
        print(f"ERROR: context file not found: {context_path}", file=sys.stderr)
        sys.exit(1)

    # Engine version and context SHA.
    try:
        engine_version = importlib.metadata.version("kairos-ai")
    except importlib.metadata.PackageNotFoundError:
        engine_version = "dev"

    raw_context = context_path.read_bytes()
    context_sha = hashlib.sha256(raw_context).hexdigest()

    print(f"Loading context from {context_path} ...", file=sys.stderr)
    context = BusinessContext.from_yaml(str(context_path))
    operations = list(context.operations)

    print(f"Resolving Phoenix project '{args.project}' at {args.endpoint} ...", file=sys.stderr)
    project_id = _resolve_project_id(args.endpoint, args.project)

    now = datetime.now(tz=UTC)
    start = now - timedelta(hours=args.hours)
    start_iso = start.isoformat().replace("+00:00", "Z")
    end_iso = now.isoformat().replace("+00:00", "Z")

    print(f"Fetching root trace IDs: last {args.hours}h ...", file=sys.stderr)
    trace_ids = _fetch_root_trace_ids(args.endpoint, project_id, start_iso, end_iso)
    print(f"  {len(trace_ids)} trace IDs found.", file=sys.stderr)

    if not trace_ids:
        print("ERROR: no traces found in Phoenix. Is Phoenix running?", file=sys.stderr)
        sys.exit(1)

    reader = PhoenixReader(endpoint=args.endpoint, project=args.project)

    # Per-workflow accumulators.
    op_names = [op.name for op in operations]
    wf_full: dict[str, int] = dict.fromkeys(op_names, 0)
    wf_attempted: dict[str, int] = dict.fromkeys(op_names, 0)
    wf_passed: dict[str, int] = dict.fromkeys(op_names, 0)
    wf_computable: dict[str, int] = dict.fromkeys(op_names, 0)
    wf_escalated: dict[str, int] = dict.fromkeys(op_names, 0)
    wf_failure_reasons: dict[str, Counter] = {n: Counter() for n in op_names}
    wf_findings: dict[str, set[str]] = {n: set() for n in op_names}  # deduped by trace_id
    wf_token_waste: dict[str, int] = dict.fromkeys(op_names, 0)
    wf_member_trace_ids: dict[str, set[str]] = {n: set() for n in op_names}

    # Global: memberships per trace, for mean computation.
    memberships_per_trace: list[int] = []

    # Costliest traces: (total_tokens, trace_id, primary_workflow).
    costliest: list[tuple[int, str, str]] = []

    unmapped_count = 0
    analyzed_count = 0
    errors = 0

    print(f"Processing {len(trace_ids)} traces ...", file=sys.stderr)

    for i, trace_id in enumerate(trace_ids):
        if i % 50 == 0 and i > 0:
            print(f"  {i}/{len(trace_ids)} ...", file=sys.stderr)
        try:
            envelope = reader.fetch_envelope(trace_id)
        except Exception as exc:  # noqa: BLE001
            print(f"  SKIP {trace_id[:16]}: {exc}", file=sys.stderr)
            errors += 1
            continue
        if not envelope.is_valid:
            continue

        analyzed_count += 1

        # Memberships.
        memberships = map_envelope_multilabel(envelope, operations)
        memberships_per_trace.append(len(memberships))

        primary_name = _primary_workflow_name(envelope, context)

        if not memberships:
            unmapped_count += 1
        else:
            for m in memberships:
                wf_member_trace_ids[m.operation_name].add(trace_id)
                if m.kind == MembershipKind.FULL:
                    wf_full[m.operation_name] += 1
                elif m.kind == MembershipKind.ATTEMPTED:
                    wf_attempted[m.operation_name] += 1

        # Outcome: evaluate against primary op only.
        if primary_name:
            op = next((o for o in operations if o.name == primary_name), None)
            if op:
                result: OutcomeResult = evaluate_outcome(envelope, op)
                if result.computable:
                    wf_computable[primary_name] += 1
                    if result.outcome_pass:
                        wf_passed[primary_name] += 1
                        if envelope.terminal_status == TerminalStatus.HUMAN_ESCALATION:
                            wf_escalated[primary_name] += 1
                    else:
                        if result.failure_reason is not None:
                            wf_failure_reasons[primary_name][result.failure_reason.value] += 1

        # Token totals for costliest traces.
        total_tokens = sum(
            getattr(step, "total_tokens", 0) or 0 for step in envelope.steps
        )
        if total_tokens > 0 and primary_name:
            costliest.append((total_tokens, trace_id, primary_name or "unmapped"))

    print(
        f"Done. analyzed={analyzed_count}, unmapped={unmapped_count}, errors={errors}",
        file=sys.stderr,
    )

    # ── Compute derived metrics ───────────────────────────────────────────────

    unmapped_pct = (unmapped_count / analyzed_count * 100) if analyzed_count else 0.0
    mean_memberships = (sum(memberships_per_trace) / len(memberships_per_trace)) if memberships_per_trace else 0.0

    costliest_sorted = sorted(costliest, key=lambda x: -x[0])[:5]

    # ── Build markdown ────────────────────────────────────────────────────────

    lines: list[str] = []
    lines.append(f"# Honest Snapshot {args.snapshot_num}")
    lines.append(f"")
    lines.append(
        f"**Date:** {now.strftime('%Y-%m-%d %H:%M UTC')}  "
        f"**Config SHA:** `{context_sha[:8]}`  "
        f"**Engine:** `{engine_version}`"
    )
    lines.append(f"")
    lines.append(f"**Window:** last {args.hours}h  **Phoenix:** {args.endpoint} / project: `{args.project}`")
    lines.append(f"")
    lines.append(
        f"| Metric | Value |"
    )
    lines.append("|---|---|")
    lines.append(f"| traces analyzed | {analyzed_count} |")
    lines.append(f"| unmapped | {unmapped_count} ({unmapped_pct:.1f}%) |")
    lines.append(f"| mean memberships/trace (global) | {mean_memberships:.2f} |")
    lines.append(f"")

    # Exit-bar check.
    if mean_memberships > 1.5:
        lines.append(
            f"> WARNING: mean memberships/trace = {mean_memberships:.2f} > 1.5 (exit bar). "
            "Dedup may not be working correctly."
        )
        lines.append(f"")
    else:
        lines.append(
            f"> Exit bar: mean memberships/trace = {mean_memberships:.2f} ≤ 1.5 ✓"
        )
        lines.append(f"")

    lines.append(f"## Per-workflow breakdown")
    lines.append(f"")

    for op in operations:
        n = op.name
        full = wf_full[n]
        attempted = wf_attempted[n]
        total_members = full + attempted
        computable = wf_computable[n]
        passed = wf_passed[n]
        escalated = wf_escalated[n]

        outcome_rate: float | None = (passed / computable) if computable > 0 else None
        esc_rate: float | None = (escalated / computable) if computable > 0 else None

        lines.append(f"### {n}")
        lines.append(f"")
        lines.append(f"| Field | Value |")
        lines.append(f"|---|---|")
        lines.append(f"| full | {full} |")
        lines.append(f"| attempted | {attempted} |")
        lines.append(f"| total members | {total_members} |")
        lines.append(f"| computable | {computable} |")
        lines.append(f"| passed | {passed} |")
        outcome_str = f"{outcome_rate:.2f}" if outcome_rate is not None else "n/a"
        esc_str = f"{esc_rate:.2f}" if esc_rate is not None else "n/a"
        lines.append(f"| outcome_rate | {outcome_str} |")
        lines.append(f"| human_escalation_rate | {esc_str} |")
        lines.append(f"| deduped finding count | 0 (detection runs in full pipeline) |")
        lines.append(f"| token waste total | n/a (requires full pipeline) |")
        lines.append(f"")

        fr_counts = wf_failure_reasons[n]
        if fr_counts:
            lines.append(f"**Failure reasons:**")
            lines.append(f"")
            lines.append(f"| reason | count |")
            lines.append(f"|---|---|")
            for reason, count in fr_counts.most_common():
                lines.append(f"| {reason} | {count} |")
            lines.append(f"")

    lines.append(f"## Top 5 costliest traces (by total tokens)")
    lines.append(f"")
    if costliest_sorted:
        lines.append(f"| Tokens | Workflow | Link |")
        lines.append(f"|---|---|---|")
        for tokens, tid, wf_name in costliest_sorted:
            url = _phoenix_url(tid, args.endpoint, args.project)
            lines.append(f"| {tokens:,} | {wf_name} | [{tid[:16]}…]({url}) |")
    else:
        lines.append(
            "_Token data not available (live traces have no token instrumentation in this window)._"
        )
    lines.append(f"")
    lines.append(f"---")
    lines.append(f"")
    lines.append(f"*Generated by `scripts/honest_snapshot.py`. Re-run to refresh.*")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n")
    print(f"Wrote snapshot to {out_path}", file=sys.stderr)
    print(str(out_path))


if __name__ == "__main__":
    main()
