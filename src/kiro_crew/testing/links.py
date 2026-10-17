"""Directory-link staging for tests that must run on Windows too.

One home for :func:`make_dir_link`. ``test/conftest.py`` re-exports it, and the
app-embedded test packages under ``src/kiro_crew/apps/builtins`` import it from
here, because only the ``test/`` testpath gets that conftest.
"""

from __future__ import annotations

import pathlib

from kiro_crew import platform_compat


def make_dir_link(link: pathlib.Path, target: pathlib.Path) -> None:
    """Create a reparse point at ``link`` that resolves to the directory ``target``.

    A directory symlink needs SeCreateSymbolicLinkPrivilege on Windows (WinError
    1314 in an unelevated shell), while a junction needs none and is followed by
    the same reparse machinery -- ``rglob``, ``resolve`` and
    ``GetFinalPathNameByHandleW`` all traverse it identically. So the behaviour
    under test stays exercised on Windows instead of being skipped.

    ``platform_compat.symlink_or_junction`` is deliberately NOT used here. It
    tries ``os.symlink`` FIRST and falls back to a junction only where the
    privilege is missing, so a runner with Developer Mode or an elevated shell
    gets a SYMLINK and the junction arm these tests exist for is never exercised
    -- silently, while still reporting green. ``CreateJunction`` is what that
    helper falls back to, taken directly so the link type is not left to the host.
    """
    if platform_compat.IS_WINDOWS:
        # Function-local because the module does not exist off Windows, so a
        # top-level import would break collection on POSIX.
        import _winapi

        # getattr, not a typed attribute: typeshed guards CreateJunction behind
        # sys.platform == "win32", so a direct reference fails mypy on Linux.
        create_junction = getattr(_winapi, "CreateJunction", None)
        assert create_junction is not None, "_winapi.CreateJunction missing on Windows"
        create_junction(str(target), str(link))
        return
    link.symlink_to(target, target_is_directory=True)
