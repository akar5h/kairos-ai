"""observed_tools.py — micro-lint: query Phoenix GraphQL for span tool names.

Answers two questions the Day 5 context.yaml rewrite needs:
  1. Which tool names actually exist as spans in the live window?
  2. How ubiquitous (trace_base_rate) is each tool?

With --context <yaml path>, also flags declared tools
(expected_tools / required_side_effect_tools) never seen in the live window.

Tool-name extraction reuses _resolve_tool_name from genai_mapping.py —
that function is the single source of truth; importing it avoids the class
of bug where two extraction implementations diverge.

Usage:
    python scripts/observed_tools.py [--endpoint URL] [--project NAME] [--hours N] [--context YAML]
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any
import urllib.request
import urllib.error
import json

# ── Tool-name extraction: import from genai_mapping.py ──────────────────────
# The engine's single source of truth for resolving tool_name from a span's
# attributes. We pass a dict (not ReadableSpan) so we construct a minimal
# duck-typed proxy.
try:
    from kairos.readers.genai_mapping import _resolve_tool_name as _engine_resolve_tool_name  # type: ignore[import]

    def extract_tool_name(span_name: str, attrs: dict[str, Any]) -> str | None:
        """Delegate to genai_mapping._resolve_tool_name via a minimal span proxy."""

        class _SpanProxy:
            name = span_name
            attributes = attrs

        return _engine_resolve_tool_name(_SpanProxy(), attrs)  # type: ignore[arg-type]

except ImportError:
    # TODO: unify — if the import above fails, this fallback replicates the
    # same precedence as genai_mapping._resolve_tool_name (source of truth:
    # src/kairos/readers/genai_mapping.py, _resolve_tool_name, lines ~449-460).
    def extract_tool_name(span_name: str, attrs: dict[str, Any]) -> str | None:  # type: ignore[misc]
        """Fallback replication of genai_mapping._resolve_tool_name.

        Precedence: tool.name → gen_ai.tool.name → tool_name (top-level attr)
        → span name prefix "tool.".
        """
        name = attrs.get("tool.name") or attrs.get("gen_ai.tool.name") or attrs.get("tool_name")
        if isinstance(name, str) and name:
            return name
        if span_name.startswith("tool."):
            stripped = span_name[len("tool."):]
            if stripped:
                return stripped
        return None


# ── GraphQL helpers ──────────────────────────────────────────────────────────

def _gql(endpoint: str, query: str) -> Any:
    """POST a GraphQL query to endpoint, return parsed JSON data field."""
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
    """Return the Phoenix node ID for the named project."""
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


# ── Span fetch with cursor-based pagination ──────────────────────────────────
# Phoenix 7.x rejects query variable declarations — all values must be inlined.
# This mirrors the working pattern in kairos-analysis-views/src/worker.ts:46-94.

_SPAN_FIELDS = "name context { traceId } attributes"


def _fetch_all_spans(endpoint: str, project_id: str, start_iso: str, end_iso: str) -> list[dict[str, Any]]:
    """Paginate all spans in the time window; return raw dicts."""
    spans: list[dict[str, Any]] = []
    cursor: str | None = None

    while True:
        after_clause = f', after: "{cursor}"' if cursor is not None else ""
        # rootSpansOnly: false — we want ALL spans (including tool spans).
        query = (
            f'{{ node(id: "{project_id}") {{ ... on Project {{ '
            f'spans(first: 100{after_clause}, rootSpansOnly: false, '
            f'timeRange: {{start: "{start_iso}", end: "{end_iso}"}}) {{ '
            f'pageInfo {{ hasNextPage endCursor }} '
            f'edges {{ node {{ {_SPAN_FIELDS} }} }} '
            f'}} }} }} }}'
        )
        data = _gql(endpoint, query)
        project_data = data.get("node", {}).get("spans")
        if not project_data:
            break

        for edge in project_data.get("edges", []):
            node = edge.get("node")
            if node:
                spans.append(node)

        page_info = project_data.get("pageInfo", {})
        if not page_info.get("hasNextPage") or not page_info.get("endCursor"):
            break
        cursor = page_info["endCursor"]

    return spans


# ── Attribute parsing ────────────────────────────────────────────────────────

def _parse_attributes(raw_attrs: Any) -> dict[str, Any]:
    """Phoenix returns attributes as JSON string or list of {key,value} or dict."""
    if raw_attrs is None:
        return {}
    if isinstance(raw_attrs, dict):
        return raw_attrs
    if isinstance(raw_attrs, str):
        try:
            parsed = json.loads(raw_attrs)
        except (json.JSONDecodeError, ValueError):
            return {}
        if isinstance(parsed, dict):
            return parsed
        # Phoenix also serialises as [{key, value}, ...]
        if isinstance(parsed, list):
            return {item["key"]: item["value"] for item in parsed if "key" in item}
    if isinstance(raw_attrs, list):
        return {item["key"]: item["value"] for item in raw_attrs if "key" in item}
    return {}


# ── Context YAML parsing ─────────────────────────────────────────────────────

def _load_declared_tools(context_path: str) -> set[str]:
    """Collect all expected_tools + required_side_effect_tools from context.yaml."""
    try:
        import yaml  # type: ignore[import]
    except ImportError:
        print("WARNING: PyYAML not available; skipping --context check.", file=sys.stderr)
        return set()

    with open(context_path) as f:
        doc = yaml.safe_load(f)

    declared: set[str] = set()
    for op in doc.get("operations", []):
        for t in op.get("expected_tools", []):
            declared.add(str(t))
        for t in op.get("required_side_effect_tools", []):
            declared.add(str(t))
    return declared


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--endpoint", default="http://localhost:6006", help="Phoenix base URL (default: %(default)s)")
    parser.add_argument("--project", default="default", help="Phoenix project name (default: %(default)s)")
    parser.add_argument("--hours", type=int, default=168, help="Look-back window in hours (default: %(default)s)")
    parser.add_argument("--context", default=None, help="Path to context.yaml to flag never-observed declared tools")
    args = parser.parse_args()

    now = datetime.now(tz=UTC)
    start = now - timedelta(hours=args.hours)
    start_iso = start.isoformat().replace("+00:00", "Z")
    end_iso = now.isoformat().replace("+00:00", "Z")

    print(f"Fetching spans from {args.endpoint} / project={args.project} / last {args.hours}h ...", file=sys.stderr)

    project_id = _resolve_project_id(args.endpoint, args.project)
    raw_spans = _fetch_all_spans(args.endpoint, project_id, start_iso, end_iso)

    print(f"  {len(raw_spans)} spans fetched.", file=sys.stderr)

    # Count tool names → set of trace IDs
    tool_traces: dict[str, set[str]] = defaultdict(set)
    all_traces: set[str] = set()

    for span in raw_spans:
        trace_id = span.get("context", {}).get("traceId", "")
        if trace_id:
            all_traces.add(trace_id)

        span_name: str = span.get("name") or ""
        attrs = _parse_attributes(span.get("attributes"))
        tool_name = extract_tool_name(span_name, attrs)
        if tool_name and trace_id:
            tool_traces[tool_name].add(trace_id)

    total_traces = len(all_traces)

    # Build sorted table: most common first
    rows = sorted(
        ((name, len(tids), len(tids) / total_traces if total_traces else 0.0) for name, tids in tool_traces.items()),
        key=lambda r: r[1],
        reverse=True,
    )

    # Print table
    col_w = max((len(r[0]) for r in rows), default=4)
    col_w = max(col_w, 4)
    header = f"{'tool':<{col_w}}  {'span_count':>10}  {'trace_base_rate':>15}"
    sep = "-" * len(header)
    print(header)
    print(sep)
    for tool, span_count, base_rate in rows:
        print(f"{tool:<{col_w}}  {span_count:>10}  {base_rate:>15.4f}")
    print(sep)
    print(f"{'TOTAL traces in window':<{col_w}}  {'':>10}  {total_traces:>15}")

    # Flag declared-but-never-observed tools
    if args.context:
        declared = _load_declared_tools(args.context)
        observed_names = {r[0] for r in rows}
        never_observed = sorted(declared - observed_names)
        if never_observed:
            print("\nDeclared tools NEVER observed in this window:")
            for t in never_observed:
                print(f"  - {t}")
        else:
            print("\nAll declared tools were observed at least once.")


if __name__ == "__main__":
    main()
