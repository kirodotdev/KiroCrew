"""Whether this host can run git, asked before a registry install or update starts.

Every registry row is fetched with git: the manifest prefetch, the clone and the
in-place update all spawn it. On a host without git each of those spawns would
fail deep in the checkout, and the user would read git's own failure (``git init
failed``) or a refusal that blames a healthy checkout, after an update has
stopped the app's backend. Asking once, up front, gives one plain sentence and a
``git_unavailable`` code instead, and leaves the app untouched.

The question is the same ``shutil.which("git")`` lookup the former built-in
adoption asks before it chooses between a git fetch and the pin archive, over
the same ``PATH`` every registry git spawn inherits.
"""

from __future__ import annotations

import shutil
from typing import Any

GIT_UNAVAILABLE_CODE = "git_unavailable"

GIT_UNAVAILABLE_ERROR = (
    "Installing or updating an App Store app needs git, which is not installed "
    "on this machine. Install git and retry."
)


def git_unavailable_refusal(name: str) -> dict[str, Any] | None:
    """The refusal for a registry install or update of *name* when git is missing.

    ``None`` when ``git`` resolves on ``PATH``. Otherwise the result dict both
    ``install_from_registry`` and the update route return, with the human
    sentence in ``error`` (the install banner renders it) and the machine slug
    in ``code``.
    """
    if shutil.which("git") is not None:
        return None
    return {
        "ok": False,
        "name": name,
        "code": GIT_UNAVAILABLE_CODE,
        "error": GIT_UNAVAILABLE_ERROR,
    }
