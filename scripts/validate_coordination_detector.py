"""Validate detect_coordination_context against the 24 owner-labeled coordination traces.

Usage:
    uv run scripts/validate_coordination_detector.py

Reads trace_ids from eval/review/answers.jsonl (class=="haywire"),
fetches each envelope from Phoenix, runs detect_coordination_context with
the config/context.yaml markers/tools, and reports the recall count.

Phoenix must be running at http://localhost:6006.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_REPO = Path(__file__).parent.parent
sys.path.insert(0, str(_REPO / "src"))

from kairos.detection.coordination import detect_coordination_context  # noqa: E402
from kairos.readers.phoenix import PhoenixReader  # noqa: E402
from kairos.taxonomy.business_context import BusinessContext  # noqa: E402

ANSWERS_PATH = _REPO / "eval" / "review" / "answers.jsonl"
CONTEXT_PATH = _REPO / "config" / "context.yaml"
PHOENIX_ENDPOINT = "http://localhost:6006"


def main() -> None:
    # Load context for markers/tools.
    ctx = BusinessContext.from_yaml(CONTEXT_PATH)
    markers = ctx.coordination_markers
    tools = ctx.coordination_tools
    print(f"Loaded context: markers={markers}, tools={tools}")

    # Read the 24 owner-labeled haywire trace_ids.
    with ANSWERS_PATH.open() as f:
        haywire_entries = [json.loads(l) for l in f if l.strip() and json.loads(l).get("class") == "haywire"]

    # Last-wins dedup by trace_id.
    seen: dict[str, dict] = {}
    for entry in haywire_entries:
        seen[entry["trace_id"]] = entry
    trace_ids = list(seen.keys())
    total = len(trace_ids)
    print(f"Owner-labeled coordination traces: {total}")

    reader = PhoenixReader(endpoint=PHOENIX_ENDPOINT, project="default")

    fired = 0
    misses: list[str] = []
    fetch_errors: list[str] = []

    for trace_id in trace_ids:
        try:
            envelope = reader.fetch_envelope(trace_id)
        except Exception as exc:  # noqa: BLE001
            print(f"  FETCH_ERROR {trace_id[:16]}: {exc}")
            fetch_errors.append(trace_id)
            continue

        finding = detect_coordination_context(envelope, markers=markers, tools=tools)
        if finding is not None:
            fired += 1
        else:
            misses.append(trace_id)
            print(f"  MISS {trace_id[:16]}: user_input={repr((envelope.user_input or '')[:100])}")

    print()
    print(f"Results: {fired}/{total} fired (recall {fired/total:.1%})")
    if misses:
        print(f"Misses ({len(misses)}):")
        for t in misses:
            print(f"  {t}")
    if fetch_errors:
        print(f"Fetch errors ({len(fetch_errors)}) — not counted in denominator:")
        for t in fetch_errors:
            print(f"  {t}")


if __name__ == "__main__":
    main()
