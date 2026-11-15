"""Modules whose comments promise they stay light at import, held to the promise.

Each row names a module and what importing it must NOT load. The check runs in a
fresh interpreter against real ``sys.modules``, because a module can be pulled in
transitively by anything the module under test imports, which reading one file's
import block cannot see.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

#: module -> modules its import must leave unloaded, each with the comment that says so.
_FLOORS = {
    # acp_backends.py module docstring: no config or platform at module scope, because
    # config.loader reaches resolve_selected_backend from inside KiroCrewConfig.load().
    "kiro_crew.acp_backends": ("kiro_crew.config", "kiro_crew.platform"),
    # dashboard/session_health.py: duck-types the store and never imports taskq.
    "kiro_crew.dashboard.session_health": ("kiro_crew.taskq",),
}


@pytest.mark.parametrize("module", sorted(_FLOORS))
def test_importing_the_module_leaves_its_heavy_dependencies_unloaded(module, tmp_path):
    forbidden = _FLOORS[module]
    probe = f"import sys, {module}; " f"print(sorted(m for m in {forbidden!r} if m in sys.modules))"
    # ``-B`` and a throwaway cwd keep the child from writing bytecode or files into
    # the checkout.
    result = subprocess.run(
        [sys.executable, "-X", "utf8", "-B", "-c", probe],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        cwd=tmp_path,
        timeout=120,  # flake-ok: a ceiling on a child import, never a wait for an event
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]", f"importing {module} loaded {result.stdout.strip()}"
