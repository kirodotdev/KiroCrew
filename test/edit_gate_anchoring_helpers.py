"""POSIX path grammar for ``platform.tool_paths._is_anchored`` in gate tests.

The repo's convention for anything two test modules both need is a dedicated
``*_helpers.py`` imported by BARE name (see ``chat_test_helpers``,
``mcp_merge_helpers``). The two edit-gate suites
(``test_hooks_edit_gate_diff_path.py``, ``test_llm_helpers_edit_gate.py``)
spell their targets POSIX-rooted (``/tmp/a``) or home-relative
(``~/.kiro/crew/config.json``). Their subject is the GATE, not the path grammar,
so each pins ``_is_anchored``'s path module to this one on every host:

* On Windows ``/tmp/a`` is DRIVE-RELATIVE and ``_is_anchored`` (correctly)
  refuses it under ``ntpath``.
* ``posixpath.expanduser`` reads ``HOME``, which the Windows runner spells
  ``C:\\Users\\...``, so a plain ``posixpath`` pin left ``~`` unexpanded and the
  gate denied a home-relative target as relative.

``~`` names a file independently of the CWD by construction, so it expands to a
fixed POSIX home here without consulting the environment. ``~user`` is left
alone (and therefore unanchored), as ``posixpath`` would leave it with no
matching account.
"""

from __future__ import annotations

import posixpath

_HOME = "/home/tester"


class PosixAnchoring:
    """Drop-in for ``tool_paths._PATH``: ``posixpath`` with an env-free ``~``."""

    expandvars = staticmethod(posixpath.expandvars)
    isabs = staticmethod(posixpath.isabs)

    @staticmethod
    def expanduser(path: str) -> str:
        if path == "~" or path.startswith("~/"):
            return _HOME + path[1:]
        return path


def pin_posix_anchoring(mp) -> None:
    """Point ``_is_anchored`` at :class:`PosixAnchoring` through *mp*.

    *mp* is the caller's ``MonkeyPatch`` -- an autouse fixture must pass
    ``_floor_monkeypatch`` (D11), never the shared ``monkeypatch``.
    """
    from kiro_crew.platform import tool_paths

    mp.setattr(tool_paths, "_PATH", PosixAnchoring)
