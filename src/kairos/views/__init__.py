"""Presentation layer — turn engine output into JSON view payloads for the UI.

Phase C-UI (XER-71) chose the split-UI option: raw traces stay in the Phoenix
UI (deep-linked, not forked), and the differentiated cohort / workflow-divergence
/ correctness analysis renders in Paperclip-native MIT views. This package holds
the *data contract* between Kairos and those views — no HTML/JS, only the
JSON-serializable shapes and the Phoenix deep-link builder.
"""

from kairos.views.analysis_view import (
    AnalysisSummary,
    AnalysisView,
    CohortView,
    CorrectnessView,
    DivergenceRow,
    METRIC_DESCRIPTIONS,
    WorkflowView,
    build_analysis_view,
    phoenix_trace_url,
)

__all__ = [
    "AnalysisSummary",
    "AnalysisView",
    "CohortView",
    "CorrectnessView",
    "DivergenceRow",
    "METRIC_DESCRIPTIONS",
    "WorkflowView",
    "build_analysis_view",
    "phoenix_trace_url",
]
