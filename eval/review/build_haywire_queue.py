"""build_haywire_queue.py — Build the haywire-restart owner-review queue.

Sources the 41 traces with restart_count>0 by recomputing features directly
from the live Phoenix corpus (simpler + always fresh; avoids Postgres dependency
on the discovery_queue table which may not be populated in all environments).

For each trace the script:
  1. Fetches the PhoenixReader envelope (with transcript enrichment).
  2. Recomputes _find_session_restart_indices and _post_restart_rework_count.
  3. Builds a review-queue entry in the same schema that app.py reads, with:
     - A haywire-specific ``question`` naming the restart steps.
     - ``is_evidence=True`` on restart-boundary steps and up to N post-restart steps.
     - ``detector_note`` on each highlighted step ("← session restart here" or
       "post-restart: <tool> [<args_digest>] — may redo step <k>").
     - ``restart_count`` + ``post_restart_rework`` in the entry header.
  4. Collapses long identical-tool runs (existing collapse logic).
  5. Includes traces with missing transcripts — still included with
     restart-span-only context + a "(no transcript)" note.

Output: eval/review/haywire_queue.json (regenerable; gitignored).

The queue is consumed by app.py; set QUEUE_PATH=eval/review/haywire_queue.json
(env var) before launching the app, or pass --out to point to this file then
symlink / copy as needed.

Answers written via app.py persist to eval/review/answers.jsonl with
  ``class: "haywire"``  so they are distinguishable from prior label rounds.
Existing answers.jsonl lines are NEVER modified or removed (append-only).

Usage:
    uv run eval/review/build_haywire_queue.py [options]

Options:
    --endpoint URL   Phoenix base URL (default: http://localhost:6006)
    --project NAME   Phoenix project name (default: default)
    --context PATH   Path to context.yaml (default: config/context.yaml)
    --out PATH       Output path (default: eval/review/haywire_queue.json)
    --hours N        Look-back window in hours when listing traces (default: 720)

Security:
    Every args/output digest goes through redact() from transcript_align.
    The final JSON is secret-grepped before exit; any hit causes a non-zero
    exit and a clear error message — never silently leaks credentials.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

# ── path bootstrap ─────────────────────────────────────────────────────────────
_HERE = Path(__file__).parent
_REPO = _HERE.parent.parent
sys.path.insert(0, str(_REPO / "src"))

from kairos.detection.session_quality import _find_session_restart_indices  # noqa: E402
from kairos.loop.discover import _post_restart_rework_count  # noqa: E402
from kairos.models.enums import StepType  # noqa: E402
from kairos.readers.phoenix import PhoenixReader  # noqa: E402

# Reuse transcript alignment + redaction from sibling module
sys.path.insert(0, str(_HERE))
from build_queue import (  # type: ignore[import-untyped]  # noqa: E402
    _aggregate_tokens,
    _gql,
    _resolve_project_id,
    _root_span_meta,
    _session_id_from_steps,
    _step_args_digest,
    _step_output_digest,
    _tool_label,
    _trace_window,
)
from transcript_align import (  # type: ignore[import-untyped]  # noqa: E402
    NO_MATCH,
    TranscriptCall,
    align_trace_to_transcript,
    attach_reactions,
    build_conversation,
    call_full_input,
    call_full_output,
    entry_frame_fields,
    load_windowed_frame,
    redact,
)

if TYPE_CHECKING:
    from kairos.models.trace import Step, TraceEnvelope

# ── Constants ──────────────────────────────────────────────────────────────────
DEFAULT_ENDPOINT = "http://localhost:6006"
DEFAULT_PROJECT = "default"
DEFAULT_CONTEXT = str(_REPO / "config" / "context.yaml")
DEFAULT_OUT = str(_HERE / "haywire_queue.json")
DEFAULT_HOURS = 720

# How many pre-restart steps to always show (uncollapsed context).
PRE_RESTART_CONTEXT = 3
# How many post-restart steps to always show (evidence window).
POST_RESTART_SHOW = 8

# Collapse consecutive same-tool runs of this length or more — reuse threshold.
_COLLAPSE_RUN_MIN = 4

# ── Secret-grep patterns (from discover.py, re-used here) ─────────────────────
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
    hits: list[str] = []
    for pat in _SECRET_PATTERNS:
        if pat.search(text):
            hits.append(pat.pattern)
    return hits


# ── Phoenix helpers ────────────────────────────────────────────────────────────


def _fetch_all_trace_ids(
    endpoint: str, project_id: str, hours: int
) -> tuple[list[str], dict[str, dict[str, str | None]]]:
    """Paginate Phoenix root spans; collect trace IDs and root-span metadata."""
    now = datetime.now(tz=UTC)
    start = now - timedelta(hours=hours)
    start_iso = start.isoformat().replace("+00:00", "Z")
    end_iso = now.isoformat().replace("+00:00", "Z")

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


# ── Question builder ──────────────────────────────────────────────────────────


def generate_haywire_question(
    restart_indices: frozenset[int],
    restart_count: int,
    post_restart_rework: int,
) -> str:
    """Generate the haywire-restart review question for the owner.

    Names the restart step indices and asks the owner to judge whether the
    agent went haywire (re-did already-done work) or recovered sensibly.
    """
    sorted_indices = sorted(restart_indices)
    step_str = ", ".join(str(i) for i in sorted_indices)
    n_word = "time" if restart_count == 1 else "times"
    rework_note = ""
    if post_restart_rework > 0:
        rework_note = (
            f" Automated arg-match found {post_restart_rework} post-restart "
            f"step(s) whose args hash-match a pre-restart step (potential rework — "
            f"highlighted in orange)."
        )
    return (
        f"This session RESTARTED {restart_count} {n_word} "
        f"(at step{'s' if restart_count > 1 else ''} {step_str}). "
        f"After each restart, did the agent run HAYWIRE — re-do work already done, "
        f"re-read files it already read, or decide from scratch ignoring prior progress — "
        f"or did it recover sensibly and continue? Your call. "
        f"(Also: was the restart itself avoidable?){rework_note}"
    )


# ── Pre-restart arg digest set (for rework matching in notes) ─────────────────


def _build_pre_restart_arg_digest_map(
    steps: list[Step],
    first_restart_idx: int,
) -> dict[str, list[int]]:
    """Build {arg_digest: [step_indices_that_had_this_digest]} for pre-restart tool steps.

    Used to annotate post-restart steps with "may redo step K?" in detector_note.
    Uses the same digest approach as _post_restart_rework_count in discover.py
    but captures all matches (not just a count), so we can report "step K".
    """
    import hashlib

    digest_to_steps: dict[str, list[int]] = {}
    for step in steps:
        if step.step_type != StepType.TOOL_CALL or step.tool_name is None:
            continue
        if step.step_index >= first_restart_idx:
            break
        args = step.tool_args_normalized or step.tool_args
        if not args:
            continue
        key = f"{step.tool_name}:{sorted(args.items())}"
        digest = hashlib.sha256(key.encode()).hexdigest()[:16]
        digest_to_steps.setdefault(digest, []).append(step.step_index)
    return digest_to_steps


def _post_restart_arg_digest(step: Step) -> str | None:
    """Compute the 16-hex arg digest for a post-restart tool step, or None."""
    import hashlib

    if step.step_type != StepType.TOOL_CALL or not step.tool_name:
        return None
    args = step.tool_args_normalized or step.tool_args
    if not args:
        return None
    key = f"{step.tool_name}:{sorted(args.items())}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


# ── Step list builder (haywire-aware) ─────────────────────────────────────────


def build_haywire_step_list(
    envelope: TraceEnvelope,
    restart_indices: frozenset[int],
    transcript_map: dict[int, TranscriptCall | None],
    has_transcript: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Build step list with haywire-restart highlighting.

    Strategy:
      - Always show (uncollapsed) the last PRE_RESTART_CONTEXT steps before
        each restart boundary + the restart step itself + POST_RESTART_SHOW
        steps after the restart.
      - Collapse long consecutive same-tool runs OUTSIDE the highlighted windows.
      - Mark restart-boundary steps as is_evidence=True with detector_note
        "← session restart here".
      - Mark post-restart steps as is_evidence=True with detector_note
        "post-restart: <tool> <args_digest> — may redo step <K>?" when a
        pre-restart arg-digest match exists.

    Returns (step_entries, collapsed_runs).
    """
    steps = envelope.steps
    if not steps:
        return [], []

    sorted_restart = sorted(restart_indices)
    first_restart_idx = sorted_restart[0]

    # Build pre-restart digest map for rework-note generation.
    pre_digest_map = _build_pre_restart_arg_digest_map(steps, first_restart_idx)

    # Determine which step_indices should NEVER be collapsed (evidence windows).
    always_show: set[int] = set()
    # Also track which steps are restart boundaries vs post-restart.
    restart_boundary_step_indices: set[int] = set(restart_indices)
    post_restart_step_indices: set[int] = set()

    for r_idx in sorted_restart:
        # Pre-restart context: find the tool steps before r_idx.
        pre_steps = [s for s in steps if s.step_index < r_idx]
        pre_context = pre_steps[-PRE_RESTART_CONTEXT:] if pre_steps else []
        for s in pre_context:
            always_show.add(s.step_index)
        # The restart step itself.
        always_show.add(r_idx)
        # Post-restart steps.
        post_steps = [s for s in steps if s.step_index > r_idx]
        for s in post_steps[:POST_RESTART_SHOW]:
            always_show.add(s.step_index)
            if s.step_type == StepType.TOOL_CALL:
                post_restart_step_indices.add(s.step_index)

    # ── Collapse logic (adapted from build_queue.build_step_list) ─────────────

    from kairos.models.enums import StepStatus

    collapsed_indices: set[int] = set()
    collapsed_runs: list[dict[str, Any]] = []

    def _collapsible(s: Step, tool: str) -> bool:
        return (
            s.step_type == StepType.TOOL_CALL
            and s.status == StepStatus.OK
            and s.step_index not in always_show
            and _tool_label(s) == tool
        )

    i = 0
    while i < len(steps):
        step = steps[i]
        tool = _tool_label(step)
        if _collapsible(step, tool):
            run_positions = [i]
            j = i + 1
            while j < len(steps):
                if _collapsible(steps[j], tool):
                    run_positions.append(j)
                    j += 1
                    continue
                if steps[j].step_type == StepType.LLM:
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
                for k in range(run_positions[0], run_positions[-1] + 1):
                    collapsed_indices.add(steps[k].step_index)
                i = run_positions[-1] + 1
                continue
        i += 1

    # ── Build step entries ─────────────────────────────────────────────────────
    step_entries: list[dict[str, Any]] = []
    prev_ts: datetime | None = None
    for step in steps:
        idx = step.step_index
        is_evidence = idx in always_show
        args_digest = _step_args_digest(step, transcript_map)
        out_digest = _step_output_digest(step, transcript_map)

        call = transcript_map.get(idx) if step.step_type == StepType.TOOL_CALL else None
        time_gap_s: float | None = None
        if call is not None and call.ts is not None:
            if prev_ts is not None:
                time_gap_s = round((call.ts - prev_ts).total_seconds(), 1)
            prev_ts = call.ts

        # detector_note: restart boundary or post-restart
        detector_note: str | None = None
        if idx in restart_boundary_step_indices:
            no_tx = " (no transcript)" if not has_transcript else ""
            detector_note = f"← session restart here{no_tx}"
        elif idx in post_restart_step_indices:
            tool_name = _tool_label(step)
            short_args = args_digest[:60] if args_digest and args_digest != NO_MATCH else ""
            # Check for pre-restart arg-digest match.
            post_dig = _post_restart_arg_digest(step)
            rework_note = ""
            if post_dig and post_dig in pre_digest_map:
                prior_steps = pre_digest_map[post_dig]
                prior_str = ", ".join(str(k) for k in prior_steps[:3])
                rework_note = f" — re-doing step {prior_str}?"
            note_parts = [f"post-restart: {tool_name}"]
            if short_args:
                note_parts.append(f"[{short_args}]")
            note_parts_str = " ".join(note_parts)
            detector_note = f"{note_parts_str}{rework_note}"

        entry: dict[str, Any] = {
            "index": idx,
            "tool": _tool_label(step),
            "status": step.status.value,
            "status_source": step.status_source.value,
            "args_digest": args_digest,
            "output_digest": out_digest,
            # Full redacted transcript content (labeling view) — "" when no match.
            "input_full": call_full_input(call) if call is not None else "",
            "output_full": call_full_output(call) if call is not None else "",
            "is_error_struct": bool(call.is_error) if call is not None else False,
            "time_gap_s": time_gap_s,
            "is_evidence": is_evidence,
            "collapsed": idx in collapsed_indices,
        }
        if detector_note:
            entry["detector_note"] = detector_note

        step_entries.append(entry)

    return step_entries, collapsed_runs


# ── Main ──────────────────────────────────────────────────────────────────────


def main() -> None:  # noqa: C901
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--project", default=DEFAULT_PROJECT)
    parser.add_argument("--context", default=DEFAULT_CONTEXT)
    parser.add_argument("--out", default=DEFAULT_OUT)
    parser.add_argument(
        "--hours",
        type=int,
        default=DEFAULT_HOURS,
        help="Look-back window in hours for Phoenix listing (default: %(default)s)",
    )
    cli_args = parser.parse_args()

    print(f"Resolving Phoenix project '{cli_args.project}' at {cli_args.endpoint} ...", file=sys.stderr)
    project_id = _resolve_project_id(cli_args.endpoint, cli_args.project)

    print(f"Fetching trace IDs (last {cli_args.hours}h) ...", file=sys.stderr)
    all_trace_ids, meta_by_trace = _fetch_all_trace_ids(cli_args.endpoint, project_id, cli_args.hours)
    print(f"  {len(all_trace_ids)} total traces found.", file=sys.stderr)

    reader = PhoenixReader(endpoint=cli_args.endpoint, project=cli_args.project)

    # ── Pass 1: fetch envelopes and compute restart features ──────────────────
    # We need corpus-wide z-scores only for struggle/token/latency (not used in
    # haywire queue question), so we skip the full corpus feature pass.
    # We recompute only restart_count + post_restart_rework per trace.

    restart_traces: list[tuple[str, TraceEnvelope, int, int, frozenset[int], dict[str, str | None]]] = []
    fetch_errors = 0

    for i, trace_id in enumerate(all_trace_ids):
        if i % 25 == 0 and i > 0:
            print(f"  scanning {i}/{len(all_trace_ids)} ...", file=sys.stderr)
        try:
            envelope = reader.fetch_envelope(trace_id)
        except Exception as exc:  # noqa: BLE001
            print(f"  SKIP {trace_id[:16]}: fetch error: {exc}", file=sys.stderr)
            fetch_errors += 1
            continue

        if not envelope.is_valid:
            print(f"  SKIP {trace_id[:16]}: invalid envelope", file=sys.stderr)
            continue

        restart_indices = _find_session_restart_indices(envelope.steps)
        restart_count = len(restart_indices)
        if restart_count == 0:
            continue

        rework = _post_restart_rework_count(envelope.steps, restart_indices)
        meta = meta_by_trace.get(trace_id, {"session_id": None, "issue": None, "agent": None})
        restart_traces.append((trace_id, envelope, restart_count, rework, restart_indices, meta))

    print(
        f"  {len(restart_traces)} traces with restart_count>0 found "
        f"({fetch_errors} fetch errors).",
        file=sys.stderr,
    )

    # ── Pass 2: build queue entries ────────────────────────────────────────────
    entries: list[dict[str, Any]] = []

    for trace_id, envelope, restart_count, rework, restart_indices, meta in restart_traces:
        # Transcript alignment — real args/outputs from session JSONL.
        session_id = _session_id_from_steps(envelope) or meta.get("session_id")
        start, end = _trace_window(envelope)
        transcript_map = align_trace_to_transcript(envelope.steps, session_id, start, end)
        has_transcript = bool(transcript_map)

        step_entries, collapsed_runs = build_haywire_step_list(
            envelope, restart_indices, transcript_map, has_transcript
        )

        # Conversation frame + per-step reaction (restart boundaries get the
        # agent's stated next move — the key haywire-vs-recovered tell).
        frame, windowed_events = load_windowed_frame(session_id, start, end)
        frame_fields = entry_frame_fields(frame, windowed_events, start)
        attach_reactions(
            step_entries, transcript_map, windowed_events, restart_indices=restart_indices
        )

        question = generate_haywire_question(restart_indices, restart_count, rework)

        phoenix_url = (
            f"{cli_args.endpoint.rstrip('/')}/projects/{project_id}/traces/{trace_id}"
        )

        entry: dict[str, Any] = {
            "trace_id": trace_id,
            "phoenix_url": phoenix_url,
            "agent": redact(meta.get("agent") or "unknown"),
            "primary_workflow": "haywire_restart_review",
            "membership_kind": "restart",
            "verdict": "non_computable",
            "failure_reason": None,
            "evidence_step_index": min(restart_indices) if restart_indices else None,
            "steps": step_entries,
            "collapsed_runs": collapsed_runs,
            "tokens": _aggregate_tokens(envelope),
            "question": question,
            # Haywire-specific fields
            "restart_count": restart_count,
            "post_restart_rework": rework,
            "restart_step_indices": sorted(restart_indices),
            "has_transcript": has_transcript,
            # Conversation frame (labeling view).
            "task": frame_fields["task"],
            "surface": frame_fields["surface"],
            "user_events": frame_fields["user_events"],
            # Merged chronological conversation (primary reading surface in app.py).
            "conversation": build_conversation(session_id, start, end),
            # class field so answers.jsonl entries are distinguishable
            "class": "haywire",
        }
        entries.append(entry)

    print(f"Built {len(entries)} haywire queue entries.", file=sys.stderr)

    # ── Write output ──────────────────────────────────────────────────────────
    out_path = Path(cli_args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    output_json = json.dumps(entries, indent=2, default=str) + "\n"

    # ── Secret grep ───────────────────────────────────────────────────────────
    hits = _secret_grep_json(output_json)
    if hits:
        print(
            f"ERROR: secret-grep found {len(hits)} hit(s) in output: {hits}",
            file=sys.stderr,
        )
        print("  Output NOT written. Fix redaction before retrying.", file=sys.stderr)
        sys.exit(2)
    print("secret-grep: 0 hits (clean).", file=sys.stderr)

    out_path.write_text(output_json)
    print(f"Wrote {len(entries)} entries to {out_path}", file=sys.stderr)
    print(str(out_path))


if __name__ == "__main__":
    main()
