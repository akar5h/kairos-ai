/**
 * TypeScript types derived from the Kairos read API.
 *
 * Sources:
 *   - src/kairos/models/trace.py  — Step, TraceEnvelope
 *   - src/kairos/models/enums.py  — StepType, StepStatus, TerminalStatus, OutputType
 *   - src/kairos/api/read.py      — TraceSummary, SessionSummary, TraceInSession,
 *                                   RawSpan, SearchHits, FindingRow, LabelRow
 */

// ── Enums ──────────────────────────────────────────────────────────────────────

export type StepType = "llm" | "tool_call" | "retrieval" | "agent" | "other";

export type StepStatus = "ok" | "error";

export type StepStatusSource =
  | "attr_success"
  | "otel_status"
  | "kairos_outcome"
  | "adapter"
  | "textual"
  | "none";

export type TerminalStatus =
  | "completed"
  | "error"
  | "timeout"
  | "human_escalation"
  | "unknown";

export type OutputType = "text" | "file" | "api_call" | "mixed" | "unknown";

// ── Step ──────────────────────────────────────────────────────────────────────

export interface Step {
  step_index: number;
  step_type: StepType;
  agent_name: string | null;
  node_name: string | null;

  // Tool call fields
  tool_name: string | null;
  tool_args: Record<string, unknown> | null;
  tool_args_normalized: Record<string, unknown> | null;
  tool_output: string | null;

  // LLM fields
  llm_input: string | null;
  llm_output: string | null;
  llm_model: string | null;

  // Retrieval fields
  retrieval_query: string | null;
  retrieval_chunks: string[] | null;

  // Metrics
  input_tokens: number | null;
  output_tokens: number | null;
  cache_read_tokens: number;
  total_tokens: number | null;
  tokens_instrumented: boolean;
  latency_ms: number | null;

  // Status
  status: StepStatus;
  status_source: StepStatusSource;
  error_message: string | null;

  // Raw span attributes (OTel path only)
  attrs: Record<string, unknown> | null;

  // Hierarchy
  parent_step_index: number | null;

  // Timestamps (ISO strings from JSON serialization)
  started_at: string | null;
  ended_at: string | null;

  // Provenance
  source_observation_id: string | null;
}

// ── TraceEnvelope ─────────────────────────────────────────────────────────────

export interface TraceEnvelope {
  // Identity
  trace_id: string;
  source: string;
  source_trace_id: string | null;

  // Intent
  user_input: string | null;
  system_prompt: string | null;
  agent_type: string | null;

  // Execution
  steps: Step[];

  // Aggregated metrics
  total_tokens: number;
  total_input_tokens: number;
  total_output_tokens: number;
  total_latency_ms: number;
  step_count: number;

  // Terminal state
  terminal_status: TerminalStatus;
  output_type: OutputType;

  // Derived
  tool_sequence: string[];
  tool_bigrams: [string, string][];
  unique_tool_count: number;
  error_count: number;
  has_retrieval: boolean;
  retrieval_step_count: number;

  // Metadata
  session_id: string | null;
  user_id: string | null;
  tags: string[];
  metadata: Record<string, unknown> | null;

  // Timestamps
  started_at: string | null;
  ended_at: string | null;
  normalized_at: string;

  // Provenance
  source_metadata: Record<string, unknown> | null;

  // Correlation
  correlation_key_value: string | null;

  // Integrity
  integrity: "complete" | "partial";

  // Validation
  is_valid: boolean;
  validation_warnings: string[];
}

// ── TraceSummary (GET /v1/traces) ─────────────────────────────────────────────

export interface TraceSummary {
  trace_id: string;
  started_at: string | null;
  span_count: number;
  error_count: number;
}

// ── SessionSummary (GET /v1/sessions) ────────────────────────────────────────

export interface SessionSummary {
  session_id: string;
  trace_count: number;
  span_count: number;
  error_count: number;
  started_at: string | null;
  ended_at: string | null;
  tools: string[];
}

// ── TraceInSession (GET /v1/sessions/{session_id}) ────────────────────────────

export interface TraceInSession {
  trace_id: string;
  span_count: number;
  error_count: number;
  started_at: string | null;
  ended_at: string | null;
  tools: string[];
}

// ── RawSpan (GET /v1/traces/{trace_id}/spans) ────────────────────────────────

export interface RawSpan {
  span_id: string;
  parent_span_id: string | null;
  name: string;
  tool_name: string | null;
  status_code: string | null;
  start_time: string | null;
  end_time: string | null;
  attributes: Record<string, unknown>;
}

// ── SearchHits (GET /v1/search) ───────────────────────────────────────────────

export interface SearchSessionHit {
  session_id: string;
  trace_count: number;
  span_count: number;
  started_at: string | null;
}

export interface SearchTraceHit {
  trace_id: string;
  session_id: string | null;
  span_count: number | null;
  error_count: number | null;
  started_at: string | null;
  snippet?: string;
}

export interface SearchSpanHit {
  span_id: string;
  trace_id: string;
  session_id: string | null;
  name: string;
  tool_name: string | null;
  status_code: string | null;
  started_at: string | null;
}

export interface SearchHits {
  sessions: SearchSessionHit[];
  traces: SearchTraceHit[];
  spans: SearchSpanHit[];
}

// ── FindingRow (GET /v1/findings) ─────────────────────────────────────────────

export interface FindingRow {
  night_id: string;
  trace_id: string;
  unit_id: string;
  workflow: string;
  agent: string;
  detector: string;
  severity: string;
  evidence_steps: number[];
  tokens: number;
  struggle: number;
  outcome: string;
  config_hash: string;
  ingested_at: string;
}

// ── LabelRow (GET /v1/labels) ─────────────────────────────────────────────────

export interface LabelRow {
  id: string;
  trace_id: string;
  question: string;
  answer: string;
  verdict: string;
  label_class: string;
  ts: string;
}

// ── CreateLabel (POST /v1/labels body) ───────────────────────────────────────

export type Verdict = "tp" | "fp" | "fn";

export interface CreateLabelBody {
  trace_id: string;
  answer: string;
  question?: string;
  verdict?: Verdict | null;
  label_class?: string;
}

// ── ClusterSummary (GET /v1/clusters) ─────────────────────────────────────────

export type ClusterStatus = "open" | "resolved" | "regressed";

export interface ClusterSummary {
  cluster_key: string;
  status: ClusterStatus;
  trace_count: number;
  min_night_id: string | null;
  kinds: string[];
  sample_features: Record<string, unknown>;
}

// ── ClusterStatusUpdate (POST /v1/clusters/{key}/resolve|regress) ─────────────

export interface ClusterStatusUpdate {
  cluster_key: string;
  status: ClusterStatus;
  updated: boolean;
}

// ── ClusterTraceMember (GET /v1/clusters/{cluster_key}/traces) ────────────────

export interface ClusterTraceMember {
  trace_id: string;
  labeled: boolean;
}

// ── StatsResponse (GET /v1/stats) ─────────────────────────────────────────────

export interface StatsResponse {
  total_sessions: number;
  total_spans: number;
  total_input_tokens: number;
  total_output_tokens: number;
  total_cache_read_tokens: number;
  total_cache_creation_tokens: number;
  total_errors: number;
  estimated_cost_usd: number;
  sessions_today: number;
  spans_today: number;
}

// ── ClusterRefreshResponse (POST /v1/clusters/refresh) ────────────────────────

export interface ClusterRefreshResponse {
  status: string;
  clusters_found: number;
  traces_processed: number;
}
