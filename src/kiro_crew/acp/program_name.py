"""A shell program word, read the way Windows runs it.

One definition for the batch write guard (:mod:`kiro_crew.acp.batch_guard`) and the
interactive classifier (:mod:`kiro_crew.acp.liveness_interactive`).
"""

from __future__ import annotations

import ntpath


def windows_program_name(word: str) -> str:
    """*word* as the program Windows runs: ``git.exe``, ``Git`` and ``C:\\...\\git.exe`` are ``git``."""
    name = ntpath.basename(word).casefold()
    return name[:-4] if name.endswith(".exe") else name
