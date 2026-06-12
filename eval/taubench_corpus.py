"""tau-bench pairing loader — Step 1 of Day 6 agreement harness.

Reads ablation bundles from ~/tau-agent/results/ablation_bundles/*.json,
normalizes each trajectory (``traj`` field in checkpoint_rows) into a
TraceEnvelope, and emits:

  eval/corpus/taubench/{trace_id}.json   — one TraceEnvelope per pair
  eval/corpus/taubench/labels.jsonl      — one label record per pair

Coverage report (pairs found / rows total, per bundle) is printed to
stdout and returned as a dict.

Pairing key: task_id (from checkpoint_rows[i].task_id) matches
task_index in the semantic_sessions/task-{id}-trial-0.json file.
The trace_id is synthesised as ``{bundle_stem}__{mode}__{task_id}__{trial}``.

Label semantics:
  reward == 1.0  → label "PASS"
  reward == 0.0  → label "FAIL"
  0 < reward < 1 → label "PARTIAL" (excluded from binary agreement)
"""

from __future__ import annotations

import hashlib
import json
import logging
import sys
from pathlib import Path
from typing import Any

from kairos.models.enums import (
    StepStatus,
    StepStatusSource,
    StepType,
    TerminalStatus,
)
from kairos.models.trace import Step, TraceEnvelope

logger = logging.getLogger(__name__)

# ── Paths ────────────────────────────────────────────────────────────────

BUNDLES_DIR = Path.home() / "tau-agent" / "results" / "ablation_bundles"
TAU_DATA_DIR = Path.home() / "tau-agent"
CORPUS_DIR = Path(__file__).parent / "corpus" / "taubench"

# Sentinel content in tau-bench user turns that end the conversation.
_STOP_SENTINEL = "###STOP###"

# Tau-bench tool names that carry no business information (internal bookkeeping).
_THINK_TOOLS = frozenset({"think", "calculate"})


# ── Trajectory → TraceEnvelope normaliser ───────────────────────────────

def _label(reward: float) -> str:
    if reward == 1.0:
        return "PASS"
    if reward == 0.0:
        return "FAIL"
    return "PARTIAL"


def _deterministic_id(bundle_stem: str, mode: str, task_id: int, trial: int) -> str:
    """Stable trace_id that encodes all four pairing keys."""
    raw = f"{bundle_stem}__{mode}__{task_id}__{trial}"
    return hashlib.md5(raw.encode()).hexdigest()  # noqa: S324 — non-security id


def _terminal_status(traj: list[dict[str, Any]]) -> TerminalStatus:
    """Infer terminal status from the conversation trajectory.

    Rules (in priority order):
      1. Any assistant turn that calls transfer_to_human_agents → HUMAN_ESCALATION
      2. Last user turn is ###STOP### → COMPLETED  (normal end)
      3. Last message is assistant (no STOP from user) → COMPLETED
      4. Fallback → COMPLETED (short/truncated traces; treat as best-effort)
    """
    for msg in traj:
        if msg.get("role") != "assistant":
            continue
        for tc in msg.get("tool_calls") or []:
            if tc.get("function", {}).get("name") == "transfer_to_human_agents":
                return TerminalStatus.HUMAN_ESCALATION

    user_turns = [m for m in traj if m.get("role") == "user"]
    if user_turns:
        last_content = user_turns[-1].get("content", "")
        if isinstance(last_content, str) and _STOP_SENTINEL in last_content:
            return TerminalStatus.COMPLETED

    return TerminalStatus.COMPLETED


def _normalize_traj(
    traj: list[dict[str, Any]],
    trace_id: str,
    task_id: int,
    task_instruction: str | None,
    trial: int,
) -> TraceEnvelope:
    """Convert a tau-bench trajectory into a TraceEnvelope.

    Mapping:
      - Each assistant tool_call becomes a Step(step_type=TOOL_CALL).
      - tool output comes from the following "tool" role message.
      - LLM turns (assistant without tool_calls) become Step(step_type=LLM).
      - System / user turns are skipped (captured as user_input / system_prompt).
      - tool_output is the JSON string returned by the corresponding tool message.
      - status_source is set to ADAPTER (structured signal: tool call completed).
      - All tool_output is the raw string from the "tool" message.
    """
    steps: list[Step] = []
    step_idx = 0

    # Extract preamble fields from the first messages.
    system_prompt: str | None = None
    user_input: str | None = None
    for msg in traj:
        role = msg.get("role")
        if role == "system" and system_prompt is None:
            system_prompt = msg.get("content")
        elif role == "user" and user_input is None:
            content = msg.get("content", "")
            if isinstance(content, str) and _STOP_SENTINEL not in content:
                user_input = content

    # Build a lookup: tool_call_id → tool_output (from "tool" messages).
    tool_output_by_id: dict[str, str] = {}
    for msg in traj:
        if msg.get("role") == "tool":
            call_id = msg.get("tool_call_id", "")
            content = msg.get("content", "")
            if call_id:
                tool_output_by_id[call_id] = content if isinstance(content, str) else json.dumps(content)

    # Walk the trajectory and emit steps for tool calls and LLM turns.
    for msg in traj:
        role = msg.get("role")
        if role != "assistant":
            continue

        tool_calls = msg.get("tool_calls") or []
        if tool_calls:
            for tc in tool_calls:
                fn = tc.get("function", {})
                name = fn.get("name", "")
                args_str = fn.get("arguments", "{}")
                call_id = tc.get("id", "")

                if isinstance(args_str, dict):
                    args = args_str
                elif args_str:
                    try:
                        parsed = json.loads(args_str)
                        args = parsed if isinstance(parsed, dict) else {"_raw": args_str}
                    except (json.JSONDecodeError, TypeError):
                        args = {"_raw": args_str}
                else:
                    args = {}

                tool_output = tool_output_by_id.get(call_id)

                steps.append(
                    Step(
                        step_index=step_idx,
                        step_type=StepType.TOOL_CALL,
                        tool_name=name,
                        tool_args=args,
                        tool_output=tool_output,
                        status=StepStatus.OK,
                        # Structured signal: the tool call completed and we have output.
                        # Tools without output (no matching tool message) keep NONE.
                        status_source=(
                            StepStatusSource.ADAPTER if tool_output is not None else StepStatusSource.NONE
                        ),
                    )
                )
                step_idx += 1
        else:
            # Pure LLM text turn.
            content = msg.get("content")
            if content:
                steps.append(
                    Step(
                        step_index=step_idx,
                        step_type=StepType.LLM,
                        llm_output=content if isinstance(content, str) else json.dumps(content),
                        status=StepStatus.OK,
                        status_source=StepStatusSource.NONE,
                    )
                )
                step_idx += 1

    terminal = _terminal_status(traj)

    return TraceEnvelope(
        trace_id=trace_id,
        source="tau_bench",
        source_trace_id=f"task-{task_id}-trial-{trial}",
        user_input=user_input or task_instruction,
        system_prompt=system_prompt,
        steps=steps,
        terminal_status=terminal,
        integrity="complete",
        session_id=f"task-{task_id}",
        metadata={
            "task_id": task_id,
            "trial": trial,
        },
    )


# ── Bundle loader ────────────────────────────────────────────────────────

def _load_bundle(bundle_path: Path) -> dict[str, Any]:
    with bundle_path.open() as f:
        return json.load(f)  # type: ignore[no-any-return]


def _task_instruction(run_dir_abs: Path, task_id: int, trial: int) -> str | None:
    """Pull task instruction from semantic_sessions if available."""
    session_path = run_dir_abs / "semantic_sessions" / f"task-{task_id}-trial-{trial}.json"
    if not session_path.exists():
        return None
    try:
        with session_path.open() as f:
            data = json.load(f)
        return data.get("task_instruction")  # type: ignore[no-any-return]
    except Exception:  # noqa: BLE001
        return None


def _env_from_bundle(bundle: dict[str, Any]) -> str:
    return bundle.get("args", {}).get("env", "unknown")  # type: ignore[no-any-return]


def _model_from_bundle(bundle: dict[str, Any]) -> str:
    run_model = bundle.get("args", {}).get("model") or "unknown"
    return run_model  # type: ignore[no-any-return]


# ── Main entry point ─────────────────────────────────────────────────────

def build_corpus(
    bundles_dir: Path = BUNDLES_DIR,
    corpus_dir: Path = CORPUS_DIR,
    *,
    verbose: bool = True,
) -> dict[str, dict[str, int]]:
    """Load all bundles, emit corpus traces + labels.jsonl, return coverage dict.

    Returns a dict keyed by bundle filename:
        {bundle: {"total_rows": N, "paired": M, "skipped": K, "partial": P}}

    Skipped rows are logged with their reason. Partial rows are included in
    labels.jsonl (with label=PARTIAL) but excluded from binary agreement.
    """
    corpus_dir.mkdir(parents=True, exist_ok=True)
    labels_path = corpus_dir / "labels.jsonl"

    bundle_paths = sorted(bundles_dir.glob("*.json"))
    if not bundle_paths:
        raise FileNotFoundError(f"No bundle files found in {bundles_dir}")

    coverage: dict[str, dict[str, int]] = {}
    written_trace_ids: set[str] = set()

    with labels_path.open("w") as labels_fh:
        for bundle_path in bundle_paths:
            bundle = _load_bundle(bundle_path)
            bundle_stem = bundle_path.stem
            env = _env_from_bundle(bundle)
            model = _model_from_bundle(bundle)
            modes: list[dict[str, Any]] = bundle.get("modes", [])

            bundle_stats: dict[str, int] = {
                "total_rows": 0,
                "paired": 0,
                "skipped": 0,
                "partial": 0,
            }

            for mode_entry in modes:
                mode_name: str = mode_entry.get("mode", "unknown")
                checkpoint_rows: list[dict[str, Any]] | None = mode_entry.get("checkpoint_rows")
                if not checkpoint_rows:
                    continue

                # Resolve kairos_run_dir for semantic_sessions lookup.
                kairos_run_dir: str | None = mode_entry.get("kairos_run_dir")
                run_dir_abs: Path | None = None
                if kairos_run_dir:
                    candidate = Path(kairos_run_dir)
                    if not candidate.is_absolute():
                        candidate = TAU_DATA_DIR / kairos_run_dir
                    if candidate.exists():
                        run_dir_abs = candidate

                for row in checkpoint_rows:
                    bundle_stats["total_rows"] += 1
                    task_id: int = row.get("task_id", -1)
                    reward: float = float(row.get("reward", -1.0))
                    trial: int = row.get("trial", 0)
                    traj: list[dict[str, Any]] = row.get("traj", [])

                    if not traj:
                        bundle_stats["skipped"] += 1
                        logger.warning(
                            "skip: empty traj bundle=%s mode=%s task_id=%s",
                            bundle_stem,
                            mode_name,
                            task_id,
                        )
                        continue

                    if reward < 0:
                        bundle_stats["skipped"] += 1
                        logger.warning(
                            "skip: invalid reward=%s bundle=%s mode=%s task_id=%s",
                            reward,
                            bundle_stem,
                            mode_name,
                            task_id,
                        )
                        continue

                    label = _label(reward)
                    if label == "PARTIAL":
                        bundle_stats["partial"] += 1

                    trace_id = _deterministic_id(bundle_stem, mode_name, task_id, trial)

                    # Retrieve task instruction (may be None — traj user_input is fallback).
                    instruction = _task_instruction(run_dir_abs, task_id, trial) if run_dir_abs else None

                    envelope = _normalize_traj(traj, trace_id, task_id, instruction, trial)

                    # Write trace JSON (overwrite if duplicate trace_id from different bundles
                    # is impossible by construction — the id encodes bundle_stem+mode).
                    trace_path = corpus_dir / f"{trace_id}.json"
                    with trace_path.open("w") as tf:
                        tf.write(envelope.model_dump_json(indent=2))

                    # Write label record.
                    label_record: dict[str, Any] = {
                        "trace_id": trace_id,
                        "task_id": task_id,
                        "trial": trial,
                        "env": env,
                        "model": model,
                        "reward": reward,
                        "label": label,
                        "bundle": bundle_path.name,
                        "mode": mode_name,
                    }
                    labels_fh.write(json.dumps(label_record) + "\n")

                    written_trace_ids.add(trace_id)
                    bundle_stats["paired"] += 1

            coverage[bundle_path.name] = bundle_stats

    # Print coverage report.
    if verbose:
        _print_coverage(coverage)

    return coverage


def _print_coverage(coverage: dict[str, dict[str, int]]) -> None:
    total_rows = sum(v["total_rows"] for v in coverage.values())
    total_paired = sum(v["paired"] for v in coverage.values())
    total_skipped = sum(v["skipped"] for v in coverage.values())
    total_partial = sum(v["partial"] for v in coverage.values())

    print("\n=== tau-bench corpus coverage ===")
    print(f"{'Bundle':<60} {'Rows':>5} {'Paired':>7} {'Skip':>5} {'Partial':>8}")
    print("-" * 90)
    for bundle_name, stats in sorted(coverage.items()):
        print(
            f"{bundle_name:<60} {stats['total_rows']:>5} {stats['paired']:>7} "
            f"{stats['skipped']:>5} {stats['partial']:>8}"
        )
    print("-" * 90)
    print(
        f"{'TOTAL':<60} {total_rows:>5} {total_paired:>7} "
        f"{total_skipped:>5} {total_partial:>8}"
    )
    binary_eligible = total_paired - total_partial
    print(f"\nBinary-eligible (non-partial): {binary_eligible}")
    print(f"Corpus dir: {CORPUS_DIR}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    build_corpus()
