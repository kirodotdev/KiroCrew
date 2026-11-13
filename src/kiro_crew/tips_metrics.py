"""Counters for ``POST /api/tips/feedback``: label a tip and record its reaction.

Split from :mod:`kiro_crew.tips` so the route module stays a route module. The
labels are closed by :func:`kiro_crew.metrics.feature_usage.record_tip_feedback`;
this module reads which list the tip came from and which doc it is about,
and names a shipped curated tip by its id.
Nothing here raises into the route.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.tips import TipsCache, TipsState

logger = logging.getLogger(__name__)


def tip_metric_labels(cache: "TipsCache", st: "TipsState", tip_id: str) -> tuple[str, str]:
    """``(source, doc)`` of *tip_id* for ``kirocrew.tips.feedback``. Never raises.

    Read directly from what the route holds, with no guessing from the id:

    * ``source`` is ``curated`` when the id is in ``cache.curated``; for the
      offered tip it is ``generated`` when that tip is in the stored pool and
      ``catalog`` when it is not (the route then served it from the catalog
      fallback). Anything else, including a reaction that arrives after
      ``shown`` cleared the offered slot, is ``other``.
    * ``doc`` is the tip's own ``doc`` field (a curated tip's ``doc_link`` when
      ``doc`` is empty), read from the curated list, the offered tip,
      ``shown_docs`` or the stored pool, in that order. The counter keeps it only when it is in
      ``TIP_DOC_ALLOWLIST``.
    """
    try:
        if not tip_id:
            return "other", ""
        for t in cache.curated:
            if isinstance(t, dict) and t.get("id") == tip_id:
                return "curated", str(t.get("doc") or t.get("doc_link") or "")
        offered = st.offered
        if isinstance(offered, dict) and offered.get("id") == tip_id:
            doc = str(offered.get("doc") or "")
            in_pool = any(
                isinstance(t, dict) and t.get("id") == tip_id and str(t.get("doc") or "") == doc
                for t in st.tips
            )
            return ("generated" if in_pool else "catalog"), doc
        shown_doc = st.shown_docs.get(tip_id)
        if isinstance(shown_doc, str) and shown_doc:
            return "other", shown_doc
        for t in st.tips:
            if isinstance(t, dict) and t.get("id") == tip_id:
                return "other", str(t.get("doc") or "")
        return "other", ""
    except Exception:
        logger.debug("tips metric labelling failed", exc_info=True)
        return "other", ""


def record_tip_feedback_metric(action: str, source: str, doc: str, tip_id: str = "") -> None:
    """Count one saved tip reaction and note a tip recommendation. Never raises.

    Runs only after the feedback was saved, so it cannot change the response.
    The counter's ``feature`` names the tip: a curated tip shipped with the
    package by its id (most of them carry no ``doc``), any other tip by its doc.
    A tip that was shown or acknowledged counts as a recommendation of its
    doc's feature for ``milestone.reached{recommended_by}``.
    """
    try:
        from kiro_crew.metrics.feature_usage import record_tip_feedback, shipped_curated_tip_ids

        curated = source == "curated" and tip_id in shipped_curated_tip_ids()
        record_tip_feedback(action, source, tip_id if curated else doc)
        if action in ("shown", "ack") and doc:
            from kiro_crew import milestones

            milestones.record_recommended("tip", doc)
    except Exception:
        logger.debug("tips feedback metric failed", exc_info=True)
