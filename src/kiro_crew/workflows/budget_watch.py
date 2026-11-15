"""Warn when a run passes its ``budget_total``, without stopping it.

``Budget.charge`` is never called by the engine (``docs/system-specs/modules/
workflows.md``, the ``Budget`` open question), so a ``budget_total`` above 0 never
stopped a run. Enforcing it now would newly stop runs that finish today, and is
left to a maintainer decision. This module only measures and warns.

shortcut: tokens are estimated as characters / 4 of each live call's prompt and
result, because the executors do not hand per-call usage back to the runner;
switch to the provider's reported usage once ``agent_fn`` returns it.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: Characters per token for the estimate (a common rough rule for English text).
_CHARS_PER_TOKEN = 4


class BudgetWatch:
    """Estimated token use for one run, and the one warning when it passes the total."""

    def __init__(self, total: Optional[int], *, run_id: str = "") -> None:
        self.total = total
        self._run_id = run_id
        self.estimated = 0
        self._warned = False

    def add(self, prompt: Any, result: Any) -> str:
        """Count one live call; return the warning the first time the total is passed."""
        if self.total is None:
            return ""
        chars = len(str(prompt)) + (0 if result is None else len(str(result)))
        self.estimated += max(1, chars // _CHARS_PER_TOKEN)
        if self._warned or self.estimated <= self.total:
            return ""
        self._warned = True
        warning = (
            f"budget_total {self.total} passed: about {self.estimated} tokens used so far "
            "(estimated from prompt and result size); the run is not stopped"
        )
        logger.warning("workflow %s: %s", self._run_id, warning)
        return warning
