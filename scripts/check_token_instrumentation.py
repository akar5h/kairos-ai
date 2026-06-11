"""check_token_instrumentation.py — Day 2 live exit criterion check.

Fetches ~50 recent trace IDs from live Phoenix (http://localhost:6006, project
"default"), loads their spans from the same paginated window, and reports:
  - Fraction of LLM steps with tokens_instrumented=True
  - Total and cache token sums for 3 sample traces

Exit criterion: ≥80% of LLM steps instrumented.
If below threshold, reports which attribute keys the misses have (to guide
ladder extension) — does not fudge results.

Usage:
    python scripts/check_token_instrumentation.py [--endpoint URL] [--project NAME] [--n-traces N]
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any


def _gql(endpoint: str, query: str) -> Any:
    """POST a GraphQL query and return parsed data field."""
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
    print(f"ERROR: project '{project_name}' not found. Available: {', '.join(available)}", file=sys.stderr)
    sys.exit(1)


_SPAN_FIELDS = "name context { traceId spanId } parentId attributes"


def _fetch_all_spans(endpoint: str, project_id: str, start_iso: str, end_iso: str) -> list[dict[str, Any]]:
    """Paginate all spans in the time window; return raw dicts."""
    spans: list[dict[str, Any]] = []
    cursor: str | None = None

    while True:
        after_clause = f', after: "{cursor}"' if cursor is not None else ""
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


def _parse_attributes(raw_attrs: Any) -> dict[str, Any]:
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
        if isinstance(parsed, list):
            return {item["key"]: item["value"] for item in parsed if "key" in item}
    if isinstance(raw_attrs, list):
        return {item["key"]: item["value"] for item in raw_attrs if "key" in item}
    return {}


def _is_llm_span(name: str, attrs: dict[str, Any]) -> bool:
    """Heuristic: is this span an LLM span?"""
    if name == "claude_code.llm_request":
        return True
    if attrs.get("gen_ai.system"):
        return True
    if str(attrs.get("openinference.span.kind", "")).upper() == "LLM":
        return True
    return False


# Token key ladder — mirrors USAGE_KEY_LADDER in genai_mapping.py
_LADDER: list[tuple[str, str, str]] = [
    ("input_tokens", "output_tokens", "cache_read_tokens"),
    ("gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens", "gen_ai.usage.cache_read_input_tokens"),
    ("llm.token_count.prompt", "llm.token_count.completion", "llm.token_count.prompt_details.cache_read"),
]


def _check_span_instrumented(attrs: dict[str, Any]) -> tuple[bool, str, int, int, int]:
    """Return (instrumented, rung_name, total_spend, cache_tokens, output_tokens)."""
    for k_in, k_out, k_cache in _LADDER:
        if k_in in attrs and k_out in attrs:
            in_tok = int(attrs[k_in] or 0)
            out_tok = int(attrs[k_out] or 0)
            cache_tok = int(attrs.get(k_cache) or 0)
            total = out_tok + max(in_tok - cache_tok, 0)
            return True, f"{k_in}/{k_out}", total, cache_tok, out_tok
    return False, "none", 0, 0, 0


def _get_miss_keys(attrs: dict[str, Any]) -> list[str]:
    """Return attribute keys that might be token-related but didn't match."""
    return [
        k
        for k, v in attrs.items()
        if isinstance(v, (int, float, str)) and ("token" in k.lower() or "usage" in k.lower())
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--endpoint", default="http://localhost:6006")
    parser.add_argument("--project", default="default")
    parser.add_argument("--n-traces", type=int, default=50)
    parser.add_argument("--hours", type=int, default=168)
    args = parser.parse_args()

    now = datetime.now(tz=UTC)
    start = now - timedelta(hours=args.hours)
    start_iso = start.isoformat().replace("+00:00", "Z")
    end_iso = now.isoformat().replace("+00:00", "Z")

    print(f"Connecting to Phoenix at {args.endpoint} / project={args.project} ...", file=sys.stderr)
    project_id = _resolve_project_id(args.endpoint, args.project)

    print(f"Fetching all spans in last {args.hours}h window ...", file=sys.stderr)
    all_spans = _fetch_all_spans(args.endpoint, project_id, start_iso, end_iso)
    print(f"  {len(all_spans)} spans fetched.", file=sys.stderr)

    if not all_spans:
        print("No spans found. Cannot verify instrumentation.", file=sys.stderr)
        sys.exit(1)

    # Group by traceId
    by_trace: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for span in all_spans:
        tid = span.get("context", {}).get("traceId", "")
        if tid:
            by_trace[tid].append(span)

    # Limit to first N trace IDs
    selected_trace_ids = list(by_trace.keys())[: args.n_traces]
    print(f"  Analyzing {len(selected_trace_ids)} traces ...", file=sys.stderr)

    total_llm_steps = 0
    instrumented_steps = 0
    miss_keys: dict[str, int] = {}
    sample_summaries: list[dict[str, Any]] = []

    for tid in selected_trace_ids:
        spans = by_trace[tid]
        trace_instrumented = 0
        trace_total = 0
        trace_cache = 0
        trace_llm_count = 0

        for span in spans:
            name = span.get("name", "")
            attrs = _parse_attributes(span.get("attributes"))
            if not _is_llm_span(name, attrs):
                continue

            trace_llm_count += 1
            total_llm_steps += 1

            is_inst, rung, total, cache_tok, _ = _check_span_instrumented(attrs)
            if is_inst:
                instrumented_steps += 1
                trace_instrumented += 1
                trace_total += total
                trace_cache += cache_tok
            else:
                for k in _get_miss_keys(attrs):
                    miss_keys[k] = miss_keys.get(k, 0) + 1

        if len(sample_summaries) < 3 and trace_llm_count > 0:
            sample_summaries.append(
                {
                    "trace_id": tid,
                    "llm_steps": trace_llm_count,
                    "instrumented": trace_instrumented,
                    "total_tokens": trace_total,
                    "cache_tokens": trace_cache,
                }
            )

    print()
    print("=" * 60)
    print("Day 2 Token Instrumentation Exit Criterion Report")
    print("=" * 60)
    print(f"Traces analyzed      : {len(selected_trace_ids)}")
    print(f"LLM steps found      : {total_llm_steps}")
    print(f"Instrumented steps   : {instrumented_steps}")

    frac = instrumented_steps / total_llm_steps if total_llm_steps else 0.0
    print(f"Instrumented fraction: {frac:.1%}")
    print()

    threshold = 0.80
    if frac >= threshold:
        print(f"PASS — ≥{threshold:.0%} instrumented. Day 2 exit criterion met.")
    else:
        print(f"FAIL — {frac:.1%} < {threshold:.0%}. Below threshold.")
        if miss_keys:
            print("\nKeys found on uninstrumented LLM spans (top 10 candidates):")
            for k, cnt in sorted(miss_keys.items(), key=lambda x: -x[1])[:10]:
                print(f"  {k!r:50s}  seen on {cnt} misses")
            print("\nAdd these keys to USAGE_KEY_LADDER in src/kairos/readers/genai_mapping.py")

    print()
    print("Sample trace summaries (first 3 with LLM steps):")
    for s in sample_summaries:
        print(
            f"  trace_id={s['trace_id'][:16]}…  "
            f"llm_steps={s['llm_steps']}  "
            f"instrumented={s['instrumented']}  "
            f"total_tokens={s['total_tokens']}  "
            f"cache_tokens={s['cache_tokens']}"
        )

    sys.exit(0 if frac >= threshold else 1)


if __name__ == "__main__":
    main()
