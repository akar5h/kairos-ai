"""P4.3 — Cluster diff: detect new clusters after a discover.py run."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ClusterDiff:
    new_keys: list[str]
    """cluster_keys present in after but not before."""
    removed_keys: list[str]
    """cluster_keys present in before but not after (rare; logged by caller)."""
    unchanged_count: int
    """Number of keys present in both before and after."""


def diff_clusters(before: set[str], after: set[str]) -> ClusterDiff:
    """Compute the diff between two cluster-key snapshots.

    Pure function — no DB, no side effects.
    """
    new = sorted(after - before)
    removed = sorted(before - after)
    unchanged = len(before & after)
    return ClusterDiff(new_keys=new, removed_keys=removed, unchanged_count=unchanged)
