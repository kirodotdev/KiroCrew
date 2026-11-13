"""A hooks directory whose ``pre-commit`` refuses every commit.

It stands in for a developer host whose ``~/.gitconfig`` sets ``core.hooksPath`` to hooks
that block commits on protected branches such as ``main``, which CI runners never have.
"""

from __future__ import annotations

from pathlib import Path


def refusing_hooks_config(directory: Path) -> str:
    """Write the hook under *directory*; return the config lines that point git at it."""
    hooks = directory / "hooks"
    hooks.mkdir(parents=True)
    hook = hooks / "pre-commit"
    hook.write_text(
        "#!/bin/sh\necho 'host hook refused the commit' >&2\nexit 1\n", encoding="utf-8"
    )
    hook.chmod(0o755)
    return f"[core]\n\thooksPath = {hooks.as_posix()}\n"
