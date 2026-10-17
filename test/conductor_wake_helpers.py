"""The ids, the service stubs and the push driver the conductor wake test modules share.

``CONDUCTOR`` / ``WORKER`` / ``LOOP_ID`` (with ``WORKER_B`` for the cases that bind a
second worker), the autouse ``_isolated_home`` (an own data home and an empty
wake registry per test), the ``_Svc`` stub with its ``_Loop`` and ``_Monitor``, ``_bind`` and
``_item`` over a real conductor ledger, ``_fire`` (what a worker's report pushes, with a
service standing in for the live one) and ``_service`` (the real ``AutoNudgeService``).
``test_conductor_wake.py``, ``test_conductor_wake_pushed_ticks.py`` and
``test_conductor_wake_batching.py`` import them by name.
"""

from __future__ import annotations

import pytest

from kiro_crew import conductor_wake, work_ledger

CONDUCTOR = "chat-conductor"
WORKER = "chat-worker"
LOOP_ID = "loop-1"


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- the conductor wake modules' autouse isolation (own data home, empty wake registry), moved verbatim; none of them calls monkeypatch.undo()
def _isolated_home(tmp_path, monkeypatch):
    """Own data home per test, so no binding file outlives its own test; and an empty
    registry, so no board a previous test observed answers this one's lookup."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    conductor_wake.reset_for_tests()
    yield
    conductor_wake.reset_for_tests()


# ── stubs ────────────────────────────────────────────────────────────────────


class _Monitor:
    def __init__(self, kind: str = "work-ledger") -> None:
        self.kind = kind
        self.target = CONDUCTOR
        # The REAL current version, read rather than written: both zero-delay arms refuse a
        # record this gateway cannot interpret, so a stub pinning a literal here would stop
        # exercising that refusal the moment the version moved.
        from kiro_crew.monitoring.models import MONITOR_STATE_VERSION

        self.version = MONITOR_STATE_VERSION


class _Loop:
    def __init__(self, *, kind: str = "work-ledger", active: bool = True) -> None:
        self.id = LOOP_ID
        self.slot_key = CONDUCTOR
        self.active = active
        self.monitor = _Monitor(kind) if kind else None


class _Svc:
    """The three things the lookup reads off a service, and nothing else.

    ``_reconciler`` is how the thread entry point finds the event loop to schedule onto;
    the real service holds the same attribute for the same task.
    """

    def __init__(self, loop: "_Loop | None" = None, *, refuse: str = "") -> None:
        self._loop = loop
        self._refuse = refuse
        self.fired: list[str] = []
        self.deferred: list[str] = []
        self.delays: list[float] = []
        self._reconciler = None
        self._timers: dict[str, object] = {}

    def get_by_slot(self, slot_key: str) -> "_Loop | None":
        return self._loop if self._loop is not None and slot_key == CONDUCTOR else None

    async def fire_now(self, loop_id, *, defer_if_firing=False, delay=0.0):
        self.delays.append(delay)
        if self._refuse:
            if defer_if_firing:
                self.deferred.append(loop_id)
            return None, self._refuse, 409
        self.fired.append(loop_id)
        return self._loop, "", 200


# The probe tests the docstring names are the worker_closed cases in
# test_conductor_wake.py.
def _bind(worker: str = WORKER, *, status: str | None = None, verdict: str = "") -> str:
    """A real conductor ledger with one item bound to *worker*. Returns the item id.

    A real store rather than a fake one: the loop-side lookup reads the binding this
    writes, and the probe tests below read the item back through it.
    """
    work_ledger.ensure_conductor(CONDUCTOR, goal="drive the fleet")
    created = work_ledger.apply_conductor_action(
        CONDUCTOR, "create", title="t", acceptance={"kind": "human_approval"}
    )
    item_id = str(created["item"].item_id)
    work_ledger.apply_conductor_action(
        CONDUCTOR, "bind", item_id=item_id, worker_session_key=worker
    )
    if status is not None:
        work_ledger.apply_worker_report(CONDUCTOR, item_id, status=status, summary="s")
    if verdict:
        work_ledger.apply_conductor_action(
            CONDUCTOR, "verdict", item_id=item_id, verdict=verdict, fails=1
        )
    return item_id


def _item(item_id: str):
    found = work_ledger.read_work_item(CONDUCTOR, item_id)
    assert found is not None
    return found


async def _fire(svc, worker_slot: str) -> str:
    """What *worker_slot*'s report pushes, with *svc* standing in for the live service.

    The trigger resolves a conductor and an item from the rendered board the bus hands
    it. A case reaching the same pair from the binding the store already holds drives
    ``_fire`` over a real ledger without rebuilding a fold render, which is what these
    cases are about: the lookup from a conductor slot to its armed loop, and what each
    of ``fire_now``'s answers costs.
    """
    import kiro_crew.autonudge as autonudge

    original = autonudge.get_instance
    # flake-ok: pre-existing rebind of autonudge.get_instance that the finally below restores, moved verbatim; tracked for a de-flake pass
    autonudge.get_instance = lambda: svc  # type: ignore[assignment]
    try:
        binding = work_ledger.read_binding(worker_slot)
        if binding is None:
            return ""
        return await conductor_wake._fire(svc, binding[0], binding[1])
    finally:
        autonudge.get_instance = original  # type: ignore[assignment]


def _service(base_dir, **kwargs):
    from kiro_crew.autonudge import AutoNudgeService

    return AutoNudgeService(base_dir=base_dir, **kwargs)


WORKER_B = "chat-worker-b"
