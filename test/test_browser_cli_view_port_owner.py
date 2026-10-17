"""Port ownership of ``playwright-cli show``: reachability is not identity.

The owner lookup tells the child from a squatter on its port: the direct child and its
descendants are proven and a squatter is named, a missing lookup tool proves nothing, a
recycled Windows root or descendant is never adopted, one failed probe is transient while
the control probe works, the listener self-test is scoped to its own control PID and cached
per tool path, and start refuses a squatter on the child port while it adopts a proven
child. The shared helpers live in ``browser_cli_view_helpers.py``.
"""

from __future__ import annotations

import os

import pytest
from browser_cli_view_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _REAL_PROCESS_OWNS_LOOPBACK_LISTENER,
    FakeProc,
    _child_record,
    _FakeClock,
    _linux_shape,
    _recorded_child_port,
    _recorded_info,
    _recorded_proc,
    _stub_port_owner,
    _windows_shape,
    reset_state_fixture,
)

from kiro_crew import platform_compat
from kiro_crew.browser_cli import view as mod

_REAL_STRUCTURAL_BLINDNESS_PROBE = mod._structurally_blind_listener_attribution


# ── port ownership: reachability is not identity ────────────────────────────
#
# `_free_port` releases its probe socket before the child binds it, so a local
# process can take the number in between. `_healthy` then answers True for the
# squatter exactly as it would for our child, and the panel frames whatever the
# squatter serves — with input forwarding attached.


def test_port_owner_proves_the_direct_child(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = FakeProc(pid=4242)
    _windows_shape(monkeypatch)
    _stub_port_owner(monkeypatch, listener_pids=(4242,))

    assert mod._port_owner(45613, proc) == mod._OWNER_CHILD


def test_port_owner_proves_a_descendant(monkeypatch: pytest.MonkeyPatch) -> None:
    """The CLI spawns Node and helpers, so the listener is often not the child."""
    proc = FakeProc(pid=4242)
    _windows_shape(monkeypatch)
    _stub_port_owner(monkeypatch, listener_pids=(9931,), descendants=(9931,))
    monkeypatch.setattr(mod, "_listener_lookup_functional", lambda: True)
    monkeypatch.setattr(
        platform_compat,
        "process_owns_loopback_listener",
        lambda pid, port: pid == 9931,
    )

    assert mod._port_owner(45613, proc) == mod._OWNER_CHILD


@pytest.mark.parametrize(
    ("lstart_recheck", "expected"),
    [
        pytest.param(
            "Mon Jan  1 00:00:01 2024",
            mod._OWNER_CHILD,
            id="same-lstart-source",
        ),
        pytest.param(None, mod._OWNER_UNPROVEN, id="atomic-source-only"),
    ],
)
def test_posix_fallback_identity_rechecks_with_its_capture_source(
    monkeypatch: pytest.MonkeyPatch,
    lstart_recheck: str | None,
    expected: str,
) -> None:
    proc = FakeProc(pid=10)
    root_lstart = "Mon Jan  1 00:00:00 2024"
    child_lstart = "Mon Jan  1 00:00:01 2024"
    proof = proc.binding
    proof.root_identity = platform_compat.ProcessDescendantIdentity(
        proc.pid,
        0,
        root_lstart,
        platform_compat.ProcessIdentitySource.LSTART,
    )
    listener = platform_compat.PortListener(11, "127.0.0.1", "4")
    snapshot = {
        10: platform_compat._PosixProcessSnapshotRow(1, root_lstart),
        11: platform_compat._PosixProcessSnapshotRow(10, child_lstart),
    }
    sources: list[object] = []
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
    monkeypatch.setattr(platform_compat, "IS_LINUX", False)
    monkeypatch.setattr(platform_compat, "IS_POSIX", True)
    monkeypatch.setattr(platform_compat, "listening_pid_tool_available", lambda: True)
    monkeypatch.setattr(
        platform_compat,
        "probe_port_listeners",
        lambda port: ([listener], True),
    )
    monkeypatch.setattr(platform_compat, "_posix_process_snapshot", lambda: snapshot)
    monkeypatch.setattr(
        platform_compat,
        "get_process_start_id",
        lambda pid: str(pid) if pid == proc.pid else "atomic-child-start",
    )

    def _source_recheck(pid: int, source: object) -> str | None:
        sources.append(source)
        return root_lstart if pid == proc.pid else lstart_recheck

    monkeypatch.setattr(
        platform_compat,
        "process_start_id_for_source",
        _source_recheck,
        raising=False,
    )

    assert mod._port_owner(45613, proc) == expected
    assert sources
    assert {getattr(source, "value", None) for source in sources} == {"lstart"}


def test_owner_disappearing_during_identity_walk_is_inconclusive_and_not_reaped(
    monkeypatch: pytest.MonkeyPatch,
    reset_state: list[int],
) -> None:
    proc = FakeProc(pid=4242)
    owner_pid = 9931
    listener = platform_compat.PortListener(owner_pid, "127.0.0.1", "4")
    owner_starts = iter((str(owner_pid), None))
    verdicts: list[str] = []
    _linux_shape(monkeypatch)
    monkeypatch.setattr(platform_compat, "listening_pid_tool_available", lambda: True)
    monkeypatch.setattr(
        platform_compat,
        "probe_port_listeners",
        lambda port: ([listener], True),
    )
    monkeypatch.setattr(
        platform_compat,
        "process_descendant_identities",
        lambda pid, candidate_pids=None: [],
    )
    monkeypatch.setattr(
        platform_compat,
        "get_process_start_id",
        lambda pid: str(pid) if pid == proc.pid else next(owner_starts, None),
    )
    monkeypatch.setattr(platform_compat, "process_start_time", lambda pid: None)
    monkeypatch.setattr(
        platform_compat,
        "process_owns_loopback_listener",
        lambda pid, port: False,
    )
    monkeypatch.setattr(mod, "_child_reported_binding", lambda child, port: False)
    monkeypatch.setattr(mod, "_healthy", lambda port: True)
    monkeypatch.setattr(mod, "cli_path", lambda: None)
    real_port_owner = mod._port_owner

    def _record_port_owner(port: int, child: FakeProc) -> str:
        verdict = real_port_owner(port, child)
        verdicts.append(verdict)
        return verdict

    monkeypatch.setattr(mod, "_port_owner", _record_port_owner)
    mod._child = _child_record(proc, 45613)

    assert mod.ensure_running() is None
    assert verdicts == [mod._OWNER_UNPROVEN]
    assert _recorded_proc() is proc
    assert reset_state == []


def test_windows_recycled_root_cannot_authorize_its_listener(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc = FakeProc(pid=4242)
    listener = platform_compat.PortListener(4242, "127.0.0.1", "4")
    _windows_shape(monkeypatch)
    monkeypatch.setattr(platform_compat, "listening_pid_tool_available", lambda: True)
    monkeypatch.setattr(
        platform_compat,
        "probe_port_listeners",
        lambda port: ([listener], True),
    )
    monkeypatch.setattr(
        platform_compat,
        "process_descendant_identities",
        lambda pid, candidate_pids=None: [],
    )
    monkeypatch.setattr(platform_compat, "get_process_start_id", lambda pid: "recycled")

    assert mod._port_owner(45613, proc) == mod._OWNER_FOREIGN


def test_windows_descendant_recycled_before_confirming_probe_is_not_adopted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc = FakeProc(pid=4242)
    identity = platform_compat.ProcessDescendantIdentity(9931, 4242, "old")
    starts = iter(("old", "new"))
    listener = platform_compat.PortListener(9931, "127.0.0.1", "4")
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(platform_compat, "IS_POSIX", False)
    monkeypatch.setattr(platform_compat, "IS_LINUX", False)
    monkeypatch.setattr(platform_compat, "listening_pid_tool_available", lambda: True)
    monkeypatch.setattr(
        platform_compat,
        "probe_port_listeners",
        lambda port: ([listener], True),
    )
    monkeypatch.setattr(platform_compat, "process_descendants", lambda pid: [9931])
    monkeypatch.setattr(
        platform_compat,
        "process_descendant_identities",
        lambda pid, candidate_pids=None: [identity],
    )
    monkeypatch.setattr(
        platform_compat,
        "get_process_start_id",
        lambda pid: str(pid) if pid == proc.pid else next(starts, "new"),
    )
    monkeypatch.setattr(
        platform_compat,
        "process_owns_loopback_listener",
        lambda pid, port: None,
    )
    monkeypatch.setattr(mod, "_structurally_blind_listener_attribution", lambda: False)
    monkeypatch.setattr(mod, "_child_reported_binding", lambda child, port: False)

    assert mod._verify_child_listener(proc, 45613, allow_report=False) == (None, False)


@pytest.mark.parametrize(
    ("second_map", "expected"),
    [
        ({10: 1, 11: 10, 13: 10}, mod._OWNER_CHILD),
        ({10: 1, 11: 12, 12: 10}, mod._OWNER_UNPROVEN),
    ],
    ids=["sibling-churn", "listener-chain-changed"],
)
def test_windows_owner_stability_is_scoped_to_the_listener_chain(
    monkeypatch: pytest.MonkeyPatch,
    second_map: dict[int, int],
    expected: str,
) -> None:
    proc = FakeProc(pid=10)
    first_map = {10: 1, 11: 10, 12: 10}
    maps = iter((first_map, first_map, second_map))
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(platform_compat, "IS_POSIX", False)
    monkeypatch.setattr(platform_compat, "IS_LINUX", False)
    monkeypatch.setattr(mod, "_listener_lookup_functional", lambda: True)
    monkeypatch.setattr(
        platform_compat,
        "process_owns_loopback_listener",
        lambda pid, port: pid == 11,
    )
    monkeypatch.setattr(
        platform_compat,
        "probe_port_listeners",
        lambda port: pytest.fail("Windows ownership must not invoke netstat"),
    )
    monkeypatch.setattr(platform_compat, "_windows_process_parent_map", lambda: next(maps))
    monkeypatch.setattr(
        platform_compat,
        "get_process_start_id",
        lambda pid: str(pid),
    )

    assert mod._port_owner(45613, proc) == expected


def test_owner_child_proof_is_rechecked_before_adoption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc = FakeProc(pid=4242)
    proof = platform_compat.ProcessDescendantIdentity(9931, 4242, "old")
    monkeypatch.setattr(mod, "_port_owner", lambda port, child: mod._OWNER_CHILD)
    monkeypatch.setattr(
        mod,
        "_take_port_owner_identity_proof",
        lambda child, port: proof,
    )
    monkeypatch.setattr(
        platform_compat,
        "get_process_start_id",
        lambda pid: str(pid) if pid == proc.pid else "new",
    )
    monkeypatch.setattr(mod, "_child_reported_binding", lambda child, port: False)

    assert mod._verify_child_listener(proc, 45613, allow_report=False) == (False, False)


def test_port_owner_names_a_squatter(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = FakeProc(pid=4242)
    _windows_shape(monkeypatch)
    _stub_port_owner(monkeypatch, listener_pids=(777,), descendants=(9931,))
    monkeypatch.setattr(mod, "_listener_lookup_functional", lambda: True)
    monkeypatch.setattr(
        platform_compat,
        "process_owns_loopback_listener",
        lambda pid, port: False,
    )

    assert mod._port_owner(45613, proc) == mod._OWNER_FOREIGN


def test_port_owner_is_unproven_without_the_lookup_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one fail-open branch: a host without lsof must keep its panel.

    Static per host rather than per start, and not attacker-controllable --
    removing the tool needs the access that makes this panel moot.
    """
    proc = FakeProc(pid=4242)
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
    monkeypatch.setattr(platform_compat, "IS_LINUX", False)
    monkeypatch.setattr(platform_compat, "IS_POSIX", True)
    _stub_port_owner(monkeypatch, listener_pids=(777,), tool=False)

    assert mod._port_owner(45613, proc) == mod._OWNER_UNPROVEN


def test_port_owner_treats_one_failed_probe_as_transient_when_control_is_functional(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc = FakeProc(pid=4242)
    target_port = 45613
    control_port = 45614
    probes: list[tuple[int, int]] = []
    _windows_shape(monkeypatch)

    class _ControlListener:
        def getsockname(self) -> tuple[str, int]:
            return (mod.LOOPBACK_HOST, control_port)

        def close(self) -> None:
            pass

    def _owns_listener(pid: int, port: int) -> bool | None:
        probes.append((pid, port))
        if (pid, port) == (proc.pid, target_port):
            return None
        if (pid, port) == (os.getpid(), control_port):
            return True
        pytest.fail(f"unexpected Windows listener probe: pid={pid}, port={port}")

    monkeypatch.setattr(mod, "_listener_lookup_self_test_cache", None, raising=False)
    monkeypatch.setattr(mod, "_claim_listener", lambda port: _ControlListener())
    monkeypatch.setattr(
        platform_compat,
        "process_owns_loopback_listener",
        _owns_listener,
    )
    monkeypatch.setattr(
        platform_compat,
        "listening_pid_tool_available",
        lambda: pytest.fail("Windows ownership must not resolve netstat"),
    )
    monkeypatch.setattr(
        platform_compat,
        "probe_port_listeners",
        lambda port: pytest.fail("Windows ownership must not invoke netstat"),
    )

    assert mod._port_owner(target_port, proc) == mod._OWNER_UNPROVEN
    assert probes == [(proc.pid, target_port), (os.getpid(), control_port)]


def test_windows_port_owner_never_invokes_netstat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc = FakeProc(pid=4242)
    _windows_shape(monkeypatch)
    monkeypatch.setattr(
        platform_compat,
        "listening_pid_tool_available",
        lambda: pytest.fail("Windows ownership must not resolve netstat"),
    )
    monkeypatch.setattr(
        platform_compat,
        "probe_port_listeners",
        lambda port: pytest.fail("Windows ownership must not invoke netstat"),
    )
    monkeypatch.setattr(
        platform_compat,
        "process_owns_loopback_listener",
        lambda pid, port: True,
    )

    assert mod._port_owner(45613, proc) == mod._OWNER_CHILD


@pytest.mark.parametrize(
    "target_result",
    [None, False],
    ids=["target-incomplete", "target-empty"],
)
def test_inconclusive_control_probe_never_claims_foreign(
    monkeypatch: pytest.MonkeyPatch,
    target_result: bool | None,
) -> None:
    target_port = 45613
    control_port = 45614
    proc = FakeProc(pid=4242)
    probes: list[tuple[int, int]] = []
    _windows_shape(monkeypatch)

    class _ControlListener:
        def getsockname(self) -> tuple[str, int]:
            return (mod.LOOPBACK_HOST, control_port)

        def close(self) -> None:
            pass

    def _owns_listener(pid: int, port: int) -> bool | None:
        probes.append((pid, port))
        if (pid, port) == (proc.pid, target_port):
            return target_result
        if (pid, port) == (os.getpid(), control_port):
            return None
        pytest.fail(f"unexpected Windows listener probe: pid={pid}, port={port}")

    monkeypatch.setattr(mod, "_listener_lookup_self_test_cache", None, raising=False)
    monkeypatch.setattr(mod, "_claim_listener", lambda port: _ControlListener())
    monkeypatch.setattr(
        platform_compat,
        "process_owns_loopback_listener",
        _owns_listener,
    )
    monkeypatch.setattr(
        platform_compat,
        "listening_pid_tool_available",
        lambda: pytest.fail("Windows ownership must not resolve netstat"),
    )
    monkeypatch.setattr(
        platform_compat,
        "probe_port_listeners",
        lambda port: pytest.fail("Windows ownership must not invoke netstat"),
    )

    assert mod._port_owner(target_port, proc) == mod._OWNER_UNPROVEN
    assert probes == [(proc.pid, target_port), (os.getpid(), control_port)]


def test_transient_probe_failure_cannot_promote_startup_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc = FakeProc(pid=4242)
    monkeypatch.setattr(mod, "_port_owner", lambda port, child: mod._OWNER_UNPROVEN)
    monkeypatch.setattr(mod, "_child_reported_binding", lambda child, port: True)
    monkeypatch.setattr(
        platform_compat,
        "process_owns_loopback_listener",
        lambda pid, port: None,
    )
    monkeypatch.setattr(
        platform_compat,
        "process_descendant_identities",
        lambda pid, candidate_pids=None: None,
    )
    monkeypatch.setattr(mod, "_structurally_blind_listener_attribution", lambda: False)

    assert mod._verify_child_listener(proc, 45613, allow_report=True) == (None, False)


@pytest.mark.parametrize(
    ("observed", "expected"),
    [
        pytest.param(True, True, id="own-control-pid"),
        pytest.param(False, False, id="completed-without-own-pid"),
        pytest.param(None, None, id="table-failure"),
    ],
)
def test_windows_listener_self_test_requires_own_control_pid(
    monkeypatch: pytest.MonkeyPatch,
    observed: bool | None,
    expected: bool | None,
) -> None:
    control_port = 45614
    probes: list[tuple[int, int]] = []

    class _ControlListener:
        def getsockname(self) -> tuple[str, int]:
            return (mod.LOOPBACK_HOST, control_port)

        def close(self) -> None:
            pass

    _windows_shape(monkeypatch)
    monkeypatch.setattr(mod, "_listener_lookup_self_test_cache", None, raising=False)
    monkeypatch.setattr(mod, "_claim_listener", lambda port: _ControlListener())
    monkeypatch.setattr(
        platform_compat,
        "process_owns_loopback_listener",
        lambda pid, port: probes.append((pid, port)) or observed,
    )
    monkeypatch.setattr(
        platform_compat,
        "probe_port_listeners",
        lambda port: pytest.fail("Windows self-test must not invoke netstat"),
    )

    assert mod._listener_lookup_functional() is expected
    assert probes == [(os.getpid(), control_port)]


def test_windows_inconclusive_self_test_is_not_blind_or_adoptable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc = FakeProc(pid=4242)
    _windows_shape(monkeypatch)
    monkeypatch.setattr(
        mod,
        "_structurally_blind_listener_attribution",
        _REAL_STRUCTURAL_BLINDNESS_PROBE,
    )
    monkeypatch.setattr(mod, "_listener_lookup_functional", lambda: None)
    monkeypatch.setattr(mod, "_port_owner", lambda port, child: mod._OWNER_UNPROVEN)
    monkeypatch.setattr(mod, "_child_reported_binding", lambda child, port: True)
    monkeypatch.setattr(
        platform_compat,
        "process_owns_loopback_listener",
        lambda pid, port: None,
    )
    monkeypatch.setattr(
        platform_compat,
        "process_descendant_identities",
        lambda pid: None,
    )

    structurally_blind = mod._structurally_blind_listener_attribution()
    verification = mod._verify_child_listener(proc, 45613, allow_report=True)

    assert structurally_blind is False
    assert verification == (None, False)


def test_two_status_calls_run_one_self_test_after_the_target_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child_port = 45613
    control_port = 45614
    claims: list[int] = []
    probes: list[int] = []

    class _ControlListener:
        def getsockname(self) -> tuple[str, int]:
            return (mod.LOOPBACK_HOST, control_port)

        def close(self) -> None:
            pass

    proc = FakeProc(pid=4242)
    monkeypatch.setattr(mod, "_child", _child_record(proc, child_port))
    monkeypatch.setattr(
        mod,
        "_structurally_blind_listener_attribution",
        _REAL_STRUCTURAL_BLINDNESS_PROBE,
    )
    monkeypatch.setattr(mod, "_listener_lookup_self_test_cache", None, raising=False)
    monkeypatch.setattr(mod, "cli_path", lambda: "/n/pw")
    monkeypatch.setattr(mod, "_healthy", lambda port: True)
    monkeypatch.setattr(platform_compat, "IS_LINUX", False)
    monkeypatch.setattr(platform_compat, "IS_POSIX", True)
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
    monkeypatch.setattr(platform_compat, "listening_pid_tool_available", lambda: True)
    monkeypatch.setattr(
        platform_compat,
        "trusted_system_bin",
        lambda name: "/usr/bin/lsof" if name == "lsof" else None,
    )
    monkeypatch.setattr(
        mod,
        "_claim_listener",
        lambda port: claims.append(port) or _ControlListener(),
    )

    def _probe(port: int) -> tuple[list[platform_compat.PortListener], bool]:
        probes.append(port)
        return [], True

    monkeypatch.setattr(platform_compat, "probe_port_listeners", _probe)
    monkeypatch.setattr(
        platform_compat, "process_descendant_identities", lambda pid, candidate_pids=None: []
    )
    monkeypatch.setattr(
        platform_compat,
        "process_owns_loopback_listener",
        lambda pid, port: False,
    )

    mod.status()
    mod.status()

    assert claims == [0]
    assert probes == [child_port, control_port, child_port]


def test_listener_self_test_cache_invalidates_when_the_tool_path_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool_path = ["/usr/bin/lsof"]
    control_ports = iter((45614, 45615))
    claims: list[int] = []
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
    monkeypatch.setattr(platform_compat, "IS_POSIX", True)
    monkeypatch.setattr(platform_compat, "IS_LINUX", False)

    class _ControlListener:
        def __init__(self, port: int) -> None:
            self.port = port

        def getsockname(self) -> tuple[str, int]:
            return (mod.LOOPBACK_HOST, self.port)

        def close(self) -> None:
            pass

    monkeypatch.setattr(mod, "_listener_lookup_self_test_cache", None, raising=False)
    monkeypatch.setattr(
        platform_compat,
        "trusted_system_bin",
        lambda name: tool_path[0] if name == "lsof" else None,
    )
    monkeypatch.setattr(
        mod,
        "_claim_listener",
        lambda port: claims.append(port) or _ControlListener(next(control_ports)),
    )
    monkeypatch.setattr(
        platform_compat,
        "probe_port_listeners",
        lambda port: (
            [platform_compat.PortListener(os.getpid(), "127.0.0.1", "4")],
            True,
        ),
    )

    assert mod._listener_lookup_functional() is True
    assert mod._listener_lookup_functional() is True
    tool_path[0] = "/opt/tools/lsof"
    assert mod._listener_lookup_functional() is True

    assert claims == [0, 0]


def test_blind_lookup_squatter_is_not_adopted_without_child_binding_proof(
    monkeypatch: pytest.MonkeyPatch,
    reset_state: list[int],
) -> None:
    """A squatter cannot receive the host-scoped browser cookie through the view URL."""
    proc = FakeProc(pid=4242)
    clock = _FakeClock()
    monkeypatch.setattr(mod, "cli_path", lambda: "/n/pw")
    monkeypatch.setattr(mod, "_healthy", lambda port: True)
    monkeypatch.setattr(mod, "_spawn", lambda cli, port: proc)
    monkeypatch.setattr(mod, "time", clock)
    monkeypatch.setattr(mod, "_child_reported_binding", lambda child, port: False, raising=False)
    monkeypatch.setattr(
        platform_compat,
        "process_owns_loopback_listener",
        lambda pid, port: None,
        raising=False,
    )
    # The shared stub, not a hand-rolled copy of its three listener pins: the copy
    # dropped ``process_descendant_identities``, so every one of the 120 readiness
    # rounds ran two real ``ps`` snapshots against pid 4242 -- a host process this
    # test does not own -- and the fake-clock loop took 20 real seconds.
    _stub_port_owner(monkeypatch, listener_pids=())

    assert mod.ensure_running() is None
    assert _recorded_info() is None
    assert proc.pid in reset_state
    assert mod.status()["url"] is None


def test_blind_lookup_adopts_real_child_after_recognized_binding_report(
    monkeypatch: pytest.MonkeyPatch,
    reset_state: list[int],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A recognized URL from the trusted child's stdout is positive proof."""
    proc = FakeProc(pid=4242)
    monkeypatch.setattr(mod, "cli_path", lambda: "/n/pw")
    monkeypatch.setattr(mod, "_healthy", lambda port: True)
    monkeypatch.setattr(mod, "_spawn", lambda cli, port: proc)
    monkeypatch.setattr(mod, "_structurally_blind_listener_attribution", lambda: True)
    monkeypatch.setattr(
        mod,
        "_child_reported_port",
        lambda child, requested_port: 45613,
    )
    monkeypatch.setattr(mod, "_child_reported_binding", lambda child, port: True, raising=False)
    monkeypatch.setattr(
        platform_compat,
        "process_owns_loopback_listener",
        lambda pid, port: None,
        raising=False,
    )
    _stub_port_owner(monkeypatch, listener_pids=())

    with caplog.at_level("WARNING"):
        info = mod.ensure_running()

    assert info is not None
    assert proc.pid not in reset_state
    assert any("reported that it bound" in r.message for r in caplog.records)


def test_port_owner_refuses_when_lookup_attributes_control_but_not_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A functional Windows table seeing no target owner still means foreign."""
    proc = FakeProc(pid=4242)
    target_port = 45613
    control_port = 45614
    looked_up: list[int] = []
    _windows_shape(monkeypatch)
    _stub_port_owner(monkeypatch, listener_pids=(), descendants=())

    class _ControlListener:
        def getsockname(self) -> tuple[str, int]:
            return (mod.LOOPBACK_HOST, control_port)

        def close(self) -> None:
            pass

    def _owners(port: int) -> set[int]:
        looked_up.append(port)
        return set() if port == target_port else {os.getpid()}

    monkeypatch.setattr(mod, "_listener_lookup_self_test_cache", None, raising=False)
    monkeypatch.setattr(mod, "_claim_listener", lambda port: _ControlListener())
    monkeypatch.setattr(
        platform_compat,
        "process_owns_loopback_listener",
        _REAL_PROCESS_OWNS_LOOPBACK_LISTENER,
    )
    monkeypatch.setattr(
        platform_compat,
        "_windows_loopback_listener_owner_pids",
        _owners,
    )

    assert mod._port_owner(target_port, proc) == mod._OWNER_FOREIGN
    assert looked_up == [target_port, control_port]


def test_port_owner_preserves_when_control_lookup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed Windows control-table read is not completed non-ownership."""
    proc = FakeProc(pid=4242)
    target_port = 45613
    control_port = 45614
    looked_up: list[int] = []
    _windows_shape(monkeypatch)
    _stub_port_owner(monkeypatch, listener_pids=(), descendants=())

    class _ControlListener:
        def getsockname(self) -> tuple[str, int]:
            return (mod.LOOPBACK_HOST, control_port)

        def close(self) -> None:
            pass

    def _owners(port: int) -> set[int] | None:
        looked_up.append(port)
        return set() if port == target_port else None

    monkeypatch.setattr(mod, "_listener_lookup_self_test_cache", None, raising=False)
    monkeypatch.setattr(mod, "_claim_listener", lambda port: _ControlListener())
    monkeypatch.setattr(
        platform_compat,
        "process_owns_loopback_listener",
        _REAL_PROCESS_OWNS_LOOPBACK_LISTENER,
    )
    monkeypatch.setattr(
        platform_compat,
        "_windows_loopback_listener_owner_pids",
        _owners,
    )

    assert mod._port_owner(target_port, proc) == mod._OWNER_UNPROVEN
    assert looked_up == [target_port, control_port]


def test_port_owner_preserves_when_control_listener_cannot_be_created(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No Windows control listener means table capability stays unproven."""
    proc = FakeProc(pid=4242)
    target_port = 45613
    looked_up: list[int] = []
    _windows_shape(monkeypatch)
    _stub_port_owner(monkeypatch, listener_pids=(), descendants=())
    monkeypatch.setattr(mod, "_listener_lookup_self_test_cache", None, raising=False)
    monkeypatch.setattr(mod, "_claim_listener", lambda port: None)
    monkeypatch.setattr(
        platform_compat,
        "process_owns_loopback_listener",
        _REAL_PROCESS_OWNS_LOOPBACK_LISTENER,
    )
    monkeypatch.setattr(
        platform_compat,
        "_windows_loopback_listener_owner_pids",
        lambda port: looked_up.append(port) or set(),
    )

    assert mod._port_owner(target_port, proc) == mod._OWNER_UNPROVEN
    assert looked_up == [target_port]


def test_port_owner_refuses_without_a_child_to_compare(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No recorded child means nothing can be proved ours."""
    _stub_port_owner(monkeypatch, listener_pids=(4242,))

    assert mod._port_owner(45613, None) == mod._OWNER_FOREIGN


def test_ensure_running_refuses_a_squatter_on_the_child_port(
    monkeypatch: pytest.MonkeyPatch, reset_state: list[int]
) -> None:
    """A Windows owner-table row for a non-descendant must remain foreign."""
    proc = FakeProc(pid=4242)
    _windows_shape(monkeypatch)
    _stub_port_owner(monkeypatch, listener_pids=(), descendants=())
    monkeypatch.setattr(mod, "cli_path", lambda: "/n/pw")
    monkeypatch.setattr(mod, "_healthy", lambda port: True)
    monkeypatch.setattr(mod, "_spawn", lambda cli, port: proc)
    monkeypatch.setattr(mod, "_listener_lookup_functional", lambda: True)
    monkeypatch.setattr(
        platform_compat,
        "process_owns_loopback_listener",
        _REAL_PROCESS_OWNS_LOOPBACK_LISTENER,
    )
    monkeypatch.setattr(
        platform_compat,
        "_windows_loopback_listener_owner_pids",
        lambda port: {777},
    )

    assert mod.ensure_running() is None
    assert _recorded_info() is None
    assert _recorded_child_port() is None
    # The child we spawned is reaped rather than left holding nothing.
    assert proc.pid in reset_state
    status = mod.status()
    assert status["status"] == "stopped"
    assert "took port" in (status["reason"] or "")


def test_ensure_running_adopts_a_proven_child(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = FakeProc(pid=4242)
    monkeypatch.setattr(mod, "cli_path", lambda: "/n/pw")
    monkeypatch.setattr(mod, "_healthy", lambda port: True)
    monkeypatch.setattr(mod, "_spawn", lambda cli, port: proc)
    _stub_port_owner(monkeypatch, listener_pids=(4242,))

    info = mod.ensure_running()

    assert info is not None
    assert _recorded_child_port() == info.port


def test_ensure_running_adopts_when_tool_absent_but_child_reports_binding(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A missing PID lookup is safe when the trusted child proves its bind."""
    monkeypatch.setattr(mod, "cli_path", lambda: "/n/pw")
    monkeypatch.setattr(mod, "_healthy", lambda port: True)
    monkeypatch.setattr(mod, "_spawn", lambda cli, port: FakeProc(pid=4242))
    monkeypatch.setattr(mod, "_structurally_blind_listener_attribution", lambda: True)
    monkeypatch.setattr(
        mod,
        "_child_reported_port",
        lambda child, requested_port: 45613,
    )
    monkeypatch.setattr(mod, "_child_reported_binding", lambda child, port: True)
    monkeypatch.setattr(
        platform_compat,
        "process_owns_loopback_listener",
        lambda pid, port: None,
    )
    _stub_port_owner(monkeypatch, listener_pids=(777,), tool=False)

    with caplog.at_level("WARNING"):
        assert mod.ensure_running() is not None

    assert any("reported that it bound" in r.message for r in caplog.records)


def test_blind_global_lsof_demotes_pid_scoped_negative_to_inconclusive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc = FakeProc(pid=4242)
    _stub_port_owner(monkeypatch, listener_pids=(), tool=True)
    monkeypatch.setattr(platform_compat, "IS_LINUX", False)
    monkeypatch.setattr(platform_compat, "IS_POSIX", True)
    monkeypatch.setattr(mod, "_listener_lookup_functional", lambda: False)
    monkeypatch.setattr(mod, "_structurally_blind_listener_attribution", lambda: True)
    monkeypatch.setattr(mod, "_child_reported_binding", lambda child, port: False)
    monkeypatch.setattr(
        platform_compat,
        "process_owns_loopback_listener",
        lambda pid, port: False,
    )

    assert mod._verify_child_listener(proc, 45613, allow_report=False) == (None, False)


@pytest.mark.parametrize(
    ("target_owners", "expected"),
    [
        pytest.param({9931}, (True, False), id="descendant-owner"),
        pytest.param({7777}, (False, False), id="foreign-owner"),
        pytest.param(None, (None, False), id="table-failure"),
    ],
)
def test_windows_owner_pid_table_decides_listener_ownership(
    monkeypatch: pytest.MonkeyPatch,
    target_owners: set[int] | None,
    expected: tuple[bool | None, bool],
) -> None:
    target_port = 45613
    control_port = 45614
    proc = FakeProc(pid=4242)
    identity = platform_compat.ProcessDescendantIdentity(9931, proc.pid, "9931")

    class _ControlListener:
        def getsockname(self) -> tuple[str, int]:
            return (mod.LOOPBACK_HOST, control_port)

        def close(self) -> None:
            pass

    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(platform_compat, "IS_LINUX", False)
    monkeypatch.setattr(platform_compat, "IS_POSIX", False)
    monkeypatch.setattr(platform_compat, "listening_pid_tool_available", lambda: False)
    monkeypatch.setattr(
        platform_compat,
        "process_owns_loopback_listener",
        _REAL_PROCESS_OWNS_LOOPBACK_LISTENER,
    )
    monkeypatch.setattr(
        platform_compat,
        "probe_port_listeners",
        lambda *args, **kwargs: pytest.fail("netstat must not be used"),
    )
    monkeypatch.setattr(
        platform_compat,
        "_windows_loopback_listener_owner_pids",
        lambda port: (
            None
            if target_owners is None
            else ({os.getpid()} if port == control_port else target_owners)
        ),
        raising=False,
    )
    monkeypatch.setattr(
        platform_compat,
        "process_descendant_identities",
        lambda pid, candidate_pids=None: [identity],
    )
    monkeypatch.setattr(mod, "_claim_listener", lambda port: _ControlListener())
    monkeypatch.setattr(mod, "_listener_lookup_self_test_cache", None, raising=False)
    monkeypatch.setattr(mod, "_child_reported_binding", lambda child, port: False)

    assert mod._verify_child_listener(proc, target_port, allow_report=False) == expected
