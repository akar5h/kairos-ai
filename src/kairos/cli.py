"""Kairos CLI — on-demand analysis entrypoint.

    kairos analyze --phoenix <id> [<id> ...] --context ctx.yaml
    kairos analyze --normalized-dir <dir> --context ctx.yaml
    kairos analyze --transcript <file.jsonl> --agent claude_code --context ctx.yaml

The source is explicit at the call site — exactly one of ``--phoenix``,
``--normalized-dir``, or ``--transcript``. No try-A-then-B fallback. Live
(Phoenix) is the primary path; ``--transcript`` is the offline backfill for
non-instrumentable / historical runs. Output is the AnalysisResult serialized
as JSON.
"""

from __future__ import annotations

import dataclasses
import hashlib
import importlib.metadata
import json
import urllib.error
import urllib.request
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click
from pydantic import BaseModel

from kairos.config import settings
from kairos.engine.pipeline import KairosEngine
from kairos.log import get_logger, setup_logging
from kairos.normalization.agents.claude_code import ClaudeCodeNormalizer
from kairos.normalization.agents.codex import CodexNormalizer
from kairos.normalization.agents.paperclip import PaperclipNormalizer
from kairos.readers.db import fetch_envelope_from_db
from kairos.store.json_store import JSONStore
from kairos.taxonomy.business_context import BusinessContext
from kairos.views.analysis_view import (
    DEFAULT_PHOENIX_BASE_URL,
    DEFAULT_PHOENIX_PROJECT,
    AnalysisMeta,
    build_analysis_view,
)

if TYPE_CHECKING:
    from kairos.models.trace import TraceEnvelope
    from kairos.normalization.agents.base import AgentTranscriptNormalizer

logger = get_logger(__name__)

# Offline-backfill adapters keyed by --agent. Each reads a single JSONL
# transcript via ``normalize_jsonl``. OpenCode's multi-file session storage is
# not a single JSONL, so it is driven via its Python API, not this CLI.
_TRANSCRIPT_ADAPTERS: dict[str, type[AgentTranscriptNormalizer]] = {
    "claude_code": ClaudeCodeNormalizer,
    "codex": CodexNormalizer,
    "paperclip": PaperclipNormalizer,
}


def _to_jsonable(obj: Any) -> Any:
    """Recursively convert the AnalysisResult tree to JSON-serializable data.

    Handles the mixed dataclass / pydantic / enum graph the engine returns.
    Fails loud on anything it does not recognize rather than degrading.
    """
    if obj is None or isinstance(obj, str | int | float | bool):
        return obj
    if isinstance(obj, BaseModel):
        return obj.model_dump(mode="json")
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: _to_jsonable(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {str(k): _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple | set):
        return [_to_jsonable(v) for v in obj]
    msg = f"cannot serialize {type(obj).__name__} to JSON"
    raise TypeError(msg)


def _load_from_phoenix(
    trace_ids: tuple[str, ...],
    endpoint: str,
    span_limit: int | None = None,
) -> list[TraceEnvelope]:
    """Load envelopes for the given trace IDs from the Kairos DB (F1.5).

    The ``--phoenix`` CLI flag now reads from the local spans DB instead of
    the Phoenix HTTP API.  ``endpoint`` and ``span_limit`` are accepted but
    ignored (kept for call-site compatibility).  Set KAIROS_PG_DSN to point
    at the kairos-pg instance.
    """
    import os  # noqa: PLC0415

    dsn = os.environ.get("KAIROS_PG_DSN", "")
    if not dsn:
        msg = "--phoenix flag requires KAIROS_PG_DSN env var (kairos reads from DB, not Phoenix)"
        raise RuntimeError(msg)
    return [fetch_envelope_from_db(tid, dsn) for tid in trace_ids]


def _load_from_transcript(path: Path, agent_kind: str) -> list[TraceEnvelope]:
    adapter = _TRANSCRIPT_ADAPTERS[agent_kind]()
    return [adapter.normalize_jsonl(path)]


def _build_meta(
    context_path: Path,
    context: BusinessContext,
    trace_count_fetched: int,
    trace_count_analyzed: int,
) -> AnalysisMeta:
    """Construct the AnalysisMeta provenance block for a view run."""
    raw_bytes = context_path.read_bytes()
    sha256 = hashlib.sha256(raw_bytes).hexdigest()
    return AnalysisMeta(
        engine_version=importlib.metadata.version("kairos-ai"),
        context_path=str(context_path.resolve()),
        context_sha256=sha256,
        operation_count=len(context.operations),
        trace_count_fetched=trace_count_fetched,
        trace_count_analyzed=trace_count_analyzed,
    )


def _resolve_phoenix_project_id(endpoint: str, project_name: str) -> str | None:
    """Resolve a Phoenix project name to its GraphQL node id (base64 relay id).

    Phoenix 15.x UI routes require the node id (e.g. ``UHJvamVjdDox``) — URLs
    built with the project *name* make the UI's projectLoaderQuery fail.
    Returns ``None`` on any failure (network, project missing, malformed
    response): deep-links then fall back to the name. Never raises — a broken
    link is better than a crashed analysis run.
    """
    url = endpoint.rstrip("/") + "/graphql"
    body = json.dumps({"query": "{ projects(first: 100) { edges { node { id name } } } }"}).encode()
    req = urllib.request.Request(  # noqa: S310 — caller-supplied Phoenix endpoint
        url,
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
            parsed = json.loads(resp.read())
        edges = parsed.get("data", {}).get("projects", {}).get("edges", [])
        for edge in edges:
            node = edge.get("node", {})
            if node.get("name") == project_name:
                return str(node["id"])
        logger.warning(
            "cli.phoenix_project_id.not_found",
            project=project_name,
            available=[e.get("node", {}).get("name") for e in edges],
            hint="Deep-links will use the project name and may 404 in the Phoenix UI.",
        )
    except (urllib.error.URLError, json.JSONDecodeError, KeyError, TimeoutError) as exc:
        logger.warning(
            "cli.phoenix_project_id.resolution_failed",
            endpoint=endpoint,
            project=project_name,
            error=str(exc),
            hint="Deep-links will use the project name and may 404 in the Phoenix UI.",
        )
    return None


def _load_from_dir(directory: Path) -> list[TraceEnvelope]:
    store = JSONStore(directory)
    envelopes: list[TraceEnvelope] = []
    for trace_id in store.list_ids():
        envelope = store.load(trace_id)
        if envelope is None:
            msg = f"trace {trace_id} listed by store but failed to load"
            raise RuntimeError(msg)
        envelopes.append(envelope)
    return envelopes


def _resolve_envelopes(
    phoenix_ids: tuple[str, ...],
    normalized_dir: Path | None,
    transcript_path: Path | None,
    agent_kind: str | None,
    phoenix_endpoint: str,
    span_limit: int | None = None,
) -> list[TraceEnvelope]:
    """Load envelopes from exactly one explicit source. No fallback chain."""
    sources = [bool(phoenix_ids), normalized_dir is not None, transcript_path is not None]
    if sum(sources) != 1:
        msg = "specify exactly one source: --phoenix <ids> OR --normalized-dir <dir> OR --transcript <file>"
        raise click.UsageError(msg)
    if transcript_path is not None and agent_kind is None:
        msg = "--transcript requires --agent to select the adapter"
        raise click.UsageError(msg)

    if phoenix_ids:
        return _load_from_phoenix(phoenix_ids, phoenix_endpoint, span_limit)
    if transcript_path is not None:
        assert agent_kind is not None  # noqa: S101 — guaranteed by the --agent check above
        return _load_from_transcript(transcript_path, agent_kind)
    assert normalized_dir is not None  # noqa: S101 — guaranteed by the exactly-one-source check
    return _load_from_dir(normalized_dir)


@click.group()
def cli() -> None:
    """Kairos AI — agent tracing + on-demand failure analysis."""
    setup_logging(level=settings.log_level, json_output=settings.log_format == "json")


@cli.command()
@click.option("--phoenix", "phoenix_ids", multiple=True, help="Phoenix trace id(s) — live source.")
@click.option(
    "--normalized-dir",
    "normalized_dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Directory of normalized IR JSON files — offline source.",
)
@click.option(
    "--transcript",
    "transcript_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Raw agent transcript JSONL — offline backfill source (use with --agent).",
)
@click.option(
    "--agent",
    "agent_kind",
    type=click.Choice(sorted(_TRANSCRIPT_ADAPTERS)),
    help="Transcript adapter for --transcript.",
)
@click.option(
    "--context",
    "context_path",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Business-context YAML.",
)
@click.option("--phoenix-endpoint", default="http://localhost:6006", show_default=True)
@click.option(
    "--span-limit",
    "span_limit",
    type=int,
    default=None,
    help="Max spans per trace from Phoenix (default: 100 000). Raise for very long traces.",
)
@click.option(
    "--output",
    "output_path",
    type=click.Path(dir_okay=False, path_type=Path),
    help="Write AnalysisResult JSON here (default: stdout).",
)
def analyze(
    phoenix_ids: tuple[str, ...],
    normalized_dir: Path | None,
    transcript_path: Path | None,
    agent_kind: str | None,
    context_path: Path,
    phoenix_endpoint: str,
    span_limit: int | None,
    output_path: Path | None,
) -> None:
    """Produce an AnalysisResult from a live (Phoenix) or offline source."""
    context = BusinessContext.from_yaml(context_path)
    envelopes = _resolve_envelopes(
        phoenix_ids, normalized_dir, transcript_path, agent_kind, phoenix_endpoint, span_limit
    )

    result = KairosEngine().analyze(envelopes, context)
    payload = json.dumps(_to_jsonable(result), indent=2)

    if output_path is not None:
        output_path.write_text(payload)
        logger.info("cli.analyze.written", output=str(output_path), traces=len(envelopes))
    else:
        click.echo(payload)


@cli.command()
@click.option("--phoenix", "phoenix_ids", multiple=True, help="Phoenix trace id(s) — live source.")
@click.option(
    "--normalized-dir",
    "normalized_dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Directory of normalized IR JSON files — offline source.",
)
@click.option(
    "--transcript",
    "transcript_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Raw agent transcript JSONL — offline backfill source (use with --agent).",
)
@click.option(
    "--agent",
    "agent_kind",
    type=click.Choice(sorted(_TRANSCRIPT_ADAPTERS)),
    help="Transcript adapter for --transcript.",
)
@click.option(
    "--context",
    "context_path",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Business-context YAML.",
)
@click.option("--phoenix-endpoint", default="http://localhost:6006", show_default=True)
@click.option(
    "--phoenix-base-url",
    default=DEFAULT_PHOENIX_BASE_URL,
    show_default=True,
    help="Phoenix UI base URL used to build trace deep-links.",
)
@click.option(
    "--phoenix-project",
    default=DEFAULT_PHOENIX_PROJECT,
    show_default=True,
    help="Phoenix project for deep-links.",
)
@click.option(
    "--span-limit",
    "span_limit",
    type=int,
    default=None,
    help="Max spans per trace from Phoenix (default: 100 000). Raise for very long traces.",
)
@click.option(
    "--output",
    "output_path",
    type=click.Path(dir_okay=False, path_type=Path),
    help="Write AnalysisView JSON here (default: stdout).",
)
def view(
    phoenix_ids: tuple[str, ...],
    normalized_dir: Path | None,
    transcript_path: Path | None,
    agent_kind: str | None,
    context_path: Path,
    phoenix_endpoint: str,
    phoenix_base_url: str,
    phoenix_project: str,
    span_limit: int | None,
    output_path: Path | None,
) -> None:
    """Emit the Paperclip-native view payload (cohort/divergence/correctness).

    Same sources as ``analyze``; output is the ``AnalysisView`` JSON the
    Paperclip-native UI renders, with a Phoenix deep-link on every finding row.
    """
    context = BusinessContext.from_yaml(context_path)
    envelopes = _resolve_envelopes(
        phoenix_ids, normalized_dir, transcript_path, agent_kind, phoenix_endpoint, span_limit
    )
    trace_count_fetched = len(envelopes)

    # Envelopes that are valid after normalization are the ones that enter the
    # pipeline.  The CLI normalizes at fetch time, so all resolved envelopes
    # are already normalized — trace_count_analyzed == trace_count_fetched here.
    # The distinction matters for future callers that may filter post-fetch.
    trace_count_analyzed = len(envelopes)

    result = KairosEngine().analyze(envelopes, context)
    meta = _build_meta(context_path, context, trace_count_fetched, trace_count_analyzed)

    # Phoenix 15.x UI routes need the project NODE id, not the name. Resolve it
    # once when the source is live Phoenix (endpoint reachable by definition);
    # failure falls back to the name with a warning — never crashes the run.
    phoenix_project_id: str | None = None
    if phoenix_ids:
        phoenix_project_id = _resolve_phoenix_project_id(phoenix_endpoint, phoenix_project)

    analysis_view = build_analysis_view(
        result,
        phoenix_base_url=phoenix_base_url,
        phoenix_project=phoenix_project,
        phoenix_project_id=phoenix_project_id,
        meta=meta,
    )
    payload = analysis_view.model_dump_json(indent=2)

    if output_path is not None:
        output_path.write_text(payload)
        logger.info("cli.view.written", output=str(output_path), traces=len(envelopes))
    else:
        click.echo(payload)
