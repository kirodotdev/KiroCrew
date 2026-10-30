"""Git's own unified diff of two bodies the caller has already masked.

The dashboard file panel (``dashboard/file_api/editor.py``) and the remote Git
panel (``coder/session_browse.py``) both show git's hunks for two masked
bodies, through this one ``git diff --no-index`` spawn.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Mapping, Sequence


def no_index_diff(old: str, new: str, *, git: Sequence[str], env: Mapping[str, str]) -> str:
    """``git diff --no-index`` of two ALREADY-MASKED bodies, as text.

    A patch shows lines out of their surrounding text, which is what the
    redactor reads, so the caller masks whole bodies and only masked text is
    diffed. The temp files hold masked text only, in a private dir outside
    any repo. The output is decoded without newline translation, so a CR
    inside a line stays in that line.
    """
    with tempfile.TemporaryDirectory(prefix="kc-file-diff-") as tmp:
        a = os.path.join(tmp, "a")
        b = os.path.join(tmp, "b")
        with open(a, "w", encoding="utf-8", newline="") as fh:
            fh.write(old)
        with open(b, "w", encoding="utf-8", newline="") as fh:
            fh.write(new)
        r = subprocess.run(
            [*git, "diff", "--no-index", "--no-textconv", "--no-ext-diff", "--", a, b],
            cwd=tmp,
            capture_output=True,
            timeout=10,
            env=dict(env),
        )
    # --no-index answers 1 for "differs"; anything else is a failure, and a
    # failure must not fall back to the raw patch.
    if r.returncode not in (0, 1):
        raise subprocess.CalledProcessError(r.returncode, "git diff --no-index")
    return r.stdout.decode("utf-8", errors="replace")


def patch_hunks(patch: str) -> list[str] | None:
    """The patch's lines from its first ``@@`` on, or None when it has none.

    Split on "\\n" only: splitlines() also cuts on form feed, U+2028 and other
    characters that sit inside a patch line.
    """
    lines = patch.rstrip("\n").split("\n")
    first = next((i for i, ln in enumerate(lines) if ln.startswith("@@")), None)
    return None if first is None else lines[first:]
