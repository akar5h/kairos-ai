# Sprint Execution Doc 1/3 — TRUTH (Days 1–5)

*Child of `sprint-14day.md`. Covers Day 1 (fail-loud + hygiene), Day 2 (tokens), Days 3–4 (outcome v2), Day 5 (membership dedup + Honest Snapshot). Written for executor agents: pseudocode is normative for behavior, not for style — match existing repo idiom.*

---

## 0. Where every fix sits

```
                                   ┌─────────────────────────────────────────┐
                                   │  Paperclip node process                 │
                                   │  (claude_code OTel emitter)             │
                                   └────────────────┬────────────────────────┘
                                                    │ OTLP :4318
                                       ┌────────────▼────────────┐
                                       │ otel-collector (Docker) │
                                       └────────────┬────────────┘
                                                    │ :4319
                                       ┌────────────▼────────────┐
                                       │ Phoenix (Docker, :6006) │  ← live store; the SQLite at
                                       │ project "default"       │    ~/.phoenix/ is the DEAD archive (Day 1.3)
                                       └────────────┬────────────┘
                                                    │ GraphQL / span fetch
        kairos-analysis-views plugin ───────────────┤
        worker.ts run-analysis            [Day 1.1: │ validate config BEFORE spawn]
                                                    ▼
   ┌──────────────────────────── kairos view (CLI) ────────────────────────────┐
   │                                                                           │
   │  PhoenixReader ──► genai_mapping.py ──► LiveNormalizer ──► TraceEnvelope  │
   │                    [Day 2: token attrs]  [Day 3: status,    (IR)          │
   │                                           orphan check]                   │
   │                                                    │                      │
   │  BusinessContext (context.yaml) ──► classify_membership                   │
   │  [Day 5: excluded_tools,             [Day 5: primary label]               │
   │   rewritten coding ops]                            │                      │
   │                                                    ▼                      │
   │  outcome_metric.py ◄── [Days 3–4: evidence chain, failure_reason]         │
   │  detection/ (tier 1)                               │                      │
   │                                                    ▼                      │
   │  build_analysis_view ──► AnalysisView JSON [Day 1.2: meta block,          │
   │                                             null reliability]             │
   └───────────────────────────────────────────────────────────────────────────┘
```

Rule of the phase: **every change makes a wrong number impossible or visible. No change adds capability.**

---

## Day 1.1 — Plugin fail-loud (`views-plugin/src/worker.ts`)

The historical bug: `run-analysis` with empty `contextPath` spawned `kairos view --context ""`, which produced a structurally valid, empty AnalysisView, saved as `latest.json`. Six runs of confident garbage.

```pseudocode
// in run-analysis handler, BEFORE spawnKairosView (worker.ts ~line 408)
contextPath = params.contextPath ?? resolveEnv("KAIROS_CONTEXT_PATH", "")
if contextPath.trim() == "":
    throw PluginError("KAIROS_CONTEXT_PATH is not configured. Refusing to run: " +
                      "an analysis without workflow definitions produces a misleading empty result.")
if not fs.existsSync(contextPath):
    throw PluginError(`Context file not found: ${contextPath}`)
// cheap structural sniff — full validation is the engine's job:
raw = fs.readFileSync(contextPath, "utf8")
if raw.trim() == "" or not raw.includes("operations"):
    throw PluginError(`Context file looks empty or has no operations: ${contextPath}`)
```

Same guard in `start-batch-analysis`. Errors must surface in the UI action result (the plugin SDK propagates thrown errors — verify with a manual test, that's the acceptance demo).

**Caveat (architect's note):** do NOT validate YAML semantics in the plugin — two validators drift apart. Plugin checks existence/non-emptiness; engine owns semantic validation (Day 1.2 makes the engine strict).

## Day 1.2 — Engine: `meta` block, strict context, null reliability

**`src/kairos/views/analysis_view.py`** — add to the Pydantic model:

```pseudocode
class AnalysisMeta(BaseModel):
    engine_version: str            # importlib.metadata.version("kairos-ai")
    context_path: str
    context_sha256: str            # hashlib over raw file bytes
    operation_count: int
    trace_count_fetched: int       # envelopes resolved from the source
    trace_count_analyzed: int      # envelopes that passed normalization

class AnalysisView(BaseModel):
    ...existing fields...
    meta: AnalysisMeta | None = None   # None tolerated for old files; always set going forward
```

`build_analysis_view()` gains a `meta:` parameter; `cli.py view` constructs it (it already holds the context path and envelope counts). No timestamps inside the engine — determinism invariant; the plugin stamps wall-clock time on the saved filename as it already does.

**`src/kairos/taxonomy/business_context.py`** — `from_yaml` raises when the parsed document yields zero operations (today: silently empty). Error text must name the file path.

**`src/kairos/engine/pipeline.py` preflight** — when `len(envelopes) == 0`, reliability values become `None`, not `1.0`:

```pseudocode
def preflight(envelopes):
    if not envelopes:
        return {"terminal_status_rate": None, "tool_sequence_rate": None}
    ...existing computation...
```

TS contract (`views-plugin/src/ui/types.ts`): `reliability: Record<string, number | null>` + optional `meta`. UI renders `—` for null and a visible "no traces analyzed in this window" banner when `meta.trace_count_analyzed == 0`.

**Edge cases:** context path is a directory (existsSync passes → engine open() fails → ensure error message includes path); YAML parses to a list not a mapping (from_yaml must raise, not duck-type); all operations invalid after validation warnings (`pipeline.py:188–204`) → promote to hard error *only* when zero ops remain usable; old `latest.json` without `meta` (UI optional-chains).

**Tests:** engine — zero-op YAML raises; zero-envelope analyze returns null reliability; meta round-trips through model_dump_json. Plugin — empty contextPath throws before spawn (mock spawn, assert not called).

## Day 1.3 — Hygiene + micro-lint

```bash
mv ~/.phoenix/phoenix.db ~/.phoenix/phoenix-taubench-archive-2026-05.db
```

README gains a "Trace topology" section (collector :4317/4318 → Phoenix container :4319; UI :6006; archive location + contents + why it's kept: Day 6 corpus).

**Micro-lint** (`scripts/observed_tools.py`, ~80 lines): answers two questions the Day 5 rewrite needs — *which tool names actually exist as spans* and *how ubiquitous is each*.

```pseudocode
# GraphQL pagination over root project spans, last N hours (default 168)
spans = phoenix_graphql_fetch(project, hours, span_kind=any)
tool_names = {}          # name -> set(trace_id)
traces = set()
for span in spans:
    traces.add(span.trace_id)
    name = extract_tool_name(span)      # MUST reuse the same attribute logic
                                        # genai_mapping.py uses — import it, don't reimplement
    if name: tool_names[name].add(span.trace_id)
print table: tool | span_count | trace_base_rate (len(trace_ids)/len(traces))
flag declared-but-never-observed tools given --context context.yaml
```

**Caveat:** the tool name on `claude_code.tool` spans lives in an attribute, not the span name. The extraction helper in `genai_mapping.py` is the single source of truth — micro-lint imports it. Two extraction implementations is how the F8 class of bug is born.

**Day 1 exit:** misconfigured run errors; empty-window run shows `trace_count_analyzed: 0` and null rates; base-rate table for live tools saved to `docs/observed-tools-{date}.md`.

---

## Day 2 — Token extraction (`src/kairos/readers/genai_mapping.py`)

### Step 1: discovery — DONE (2026-06-12 manual 36h analysis, see `insight-report-0.md`)

Live `claude_code.llm_request` spans carry **top-level custom keys**: `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_creation_tokens` (plus `model`, `stop_reason`, `attempt`, `success`, `duration_ms`, `ttft_ms`, and `gen_ai.response.id`). The ladder below keeps the semconv/OpenInference rungs for portability (tau-bench corpus, future emitters), with the observed custom keys as the first rung:

```pseudocode
USAGE_KEY_LADDER = [
  ("input_tokens",              "output_tokens",              "cache_read_tokens"),                       # observed live (top-level custom)
  ("gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens", "gen_ai.usage.cache_read_input_tokens"),    # OTel GenAI semconv
  ("llm.token_count.prompt",    "llm.token_count.completion", "llm.token_count.prompt_details.cache_read"),# OpenInference
]
```

**Freed half-day goes to the F10 guard (new, mandatory):** live `claude_code.tool` spans carry NO `tool_args`/`tool_output`, and `jaccard_dict_similarity` returns **1.0 for None/None and ∅/∅** (`detection/similarity.py:10–17`) — every consecutive same-tool pair on live data scores as identical, which is what produced the historical 642-finding flood at confidence 1.0. Fix at the detector layer, not by changing Jaccard semantics globally:

```pseudocode
# detection/redundant.py, before similarity scoring:
if not (curr.tool_args_normalized or curr.tool_args) and not (nxt.tool_args_normalized or nxt.tool_args):
    continue        # args uninstrumented → no similarity opinion → no finding
# same guard concept reviewed for loops.py: identical-OUTPUT loops need tool_output;
# if output is also uninstrumented, loop_detected degrades to "same tool ≥N consecutive"
# which may NOT fire as a finding — only as a triage feature (Day 8 consumes it).
```

Unit tests: args-present pair (fires per threshold), args-absent pair (silent), mixed (silent).

### Step 2: mapping

```pseudocode
USAGE_KEY_LADDER = [
  # (input_key, output_key, cache_read_key) — first tuple fully present wins
  ("gen_ai.usage.input_tokens",  "gen_ai.usage.output_tokens",  "gen_ai.usage.cache_read_input_tokens"),
  ("llm.token_count.prompt",     "llm.token_count.completion",  "llm.token_count.prompt_details.cache_read"),
]

def extract_usage(attrs) -> Usage | None:
    for (k_in, k_out, k_cache) in USAGE_KEY_LADDER:
        if k_in in attrs and k_out in attrs:
            return Usage(
                input  = coerce_int(attrs[k_in]),       # string-typed numbers exist; coerce, warn once per run
                output = coerce_int(attrs[k_out]),
                cache_read = coerce_int(attrs.get(k_cache, 0)))
    return None     # absent ≠ zero; None means "not instrumented"
    # log.debug which ladder rung matched, once per trace
```

IR change (`src/kairos/models/trace.py`):

```pseudocode
class Step:
    total_tokens: int        # SEMANTICS CHANGE, document in docstring:
                             # output + (input - cache_read)  — the spend that detectors may call waste
    cache_read_tokens: int = 0
    tokens_instrumented: bool = False   # extract_usage() returned non-None
```

`_coverage_ratio` in `reference_behavior.py:329` switches to `tokens_instrumented` instead of `total_tokens > 0` (a genuinely-zero-token step is instrumented, not missing — rare but real for cached-everything calls).

**Edge cases:** usage on a child span while the parent `llm_request` also carries it → **count the request-level span only**, child spans ignored for usage (test with a synthetic parent/child fixture); streaming where usage arrives on a span event rather than attribute → check `span.events` in discovery, extend ladder if seen; errored LLM call without usage → `tokens_instrumented=False`, excluded from coverage ratio; negative/absurd values (emitter bugs) → clamp to 0 + warn, never propagate negatives into waste sums.

**Caveat (cost-truth):** `estimated_token_waste` in detectors keeps using `total_tokens` unchanged — the new semantics flow through automatically. Add one regression test pinning: a finding on a step with `cache_read=9000, input=10000, output=500` reports waste `1500`, not `10500`.

**Day 2 exit:** rerun on 50 live traces → ≥80% of LLM steps `tokens_instrumented`; one workflow-level waste sum is nonzero in a test analysis.

---

## Days 3–4 — Outcome v2

### The evidence ladder

```
 per tool step:                                  per trace:
 ┌──────────────────────────────┐                ┌─────────────────────────────────┐
 │ 1. kairos.outcome attr?      │── present ──►  │ terminal_status mapping:        │
 │    (explicit override)       │   use it       │  claude_code session ended      │
 │ 2. OTel span status?         │── ERROR ──►    │  blocked_on_user → HUMAN_       │
 │    (primary for claude_code) │   step failed  │  ESCALATION (pass-eligible,     │
 │ 3. adapter extractor         │── verdict ──►  │  counted in escalation_rate)    │
 │    (per agent-kind hook)     │   use it       └─────────────────────────────────┘
 │ 4. textual markers           │── last resort
 │    word-boundary, LAST 500   │   only when 1–3 all silent
 │    chars of output only      │
 └──────────────────────────────┘
 Ladder is ORDERED and SHORT-CIRCUITING. A rung that answers stops the descent.
 Rung 4 never overrides rung 2: status OK + "error" in output = OK.
```

### Implementation

**`normalization/agents/base.py`** — new optional hook:

```pseudocode
class AgentNormalizer:
    def step_outcome(self, step: Step) -> StepOutcome | None:
        """Return OK/FAILED/None. None = no opinion, ladder continues."""
        return None
```

**`claude_code.py` extractor:**

```pseudocode
def step_outcome(step):
    if step.tool_name == "Bash":
        exit_code = step.attrs.get("exit_code")        # verify actual key in Day 2's discovery dump
        if exit_code is not None:
            return OK if int(exit_code) == 0 else FAILED
    if step.tool_output and step.tool_output.startswith(HARNESS_ERROR_PREFIXES):
        # ("Error:", "InputValidationError", "PermissionError: ") — prefixes, anchored at char 0
        return FAILED
    return None
```

**`outcome_metric.py` rung 4 rewrite:**

```pseudocode
_MARKER_RE = compile(r"\b(failed|failure|error|exception|denied|validation failed|not submitted)\b", IGNORECASE)
_NEGATED_RE = compile(r"\b(no|0|zero|without)\s+(errors?|failures?)\b", IGNORECASE)

def textual_failure(output) -> bool:
    tail = output[-500:]
    if _NEGATED_RE.search(tail): ...mask negated spans before matching...
    return bool(_MARKER_RE.search(tail_after_negation_mask))
```

The negation mask: cheapest correct approach is to delete negated-phrase matches from the tail string before running `_MARKER_RE`. Test table is normative:

| output tail | verdict |
|---|---|
| `"...build complete. 0 errors, 0 warnings"` | pass |
| `"...Error: ENOENT no such file"` | fail |
| `"...fixed the error handling, tests green"` | **pass** — wait, "error" matches; this is WHY rung 4 is last resort and status wins. With status present this rung never runs. Without status: accept the false fail, it lands in failure_reason as `side_effect_output_failed` and is auditable. Documented limitation, not a bug to over-engineer on Day 3. |
| `"...no errors found"` | pass |
| `""` (empty, status OK) | pass (OWNER-DECISION default: silence + OK status = consent) |

**`failure_reason` enum** added to `OutcomeResult` and surfaced through `CorrectnessView` → plugin `types.ts`:

```
terminal_error | terminal_unknown | critical_tool_error |
missing_side_effect | side_effect_output_failed | partial_trace
```

plus `evidence: {step_index, rung}` so every fail is one click from its cause.

**Terminal mapping** (`genai_mapping.py` / `live_normalizer.py`): a trace whose last meaningful span is `claude_code.tool.blocked_on_user` (or session-end attr indicates awaiting input) → `TerminalStatus.HUMAN_ESCALATION`. Already pass-eligible in `outcome_metric.py:181–187`. New workflow-level metric: `human_escalation_rate = escalated / computable` in `CorrectnessView` — for a governed agent, escalating correctly is a success mode and ALSO a number worth watching (the autonomy dial).

**Orphan check** (lite integrity gate), in LiveNormalizer:

```pseudocode
span_ids = {s.span_id for s in spans}
orphans  = [s for s in spans if s.parent_id and s.parent_id not in span_ids and not s.is_root]
envelope.integrity = "partial" if orphans else "complete"
# outcome evaluation: integrity == "partial" → computable=False, failure_reason=partial_trace
```

**Edge cases:** binary/non-UTF8 output → rung 4 skipped entirely (decode errors = no textual opinion); a side-effect tool failing at step 3 then succeeding at step 30 → existing recovery semantics hold (any later success of the same tool recovers — `outcome_metric.py:82–112` untouched); 5,000-step trace → ladder is O(1) per step, full eval O(n), assert no quadratic scan sneaks in (the existing recovery check is O(n²) worst case on pathological traces — acceptable at current scale, leave a `# PERF` note, don't fix in-sprint).

**Day 4 exit gate (human):** owner reads 20 live trace verdicts (trace link + verdict + failure_reason + evidence). ≥18/20 agreement → proceed. Below → fix the top disagreement class same day; Day 5 absorbs the slip, snapshot moves to Day 6 morning. The 20 traces: stratified — 10 outcome-fail, 5 pass, 5 escalated.

---

## Day 5 — Membership dedup + Honest Snapshot

### context.yaml rewrite (normative; adjust only on Day 1 base-rate evidence)

```yaml
# config/context.yaml  — coding agents only; lead ops moved to
# config/context.lead-pipeline.yaml.disabled (verbatim copy, header comment explaining why)

operations:
  - name: "Code Implementation"
    expected_tools: [Read, Edit, Write, Bash, Grep, Glob]
    required_side_effect_tools: [Edit, Write]      # either qualifies (existing any-of gate)
    priority: high

  - name: "Codebase Research"
    expected_tools: [Read, Grep, Glob, Bash]
    required_side_effect_tools: [Read]
    excluded_tools: [Edit, Write]                  # NEW FIELD — read-only by definition
    priority: medium

  - name: "Multi-Agent Orchestration"
    expected_tools: [Agent, Bash, Read]
    required_side_effect_tools: [Agent]
    priority: high                                  # likely zero matches; lint reports it honestly

  - name: "Paperclip Coordination"
    expected_tools: [Bash, Skill]
    required_side_effect_tools: [Skill]            # Bash dropped: ubiquitous (Day 1 table will show ~>0.8)
    priority: high
    # If Day 1 shows zero Skill spans: leave as-is; Coordination traces fall to
    # Code Implementation or unmapped. HONEST > clever. Arg-pattern matching is roadmap.
```

### Engine changes (`pipeline.py:109–174` region)

```pseudocode
# 1) excluded_tools gate — FIRST check in classify_membership:
if any(successful_call(trace, t) for t in op.excluded_tools):
    return NONE

# 2) primary label — after all memberships computed for a trace:
def primary_workflow(memberships):           # memberships: [(op, kind, recall)]
    full = [m for m in memberships if m.kind == FULL] or memberships
    return max(full, key = (m.recall, priority_rank(m.op), m.op.name))   # deterministic tiebreak

# 3) finding attribution — detectors currently run per-op over that op's members,
#    so one trace's findings recompute per op. Change: run tier-1 ONCE per trace,
#    attribute the findings to the trace's primary workflow only.
#    (Also a perf win: detection cost drops ~3x on overlapping traces.)
per_trace_findings = {t.trace_id: detect_tier1([t], median_steps) for t in all_traces}
workflow.findings = concat(per_trace_findings[t] for t in traces if primary(t) == workflow)
```

**Caveat:** `cluster_median_steps` (the loop-guard input) was per-op; with per-trace detection use the primary workflow's median. Slight behavior change — note in CHANGELOG, covered by the Day 7 labeling anyway.

View: `WorkflowView.cohort` unchanged; add `secondary_membership_count` per workflow and per-trace `primary` marker in divergence/finding rows (plugin types updated, rendering minimal — a badge).

**Edge cases:** trace matching nothing after the rewrite → unmapped (expected to RISE; that's honesty, the UI already renders unmapped); FULL in op A + ATTEMPTED in op B → primary prefers FULL regardless of recall; recall tie between Code Implementation and Research on an edit-heavy trace → impossible by construction (excluded_tools makes them disjoint) — assert disjointness of the two ops in a test.

### Honest Snapshot (`docs/honest-snapshot-1.md`)

Generated by a small script (rerunnable — it's also the delta tool for Day 14):

```
# Honest Snapshot — {date}, config {context_sha256[:8]}, engine {version}
traces analyzed: N (window: 7d)         unmapped: M (%)
per workflow:
  outcome_rate (passed/computable), human_escalation_rate,
  failure_reason histogram, finding count (deduped), token waste total,
  memberships: full/attempted, mean memberships per trace (global)
top 5 costliest traces (links)
```

**Day 5 exit:** mean memberships/trace ≤1.5; findings counted once; snapshot committed. This file is the baseline every later delta references.

---

## Phase test matrix (ships with the code, not after)

| Area | Fixture | Asserts |
|---|---|---|
| W1 | empty-window run | meta present, null rates, UI banner |
| W1 | zero-op YAML | from_yaml raises with path in message |
| W2 | parent+child usage spans | counted once |
| W2 | cache-heavy step | waste excludes cache_read |
| W3 | "0 errors" tail | pass |
| W3 | status ERROR + clean text | fail (rung 2 wins) |
| W3 | status OK + "error" text | pass (rung 2 wins) |
| W3 | blocked_on_user session | HUMAN_ESCALATION, pass-eligible, escalation_rate counts |
| W3 | orphan parent span | partial → non-computable, reason=partial_trace |
| W4 | edit-heavy trace | matches Code Impl, NOT Research |
| W4 | read-only trace | matches Research, NOT Code Impl |
| W4 | same trace, 2 ops pre-fix | finding appears once, under primary |
```
