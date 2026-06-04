"""Smoke test: package imports and exposes a version."""

from __future__ import annotations

import kairos


def test_version_present() -> None:
    assert isinstance(kairos.__version__, str)
    assert kairos.__version__
