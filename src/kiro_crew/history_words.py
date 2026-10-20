"""Chinese dictionary words for session search, held once per host by the gateway.

:func:`kiro_crew.history_search.parse_search_query` splits a pure-Han query run
into words through whichever segmenter is registered. The gateway registers
:func:`segment_runs`, which holds the host's only copy of the dictionary: rjieba,
the jieba-rs binding, costs about 45 MB once loaded. An mcp-core process never
imports it; it asks the gateway over ``POST /api/search/segment``
(:mod:`kiro_crew.dashboard.handlers.search_segment`).

The dictionary loads on the first Chinese query, not at boot, and at most once. A
failed import is remembered, and every query then keeps the character path.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Sequence

logger = logging.getLogger(__name__)

_load_lock = threading.Lock()
_cut: Callable[..., list[str]] | None = None
_unavailable = False


def _loaded_cut() -> Callable[..., list[str]] | None:
    """``rjieba.cut``, importing it on first use; ``None`` when it cannot load."""
    global _cut, _unavailable
    with _load_lock:
        if _cut is None and not _unavailable:
            try:
                import rjieba
            except Exception:
                # ImportError, or a wheel whose native library will not load.
                _unavailable = True
                logger.warning(
                    "rjieba is unavailable, so Chinese session search keeps the " "character path",
                    exc_info=True,
                )
            else:
                _cut = rjieba.cut
        return _cut


def segment_runs(runs: Sequence[str]) -> list[list[str]] | None:
    """Each of *runs* split into dictionary words, or ``None`` with no dictionary.

    HMM on: jieba's model for words the dictionary lacks keeps a name such as
    ``吉卜力`` whole instead of three single characters.
    """
    cut = _cut if _cut is not None else _loaded_cut()
    if cut is None:
        return None
    return [list(cut(run, hmm=True)) for run in runs]
