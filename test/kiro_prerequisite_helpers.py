"""Fakes, fixtures and helpers for the ``test_kiro_prerequisite*.py`` modules.

The two autouse fixtures keep every test off the developer's real ``~/.kiro/agents``;
each module imports them, so every test sees the same autouse set.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kiro_crew.kiro_prerequisite import KiroPrerequisiteService, ProcessResult
from kiro_crew.testing.wait import await_until


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- no test in the test_kiro_prerequisite*.py modules calls monkeypatch.undo()
def _isolate_agent_specs(tmp_path_factory, monkeypatch):
    """Point the agents dir at a POPULATED tmp dir for every test in this module.

    Readiness folds agent-spec presence into ``ready``: a missing spec means
    kiro-cli cannot resolve the agent, so the install is not ready. Those specs
    live in a MACHINE-WIDE directory that no ``KIROCREW_HOME`` override isolates,
    so an unpinned probe test reads the developer's real ``~/.kiro/agents`` and its
    verdict follows host state rather than the code under test. This pin removes
    that confound.

    Tests that exercise the missing-spec behaviour re-patch the same hook inside
    the test body, which runs after this fixture and therefore wins.
    """
    from kiro_crew import agent as agent_module
    from kiro_crew.agent_files import REQUIRED_KIRO_AGENT_FILES

    agents = tmp_path_factory.mktemp("kiro-agents")
    for name in REQUIRED_KIRO_AGENT_FILES:
        (agents / name).write_text('{"name": "probe-fixture"}', encoding="utf-8")
    monkeypatch.setattr(agent_module, "KIRO_AGENTS_DIR", agents)


def _make_executable(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o700)


class _FakeRuntime:
    def __init__(self, executable: Path) -> None:
        self.executable = executable
        self.installed = executable.exists()
        self.authenticated = False
        # Whether this fake CLI exposes the `acp` subcommand the readiness probe
        # now checks with `acp --help`. Defaults True so an authenticated fake is
        # `ready` exactly as before this probe existed; a test that exercises the
        # too-old path flips it to False.
        self.acp_supported = True
        self.calls: list[tuple[str, list[str]]] = []
        self.kwargs: list[dict[str, Any]] = []

    async def run(
        self,
        command: str,
        args: list[str],
        **kwargs: Any,
    ) -> ProcessResult:
        self.calls.append((command, args))
        self.kwargs.append(kwargs)
        if args == ["--version"]:
            return ProcessResult(ok=self.installed)
        if args == ["whoami"]:
            return ProcessResult(ok=self.authenticated)
        if args == ["acp", "--help"]:
            if self.acp_supported:
                return ProcessResult(ok=True, output="Usage: kiro-cli acp [OPTIONS]")
            return ProcessResult(
                ok=False,
                returncode=2,
                output="error: unrecognized subcommand 'acp'",
            )
        return ProcessResult(ok=False)


async def _no_audit(**kwargs: Any) -> None:
    del kwargs


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- no test in the test_kiro_prerequisite*.py modules calls monkeypatch.undo()
def _agents_dir_never_the_real_home(
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep every test in this module off the developer's real ``~/.kiro/agents``.

    The readiness probe asks kiro-cli to validate each required spec it finds, and
    that enumeration resolves through the shared (KIRO_HOME-aware) agents dir. With
    KIRO_HOME unset, tests that only meant to assert binary resolution would spawn
    a validate per spec that happens to exist on the host — making their call-count
    assertions depend on the machine they run on.
    """
    monkeypatch.setenv("KIRO_HOME", str(tmp_path_factory.mktemp("kiro-home")))
    import kiro_crew.kiro_prerequisite as kiro_prerequisite_module

    monkeypatch.setattr(kiro_prerequisite_module, "_default_spec_lister", lambda: [], raising=True)


async def _wait_for_operation(service: KiroPrerequisiteService) -> None:
    task = service._task
    assert task is not None
    await await_until(task, what="the prerequisite operation")
