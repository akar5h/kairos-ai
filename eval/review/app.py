"""app.py — Trace review app (Streamlit).

Launch: uv run streamlit run eval/review/app.py

One trace per screen. Keyboard-first, QA style.
Reviewer reads one screen, types a free-text answer, presses Save & Next.
Answers persist immediately to eval/review/answers.jsonl (append-only).
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import streamlit as st

# ── Paths ──────────────────────────────────────────────────────────────────────
_HERE = Path(__file__).parent

# Support QUEUE_PATH env var for alternate queues (e.g. haywire_queue.json).
# Relative paths are resolved relative to the repo root (two levels up from
# eval/review/).  Absolute paths are used as-is.
_QUEUE_PATH_ENV = os.environ.get("QUEUE_PATH", "")
if _QUEUE_PATH_ENV:
    _qp = Path(_QUEUE_PATH_ENV)
    QUEUE_PATH = _qp if _qp.is_absolute() else (_HERE.parent.parent / _qp)
else:
    QUEUE_PATH = _HERE / "queue.json"

ANSWERS_PATH = _HERE / "answers.jsonl"

# ── Verdict styling ────────────────────────────────────────────────────────────
_VERDICT_COLOR = {
    "pass": "#2ecc71",
    "fail": "#e74c3c",
    "non_computable": "#95a5a6",
    "escalated": "#f39c12",
}
_VERDICT_LABEL = {
    "pass": "PASS",
    "fail": "FAIL",
    "non_computable": "NON-COMPUTABLE",
    "escalated": "ESCALATED",
}

# ── Taxonomy sidebar content ───────────────────────────────────────────────────
_TAXONOMY_MD = """
### Verdict taxonomy

| Verdict | Meaning |
|---------|---------|
| **pass** | Contract completed — required side-effect succeeded |
| **fail** | A condition broke — see failure reason |
| **non_computable** | Insufficient evidence; engine refuses to guess |

### Failure reasons

| Reason | Meaning |
|--------|---------|
| `missing_side_effect` | Required write tool was never called or every call failed |
| `side_effect_output_failed` | Tool succeeded structurally but output text says error |
| `critical_tool_error` | Key tool errored, never recovered within trace |
| `terminal_error` | Session ended in error or timeout |
| `terminal_unknown` | Session terminal status undetermined |
| `partial_trace` | Spans missing — trace is structurally incomplete |

### Membership kinds

| Kind | Meaning |
|------|---------|
| **FULL** | Signature tool succeeded — workflow completed |
| **ATTEMPTED** | Workflow tools touched but signature incomplete |
| **unmapped** | No workflow claims this trace |

### Step status

`ok` = success  ·  `error` = failure

### Status sources (evidence rung)

`attr_success` → claude_code attribute
`otel_status` → OTel span status
`adapter` → per-agent extractor hook
`textual` → word-boundary text scan (last resort)
`none` → no signal; defaulted to ok
"""

# ── Page config ────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Trace Review",
    page_icon="🔍",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Data loading ───────────────────────────────────────────────────────────────


@st.cache_data
def load_queue() -> list[dict[str, Any]]:
    if not QUEUE_PATH.exists():
        return []
    with QUEUE_PATH.open() as f:
        result: list[dict[str, Any]] = json.load(f)
        return result


def load_answers() -> dict[str, dict[str, Any]]:
    """Load answers keyed by trace_id (last answer wins on duplicates)."""
    answers: dict[str, dict[str, Any]] = {}
    if not ANSWERS_PATH.exists():
        return answers
    with ANSWERS_PATH.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec: dict[str, Any] = json.loads(line)
                answers[rec["trace_id"]] = rec
            except (json.JSONDecodeError, KeyError):
                continue
    return answers


def save_answer(
    trace_id: str,
    question: str,
    answer: str,
    verdict_shown: str,
    relabel: bool = False,
    disagreement_kind: str | None = None,
    entry_class: str | None = None,
) -> None:
    """Append one answer record to answers.jsonl immediately.

    Re-label entries from the disagreement queue include ``relabel=true``
    and ``disagreement_kind`` so they are distinguishable from originals.
    Haywire-restart entries include ``class: "haywire"`` so they feed a
    separate detector corpus.
    Existing lines are never overwritten or deleted — append-only.
    """
    rec: dict[str, Any] = {
        "trace_id": trace_id,
        "question": question,
        "answer": answer,
        "verdict_shown": verdict_shown,
        "ts": datetime.now(tz=UTC).isoformat(),
    }
    if relabel:
        rec["relabel"] = True
    if disagreement_kind:
        rec["disagreement_kind"] = disagreement_kind
    if entry_class:
        rec["class"] = entry_class
    with ANSWERS_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


# ── State init ─────────────────────────────────────────────────────────────────


def _init_state(queue: list[dict[str, Any]], answers: dict[str, dict[str, Any]]) -> None:
    """Set up st.session_state on first run."""
    if "order" not in st.session_state:
        # Traces with existing answers go to the end; unanswered first
        answered = set(answers)
        unanswered = [e["trace_id"] for e in queue if e["trace_id"] not in answered]
        answered_list = [e["trace_id"] for e in queue if e["trace_id"] in answered]
        st.session_state["order"] = unanswered + answered_list
    if "current_index" not in st.session_state:
        st.session_state["current_index"] = 0


# ── Conversation renderer ──────────────────────────────────────────────────────


def _render_conversation(entry: dict[str, Any]) -> None:
    """Render the merged chronological conversation as the primary reading surface.

    Items are interleaved user turns, assistant reasoning, and tool calls in
    timestamp order — read top to bottom like the real session.

    Kinds:
      user / interrupt  — left-aligned block (blue / red)
      assistant_text    — muted grey reasoning block
      tool              — monospace header + code input + optional output expander
      api_error         — orange row
      truncated         — grey caption
    """
    items: list[dict[str, Any]] = entry.get("conversation", [])
    if not items:
        st.caption("No conversation data available for this trace.")
        return

    for item in items:
        kind = item.get("kind", "")
        off = item.get("ts_offset_s")
        off_str = f"+{off:.0f}s" if isinstance(off, (int, float)) else ""
        small_off = f'<span style="color:#999;font-size:0.8em">{off_str}</span> ' if off_str else ""

        if kind == "user":
            text = item.get("text", "")
            st.markdown(
                f'<div style="color:#3498db;margin:6px 0">'
                f'👤 {small_off}<span style="font-weight:bold">{text}</span></div>',
                unsafe_allow_html=True,
            )

        elif kind == "interrupt":
            text = item.get("text", "")
            st.markdown(
                f'<div style="color:#e74c3c;font-weight:bold;margin:6px 0">'
                f'🛑 {small_off}{text}</div>',
                unsafe_allow_html=True,
            )

        elif kind == "assistant_text":
            text = item.get("text", "")
            st.markdown(
                f'<div style="color:#888;font-size:0.9em;margin:4px 0 4px 20px">'
                f'🤖 {small_off}{text}</div>',
                unsafe_allow_html=True,
            )

        elif kind == "tool":
            tool_name = item.get("tool", "?")
            input_full = item.get("input_full", "")
            output_full = item.get("output_full", "")
            is_error = bool(item.get("is_error"))

            st.markdown(
                f'<div style="font-family:monospace;font-size:0.85em;'
                f'color:#5d6d7e;margin:4px 0">'
                f'🔧 <strong>{tool_name}</strong>  {small_off}</div>',
                unsafe_allow_html=True,
            )
            if input_full:
                st.code(input_full)
            if output_full:
                if is_error:
                    st.markdown(
                        '<span style="color:#e74c3c;font-weight:bold;font-size:0.8em">'
                        "⚠ tool returned error</span>",
                        unsafe_allow_html=True,
                    )
                with st.expander("output", expanded=is_error):
                    st.code(output_full)

        elif kind == "api_error":
            text = item.get("text", "")
            st.markdown(
                f'<div style="color:#f39c12;font-size:0.9em;margin:4px 0">'
                f'⚙️ {small_off}{text}</div>',
                unsafe_allow_html=True,
            )

        elif kind == "truncated":
            dropped = item.get("dropped", 0)
            st.caption(f"… {dropped} earlier items elided")


# ── Step timeline renderer ─────────────────────────────────────────────────────

_STEP_STATUS_ICON = {"ok": "✅", "error": "❌"}


_FRAME_EVENT_STYLE = {
    "user": ("👤", "#3498db"),
    "interrupt": ("🛑", "#e74c3c"),
    "api_error": ("⚙️", "#f39c12"),
}


def _render_conversation_frame(entry: dict[str, Any]) -> None:
    """Render the user-turn / interrupt / api-error spine above the timeline.

    Interrupts are loud (red): the user stepping in mid-trace is the highest-
    precision 'agent went wrong' signal we have.
    """
    events: list[dict[str, Any]] = entry.get("user_events", [])
    if not events:
        return
    st.markdown("#### Conversation frame")
    for ev in events:
        kind = ev.get("kind", "user")
        icon, color = _FRAME_EVENT_STYLE.get(kind, ("👤", "#888"))
        off = ev.get("ts_offset_s")
        off_str = f"+{off:.0f}s" if isinstance(off, (int, float)) else ""
        weight = "bold" if kind == "interrupt" else "normal"
        st.markdown(
            f'<div style="color:{color};font-weight:{weight};font-size:0.9em">'
            f'{icon} <span style="color:#999;font-size:0.85em">{off_str}</span> '
            f"{ev.get('text', '')}</div>",
            unsafe_allow_html=True,
        )
    st.divider()


def _render_step_summary(entry: dict[str, Any]) -> None:
    """Quick gestalt: tool histogram + error count + restart features.

    Counts every step (collapsed runs included — they remain in ``steps``),
    so the owner sees Bash×N / Read×N / Edit×N / Write×N and how many failed
    before scrolling the timeline.
    """
    steps: list[dict[str, Any]] = entry.get("steps", [])
    if not steps:
        return

    tool_counts: dict[str, int] = {}
    error_count = 0
    error_tools: dict[str, int] = {}
    for step in steps:
        tool = step.get("tool", "?")
        tool_counts[tool] = tool_counts.get(tool, 0) + 1
        if step.get("status") == "error":
            error_count += 1
            error_tools[tool] = error_tools.get(tool, 0) + 1

    # Tool histogram, busiest first.
    hist = " · ".join(
        f"{tool}×{n}" for tool, n in sorted(tool_counts.items(), key=lambda kv: -kv[1])
    )
    parts = [f"**{len(steps)} steps** — {hist}"]

    if error_count:
        err_detail = ", ".join(
            f"{tool}×{n}" for tool, n in sorted(error_tools.items(), key=lambda kv: -kv[1])
        )
        parts.append(
            f'<span style="color:#e74c3c;font-weight:bold">❌ {error_count} failed</span> '
            f'<span style="color:#e74c3c">({err_detail})</span>'
        )
    else:
        parts.append('<span style="color:#2ecc71">no failed steps</span>')

    restart_count = entry.get("restart_count")
    rework = entry.get("post_restart_rework")
    if restart_count:
        parts.append(
            f'<span style="color:#f39c12">↻ {restart_count} restart(s), '
            f"{rework or 0} arg-match rework</span>"
        )

    st.markdown(
        '<div style="font-size:0.9em;line-height:1.8">' + " &nbsp;|&nbsp; ".join(parts) + "</div>",
        unsafe_allow_html=True,
    )


def _render_step_timeline(entry: dict[str, Any]) -> None:
    steps: list[dict[str, Any]] = entry.get("steps", [])
    collapsed_runs: list[dict[str, Any]] = entry.get("collapsed_runs", [])

    # Build collapsed index set for rendering
    collapsed_first_indices: dict[int, dict[str, Any]] = {}
    collapsed_index_set: set[int] = set()
    for cr in collapsed_runs:
        collapsed_first_indices[cr["first_index"]] = cr
        for idx in range(cr["first_index"], cr["last_index"] + 1):
            collapsed_index_set.add(idx)

    rendered_collapsed: set[int] = set()

    for step in steps:
        idx = step["index"]

        # Collapsed run — render summary row once at the first step of the run
        if idx in collapsed_index_set:
            run_entry: dict[str, Any] | None = collapsed_first_indices.get(idx)
            if run_entry and idx not in rendered_collapsed:
                rendered_collapsed.add(idx)
                tool = run_entry["tool"]
                count = run_entry["count"]
                first_i = run_entry["first_index"]
                last_i = run_entry["last_index"]
                first_arg = run_entry.get("first_args_digest", "")
                last_arg = run_entry.get("last_args_digest", "")
                with st.expander(
                    f"⬜ **{tool}** ×{count} (steps {first_i}–{last_i}) — consecutive, collapsed"
                ):
                    st.markdown(f"**First:** `{first_arg}`")
                    st.markdown(f"**Last:**  `{last_arg}`")
            continue

        # Regular step
        is_evidence = step.get("is_evidence", False)
        status = step.get("status", "ok")
        icon = _STEP_STATUS_ICON.get(status, "⬜")
        tool = step.get("tool", "?")
        args = step.get("args_digest", "")
        out = step.get("output_digest", "")
        src = step.get("status_source", "")

        if is_evidence:
            st.markdown("---")
            st.markdown("**→ Evidence step ↓**")

        is_error_struct = step.get("is_error_struct", False)
        input_full = step.get("input_full", "")
        output_full = step.get("output_full", "")
        time_gap = step.get("time_gap_s")
        reaction = step.get("reaction", "")

        # Time gap — a stall (long gap) before this step is itself a signal.
        if isinstance(time_gap, (int, float)) and time_gap > 5:
            st.markdown(
                f'<span style="font-size:0.75em;color:#999">⏱ +{time_gap:.0f}s</span>',
                unsafe_allow_html=True,
            )

        cols = st.columns([0.05, 0.15, 0.40, 0.35, 0.05])
        with cols[0]:
            st.markdown(f"{icon}")
        with cols[1]:
            st.markdown(f"**{tool}**")
        with cols[2]:
            if args:
                st.markdown(f"`{args}`")
        with cols[3]:
            if out:
                # Error steps surface their output in red — this is the
                # "what failed" signal the owner needs to judge haywire.
                out_color = "#e74c3c" if status == "error" else "#888"
                out_weight = "bold" if status == "error" else "normal"
                st.markdown(
                    f'<span style="font-family:monospace;color:{out_color};'
                    f'font-weight:{out_weight};font-size:0.85em">{out}</span>',
                    unsafe_allow_html=True,
                )
            elif status == "error":
                st.markdown(
                    '<span style="color:#e74c3c;font-size:0.85em">(error — no output captured)</span>',
                    unsafe_allow_html=True,
                )
        with cols[4]:
            if src and src != "none":
                st.markdown(f'<span style="font-size:0.75em;color:#aaa">{src}</span>', unsafe_allow_html=True)

        # Structured error badge — the tool LITERALLY returned is_error
        # (distinct from the inferred status icon above).
        if is_error_struct:
            st.markdown(
                '<span style="color:#e74c3c;font-weight:bold;font-size:0.8em">'
                "⚠ tool returned error</span>",
                unsafe_allow_html=True,
            )

        # Full redacted input/output — the document, not the digest. Errors
        # auto-expand: that's where the "what failed" answer lives.
        if input_full or output_full:
            expanded = status == "error" or is_error_struct
            with st.expander("▸ full input / output", expanded=expanded):
                if input_full:
                    st.markdown("**input**")
                    st.code(input_full)
                if output_full:
                    st.markdown("**output**")
                    st.code(output_full)

        # Agent's stated reaction right after a failure/restart — the strongest
        # haywire-vs-recovered tell.
        if reaction:
            st.markdown(
                f'<span style="font-size:0.85em;color:#8e44ad;font-style:italic">'
                f"↳ agent then: {reaction}</span>",
                unsafe_allow_html=True,
            )

        # Detector note (disagreement queue only — not present on normal queue entries)
        detector_note = step.get("detector_note")
        if detector_note:
            st.markdown(
                f'<span style="font-size:0.8em;color:#e67e22;font-style:italic">🔍 {detector_note}</span>',
                unsafe_allow_html=True,
            )

        if is_evidence:
            st.markdown("---")


# ── Main app ───────────────────────────────────────────────────────────────────


def main() -> None:
    # Sidebar: taxonomy explainer
    with st.sidebar:
        st.markdown("## Kairos taxonomy")
        st.markdown(_TAXONOMY_MD)

    queue = load_queue()
    if not queue:
        st.error(
            f"Queue not found at {QUEUE_PATH}. "
            f"Run: uv run eval/review/build_queue.py  "
            f"(or set QUEUE_PATH=eval/review/haywire_queue.json and run build_haywire_queue.py)"
        )
        st.stop()

    answers = load_answers()
    _init_state(queue, answers)

    order: list[str] = st.session_state["order"]
    idx: int = st.session_state["current_index"]

    if idx >= len(order):
        st.success(f"All {len(order)} traces reviewed!")
        if st.button("Start over"):
            st.session_state["current_index"] = 0
            st.rerun()
        return

    # Lookup entry for current trace
    trace_id = order[idx]
    entry_map = {e["trace_id"]: e for e in queue}
    entry = entry_map.get(trace_id)
    if entry is None:
        # Trace in order but missing from queue (queue rebuilt) — skip
        st.session_state["current_index"] += 1
        st.rerun()
        return

    # ── Progress bar ──────────────────────────────────────────────────────────
    total = len(order)
    st.markdown(f"### Trace {idx + 1} / {total}")
    st.progress((idx) / max(total, 1))

    # ── Header ────────────────────────────────────────────────────────────────
    verdict = entry.get("verdict", "non_computable")
    color = _VERDICT_COLOR.get(verdict, "#95a5a6")
    verdict_label = _VERDICT_LABEL.get(verdict, verdict.upper())
    failure_reason = entry.get("failure_reason") or ""
    workflow = entry.get("primary_workflow", "unknown")
    agent = entry.get("agent", "unknown")
    phoenix_url = entry.get("phoenix_url", "")
    membership_kind = entry.get("membership_kind", "")

    col_h1, col_h2, col_h3 = st.columns([0.45, 0.35, 0.20])
    with col_h1:
        st.markdown(
            f"**Agent:** `{agent}`  \n"
            f"**Workflow:** `{workflow}` · `{membership_kind}`"
        )
    with col_h2:
        st.markdown(
            f'<span style="background:{color};color:white;padding:4px 10px;border-radius:4px;font-weight:bold">'
            f"{verdict_label}</span>",
            unsafe_allow_html=True,
        )
        if failure_reason:
            plain = {
                "missing_side_effect": "required write tool never succeeded",
                "side_effect_output_failed": "tool ran but output says error",
                "critical_tool_error": "key tool errored, no recovery",
                "terminal_error": "session ended in error/timeout",
                "terminal_unknown": "terminal status undetermined",
                "partial_trace": "spans missing",
            }.get(failure_reason, failure_reason)
            st.markdown(f"*{failure_reason}* — {plain}")
    with col_h3:
        if phoenix_url:
            st.markdown(f"[↗ Phoenix]({phoenix_url})", unsafe_allow_html=False)
        tokens = entry.get("tokens", {})
        if tokens:
            st.markdown(
                f"in={tokens.get('input', 0):,} "
                f"out={tokens.get('output', 0):,} "
                f"cache={tokens.get('cache_read', 0):,}"
            )

    # ── Task + surface (the frame: what was asked, on what) ───────────────────
    task = entry.get("task", "")
    if task:
        st.markdown(f"> **Task:** {task}")
    surface = entry.get("surface", {})
    if surface:
        chips = [
            f"`{k}` {surface[k]}"
            for k in ("cwd", "git_branch", "model", "permission_mode")
            if surface.get(k)
        ]
        if chips:
            st.markdown(
                '<span style="font-size:0.8em;color:#888">' + " · ".join(chips) + "</span>",
                unsafe_allow_html=True,
            )

    st.divider()

    # ── Conversation (primary reading surface) ────────────────────────────────
    st.markdown("#### Conversation")
    _render_conversation(entry)

    st.divider()

    # ── Engine step-timeline (collapsed by default) ───────────────────────────
    with st.expander("🔬 Engine step-timeline (restart analysis)", expanded=False):
        _render_conversation_frame(entry)
        st.markdown("#### Step timeline")
        _render_step_summary(entry)
        _render_step_timeline(entry)

    st.divider()

    # ── Question + answer ─────────────────────────────────────────────────────
    question = entry.get("question", "What's your read on this trace?")

    st.markdown(f"### {question}")

    # Show prior label for disagreement queue entries
    prior_comment = entry.get("prior_comment", "")
    if prior_comment.strip():
        st.info(f"Previously you said: {prior_comment.strip()}")

    # Prefill if already answered
    prefill = ""
    existing = answers.get(trace_id)
    if existing:
        prefill = existing.get("answer", "")
        st.info("Previously answered — answer prefilled. Save & Next to update.")

    answer_text = st.text_area(
        "Your answer",
        value=prefill,
        height=140,
        placeholder="Type your assessment here…",
        label_visibility="collapsed",
        key=f"answer_{trace_id}",
    )

    col_b1, col_b2, col_b3 = st.columns([0.20, 0.15, 0.65])
    with col_b1:
        save_clicked = st.button("💾 Save & Next", type="primary", use_container_width=True)
    with col_b2:
        skip_clicked = st.button("⏭ Skip", use_container_width=True)
    with col_b3:
        back_clicked = st.button("◀ Back", use_container_width=True)

    # Determine if this is a relabel entry (disagreement queue)
    is_relabel = bool(entry.get("disagreement_kind"))
    disagreement_kind = entry.get("disagreement_kind") or None
    # Haywire queue entries carry class="haywire" so answers are distinguishable.
    entry_class: str | None = entry.get("class") or None

    if save_clicked:
        if answer_text.strip():
            save_answer(
                trace_id,
                question,
                answer_text.strip(),
                verdict,
                relabel=is_relabel,
                disagreement_kind=disagreement_kind,
                entry_class=entry_class,
            )
            # Reload answers so prefill updates
            st.session_state["current_index"] = idx + 1
            st.rerun()
        else:
            st.warning("Type an answer before saving.")

    if skip_clicked:
        st.session_state["current_index"] = idx + 1
        st.rerun()

    if back_clicked and idx > 0:
        st.session_state["current_index"] = idx - 1
        st.rerun()


if __name__ == "__main__":
    main()
