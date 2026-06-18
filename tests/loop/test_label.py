"""Tests for P4.1 label.py — ClusterInsight + LLM cluster labeling.

Coverage:
  1. _build_prompt contains cluster_key and trace tool sequences
  2. label_cluster returns None gracefully when cluster has no traces
  3. label_cluster auto_approve logic: confidence > 0.8 AND is_coherent
  4. auto_approve NOT taken from LLM response field
  5. label_all_unlabeled skips clusters already in cluster_insights (DB test)
  6. Migration 0018 applies cleanly (DB test)
"""

from __future__ import annotations

import os
import uuid
from unittest.mock import MagicMock, patch

import pytest

from kairos.loop.label import (
    ClusterInsightResponse,
    _build_prompt,
    label_cluster,
)
from kairos.models.enums import StepStatus, StepType
from kairos.models.trace import Step, TraceEnvelope

# ── DB guard ──────────────────────────────────────────────────────────────────

_DSN = os.environ.get("KAIROS_PG_DSN", "").strip()
_DB_AVAILABLE = bool(_DSN)

_skip_no_db = pytest.mark.skipif(
    not _DB_AVAILABLE,
    reason="KAIROS_PG_DSN not set — kairos-pg not reachable",
)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_step(index: int, tool_name: str = "Bash", status: StepStatus = StepStatus.OK) -> Step:
    return Step(
        step_index=index,
        step_type=StepType.TOOL_CALL,
        tool_name=tool_name,
        status=status,
    )


def _make_envelope(trace_id: str, tools: list[str] | None = None) -> TraceEnvelope:
    steps = [_make_step(i, t) for i, t in enumerate(tools or ["Read", "Bash", "Edit"])]
    env = TraceEnvelope(
        trace_id=trace_id,
        steps=steps,
        total_tokens=500,
        total_latency_ms=1200,
    )
    return env


# ── 1. _build_prompt unit tests ───────────────────────────────────────────────


def test_build_prompt_contains_cluster_key():
    """Prompt includes the cluster_key."""
    env = _make_envelope("trace-aabb1122")
    prompt = _build_prompt("Bash|Read::token_z", [env], {})
    assert "Bash|Read::token_z" in prompt


def test_build_prompt_contains_tool_sequence():
    """Prompt includes tool sequence for each trace."""
    env = _make_envelope("trace-ccdd3344", tools=["Read", "Grep", "Write"])
    prompt = _build_prompt("some::cluster", [env], {"trace-ccdd3344": "fail"})
    # Tool sequence joined by →
    assert "Read" in prompt
    assert "Grep" in prompt
    assert "Write" in prompt


def test_build_prompt_includes_outcome():
    """Prompt includes the outcome label for each trace."""
    env = _make_envelope("trace-eeff5566")
    prompt = _build_prompt("cluster::key", [env], {"trace-eeff5566": "fail"})
    assert "fail" in prompt


def test_build_prompt_no_raw_args():
    """Prompt does not contain raw tool_args — only tool names."""
    step = Step(
        step_index=0,
        step_type=StepType.TOOL_CALL,
        tool_name="Bash",
        tool_args={"command": "rm -rf /important/data"},
        status=StepStatus.OK,
    )
    env = TraceEnvelope(trace_id="trace-sensitive", steps=[step], total_tokens=100)
    prompt = _build_prompt("test::cluster", [env], {})
    assert "rm -rf" not in prompt
    assert "/important/data" not in prompt


def test_build_prompt_multiple_traces():
    """Prompt includes all traces with their indices."""
    envs = [_make_envelope(f"trace-{i:04d}") for i in range(3)]
    prompt = _build_prompt("multi::cluster", envs, {})
    assert "Trace 1" in prompt
    assert "Trace 2" in prompt
    assert "Trace 3" in prompt


# ── 2. label_cluster — no traces ─────────────────────────────────────────────


@_skip_no_db
def test_label_cluster_no_traces_returns_none():
    """label_cluster returns None when cluster_key has no discovery_queue rows."""
    nonexistent_key = "no-such-cluster::outcome_only-" + str(uuid.uuid4())[:8]
    result = label_cluster(nonexistent_key, _DSN)
    assert result is None


# ── 3 & 4. label_cluster auto_approve logic (mocked LLM) ─────────────────────


def _make_llm_response(confidence: float, is_coherent: bool, auto_approve: bool = False) -> ClusterInsightResponse:
    return ClusterInsightResponse(
        pattern_name="test_pattern",
        description="Agent does wrong thing.",
        discriminator_hint="Check tool X before Y.",
        root_cause="Missing validation.",
        confidence=confidence,
        is_coherent=is_coherent,
        auto_approve=auto_approve,
    )


@_skip_no_db
def test_label_cluster_auto_approve_high_confidence_coherent():
    """confidence > 0.8 AND is_coherent=True → auto_approve=True."""
    cluster_key = "test::auto-approve-true-" + str(uuid.uuid4())[:8]
    trace_id = str(uuid.uuid4()).replace("-", "")

    # Seed a span so fetch_envelope_from_db can find it.
    _seed_minimal_span(trace_id)
    _seed_discovery_queue(cluster_key, trace_id)

    llm_resp = _make_llm_response(confidence=0.9, is_coherent=True, auto_approve=False)
    with patch("kairos.loop.label.LLMClient") as mock_client:
        instance = MagicMock()
        instance.generate.return_value = llm_resp
        instance.model = "test-model"
        mock_client.return_value = instance

        result = label_cluster(cluster_key, _DSN)

    _cleanup_discovery_queue(cluster_key)
    _cleanup_cluster_insights(cluster_key)

    assert result is not None
    assert result.auto_approve is True


@_skip_no_db
def test_label_cluster_auto_approve_low_confidence():
    """confidence <= 0.8 → auto_approve=False."""
    cluster_key = "test::auto-approve-low-conf-" + str(uuid.uuid4())[:8]
    trace_id = str(uuid.uuid4()).replace("-", "")

    _seed_minimal_span(trace_id)
    _seed_discovery_queue(cluster_key, trace_id)

    llm_resp = _make_llm_response(confidence=0.5, is_coherent=True)
    with patch("kairos.loop.label.LLMClient") as mock_client:
        instance = MagicMock()
        instance.generate.return_value = llm_resp
        instance.model = "test-model"
        mock_client.return_value = instance

        result = label_cluster(cluster_key, _DSN)

    _cleanup_discovery_queue(cluster_key)
    _cleanup_cluster_insights(cluster_key)

    assert result is not None
    assert result.auto_approve is False


@_skip_no_db
def test_label_cluster_auto_approve_incoherent():
    """is_coherent=False → auto_approve=False even with high confidence."""
    cluster_key = "test::auto-approve-incoherent-" + str(uuid.uuid4())[:8]
    trace_id = str(uuid.uuid4()).replace("-", "")

    _seed_minimal_span(trace_id)
    _seed_discovery_queue(cluster_key, trace_id)

    llm_resp = _make_llm_response(confidence=0.95, is_coherent=False)
    with patch("kairos.loop.label.LLMClient") as mock_client:
        instance = MagicMock()
        instance.generate.return_value = llm_resp
        instance.model = "test-model"
        mock_client.return_value = instance

        result = label_cluster(cluster_key, _DSN)

    _cleanup_discovery_queue(cluster_key)
    _cleanup_cluster_insights(cluster_key)

    assert result is not None
    assert result.auto_approve is False


@_skip_no_db
def test_label_cluster_auto_approve_ignores_llm_field():
    """LLM response auto_approve=True with confidence=0.3 → Python overrides to False."""
    cluster_key = "test::auto-approve-override-" + str(uuid.uuid4())[:8]
    trace_id = str(uuid.uuid4()).replace("-", "")

    _seed_minimal_span(trace_id)
    _seed_discovery_queue(cluster_key, trace_id)

    # LLM claims auto_approve=True but confidence is low
    llm_resp = _make_llm_response(confidence=0.3, is_coherent=True, auto_approve=True)
    with patch("kairos.loop.label.LLMClient") as mock_client:
        instance = MagicMock()
        instance.generate.return_value = llm_resp
        instance.model = "test-model"
        mock_client.return_value = instance

        result = label_cluster(cluster_key, _DSN)

    _cleanup_discovery_queue(cluster_key)
    _cleanup_cluster_insights(cluster_key)

    assert result is not None
    # Python rule: confidence=0.3 < 0.8 → auto_approve must be False
    assert result.auto_approve is False


# ── 5. label_all_unlabeled skips already-labeled clusters ─────────────────────


@_skip_no_db
def test_label_all_unlabeled_skips_existing():
    """Clusters already in cluster_insights are not re-labeled."""
    import psycopg

    from kairos.loop.label import label_all_unlabeled

    already_labeled_key = "test::already-labeled-" + str(uuid.uuid4())[:8]
    new_key = "test::new-cluster-" + str(uuid.uuid4())[:8]
    trace_id_new = str(uuid.uuid4()).replace("-", "")

    # Insert a pre-existing insight for already_labeled_key.
    with psycopg.connect(_DSN) as conn:
        conn.execute(
            """
            INSERT INTO cluster_insights
              (id, cluster_key, pattern_name, confidence, is_coherent, auto_approve)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (str(uuid.uuid4()), already_labeled_key, "existing_pattern", 0.9, True, True),
        )
        conn.commit()

    # Seed new cluster in discovery_queue + minimal span.
    _seed_minimal_span(trace_id_new)
    _seed_discovery_queue(new_key, trace_id_new)

    llm_resp = _make_llm_response(confidence=0.7, is_coherent=True)
    with patch("kairos.loop.label.LLMClient") as mock_client:
        instance = MagicMock()
        instance.generate.return_value = llm_resp
        instance.model = "test-model"
        mock_client.return_value = instance

        results = label_all_unlabeled(_DSN)

    labeled_keys = {r.cluster_key for r in results}
    # already_labeled_key was skipped
    assert already_labeled_key not in labeled_keys
    # new_key was labeled
    assert new_key in labeled_keys

    _cleanup_cluster_insights(already_labeled_key)
    _cleanup_discovery_queue(new_key)
    _cleanup_cluster_insights(new_key)


# ── 6. Migration 0018 applies cleanly ─────────────────────────────────────────


@_skip_no_db
def test_migration_0018_applies_cleanly():
    """Migration 0018 is idempotent and cluster_insights table exists after apply."""
    import psycopg

    from kairos.loop.db import apply_migrations

    # Should not raise even when already applied.
    apply_migrations()

    # Verify table exists by querying it.
    with psycopg.connect(_DSN) as conn:
        conn.execute("SELECT id, cluster_key FROM cluster_insights LIMIT 0")


# ── DB helpers ────────────────────────────────────────────────────────────────


def _seed_minimal_span(trace_id: str) -> None:
    """Insert a minimal spans row so fetch_envelope_from_db can return an envelope."""
    import psycopg
    from psycopg.types.json import Jsonb

    span_id = uuid.uuid4().hex[:16]
    with psycopg.connect(_DSN) as conn:
        conn.execute(
            """
            INSERT INTO spans
              (trace_id, span_id, name, start_time, end_time, status_code, attributes)
            VALUES (%s, %s, %s, now(), now(), %s, %s)
            ON CONFLICT DO NOTHING
            """,
            (trace_id, span_id, "kairos.task", "OK", Jsonb({})),
        )
        conn.commit()


def _seed_discovery_queue(cluster_key: str, trace_id: str) -> None:
    """Insert a discovery_queue row for test purposes."""
    import psycopg
    from psycopg.types.json import Jsonb

    row_id = uuid.uuid4().hex[:24]
    with psycopg.connect(_DSN) as conn:
        conn.execute(
            """
            INSERT INTO discovery_queue (id, night_id, kind, trace_id, cluster_key, features)
            VALUES (%s, CURRENT_DATE, %s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING
            """,
            (row_id, "anomaly", trace_id, cluster_key, Jsonb({"tool_signature": "Bash|Read"})),
        )
        conn.commit()


def _cleanup_discovery_queue(cluster_key: str) -> None:
    import psycopg

    with psycopg.connect(_DSN) as conn:
        conn.execute("DELETE FROM discovery_queue WHERE cluster_key = %s", (cluster_key,))
        conn.commit()


def _cleanup_cluster_insights(cluster_key: str) -> None:
    import psycopg

    with psycopg.connect(_DSN) as conn:
        conn.execute("DELETE FROM cluster_insights WHERE cluster_key = %s", (cluster_key,))
        conn.commit()
