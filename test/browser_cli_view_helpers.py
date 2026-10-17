"""The fake child, clock, autouse reset and host-shape stubs the browser view tests share.

``test_browser_cli_view.py`` and its ``_port_owner`` and ``_child_proof`` siblings import
these by name: the stand-in for the supervised child, the clock only the code under test
advances, the autouse fixture that clears the view's singleton state, the real
loopback-listener probe captured before it is stubbed, the Windows and Linux host shapes,
the port-owner stub and the readers of the recorded child. ``reset_state`` is declared
``name="reset_state"`` on ``reset_state_fixture`` so no test parameter shadows the import.
"""

from __future__ import annotations

import io
import threading
from collections.abc import Iterator

import pytest

from kiro_crew import platform_compat
from kiro_crew.browser_cli import view as mod

_REAL_PROCESS_OWNS_LOOPBACK_LISTENER = platform_compat.process_owns_loopback_listener


class FakeProc:
    """Stand-in for the supervised child; never touches a real process.

    It also stands in for the ``_Child`` record ``_spawn`` returns (``proc``
    is itself, ``binding`` its spawn proof), so prover tests hand it in directly.
    """

    def __init__(self, alive: bool = True, pid: int = 424242, stdout: bytes = b"") -> None:
        self.pid = pid
        self._alive = alive
        self.returncode: int | None = None if alive else 1
        self.killed = False
        self.stdout = io.BytesIO(stdout)
        proof = mod._BindingProof(port=0, reported=threading.Event())
        proof.root_identity = platform_compat.ProcessDescendantIdentity(
            pid,
            0,
            str(pid),
            platform_compat.ProcessIdentitySource.ATOMIC,
        )
        setattr(proof, "cli_version", "0.1.99")
        self.proc = self
        self.binding = proof
        self.owner_identity: object = None

    def poll(self) -> int | None:
        return None if self._alive else self.returncode

    def wait(self, timeout: float | None = None) -> int:
        self._alive = False
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self._alive = False


class _FakeClock:
    """Stand-in for the module's ``time``, advanced only by the code under test.

    ``ensure_running``'s readiness gate is a ``time.monotonic`` deadline polled
    at ``time.sleep(_POLL_INTERVAL_S)``, so driving the clock from the sleeps
    turns "wait 30 real seconds" into a fixed, instant tick count.
    """

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture(name="reset_state", autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- the autouse reset of test_browser_cli_view.py and the modules split out of it, moved verbatim; none of them calls monkeypatch.undo()
def reset_state_fixture(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[int]]:
    """Clear the module singleton and neutralize real process signalling."""
    signalled: list[int] = []
    monkeypatch.setattr(mod, "_child", None)
    monkeypatch.setattr(mod, "_relay", None)
    monkeypatch.setattr(mod, "_last_reason", None)
    monkeypatch.setattr(mod, "_proof_cache", None, raising=False)
    monkeypatch.setattr(mod, "_listener_lookup_self_test_cache", None, raising=False)
    monkeypatch.setattr(
        mod,
        "_structurally_blind_listener_attribution",
        lambda: False,
        raising=False,
    )
    monkeypatch.setattr(mod, "cli_command", lambda cli=None: [cli] if cli else None)
    monkeypatch.setattr(mod, "installed_cli_version", lambda command=None: "0.1.99")
    monkeypatch.setattr(
        platform_compat,
        "get_process_start_id",
        lambda pid: str(pid),
    )
    # Ownership lookups are undecidable by default, so no test shells out to
    # lsof/netstat by accident. `_port_owner` then answers UNPROVEN, which is the
    # pre-identity behaviour every existing test was written against; the tests
    # that exercise ownership opt in with `_stub_port_owner`.
    monkeypatch.setattr(platform_compat, "listening_pid_tool_available", lambda: False)
    # A bare FakeProc stands for a real child that owns its listener. Tests for
    # blind hosts override these two seams independently.
    monkeypatch.setattr(mod, "_child_reported_binding", lambda proc, port: True, raising=False)
    monkeypatch.setattr(
        platform_compat,
        "process_owns_loopback_listener",
        lambda pid, port: True,
        raising=False,
    )
    monkeypatch.setattr(
        platform_compat,
        "kill_process_tree",
        lambda pid, sig=platform_compat.SIGTERM: signalled.append(pid) or True,
    )
    yield signalled
    mod._child = None
    mod._relay = None
    mod._last_reason = None


def _windows_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(platform_compat, "IS_LINUX", False)
    monkeypatch.setattr(platform_compat, "IS_POSIX", False)


def _linux_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
    monkeypatch.setattr(platform_compat, "IS_LINUX", True)
    monkeypatch.setattr(platform_compat, "IS_POSIX", True)


def _stub_port_owner(
    monkeypatch: pytest.MonkeyPatch,
    *,
    listener_pids: tuple[int, ...],
    descendants: tuple[int, ...] = (),
    tool: bool = True,
) -> None:
    """Make the port->PID lookup answer with *listener_pids*.

    Stubs the ``platform_compat`` primitives rather than ``_port_owner`` itself,
    so the module's own tier logic (tool absent, empty lookup, descendant match)
    is what the tests exercise.
    """
    monkeypatch.setattr(platform_compat, "listening_pid_tool_available", lambda: tool)
    listeners = [
        platform_compat.PortListener(pid=p, address="127.0.0.1", family="4") for p in listener_pids
    ]
    monkeypatch.setattr(platform_compat, "find_port_listeners", lambda port: list(listeners))
    monkeypatch.setattr(
        platform_compat,
        "probe_port_listeners",
        lambda port: (list(listeners), True),
        raising=False,
    )
    monkeypatch.setattr(platform_compat, "process_descendants", lambda pid: list(descendants))
    monkeypatch.setattr(
        platform_compat,
        "process_descendant_identities",
        lambda root_pid, candidate_pids=None: [
            platform_compat.ProcessDescendantIdentity(
                descendant_pid,
                root_pid,
                str(descendant_pid),
            )
            for descendant_pid in descendants
        ],
        raising=False,
    )
    monkeypatch.setattr(
        platform_compat,
        "process_start_time",
        lambda pid: str(pid),
    )


def _child_record(proc: FakeProc, port: int | None) -> mod._Child:
    """The record ``ensure_running`` installs once it adopts *proc* on *port*."""
    info = mod.ShowInfo(f"http://127.0.0.1:{port}", port) if port is not None else None
    return mod._Child(proc=proc, binding=proc.binding, child_port=port, info=info)


def _recorded_proc() -> FakeProc | None:
    return mod._child.proc if mod._child is not None else None


def _recorded_info() -> mod.ShowInfo | None:
    return mod._child.info if mod._child is not None else None


def _recorded_child_port() -> int | None:
    return mod._child.child_port if mod._child is not None else None
