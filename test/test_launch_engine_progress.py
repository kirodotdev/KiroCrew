"""``RealLaunchEngine.provision(on_progress=...)``: the provision sub-step poller.

The real ``provision`` runs with ``ec2.deploy`` and ``ec2.list_stack_events``
replaced: deploy blocks on an event the test controls and the stack events are
scripted, so every ordering below is set by the test, never by a sleep.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from kiro_crew.cloud import launch_engine as le
from kiro_crew.testing.wait import wait_until

CREATING = [{"resource": "Instance", "status": "CREATE_IN_PROGRESS"}]
CREATED = CREATING + [{"resource": "Instance", "status": "CREATE_COMPLETE"}]


@pytest.fixture
def tag(request) -> str:
    # One stack tag per test, so a poller thread is found by its own test only.
    return f"kc-{request.node.name}"


@pytest.fixture
def engine(monkeypatch):
    # A poll interval this short only keeps the test quick; every assertion
    # waits on a signal the fakes raise, never on how many polls ran.
    monkeypatch.setattr(le, "_PROGRESS_POLL_S", 0.001)  # flake-ok: poll interval, not a barrier
    monkeypatch.setattr(le.sizes, "get_tier", lambda k: SimpleNamespace(key=k))
    return le.RealLaunchEngine()


class _Reports:
    """Collects ``on_progress`` reports and signals the install sub-step."""

    def __init__(self) -> None:
        self.texts: list[str] = []
        self.installing = threading.Event()

    def __call__(self, text: str) -> None:
        self.texts.append(text)
        if text == le.SUBSTEP_INSTALLING:
            self.installing.set()


def _blocking_deploy(monkeypatch, release: threading.Event, *, instance_id="i-0abc") -> None:
    def deploy(**_kw: Any) -> SimpleNamespace:
        assert release.wait(60), "the test never released deploy"
        return SimpleNamespace(instance_id=instance_id)

    monkeypatch.setattr(le.ec2, "deploy", deploy)


def _events(monkeypatch, tag: str, script: Callable[[int], list]) -> None:
    calls = {"n": 0}
    expected = tag

    def list_stack_events(tag: str, profile: str = "", region: str = "") -> list:
        assert tag == expected
        calls["n"] += 1
        return script(calls["n"])

    monkeypatch.setattr(le.ec2, "list_stack_events", list_stack_events)


def _provision_in_thread(engine, tag: str, reports) -> tuple[threading.Thread, dict]:
    out: dict = {}

    def run() -> None:
        try:
            out["iid"] = engine.provision(
                tag=tag, size_key="balanced", profile="", region="us-east-1", on_progress=reports
            )
        except BaseException as exc:  # noqa: BLE001 - handed to the test thread
            out["exc"] = exc

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t, out


def _poller(tag: str) -> threading.Thread | None:
    return next((t for t in threading.enumerate() if t.name == f"launch-progress-{tag}"), None)


def test_the_install_sub_step_is_reported_while_deploy_still_blocks(engine, tag, monkeypatch):
    release = threading.Event()
    _blocking_deploy(monkeypatch, release, instance_id="i-0deploy")
    _events(monkeypatch, tag, lambda n: CREATING if n < 3 else CREATED)
    reports = _Reports()

    t, out = _provision_in_thread(engine, tag, reports)
    wait_until(reports.installing.is_set, describe=lambda: reports.texts)
    assert t.is_alive(), "provision returned before deploy was released"
    assert reports.texts == [le.SUBSTEP_CREATING, le.SUBSTEP_INSTALLING]

    release.set()
    wait_until(lambda: not t.is_alive(), describe=lambda: out)
    assert out == {"iid": "i-0deploy"}


def test_a_poll_in_flight_never_reports_after_provision_returns(engine, tag, monkeypatch):
    release = threading.Event()
    _blocking_deploy(monkeypatch, release)
    entered, unblock = threading.Event(), threading.Event()

    def script(_n: int) -> list:
        entered.set()
        assert unblock.wait(60), "the test never unblocked the poll"
        return CREATED

    _events(monkeypatch, tag, script)
    reports = _Reports()

    t, out = _provision_in_thread(engine, tag, reports)
    wait_until(entered.is_set)
    release.set()
    # provision returns while the stack-event read is still blocked: it does
    # not wait for it.
    wait_until(lambda: not t.is_alive(), describe=lambda: out)
    assert out == {"iid": "i-0abc"}
    poller = _poller(tag)
    assert poller is not None and poller.is_alive()

    unblock.set()
    wait_until(lambda: not poller.is_alive())
    assert reports.texts == [le.SUBSTEP_CREATING]


def test_a_poll_that_raises_does_not_fail_the_launch(engine, tag, monkeypatch):
    release = threading.Event()
    _blocking_deploy(monkeypatch, release, instance_id="i-0still")

    def script(n: int) -> list:
        if n < 3:
            raise RuntimeError("aws cli exploded")
        return CREATED

    _events(monkeypatch, tag, script)
    reports = _Reports()

    t, out = _provision_in_thread(engine, tag, reports)
    # The poller survived the failed reads and went on to report.
    wait_until(reports.installing.is_set, describe=lambda: reports.texts)
    release.set()
    wait_until(lambda: not t.is_alive(), describe=lambda: out)
    assert out == {"iid": "i-0still"}


def test_deploy_raising_still_raises_and_stops_the_reports(engine, tag, monkeypatch):
    entered, unblock = threading.Event(), threading.Event()

    def deploy(**_kw: Any) -> SimpleNamespace:
        wait_until(entered.is_set)
        raise RuntimeError("stack rolled back")

    def script(_n: int) -> list:
        entered.set()
        assert unblock.wait(60), "the test never unblocked the poll"
        return CREATED

    monkeypatch.setattr(le.ec2, "deploy", deploy)
    _events(monkeypatch, tag, script)
    reports = _Reports()

    with pytest.raises(RuntimeError, match="stack rolled back"):
        engine.provision(
            tag=tag, size_key="balanced", profile="", region="us-east-1", on_progress=reports
        )
    poller = _poller(tag)
    assert poller is not None
    unblock.set()
    wait_until(lambda: not poller.is_alive())
    assert reports.texts == [le.SUBSTEP_CREATING]


def test_without_on_progress_nothing_polls(engine, tag, monkeypatch):
    monkeypatch.setattr(le.ec2, "deploy", lambda **_kw: SimpleNamespace(instance_id="i-0abc"))

    def never(*_a: Any, **_kw: Any) -> list:
        raise AssertionError("polled the stack events without on_progress")

    monkeypatch.setattr(le.ec2, "list_stack_events", never)
    assert engine.provision(tag=tag, size_key="balanced", profile="", region="us-east-1") == (
        "i-0abc"
    )
    assert _poller(tag) is None
