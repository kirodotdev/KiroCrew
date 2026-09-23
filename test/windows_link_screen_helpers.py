"""Drive ``hooks._screen_windows_links`` under simulated Windows semantics.

The project-dir preflights (``agent_discovery._link_chain_refused``) and the
directory-signature guard (``hooks.validate_file_path``) all reach the one Windows
link screen in ``hooks``. These doubles give that screen a stored-link table and
Windows gates without patching the global ``os.name`` (which would make pathlib
dispatch ``WindowsPath`` on a POSIX host), the same way ``test_hooks_coverage``
simulates it.
"""

from __future__ import annotations

import errno
import ntpath
import os
import types
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Callable

import pytest


def simulate_windows_link_screen(
    monkeypatch: pytest.MonkeyPatch,
    links: dict[str, object],
    *,
    path_module=ntpath,
    realpath: Callable[[str], str] | None = None,
    data_home: str | None = None,
    discovery_path_module=None,
) -> list[str]:
    """Install the doubles and return the list ``readlink`` records its keys in.

    *links* maps a link spelling to its stored target; a value that is an
    exception instance is raised by ``readlink`` instead (an uninspectable hop).
    Every key is a link to ``is_link_or_junction`` and ``first_linked_ancestor``.
    *discovery_path_module*, when given, becomes ``agent_discovery.os.path`` so the
    preflight's ``abspath`` anchors with the same semantics.
    """
    from kiro_crew import agent_discovery as discovery_mod
    from kiro_crew import hooks as hooks_mod
    from kiro_crew import platform_compat

    def key(path) -> str:
        return path_module.normcase(path_module.normpath(os.fspath(path)))

    table = {key(spelling): target for spelling, target in links.items()}
    calls: list[str] = []
    pure = PureWindowsPath if path_module is ntpath else PurePosixPath

    def readlink(path):
        calls.append(key(path))
        stored = table.get(key(path))
        if stored is None:
            raise OSError(errno.EINVAL, "not a link")
        if isinstance(stored, BaseException):
            raise stored
        return stored

    def is_link(path) -> bool:
        return key(path) in table

    def first_linked_ancestor(path):
        for ancestor in reversed(pure(os.fspath(path)).parents):
            if is_link(str(ancestor)):
                return str(ancestor)
        return None

    monkeypatch.setattr(
        hooks_mod,
        "os",
        types.SimpleNamespace(
            name="nt",
            sep=path_module.sep,
            environ=os.environ,
            readlink=readlink,
            path=types.SimpleNamespace(
                expanduser=path_module.expanduser,
                abspath=path_module.abspath,
                realpath=realpath or (lambda path: path),
                normcase=path_module.normcase,
                normpath=path_module.normpath,
                isabs=path_module.isabs,
                join=path_module.join,
                dirname=path_module.dirname,
                relpath=path_module.relpath,
            ),
        ),
    )
    monkeypatch.setattr(platform_compat, "first_linked_ancestor", first_linked_ancestor)
    monkeypatch.setattr(platform_compat, "is_link_or_junction", is_link)
    monkeypatch.setattr(
        hooks_mod, "_unc_data_home_root", lambda: Path(data_home) if data_home else None
    )
    monkeypatch.setattr(hooks_mod, "_unc_agents_root", lambda: None)
    monkeypatch.setattr(discovery_mod, "_WINDOWS", True)
    if discovery_path_module is not None:
        os_double = types.SimpleNamespace(**vars(discovery_mod.os))
        os_double.path = discovery_path_module
        monkeypatch.setattr(discovery_mod, "os", os_double)
    return calls
