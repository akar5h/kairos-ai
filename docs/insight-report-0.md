# Kairos Insight Report #0 — Coordination Waste in Paperclip Agents

*Date: 2026-06-12 · Window: last 36 hours · Method: manual (Phoenix GraphQL span pull + Claude Code transcript cross-reference) · Status: baseline artifact for the 14-day sprint's first intervention (Day 13)*

This is the report the nightly loop (sprint Days 11–12) will eventually produce automatically. Produced by hand once to (a) fix the worst live failure pattern with evidence, (b) serve as the "before" baseline for the first measured intervention, (c) validate that the loop's report format answers real questions.

---

## 1. Headline numbers

| Metric | Value |
|---|---|
| Traces (36h, Phoenix project `default`) | 110 (91 with tool activity) |
| Tool calls | 1,486 |
| **Bash share of all tool calls** | **1,120 (75%)** |
| Bash commands across 132 session transcripts | 3,979 |
| **Bash commands that are Paperclip-API curl/auth rituals** | **2,690 (68%)** |
| Native tool usage | Read 183 · Edit 59 · Skill 54 · Write 52 · **Grep 4 · Glob 3 · Agent 2** |
| Consecutive same-tool runs ≥4 | 117 (62 claudecoder · 33 cto · 12 qaengineer) |
| Trace tool-call counts | median 8 · p90 48 · max 92 |
| `blocked_on_user` phases | 1,546 (one per tool call) · 254s total wait · p95 691ms · max 14s |
| LLM-level health | 1,397/1,401 requests succeeded on attempt 1 · 15 failures total |

**One sentence:** the agents are healthy at the model level and wasteful at the architecture level — two-thirds of their tool activity is hand-rolled coordination bureaucracy against the Paperclip API, re-executed from scratch every heartbeat.

## 2. Failure mechanisms (ranked by waste)

### M1 — Polling-as-conversation
Session `5b5e0b06` executed the byte-identical inbox poll **81 times** in one session:
```
echo "WAKE=$PAPERCLIP_WAKE_REASON TASK=$PAPERCLIP_TASK_ID"; curl -s "$PAPERCLIP_API_URL/api/agents/me/inbox-lite" -H "Au…"
```
plus the comment-id variant ×15. Every poll = one full LLM round-trip (context replay + generation) to learn "nothing new." The agent busy-waits because nothing tells it not to and it has no wait primitive. Paperclip already wakes agents with `PAPERCLIP_WAKE_REASON` — the polling is pure loss.

### M2 — Credential re-derivation ritual
Session `f41206c8` re-extracted `BOARD_TOKEN` from `~/.paperclip/auth.json` **43×** and `AUTH_TOKEN` **21×** (78× counting variants). Root cause is environmental: the Bash tool's shell state does not persist between calls, agent instructions supply a stateful recipe, so the agent re-runs auth setup before every API call, forever. The agent is obeying; the environment is wasteful.

### M3 — Per-heartbeat startup ritual
Of every session's first three commands, **260/396 (66%) are Paperclip-API curls** — fetch issue, check inbox, read comments. Every heartbeat re-discovers state the orchestrator knew at wake time. 5–15 calls per heartbeat re-fetching injectable context.

### M4 — Shell instead of native tools
Grep/Glob called 4 and 3 times in 36 hours; the equivalent ran through Bash (`fs_inspect` = 542 commands, 14%). Bash output is unstructured and untruncated → context bloat per call. Compounding: every call carries a `blocked_on_user` permission phase (3× span inflation, 254s aggregate wait).

### Worst traces (Phoenix links)
| Run | Trace |
|---|---|
| 47× consecutive Bash (claudecoder) | `http://localhost:6006/projects/UHJvamVjdDox/traces/568077220b64c8cb05e6c2107f26fdd9` |
| 32× + 25× Bash, same trace (claudecoder) | `http://localhost:6006/projects/UHJvamVjdDox/traces/d4c37eddc89d0d55232eb01905846379` |
| 29× Bash (claudecoder) | `http://localhost:6006/projects/UHJvamVjdDox/traces/ccbe004c1d74fca49722bd70d6c143ee` |
| 28× Bash in 92-call trace (claudecoder) | `http://localhost:6006/projects/UHJvamVjdDox/traces/4d470c8f8b30ff48f8cbd3dcca8c1269` |
| Whole heartbeat = 23× Bash (cto) | `http://localhost:6006/projects/UHJvamVjdDox/traces/3bef17ac1e608b18cbffe0003cd46e9c` |
| Whole heartbeat = 21× Bash (cto) | `http://localhost:6006/projects/UHJvamVjdDox/traces/2ab4ce5cab3bd57272eefd56051047da` |

## 3. Root cause, one level deeper

The agent does what is cheapest to **express**, not what is cheapest to **execute**. Its instructions (AGENTS.md) teach curl recipes, so curl is the path of least resistance — even though **Paperclip already ships an MCP server** (`~/dev/paperclip/packages/mcp-server`) exposing exactly the needed tools: `list_issues`, `list_comments`, `checkout_issue`, `update_issue`, `add_comment`, `create_issue`, approvals, generic `api_request`, and a wait-for-service primitive. The tool layer exists; the agents were never wired to it. This is a configuration gap, not missing software, and **no Paperclip fork is required** for the fix (source is local + MIT-licensed in any case).

## 4. Intervention plan (feeds sprint Day 13)

Staged for clean measurement — one agent first (CTO: smallest blast radius, clearest ritual), others held as controls:

1. **I1 — Wire the MCP server** into the CTO agent's session config; coordination via `paperclip` MCP tools instead of curl. *(agent-side config)*
2. **I2 — Rewrite the coordination section of CTO's AGENTS.md:** use MCP tools; never poll (inbox empty → end turn; the orchestrator wakes you); never re-derive tokens (env provides them); prefer Grep/Glob/Read over shell equivalents. *(instructions)*
3. **I3 — Permission allowlist** for the MCP coordination tools + read-only commands, reserving prompts for risky calls. *(settings)*

Apply I1+I2 together (they are one coherent change: "use the tool layer"), I3 the night after if prompts dominate residual latency.

### Expected deltas (measured per agent, nightly, from span data)
| Metric | Baseline (CTO, 36h) | Expected |
|---|---|---|
| Bash share of tool calls | ~75% | < 30% |
| Identical-command repeats ≥3 per session | pervasive (M1/M2) | ~0 |
| Tool calls per heartbeat (median) | 8 (p90 48) | ↓ 30–50% |
| Outcome rate / escalation rate | per Honest Snapshot | **must not degrade** (guardrail) |

A delta that improves cost but degrades outcome is a regression (sprint edge-register #10).

## 5. Bonus discoveries for the sprint (de-risking)

- Spans already carry **`paperclip.issue`** and **`paperclip.run_id`** → Day 11's trace→issue join is a span-attribute read, not an activity_log correlation.
- `llm_request` spans carry top-level **`input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_creation_tokens`** (plus `model`, `stop_reason`, `attempt`, `success`, `duration_ms`, `ttft_ms`) → Day 2's discovery step is done; mapping starts immediately.
- `claude_code.tool` spans carry **`tool_name`** top-level but **no args/output** → tier-1 redundancy detection on live spans is name+timing only; transcript cross-reference (this report's method) or emitter enrichment is needed for arg-level Jaccard. Flag for Day 7's labeling and the roadmap's emitter work.
- Per-trace span triplet (`tool` / `tool.execution` / `tool.blocked_on_user`) means span counts ≈ 3× tool calls — normalize before any "steps" math.
