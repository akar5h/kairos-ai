"""Eval corpus assembler — versioned, fixed ground-truth corpus.

Corpus sources (three pools):

1. tau-bench: reward-labeled traces in eval/corpus/taubench/ (161 labels, 161 traces).
   Label mapping: reward=1.0 → outcome_truth="pass"; reward=0.0 → outcome_truth="fail";
   PARTIAL → excluded from outcome precision math (tracked, never fabricated).

2. Owner labels: two sources mapped to per-detector + outcome ground truth:
   - docs/spotcheck-day4.md (20 traces): AGREE column (Y/N/?) + freetext comment.
     Mapping rule (mirrors session-quality-precision.md methodology):
       Y  → outcome correct; freetext comments parsed for detector-should-fire signals
       N  → outcome WRONG (engine error on this trace)
       ?  → verdict UNCERTAIN → excluded from outcome precision
     Detector ground truth from freetext: see _SPOTCHECK_DETECTOR_TRUTH below.
   - eval/review/answers.jsonl (15 entries, some duplicate trace_ids):
     verdict_shown=pass with positive answer → outcome_truth="pass"
     Answer containing explicit failure signal (tool_use_error, silent failure,
     failures stacking) → detector_truth flags set per detector.
     Vague/inconclusive answers → UNKNOWN, excluded from precision math.

3. Live-corpus snapshot: 345 trace IDs from the backfilled kairos-pg corpus
   (deterministic list persisted at eval/corpus/live_trace_ids.txt).
   These have NO outcome ground truth — used for stability/fire-rate signals only.

Corpus hash: SHA-256 of sorted(trace_ids) over ALL three pools, hex-encoded.
Identical trace_ids → identical hash. Reproducible across runs.

DO NOT MODIFY ground-truth labels here without updating the mapping rules below
and re-running the eval harness.

Security: no raw tool outputs are stored. Only trace_ids, verdict strings, and
binary detector flags (True/False/None). No PII, no secrets.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ── Paths ────────────────────────────────────────────────────────────────────

_REPO_ROOT = Path(__file__).parent.parent.parent.parent
_TAUBENCH_DIR = _REPO_ROOT / "eval" / "corpus" / "taubench"
_ANSWERS_JSONL = _REPO_ROOT / "eval" / "review" / "answers.jsonl"
_SPOTCHECK_MD = _REPO_ROOT / "docs" / "spotcheck-day4.md"
_LIVE_IDS_FILE = _REPO_ROOT / "eval" / "corpus" / "live_trace_ids.txt"

# ── Ground-truth tables ───────────────────────────────────────────────────────

# Spotcheck-day4.md ground truth.
# Source: parsed from the Trace rows table.
# Format: trace_id_prefix → {"agree": Y|N|?, "outcome_truth": pass|fail|unknown, ...}
# Detector truth: D1=unrecovered_error, D2=struggle_ratio, D3=coordination_waste.
# Derivation: see session-quality-precision.md §"Label mapping methodology".
# UNKNOWN entries are excluded from precision math (never fabricated).

_SPOTCHECK_TRUTH: dict[str, dict[str, Any]] = {
    # Row 1: 8f0780364da8cbffa1f0544951ecce44 — agree=Y, engine says fail/missing_side_effect
    # Comment: "it failed and looped into its own HTTP request" → haywire/coordination waste
    "8f0780364da8cbff": {
        "outcome_truth": "fail",   # agree=Y means engine correct; engine said fail → truth=fail
        "D1": None,   # not directly labeled
        "D2": None,
        "D3": True,   # "looped into its own HTTP request" = coordination waste (curl repeats)
    },
    # Row 2: bd56871947a909a7... — agree=Y, engine says fail/critical_tool_error
    "bd56871947a909a7": {
        "outcome_truth": "fail",
        "D1": None,
        "D2": None,
        "D3": None,
    },
    # Row 3: 79043f7ec7bf0d1a... — agree=Y, engine says fail/missing_side_effect
    "79043f7ec7bf0d1a": {
        "outcome_truth": "fail",
        "D1": None,
        "D2": None,
        "D3": None,
    },
    # Row 4: b1c3f0272403b740... — agree=Y, comment mentions haywire restarts
    # "restarts from stale session without recovering well" → D1 (unrecovered), D2 (struggle)
    "b1c3f0272403b740": {
        "outcome_truth": "fail",
        "D1": True,    # stale session restart = unrecovered error pattern
        "D2": True,    # "restarts from stale session" = struggle
        "D3": None,
    },
    # Row 5: ea9692b98678ac4e... — agree=N, engine WRONG (misclassified as Code Implementation)
    "ea9692b98678ac4e": {
        "outcome_truth": "unknown",  # agree=N means engine incorrect → exclude from precision
        "D1": None,
        "D2": None,
        "D3": True,   # digests show repeated Slack curl calls
    },
    # Row 6: 8b0336fad7f4b1ce... — agree=? (uncertain)
    "8b0336fad7f4b1ce": {
        "outcome_truth": "unknown",  # ? → excluded
        "D1": None,
        "D2": None,
        "D3": None,
    },
    # Row 7: 5eee0136777444f3... — agree=Y, "terminated multiple times, agent runs haywire"
    "5eee0136777444f3": {
        "outcome_truth": "fail",
        "D1": True,    # "once the shell terminates, the agent just runs haywire" = unrecovered
        "D2": True,    # "terminated multiple times" = struggle
        "D3": None,
    },
    # Row 8: f07e36e3a13b9b48... — agree=Y, engine says fail/missing_side_effect
    "f07e36e3a13b9b48": {
        "outcome_truth": "fail",
        "D1": None,
        "D2": None,
        "D3": None,
    },
    # Row 9: a851f9c219fcad64... — agree=N, "workflow resumed properly post error, edits happened"
    "a851f9c219fcad64": {
        "outcome_truth": "unknown",  # agree=N → engine wrong → excluded
        "D1": None,
        "D2": None,
        "D3": None,
    },
    # Row 10: f788bf6a34304376... — agree=N, "no errors in traces"
    "f788bf6a34304376": {
        "outcome_truth": "unknown",  # agree=N → engine wrong → excluded
        "D1": False,   # owner says "what errors?" → D1 should NOT fire
        "D2": False,   # no struggle per owner
        "D3": None,
    },
    # Row 11: 8fe79bb7a022ad93... — agree=Y, pass
    "8fe79bb7a022ad93": {
        "outcome_truth": "pass",
        "D1": False,
        "D2": False,
        "D3": None,
    },
    # Row 12: 21ae18d63b6335e8... — agree=Y, pass
    "21ae18d63b6335e8": {
        "outcome_truth": "pass",
        "D1": False,
        "D2": False,
        "D3": None,
    },
    # Row 13: a9c229dd1b993134... — agree=Y, pass (Paperclip Coordination)
    "a9c229dd1b993134": {
        "outcome_truth": "pass",
        "D1": False,
        "D2": False,
        "D3": None,
    },
    # Row 14: 656619f5b3e13b8c... — agree=Y, pass
    "656619f5b3e13b8c": {
        "outcome_truth": "pass",
        "D1": False,
        "D2": False,
        "D3": None,
    },
    # Row 15: 1984809abfa7d3a7... — agree=Y, pass
    "1984809abfa7d3a7": {
        "outcome_truth": "pass",
        "D1": False,
        "D2": False,
        "D3": None,
    },
    # Row 16: 96d0f15c010f64bb... — agree=? (uncertain)
    "96d0f15c010f64bb": {
        "outcome_truth": "unknown",  # ? → excluded
        "D1": None,
        "D2": None,
        "D3": None,
    },
    # Row 17: a3bc546c39899e73... — agree=N, engine wrong
    "a3bc546c39899e73": {
        "outcome_truth": "unknown",
        "D1": None,
        "D2": None,
        "D3": None,
    },
    # Row 18: 03969588096b5b35... — agree=N, engine wrong
    "03969588096b5b35": {
        "outcome_truth": "unknown",
        "D1": None,
        "D2": None,
        "D3": None,
    },
    # Row 19: bd0ce91137f0f343... — agree=N, engine wrong
    "bd0ce91137f0f343": {
        "outcome_truth": "unknown",
        "D1": None,
        "D2": None,
        "D3": None,
    },
    # Row 20: 425764d1beab6b2f... — agree=N, engine wrong
    "425764d1beab6b2f": {
        "outcome_truth": "unknown",
        "D1": None,
        "D2": None,
        "D3": None,
    },
}

# answers.jsonl mapping rules:
# Mapping rule applied per answer:
#   verdict_shown=pass + positive answer (LGTM, ✅, "looks good", "pass") → outcome_truth=pass, no detector fires
#   verdict_shown=pass + answer mentions tool_use_error/silent failure/unrecovered → D1=True
#   verdict_shown=pass + answer mentions stacking/repetitive → D2=True
#   verdict_shown=non_computable + answer mentions failures/exit codes → D1 or D2 potential fires
#   Answer clearly vague ("I don't know", incomplete, no verdict) → UNKNOWN, excluded
# Duplicate trace_ids: if multiple answers for same trace_id, last one wins (latest timestamp).
_ANSWERS_TRUTH: dict[str, dict[str, Any]] = {
    # 1c59051c - "inconclusive, no transcript data" → UNKNOWN
    "1c59051c3ba82897": {
        "outcome_truth": "unknown",
        "D1": None, "D2": None, "D3": None,
    },
    # d82c0771 - "looks good to me" → pass, clean
    "d82c0771ddc3eedf": {
        "outcome_truth": "pass",
        "D1": False, "D2": False, "D3": None,
    },
    # d38a760a - "Bash exit code 1, never re-attempted. That's my only concern."
    # verdict_shown=pass; owner sees an unrecovered error but thinks the trace still passed
    "d38a760ac7e43101": {
        "outcome_truth": "pass",   # verdict_shown=pass; owner didn't reject it
        "D1": True,    # "never re-attempted" = D1 should fire
        "D2": None,
        "D3": None,
    },
    # 4d470c8f - "multiple continuous exit codes, instead of refire just moves on"
    "4d470c8f8b30ff48": {
        "outcome_truth": "pass",  # verdict_shown=pass
        "D1": True,    # "exit codes, no refire" = D1 should fire
        "D2": None,
        "D3": None,
    },
    # 6ceca8d5 - "tool_use_error in description, shown as success... silent failure"
    # Two entries for this trace_id, last timestamp wins (2026-06-13T08:18:27)
    "6ceca8d505fbef5f": {
        "outcome_truth": "pass",  # verdict_shown=pass (engine says pass; owner notes silent failure)
        "D1": None,   # D1 cannot see masked OK-status errors
        "D2": None,
        "D3": None,
    },
    # bc749219 - "bash command to create the PR failed. No reattempt."
    "bc749219d1bf0bac": {
        "outcome_truth": "pass",  # verdict_shown=pass
        "D1": True,    # "bash command failed, no reattempt" = D1 should fire
        "D2": None,
        "D3": None,
    },
    # 6a90e914 - "pass"
    "6a90e914578d25be": {
        "outcome_truth": "pass",
        "D1": False, "D2": False, "D3": None,
    },
    # ba036a1d - "LGTM" (two entries, both say LGTM)
    "ba036a1d86e17c79": {
        "outcome_truth": "pass",
        "D1": False, "D2": False, "D3": None,
    },
    # a1bd9de0 - non_computable; "git bash steps failing, not re-attempted... pass (partial)"
    "a1bd9de0d82346b3": {
        "outcome_truth": "unknown",  # non_computable; owner says partial pass
        "D1": True,    # "git bash steps failing, not re-attempted"
        "D2": True,    # failing steps = potential struggle
        "D3": None,
    },
    # 6071761a - non_computable; "reading files that do not exist multiple times; skill silent failure"
    "6071761adb63e378": {
        "outcome_truth": "unknown",  # non_computable
        "D1": None,   # Read errors may fire D1; Skill masked as OK
        "D2": None,
        "D3": None,
    },
    # f645a282 - non_computable; "read failed but succeeded finally; silent failures"
    "f645a282052fec46": {
        "outcome_truth": "unknown",
        "D1": None,   # partial: read eventually succeeded = recovered
        "D2": None,
        "D3": None,
    },
    # 0939a81a - non_computable; "failures stacking, exit code 4, no follow-up... failure"
    "0939a81a37d91050": {
        "outcome_truth": "fail",  # owner explicitly says failure
        "D1": True,    # "no follow-up" = unrecovered
        "D2": True,    # "failures stacking" = struggle
        "D3": None,
    },
    # 92eb1ef5 - non_computable; "repetitive bash, can't gauge intent, no verdict"
    "92eb1ef53d60db76": {
        "outcome_truth": "unknown",  # no verdict
        "D1": None,
        "D2": None,
        "D3": None,
    },
    # 0706dd7e - non_computable; "19 bash steps, no intent... in conclusion again" (truncated/vague)
    "0706dd7ee194e504": {
        "outcome_truth": "unknown",
        "D1": None,
        "D2": None,
        "D3": None,
    },
    # 6b7f7fc3 - non_computable; "clear failure growth, this is like" (truncated)
    "6b7f7fc3ec0b7a62": {
        "outcome_truth": "unknown",
        "D1": None,
        "D2": None,
        "D3": None,
    },
}


# ── Data structures ───────────────────────────────────────────────────────────


@dataclass
class CorpusEntry:
    """One item in the eval corpus.

    outcome_truth: "pass" | "fail" | "partial" | "unknown"
      - pass/fail: deterministic ground truth for outcome precision math
      - partial: tau-bench PARTIAL (excluded from binary outcome math, tracked)
      - unknown: ambiguous or excluded label (excluded from precision math)

    detector_truth: per-detector expected fire (True/False/None=unknown).
      None means we cannot determine from labels whether the detector should fire.
      None entries are EXCLUDED from detector precision math.
    """

    trace_id: str
    source: str           # "taubench" | "spotcheck" | "answers" | "live"
    outcome_truth: str    # "pass" | "fail" | "partial" | "unknown"
    detector_truth: dict[str, bool | None] = field(default_factory=dict)
    """Keys: D1, D2, D3, D4, redundant_execution. Value: True=should fire, False=should not, None=unknown."""
    tau_reward: float | None = None
    """tau-bench reward (0.0–1.0), None for non-tau sources."""


@dataclass
class EvalCorpus:
    """Versioned, fixed evaluation corpus.

    corpus_hash: SHA-256 of sorted trace_ids (all three pools).
    """

    entries: list[CorpusEntry]
    corpus_hash: str
    trace_ids: list[str]   # sorted, stable

    # Composition counts
    tau_bench_count: int = 0
    spotcheck_count: int = 0
    answers_count: int = 0
    live_count: int = 0

    def labeled_for_outcome(self) -> list[CorpusEntry]:
        """Entries with outcome_truth in {pass, fail} — usable for precision math."""
        return [e for e in self.entries if e.outcome_truth in {"pass", "fail"}]

    def labeled_for_detector(self, detector: str) -> list[CorpusEntry]:
        """Entries with a known detector truth (True or False) for `detector`."""
        return [
            e for e in self.entries
            if e.detector_truth.get(detector) is not None
        ]


# ── Tau-bench loader ──────────────────────────────────────────────────────────


def _load_taubench(corpus_dir: Path) -> list[CorpusEntry]:
    """Load tau-bench labeled traces from eval/corpus/taubench/labels.jsonl."""
    labels_path = corpus_dir / "labels.jsonl"
    if not labels_path.exists():
        return []

    entries: list[CorpusEntry] = []
    seen_trace_ids: set[str] = set()

    with labels_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            trace_id = rec["trace_id"]

            if trace_id in seen_trace_ids:
                continue
            seen_trace_ids.add(trace_id)

            label = rec["label"]          # PASS | FAIL | PARTIAL
            reward = float(rec["reward"])

            if label == "PASS":
                outcome_truth = "pass"
            elif label == "FAIL":
                outcome_truth = "fail"
            else:
                outcome_truth = "partial"  # excluded from binary math, tracked

            entries.append(
                CorpusEntry(
                    trace_id=trace_id,
                    source="taubench",
                    outcome_truth=outcome_truth,
                    detector_truth={},   # tau-bench has no detector labels
                    tau_reward=reward,
                )
            )

    return entries


# ── Spotcheck loader ──────────────────────────────────────────────────────────


def _full_trace_id_from_prefix(prefix: str, known_ids: set[str]) -> str | None:
    """Find the full trace_id for a 16-char prefix in known_ids.

    Spotcheck-day4.md stores only trace_id prefixes (first 16 hex chars).
    We match against tau-bench corpus and answers.jsonl to find the full ID,
    falling back to the prefix itself if not found.
    """
    prefix16 = prefix[:16]
    for tid in known_ids:
        if tid.startswith(prefix16):
            return tid
    return prefix16  # use prefix itself as ID (stable across runs)


def _load_spotcheck(spotcheck_entries: dict[str, dict[str, Any]]) -> list[CorpusEntry]:
    """Build CorpusEntries from the _SPOTCHECK_TRUTH table."""
    entries: list[CorpusEntry] = []
    for prefix, truth in spotcheck_entries.items():
        # Use prefix as stable trace_id (first 16 chars are the key)
        trace_id = prefix[:16]
        entries.append(
            CorpusEntry(
                trace_id=trace_id,
                source="spotcheck",
                outcome_truth=truth.get("outcome_truth", "unknown"),
                detector_truth={
                    "D1": truth.get("D1"),
                    "D2": truth.get("D2"),
                    "D3": truth.get("D3"),
                    "D4": None,
                    "redundant_execution": None,
                },
                tau_reward=None,
            )
        )
    return entries


# ── Answers loader ────────────────────────────────────────────────────────────


def _load_answers(answers_path: Path, truth_table: dict[str, dict[str, Any]]) -> list[CorpusEntry]:
    """Build CorpusEntries from answers.jsonl using the _ANSWERS_TRUTH table.

    Duplicate trace_ids: last entry (by file order, which is chronological) wins.
    The truth table is authoritative; answers.jsonl is only used to extract trace_ids.
    """
    if not answers_path.exists():
        return []

    # Collect unique trace_ids from the JSONL (deduplicate by keeping last occurrence)
    seen_order: dict[str, int] = {}
    trace_ids_in_order: list[str] = []
    with answers_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            trace_id = rec["trace_id"]
            if trace_id not in seen_order:
                trace_ids_in_order.append(trace_id)
                seen_order[trace_id] = len(trace_ids_in_order) - 1

    entries: list[CorpusEntry] = []
    for trace_id in trace_ids_in_order:
        # Match against truth table by 16-char prefix
        prefix16 = trace_id[:16]
        truth = truth_table.get(prefix16)

        if truth is None:
            # No truth entry — exclude from precision math, include for fire-rate
            entries.append(
                CorpusEntry(
                    trace_id=trace_id,
                    source="answers",
                    outcome_truth="unknown",
                    detector_truth={},
                )
            )
        else:
            entries.append(
                CorpusEntry(
                    trace_id=trace_id,
                    source="answers",
                    outcome_truth=truth.get("outcome_truth", "unknown"),
                    detector_truth={
                        "D1": truth.get("D1"),
                        "D2": truth.get("D2"),
                        "D3": truth.get("D3"),
                        "D4": None,
                        "redundant_execution": None,
                    },
                )
            )

    return entries


# ── Live corpus loader ────────────────────────────────────────────────────────


def _load_live_trace_ids(live_ids_file: Path) -> list[str]:
    """Load the fixed list of 345 backfilled trace_ids.

    If the file does not exist, returns an empty list (the live pool is
    optional — the corpus degrades gracefully to tau-bench + owner labels).
    """
    if not live_ids_file.exists():
        return []

    lines = live_ids_file.read_text().splitlines()
    return [line.strip() for line in lines if line.strip()]


def _build_live_entries(trace_ids: list[str], existing_ids: set[str]) -> list[CorpusEntry]:
    """Build CorpusEntries for live trace_ids not already in the corpus.

    Live entries have NO outcome or detector ground truth — they contribute
    fire-count and fire-rate stability signals only.
    """
    entries: list[CorpusEntry] = []
    for trace_id in trace_ids:
        if trace_id in existing_ids:
            continue  # already in corpus from another source
        entries.append(
            CorpusEntry(
                trace_id=trace_id,
                source="live",
                outcome_truth="unknown",
                detector_truth={},
            )
        )
    return entries


# ── Corpus hash ───────────────────────────────────────────────────────────────


def _compute_corpus_hash(trace_ids: list[str]) -> str:
    """SHA-256 of sorted trace_ids, hex-encoded.

    Deterministic: same set of IDs → same hash regardless of insertion order.
    """
    sorted_ids = sorted(trace_ids)
    payload = "\n".join(sorted_ids).encode()
    return hashlib.sha256(payload).hexdigest()


# ── Public API ────────────────────────────────────────────────────────────────


def build_corpus(
    taubench_dir: Path = _TAUBENCH_DIR,
    answers_path: Path = _ANSWERS_JSONL,
    live_ids_file: Path = _LIVE_IDS_FILE,
) -> EvalCorpus:
    """Assemble the versioned eval corpus from all three sources.

    Returns an EvalCorpus with a stable corpus_hash.
    The corpus is FIXED: trace_ids are deterministic and reproducible.

    Security: no raw tool outputs are included. All entries contain only
    trace_ids, verdict strings, and binary detector flags.
    """
    entries: list[CorpusEntry] = []
    seen_ids: set[str] = set()

    # Pool 1: tau-bench
    tau_entries = _load_taubench(taubench_dir)
    tau_count = 0
    for e in tau_entries:
        if e.trace_id not in seen_ids:
            entries.append(e)
            seen_ids.add(e.trace_id)
            tau_count += 1

    # Pool 2a: spotcheck-day4 (20 entries)
    spotcheck_entries = _load_spotcheck(_SPOTCHECK_TRUTH)
    spotcheck_count = 0
    for e in spotcheck_entries:
        if e.trace_id not in seen_ids:
            entries.append(e)
            seen_ids.add(e.trace_id)
            spotcheck_count += 1

    # Pool 2b: answers.jsonl (15 unique trace_ids)
    answers_entries = _load_answers(answers_path, _ANSWERS_TRUTH)
    answers_count = 0
    for e in answers_entries:
        if e.trace_id not in seen_ids:
            entries.append(e)
            seen_ids.add(e.trace_id)
            answers_count += 1

    # Pool 3: live corpus snapshot (345 trace_ids)
    live_trace_ids = _load_live_trace_ids(live_ids_file)
    live_entries = _build_live_entries(live_trace_ids, seen_ids)
    live_count = 0
    for e in live_entries:
        if e.trace_id not in seen_ids:
            entries.append(e)
            seen_ids.add(e.trace_id)
            live_count += 1

    all_trace_ids = sorted(seen_ids)
    corpus_hash = _compute_corpus_hash(all_trace_ids)

    return EvalCorpus(
        entries=entries,
        corpus_hash=corpus_hash,
        trace_ids=all_trace_ids,
        tau_bench_count=tau_count,
        spotcheck_count=spotcheck_count,
        answers_count=answers_count,
        live_count=live_count,
    )


def persist_live_trace_ids(trace_ids: list[str], path: Path = _LIVE_IDS_FILE) -> None:
    """Write the live corpus trace_id list to disk (one per line).

    Call this once to fix the live corpus snapshot. Idempotent (overwrites).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(sorted(trace_ids)) + "\n")
