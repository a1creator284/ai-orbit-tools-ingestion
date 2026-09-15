"""Quality scoring and threshold filtering (guideline §5).

* :mod:`src.scoring.rubric` — the 100-point rubric: weights, bands, criteria.
* :mod:`src.scoring.scorer` — the deterministic evaluator.
* :mod:`src.scoring.filter` — the threshold/admission policy.
"""

from src.scoring.filter import FilterDecision, FilterReport, Outcome, QualityFilter
from src.scoring.rubric import BANDS, COMPONENTS, WEIGHTS
from src.scoring.scorer import QualityScorer, rank_and_select

__all__ = [
    "BANDS",
    "COMPONENTS",
    "WEIGHTS",
    "FilterDecision",
    "FilterReport",
    "Outcome",
    "QualityFilter",
    "QualityScorer",
    "rank_and_select",
]
