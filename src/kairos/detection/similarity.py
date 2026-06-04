"""Jaccard similarity for normalized tool argument dicts."""

from __future__ import annotations

from typing import Any


def jaccard_dict_similarity(a: dict[str, Any] | None, b: dict[str, Any] | None) -> float:
    """Compute Jaccard similarity on flattened (key_path, value) pairs."""
    if a is None and b is None:
        return 1.0
    if a is None or b is None:
        return 0.0
    set_a = _flatten(a)
    set_b = _flatten(b)
    if not set_a and not set_b:
        return 1.0
    union = set_a | set_b
    intersection = set_a & set_b
    return len(intersection) / len(union)


def _flatten(d: dict[str, Any], prefix: str = "") -> set[tuple[str, str]]:
    """Recursively flatten dict to set of (key_path, token) pairs.

    String values are split into word-level tokens so that
    "alice chen python google" vs "alice chen python github"
    produces high overlap instead of zero (whole-string comparison).
    """
    items: set[tuple[str, str]] = set()
    for k, v in d.items():
        key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            items.update(_flatten(v, key))
        elif isinstance(v, list):
            for i, item in enumerate(v):
                if isinstance(item, dict):
                    items.update(_flatten(item, f"{key}.{i}"))
                else:
                    items.update(_tokenize(key, item))
        else:
            items.update(_tokenize(key, v))
    return items


def _tokenize(key: str, v: Any) -> set[tuple[str, str]]:
    """Convert a value to (key, token) pairs. Strings are split into words."""
    s = str(v).lower().strip() if not isinstance(v, bool) else str(v).lower()
    # For strings with multiple words, split into word tokens
    words = s.split()
    if len(words) > 1:
        return {(key, w) for w in words}
    return {(key, s)}
