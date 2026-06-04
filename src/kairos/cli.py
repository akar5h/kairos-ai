"""Kairos CLI — on-demand analysis entrypoint.

    kairos analyze --phoenix <id> [<id> ...] --context ctx.yaml
    kairos analyze --normalized-dir <dir> --context ctx.yaml

The source is explicit at the call site — exactly one of ``--phoenix`` or
``--normalized-dir``. No try-A-then-B fallback. Output is the AnalysisResult
serialized as JSON.
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

from kairos.engine.pipeline import KairosEngine
from kairos.log import get_logger
from kairos.readers.phoenix import PhoenixReader
from kairos.store.json_store import JSONStore
from kairos.taxonomy.business_context import BusinessContext

if TYPE_CHECKING:
    from kairos.models.trace import TraceEnvelope

logger = get_logger(__name__)


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


def _load_from_phoenix(trace_ids: tuple[str, ...], endpoint: str) -> list[TraceEnvelope]:
    reader = PhoenixReader(endpoint=endpoint)
    return [reader.fetch_envelope(tid) for tid in trace_ids]


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


@click.group()
def cli() -> None:
    """Kairos AI — agent tracing + on-demand failure analysis."""


@cli.command()
@click.option("--phoenix", "phoenix_ids", multiple=True, help="Phoenix trace id(s) — live source.")
@click.option(
    "--normalized-dir",
    "normalized_dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Directory of normalized IR JSON files — offline source.",
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
    "--output",
    "output_path",
    type=click.Path(dir_okay=False, path_type=Path),
    help="Write AnalysisResult JSON here (default: stdout).",
)
def analyze(
    phoenix_ids: tuple[str, ...],
    normalized_dir: Path | None,
    context_path: Path,
    phoenix_endpoint: str,
    output_path: Path | None,
) -> None:
    """Produce an AnalysisResult from a live (Phoenix) or offline source."""
    if bool(phoenix_ids) == bool(normalized_dir):
        msg = "specify exactly one source: --phoenix <ids> OR --normalized-dir <dir>"
        raise click.UsageError(msg)

    context = BusinessContext.from_yaml(context_path)
    if phoenix_ids:
        envelopes = _load_from_phoenix(phoenix_ids, phoenix_endpoint)
    else:
        assert normalized_dir is not None  # noqa: S101 — guaranteed by the exactly-one-source check
        envelopes = _load_from_dir(normalized_dir)

    result = KairosEngine().analyze(envelopes, context)
    payload = json.dumps(_to_jsonable(result), indent=2)

    if output_path is not None:
        output_path.write_text(payload)
        logger.info("cli.analyze.written", output=str(output_path), traces=len(envelopes))
    else:
        click.echo(payload)
