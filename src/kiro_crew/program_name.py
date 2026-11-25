"""A shell program word, read the way Windows runs it.

One definition for the batch write guard (:mod:`kiro_crew.acp.batch_guard`), the
interactive classifier (:mod:`kiro_crew.acp.liveness_interactive`) and the Spend tab's
delivered counts (:mod:`kiro_crew.dashboard.handlers.usage_outcome`). It names no agent
backend and imports only the stdlib, so it sits outside the ACP layer, where the
dashboard can read it without crossing the agent SDK boundary.
"""

from __future__ import annotations

import ntpath


def windows_program_name(word: str) -> str:
    """*word* as the program Windows runs: ``git.exe``, ``Git`` and ``C:\\...\\git.exe`` are ``git``."""
    name = ntpath.basename(word).casefold()
    return name[:-4] if name.endswith(".exe") else name
