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
import json
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
from kairos.readers.phoenix import PhoenixReader
from kairos.store.json_store import JSONStore
from kairos.taxonomy.business_context import BusinessContext
from kairos.views.analysis_view import (
    DEFAULT_PHOENIX_BASE_URL,
    DEFAULT_PHOENIX_PROJECT,
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
    if span_limit is not None:
        reader = PhoenixReader(endpoint=endpoint, span_limit=span_limit)
    else:
        reader = PhoenixReader(endpoint=endpoint)
    return [reader.fetch_envelope(tid) for tid in trace_ids]


def _load_from_transcript(path: Path, agent_kind: str) -> list[TraceEnvelope]:
    adapter = _TRANSCRIPT_ADAPTERS[agent_kind]()
    return [adapter.normalize_jsonl(path)]


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

    result = KairosEngine().analyze(envelopes, context)
    analysis_view = build_analysis_view(result, phoenix_base_url=phoenix_base_url, phoenix_project=phoenix_project)
    payload = analysis_view.model_dump_json(indent=2)

    if output_path is not None:
        output_path.write_text(payload)
        logger.info("cli.view.written", output=str(output_path), traces=len(envelopes))
    else:
        click.echo(payload)
