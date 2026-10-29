"""Host power detection reads as "unknown" on any failure, and keep-awake fails toward sleep.

Laptop detection and the collaborator checks are driven with fakes of what they
read (the power-supply tree, ``pmset``, the live allowlist, the admission
service); nothing here touches the real host's power state.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import pytest

from kiro_crew import platform_compat
from kiro_crew.dashboard import tailnet_live_allowlist
from kiro_crew.shared_crew import host_power as hp
from kiro_crew.shared_crew.admission import OperationState
from kiro_crew.shared_crew.contracts import Principal, PrincipalKind, Provenance
from kiro_crew.shared_crew.identity import local_operator_principal, tailnet_principal
from kiro_crew.shared_crew.routes_invites import WS_ACTOR_KEY


@pytest.fixture(autouse=True)
def _fresh_report() -> Iterator[None]:
    saved = dict(hp._REPORTED)
    hp._REPORTED.clear()
    yield
    hp._REPORTED.clear()
    hp._REPORTED.update(saved)


def _supply(root: Path, name: str, kind: str | None, scope: str | None = None) -> None:
    d = root / name
    d.mkdir(parents=True)
    if kind is not None:
        (d / "type").write_text(kind + "\n")
    if scope is not None:
        (d / "scope").write_text(scope + "\n")


def test_linux_battery_reads_unknown_without_a_power_supply_tree(tmp_path: Path) -> None:
    assert hp._linux_battery(tmp_path / "absent") is None


def test_linux_battery_skips_unreadable_and_peripheral_supplies(tmp_path: Path) -> None:
    _supply(tmp_path, "AC", "Mains")
    _supply(tmp_path, "odd", None)
    _supply(tmp_path, "mouse", "Battery", "Device")
    assert hp._linux_battery(tmp_path) is False
    _supply(tmp_path, "BAT0", "Battery")
    assert hp._linux_battery(tmp_path) is True


def test_macos_battery_is_unknown_without_pmset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hp.os.path, "isfile", lambda _p: False)
    assert hp._macos_battery(pytest.fail) is None


@pytest.mark.parametrize(
    ("run_result", "expected"),
    [
        (SimpleNamespace(stdout="-InternalBattery-0 (id=1) 80%"), True),
        (SimpleNamespace(stdout="Now drawing from 'AC Power'"), False),
        (subprocess.TimeoutExpired("pmset", 5), None),
    ],
    ids=["battery", "desktop", "timeout"],
)
def test_macos_battery(monkeypatch: pytest.MonkeyPatch, run_result: Any, expected: Any) -> None:
    monkeypatch.setattr(hp.os.path, "isfile", lambda p: p == hp.PMSET_PATH)
    monkeypatch.setattr(hp.os, "access", lambda p, _m: p == hp.PMSET_PATH)

    def run(*_a: Any, **_kw: Any) -> Any:
        if isinstance(run_result, BaseException):
            raise run_result
        return run_result

    assert hp._macos_battery(run) is expected


def test_windows_battery_is_unknown_off_windows() -> None:
    if platform_compat.IS_WINDOWS:
        pytest.skip("reads the real power status on Windows")
    assert hp._windows_battery() is None


def test_a_probe_that_raises_reads_unknown() -> None:
    def broken() -> bool:
        raise RuntimeError("no WMI")

    info = hp.detect_laptop(system="windows", windows_probe=broken)
    assert info == hp.LaptopInfo(False, hp.REASON_UNKNOWN)
    assert info.to_dict() == {"laptop": False, "reason": "unknown"}
    assert hp.detect_laptop(system="plan9") == hp.LaptopInfo(False, hp.REASON_UNKNOWN)
    assert hp.detect_laptop(system="windows", windows_probe=lambda: False) == hp.LaptopInfo(
        False, hp.REASON_NO_BATTERY
    )


@pytest.mark.parametrize(
    ("macos", "windows", "linux", "expected"),
    [
        (True, False, False, "darwin"),
        (False, True, False, "windows"),
        (False, False, True, "linux"),
        (False, False, False, "other"),
    ],
)
def test_system_name(
    monkeypatch: pytest.MonkeyPatch, macos: bool, windows: bool, linux: bool, expected: str
) -> None:
    monkeypatch.setattr(platform_compat, "IS_MACOS", macos)
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", windows)
    monkeypatch.setattr(platform_compat, "IS_LINUX", linux)
    assert hp._system() == expected


def test_reported_is_not_checked_until_note_host(monkeypatch: pytest.MonkeyPatch) -> None:
    assert hp.reported()["reason"] == "not_checked"
    monkeypatch.setattr(hp, "detect_laptop", lambda: hp.LaptopInfo(True, hp.REASON_BATTERY))
    monkeypatch.setattr(
        hp, "keep_awake_capability", lambda: {"state": "available", "reason": "", "backend": "x"}
    )
    noted = hp.note_host()
    assert noted == hp.reported()
    assert noted["laptop"] is True and noted["laptop_reason"] == hp.REASON_BATTERY


@pytest.mark.asyncio
async def test_probe_after_start_survives_both_probes_failing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew.dashboard import tailnet_proxy_proof

    def broken(*_a: Any) -> Any:
        raise RuntimeError("probe failed")

    monkeypatch.setattr(tailnet_proxy_proof, "proof_support", broken)
    monkeypatch.setattr(hp, "note_host", broken)
    await hp.probe_after_start()
    assert hp.reported()["reason"] == "not_checked"


# -- who keeps the host awake --------------------------------------------- #


def _person(login: str, *, tagged: bool = False) -> Principal:
    return tailnet_principal(login, "node-1", Provenance.TAILSCALE_LISTENER, tagged=tagged)


def _snapshot(**kw: Any) -> Any:
    base = dict(
        owner_login="Owner@example.com",
        owner_user_id="11",
        member_logins=frozenset(),
        approved_logins=frozenset(),
        approved_user_ids=frozenset(),
        network_trust=False,
    )
    base.update(kw)
    return SimpleNamespace(**base)


class _Ws(dict):
    def __init__(self, principal: Any, closed: bool = False) -> None:
        super().__init__({WS_ACTOR_KEY: SimpleNamespace(principal=principal)})
        self.closed = closed


class _BadWs:
    closed = False

    def get(self, _key: str) -> Any:
        raise RuntimeError("torn socket")


def test_only_an_open_collaborator_socket_keeps_the_host_awake(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tailnet_live_allowlist, "snapshot", lambda: _snapshot())
    owner = _person("owner@example.com")
    teammate = _person("bob@example.com")
    local = local_operator_principal("local-app")
    asleep = SimpleNamespace(
        _ws_clients=[_BadWs(), _Ws(owner), _Ws(local), _Ws(teammate, closed=True)]
    )
    assert hp.collaborator_connected(asleep) is False
    awake = SimpleNamespace(_ws_clients=[_Ws(teammate)])
    assert hp.collaborator_connected(awake) is True
    machine = _person("tagged-devices", tagged=True)
    assert machine.kind is PrincipalKind.MACHINE
    assert hp.collaborator_connected(SimpleNamespace(_ws_clients=[_Ws(machine)])) is True


def test_no_allowlist_snapshot_means_no_owner_to_exclude(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tailnet_live_allowlist, "snapshot", lambda: None)
    assert hp._owner_keys() == ("", "")
    state = SimpleNamespace(_ws_clients=[_Ws(_person("owner@example.com"))])
    assert hp.collaborator_connected(state) is True


def _service(*works: tuple[str, str]) -> Any:
    live = {
        i: SimpleNamespace(work=SimpleNamespace(budget_key=key, state=state))
        for i, (key, state) in enumerate(works)
    }
    return SimpleNamespace(_live=live)


def test_collaborator_work_keeps_the_host_awake_until_it_ends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew.shared_crew import access_set, admission_gate

    monkeypatch.setattr(
        tailnet_live_allowlist,
        "snapshot",
        lambda: _snapshot(member_logins=frozenset({"bob@example.com", "owner@example.com"})),
    )
    monkeypatch.setattr(
        access_set, "principal_ids_for", lambda login, _uids: frozenset({f"id:{login}"})
    )
    monkeypatch.setattr(
        admission_gate,
        "installed_service",
        lambda: _service(("id:bob@example.com", OperationState.RUNNING)),
    )
    assert hp.collaborator_work_active() is True
    monkeypatch.setattr(
        admission_gate,
        "installed_service",
        lambda: _service(
            ("id:bob@example.com", OperationState.COMPLETED),
            ("id:owner@example.com", OperationState.RUNNING),
        ),
    )
    assert hp.collaborator_work_active() is False


def test_collaborator_work_counts_approved_user_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew.shared_crew import access_set, admission_gate

    monkeypatch.setattr(
        tailnet_live_allowlist,
        "snapshot",
        lambda: _snapshot(approved_user_ids=frozenset({"11", "42"})),
    )
    monkeypatch.setattr(
        access_set, "principal_ids_for", lambda _login, uids: frozenset(f"uid:{u}" for u in uids)
    )
    monkeypatch.setattr(
        admission_gate, "installed_service", lambda: _service(("uid:42", OperationState.QUEUED))
    )
    assert hp.collaborator_work_active() is True


def test_an_unadmitted_collaborator_turn_keeps_the_host_awake(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew.shared_crew import access_set, admission_gate, turn_gate

    monkeypatch.setattr(
        tailnet_live_allowlist,
        "snapshot",
        lambda: _snapshot(member_logins=frozenset({"bob@example.com"})),
    )
    monkeypatch.setattr(
        access_set, "principal_ids_for", lambda login, _uids: frozenset({f"id:{login}"})
    )
    monkeypatch.setattr(admission_gate, "installed_service", lambda: None)
    monkeypatch.setitem(turn_gate._UNADMITTED_RUNNING, "chat-1", ("id:bob@example.com", None))
    assert hp.collaborator_work_active() is True


def test_a_network_admitted_collaborator_turn_keeps_the_host_awake(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew.shared_crew import access_set, admission_gate, turn_gate

    tailnet_live_allowlist.reset_for_tests()
    tailnet_live_allowlist.note_login("42", "eve@example.com")
    tailnet_live_allowlist.note_login("11", "owner@example.com")
    monkeypatch.setattr(tailnet_live_allowlist, "snapshot", lambda: _snapshot(network_trust=True))
    monkeypatch.setattr(
        access_set, "principal_ids_for", lambda login, _uids: frozenset({f"id:{login}"})
    )
    monkeypatch.setattr(admission_gate, "installed_service", lambda: None)
    monkeypatch.setitem(turn_gate._UNADMITTED_RUNNING, "chat-1", ("id:eve@example.com", None))
    try:
        assert hp.collaborator_work_active() is True
        monkeypatch.setitem(turn_gate._UNADMITTED_RUNNING, "chat-1", ("id:owner@example.com", None))
        assert hp.collaborator_work_active() is False
    finally:
        tailnet_live_allowlist.reset_for_tests()


def test_no_service_or_no_collaborators_means_no_work(monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew.shared_crew import admission_gate

    monkeypatch.setattr(admission_gate, "installed_service", lambda: None)
    monkeypatch.setattr(tailnet_live_allowlist, "snapshot", lambda: _snapshot())
    assert hp.collaborator_work_active() is False
    monkeypatch.setattr(admission_gate, "installed_service", lambda: _service())
    assert hp.collaborator_work_active() is False


def test_a_failing_check_lets_the_host_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(_state: Any) -> bool:
        raise RuntimeError("state torn down")

    monkeypatch.setattr(hp, "collaborator_connected", broken)
    assert hp.shared_use_wants_awake(SimpleNamespace()) is False
