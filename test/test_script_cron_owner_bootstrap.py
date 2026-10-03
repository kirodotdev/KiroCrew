"""A script cron this gateway is running may NOT mint the owner dashboard token.

``run_script_sandboxed`` launches every script cron through ``wrap_argv``, so on
Linux the cron child, and anything it shells out to, sit in their own user and
mount namespaces and fail ``_verified_host_process``. That refusal is kept on
purpose. A cron body is agent-writable, and an owner token reaches
``PATCH /api/security/denied-commands/disable-all``, so accepting a live cron
child here would let an agent switch off its own denied-command rules through a
cron. Script crons reach the dashboard with their own internal-secret credential
instead (``ScriptContext.open_session`` and siblings).

These tests pin the gate against the accept branch that was proposed and
rejected: a LIVE child in the cron registry, and a descendant of it, are refused
exactly like any other sandboxed peer.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kiro_crew import cron_script
from kiro_crew import member_memory_auth as auth
from kiro_crew import platform_compat

_ROOT = 4101
_LAUNCHER_CHILD = 4102
_TOKEN_CLI = 4103


def _live_proc(pid: int) -> SimpleNamespace:
    """A stand-in ``Popen`` whose ``poll()`` is None: the child is unreaped."""
    return SimpleNamespace(pid=pid, poll=lambda: None)


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """A private cron-child registry, so no entry outlives the test."""
    procs: dict[str, object] = {}
    monkeypatch.setattr(cron_script, "_RUNNING_PROCS", procs)
    return procs


@pytest.fixture
def process_tree(monkeypatch: pytest.MonkeyPatch) -> dict[int, int]:
    """Parent links for any ancestry walk; a pid not listed is init's child."""
    parents: dict[int, int] = {}
    monkeypatch.setattr(platform_compat, "get_ppid", lambda pid: parents.get(pid, 1))
    return parents


@pytest.fixture
def sandboxed_caller(monkeypatch: pytest.MonkeyPatch):
    """Ask the real gate on behalf of a pid that fails the host-namespace check.

    Every caller here is sandboxed the way the cron runner sandboxes all of
    them, and no app backend is running. Nothing but a cron-registry branch
    could accept it, and there must not be one.
    """
    peer: dict[str, int] = {}
    monkeypatch.setattr(auth, "_request_peer_pid", lambda _request: peer["pid"])
    monkeypatch.setattr(auth.platform_compat, "get_process_start_id", lambda _pid: "start-id")
    monkeypatch.setattr(auth, "_verified_host_process", lambda _pid: False)
    monkeypatch.setattr(auth, "_gateway_spawned_app_backend", lambda _pid: False)

    def ask(pid: int) -> bool:
        peer["pid"] = pid
        return auth.local_owner_bootstrap_allowed(SimpleNamespace())

    return ask


class TestOwnerBootstrapRefusesScriptCrons:
    def test_a_live_registered_cron_child_is_refused(
        self, registry, process_tree, sandboxed_caller
    ):
        registry["dispatcher"] = _live_proc(_ROOT)

        assert sandboxed_caller(_ROOT) is False

    def test_a_descendant_of_a_live_cron_child_is_refused(
        self, registry, process_tree, sandboxed_caller
    ):
        """The shape the issue hit: the launcher runs the script, which runs the CLI."""
        registry["dispatcher"] = _live_proc(_ROOT)
        process_tree.update({_TOKEN_CLI: _LAUNCHER_CHILD, _LAUNCHER_CHILD: _ROOT})

        assert sandboxed_caller(_TOKEN_CLI) is False

    def test_the_gate_reads_no_cron_state(self, monkeypatch, sandboxed_caller):
        """Nothing a script can influence, the registry included, enters the decision."""

        class _Watched(dict):
            reads = 0

            def values(self):  # type: ignore[override]
                type(self).reads += 1
                return super().values()

            def items(self):  # type: ignore[override]
                type(self).reads += 1
                return super().items()

        watched = _Watched(dispatcher=_live_proc(_ROOT))
        monkeypatch.setattr(cron_script, "_RUNNING_PROCS", watched)

        assert sandboxed_caller(_ROOT) is False
        assert _Watched.reads == 0
