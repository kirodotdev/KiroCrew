"""Shared pieces for the ``test_sandbox_absent_ceiling_seal*.py`` modules.

``crew_home`` points ``config_dir()`` (the live data home) and ``Path.home`` at a scratch
tree; the autouse ``_no_host_ssh_probe`` pins the host ``ssh -V`` probe, so planning a
namespace spawn runs no binary; ``_written_plan`` reads the plan data off the launcher
script ``namespace_argv`` wrote; ``_POSIX_ONLY`` skips the POSIX-launcher cases on Windows,
and ``program`` is ``sandbox_launcher_program``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from test_sandbox_launcher_program import rendered_payload

from kiro_crew import sandbox, sandbox_launcher_program

program = sandbox_launcher_program

_POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX launcher only")


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing autouse ssh probe pin moved verbatim; no test in the modules that import it calls monkeypatch.undo()
def _no_host_ssh_probe(monkeypatch):
    """Planning a namespace spawn asks the HOST's ``ssh -V`` for accept-new support.

    Many tests here plan or write a launcher; none is about that probe, and a real ssh
    spawned from the test process is a host dependency the plan must not vary with.
    Pinned so no binary runs.
    """
    monkeypatch.setattr(sandbox, "_ssh_supports_accept_new", lambda: True)


@pytest.fixture(name="crew_home")
def crew_home_fixture(tmp_path, monkeypatch):
    """Point ``config_dir()`` — the live data home — at a scratch tree.

    ``Path.home`` is pinned at the same scratch root, and that half is load-bearing rather
    than tidiness. The masked-leaf passes resolve EVERY crew-home spelling
    (:func:`sandbox._masked_crew_home_roots`), because the masks cover every spelling and a
    check scoped to the live home alone would judge one of them. Under a bare
    ``config_dir()`` patch those passes would read ``$HOME/.kiro/crew`` and
    ``$HOME/.kirocrew`` on the machine running the suite, so a developer whose real data
    home happens to hold a hard-linked credential leaf would see refusals from tests that
    never created one.
    """
    home = tmp_path / ".kiro" / "crew"
    home.mkdir(parents=True)
    monkeypatch.setattr(sandbox, "config_dir", lambda: home)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return home


def _written_plan(argv: list[str]) -> dict:
    """The plan data the launcher ``namespace_argv`` wrote carries: what the child acts on."""
    script = Path(sandbox._launcher_script_of(argv)).read_text(encoding="utf-8")
    return rendered_payload(script)
