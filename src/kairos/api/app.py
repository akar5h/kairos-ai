"""Kairos P2 FastAPI application skeleton (F1.4).

Entry point for the Kairos API server.  Keep this file minimal — it creates
the FastAPI instance, mounts routers, and exposes ``create_app()`` for
tests and for the ``__main__`` launcher.

Usage (direct)::

    uv run python -m kairos.api

Environment variables::

    KAIROS_PG_DSN   — libpq connection string (required by /v1/traces)
    KAIROS_HOST     — bind host (default: 0.0.0.0)
    KAIROS_PORT     — bind port (default: 4318)
"""

from __future__ import annotations

import fastapi

from kairos.api.otlp import router as otlp_router


def create_app() -> fastapi.FastAPI:
    """Create and return the Kairos FastAPI application instance."""
    app = fastapi.FastAPI(
        title="Kairos",
        description="Kairos AI — OTLP ingest + P2 API",
        version="0.1.0",
    )

    @app.get("/health", tags=["meta"])
    async def health() -> dict[str, str]:
        """Liveness probe — always returns ok."""
        return {"status": "ok"}

    app.include_router(otlp_router)

    return app
