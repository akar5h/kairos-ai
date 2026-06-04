"""Exit-bar guard: dropped subsystems must never reappear in the package.

The Kairos AI merge is additive but *curated* — intercept, runtime correction,
semantic recovery, the HDBSCAN discovery clustering, reporting, and baselines
were verified not imported by the engine and deliberately left behind. This
test fails loudly if any of them is reintroduced under ``src/kairos`` (either as
a package directory/module or as an import statement).
"""

from __future__ import annotations

from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "kairos"

# Package/module names that must not exist under src/kairos.
DROPPED_MODULES = (
    "intercept",
    "runtime_correction",
    "semantic_recovery",
    "reporting",
    "baselines",
    "demo_report",
)

# Specific dropped clustering members (clustering/ survives only as taxonomy/).
DROPPED_CLUSTERING = (
    "clusterer",
    "embedder",
    "subclustering",
    "health",
    "input_cleaner",
    "prefix_blocker",
)


def _all_py_files() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def test_dropped_modules_have_no_files() -> None:
    offenders: list[str] = []
    for path in _all_py_files():
        stem = path.stem
        parts = set(path.relative_to(SRC).parts)
        if stem in DROPPED_MODULES or parts & set(DROPPED_MODULES):
            offenders.append(str(path.relative_to(SRC)))
        if stem in DROPPED_CLUSTERING:
            offenders.append(str(path.relative_to(SRC)))
    assert not offenders, f"dropped modules present: {offenders}"


def test_no_imports_of_dropped_modules() -> None:
    needles = [f"kairos.{m}" for m in DROPPED_MODULES] + [f"kairos.clustering.{m}" for m in DROPPED_CLUSTERING]
    offenders: list[str] = []
    for path in _all_py_files():
        text = path.read_text(encoding="utf-8")
        for needle in needles:
            if needle in text:
                offenders.append(f"{path.relative_to(SRC)} -> {needle}")
    assert not offenders, f"dropped-module imports present: {offenders}"
