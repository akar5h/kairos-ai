# Kairos Configuration Guide

This guide covers every user-provided configuration key in `context.yaml`.
All keys are **optional** (Kairos has sensible defaults for each) except
`operations`, which is required to classify traces.  No hidden assumptions —
everything Kairos does is driven by what you put here or left out.

---

## Top-level keys

```yaml
agent_name: "My Agent"          # display label; no functional effect
agent_description: "..."        # display label; no functional effect
correlation_key: "paperclip.issue"   # OPTIONAL — see below
operations:
  - ...                          # REQUIRED — at least one
```

---

## `correlation_key` (optional)

**What it is.** A span attribute name whose *value* groups multiple traces
into one logical unit of work.

Agent sessions often span more than one OTel trace.  A Paperclip coding
agent, for example, creates several traces per issue (one per resume,
multi-agent handoff, or retry).  Without a correlation key Kairos scores
each trace independently.  With one, all traces that share the same
attribute value are rolled up into a single *unit* with:

- `unit_outcome` — outcome of the **last computable trace** chronologically
  (intermediate failures on an ultimately-green unit are progress, not
  failure; this is the *last-wins* rule)
- `unit_findings` — **union** of findings from every trace in the group
- `unit_cost` — **sum** of tokens + sum of error-count (struggle) across
  the group
- `unit_span` — earliest `started_at` … latest `ended_at` across the group

**When omitted (the default).** Each trace is its own unit.  `unit_outcome`
is the per-trace outcome.  Behaviour is byte-identical to pre-Day-9 Kairos.

**How to choose the value.** Pick the span attribute whose value is the same
for every trace that belongs to one work unit:

| System | Natural unit | `correlation_key` value |
|---|---|---|
| Paperclip (issues) | one issue = multiple agent runs | `"paperclip.issue"` |
| Paperclip (pipeline runs) | one pipeline run | `"paperclip.run_id"` |
| Chat application | one conversation thread | `"thread_id"` |
| Batch pipeline | one batch job | `"run_id"` |

The engine is domain-blind; it never hard-codes `"paperclip.issue"` or any
other name.  Whatever attribute name you configure here, the reader scans
every span in a trace for it and stores the first value found.

**Traces with no key value.** If a trace has no span carrying the configured
attribute, it is treated as *unattributed* and scored per-trace (its own
unit).  This is graceful degradation: a misconfigured key never crashes the
pipeline; it just produces per-trace units for the traces that lack the
attribute, and grouped units for the ones that have it.

**Worked example (Paperclip).** Issue `XER-199` triggered three agent runs:

```yaml
correlation_key: "paperclip.issue"
```

Kairos receives three traces, all carrying `paperclip.issue = "XER-199"`:

| trace_id | started_at | outcome |
|---|---|---|
| `trace_a` | 10:00 | FAIL (tool error) |
| `trace_b` | 10:30 | FAIL (partial) |
| `trace_c` | 11:00 | PASS |

Rollup (`last-wins` on the last computable trace):

```
unit_id            = "XER-199"
unit_outcome_pass  = True          ← trace_c is the last computable, it passed
unit_findings      = [findings from trace_a] ∪ [findings from trace_b] ∪ [findings from trace_c]
unit_total_tokens  = tokens_a + tokens_b + tokens_c
unit_struggle      = error_count_a + error_count_b + error_count_c
unit_span          = 10:00 … end_of_trace_c
```

The per-trace `OutcomeResult` objects remain available on
`AnalysisResult.workflows[*].outcome.per_trace_results` — the unit rollup
sits *alongside* them, never replacing them.

**Configuration (safe to leave out).** If your agent runs are one-to-one
with traces (or you do not yet have a correlation attribute in your spans),
simply omit `correlation_key`.  The pipeline behaves exactly as before.

---

## `operations` (required)

Each operation defines one workflow you want Kairos to classify traces into
and score.

```yaml
operations:
  - name: "Code Implementation"
    description: "Writing and editing source code"
    expected_tools: [Read, Edit, Write, Bash, Grep, Glob]
    required_side_effect_tools: [Edit, Write]   # signature tools
    side_effect_match: any                       # "all" (default) or "any"
    priority: high                               # high / medium / low (default medium)
    excluded_tools: []                           # optional; trace calls any → NONE membership
    membership_recall_threshold: null            # explicit override; null → auto
    business_goal: "..."                         # display label; no functional effect
    reliability_metric: "..."                    # display label; no functional effect
    bad_run_means: "..."                         # display label; no functional effect
    correctness_criteria: []                     # display labels for the judge tier
```

**Key rules:**

- `required_side_effect_tools` is the *signature* of the workflow.  At least
  one of these tools must appear in a trace for membership to be considered.
  Ops without it are utility patterns and will never match any trace
  (Kairos warns).
- `side_effect_match: any` — a trace is FULL if *any* required tool
  succeeded.  `all` (default) requires every one to succeed.
- `excluded_tools` — a trace that calls any of these successfully is
  classified as NONE for this op (used to prevent Codebase Research from
  matching traces that also wrote files).
- `membership_recall_threshold` — fraction of `expected_tools` the trace
  must use to qualify.  `null` → auto (1.0 for single-tool ops, 0.5
  otherwise).

---

## Detector thresholds (per operation, optional)

Session-quality detector thresholds live under each operation to allow
per-workflow tuning.  All have code defaults.  Example:

```yaml
operations:
  - name: "Code Implementation"
    ...
    # Day 8 session-quality thresholds (all optional; omit = use defaults)
    struggle_threshold: 2.0        # D2: fire when error_steps/side_effect_successes >= T
    recovery_window: 10            # D1: steps within which a same-command retry counts as recovery
    coordination_repeat_threshold: 3  # D3: min identical-arg calls to fire
    work_to_talk_threshold: 0.05   # D4: min side_effect_successes per 1k tokens
```

When omitted the defaults are used.  The defaults are documented in
`src/kairos/detection/session_quality.py` alongside the label distribution
they were derived from.

---

## What Kairos does NOT require you to declare

- **Expected tool sequences.** Kairos learns per-workflow base rates from
  clean traces (outcome-pass, low-struggle).  Expectation misses are surfaced
  for your label, not auto-fired.
- **Ground-truth labels.** Discovery surfaces anomaly candidates; you label
  them in the review app.  Kairos does not assume correctness without your
  confirmation.
- **LLM keys.** The nightly pipeline is deterministic and calls no model.
  The judge tier is deferred (Phase 4).

---

## Surfaces, ingestion families, and grouping levels

### The IR firewall

Every data source maps to a `TraceEnvelope` before the engine sees it.
The engine is source-blind — it only works on `TraceEnvelope` objects.
Two ingestion families exist today:

| Family | Entry point | Typical source |
|---|---|---|
| **OTel/span reader** | `readers/phoenix.py` → `spans_to_envelope` | Any OTel backend (Phoenix, Jaeger, OTLP) |
| **Native transcript normalizer** | `normalization/agents/{claude_code,codex,opencode,paperclip}.py` via `AgentTranscriptNormalizer.to_events` | Agent-native JSON logs |

Adding a new **OTel source** is config-only: point Kairos at the collector,
name the correlation attribute.  Zero new code.

Adding a new **native-log source** requires one small
`AgentTranscriptNormalizer` subclass (see `normalization/agents/base.py`).
No engine or downstream changes — the IR is the firewall.

### Grouping levels and `correlation_key`

Agent data has a natural nesting:

```
issue  ⊃  session  ⊃  trace  ⊃  span
```

`correlation_key` selects which level you want to group at for unit-of-work
outcome.  Choose the level whose boundary equals "one complete unit of work":

| System | Unit of work | `correlation_key` | Phoenix attribute |
|---|---|---|---|
| Paperclip (issues) | one issue (N agent runs) | `"paperclip.issue"` | `paperclip.issue` |
| Paperclip (pipeline runs) | one pipeline run | `"paperclip.run_id"` | `paperclip.run_id` |
| Standalone / local Claude Code | one session | `"session.id"` | `session.id` |
| Board / CTO Claude | one session | `"session.id"` | `session.id` |
| Generic OTel chat app | one conversation | `"thread_id"` | `thread_id` |
| Generic OTel request | one request | `"request_id"` | `request_id` |

For Paperclip, the sprint unit is the *issue* — each issue spawns one or
more agent runs (traces), and the issue is complete only when the final run
succeeds.  Hence `correlation_key: "paperclip.issue"`.

Live-verified 2026-06-13 (Phoenix `default` project, 5000-span window):
`paperclip.issue` was present on 13 of 95 traces.  The attribute appears on
every span type in a trace (interaction, llm_request, tool, tool.execution),
so reading the first span that carries it is sufficient.

**Future note (YAGNI, not built).** The hierarchy is extensible: two rollup
passes at different keys would give issue-level outcome + session-level quality
in one pipeline run.  Not implemented now; the `rollup_units` API accepts
any `correlation_key` string and is composable if needed later.
