"""Console entrypoint: ``python -m kairos.api``.

Runs the Kairos OTLP ingest server on port 4318 (matching the CC exporter
default set by install.sh: ``OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318``).

Environment variables::

    KAIROS_PG_DSN   — libpq connection string (required for /v1/traces)
    KAIROS_HOST     — bind host (default: 0.0.0.0)
    KAIROS_PORT     — bind port (default: 4318)

Usage::

    uv run python -m kairos.api
    # or
    KAIROS_PORT=4319 uv run python -m kairos.api
"""

from __future__ import annotations

import os

import uvicorn

from kairos.api.app import create_app

if __name__ == "__main__":
    host = os.environ.get("KAIROS_HOST", "0.0.0.0")  # noqa: S104 — intentional bind-all default
    port = int(os.environ.get("KAIROS_PORT", "4318"))

    app = create_app()
    uvicorn.run(app, host=host, port=port)
