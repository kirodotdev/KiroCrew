"""The fake dashboard state, crew builders and autouse isolation the crew runtime tests share.

``test_issue_radar_crew_runtime.py`` and its ``_sweep`` and ``_trust`` siblings import
these by name: the three autouse fixtures (a private SEL root per test, the app switched
on, and an isolated crew log), the fake slot, state, loop and nudge service, the app, crew
and work-item builders, and the readers the source pins share (the runtime's owner modules
and a crew's effective trust).
"""

import importlib
from typing import Any, cast
from unittest import mock

import pytest
from off_loop_helpers import off_loop

from kiro_crew.apps.builtins.issue_radar.backend import crew_runtime as cr
from kiro_crew.apps.builtins.issue_radar.backend import crew_store as cs
from kiro_crew.crew_log import projection as crew_log_projection
from kiro_crew.dashboard import chat_runner

OWNER, REPO = "o", "r"


def _runtime_modules() -> list[Any]:  # the mirror facade and every owner it serves names from
    return [cr, *map(importlib.import_module, cr._OWNERS)]


def _effectively_trusted(slot: Any) -> bool:
    """Would the SHARED approval path auto-approve this slot's tools right now?

    The one assertion worth making about a crew's trust. Every alternative is a
    proxy that can pass while the crew is in fact untrusted (or vice versa): the
    ``unattended`` flag is only intent, and ``slot._trust`` is a different grant
    this module must never write. So the tests below ask the real consumer.
    """
    return chat_runner._slot_is_trusted(slot)


@pytest.fixture(autouse=True)
def _private_sel_root_per_test(sel_private_root):
    """Every test in this module gets its OWN SEL root.

    The trust assertions here transitively depend on a fail-closed critical SEL
    audit WINNING the chain lock: ``sync_trust`` → ``activate_scoped`` audits
    before it grants, and on the event-loop thread the lock acquire is a single
    non-blocking attempt that refuses rather than stall the loop — correct
    product behaviour that must not be loosened. On a SHARED root that turns
    every trust assertion into a lock race against writers this module never
    created (another test's still-flushing events, another xdist worker on the
    same path). ``sel_private_root`` (rootdir conftest) removes the concurrent
    writer instead of coping with it: a fresh per-test, per-worker directory
    that nothing else writes. ``TestSelRootIsolation`` below pins the property
    differentially.
    """
    yield


@pytest.fixture(autouse=True)
def _app_is_on():
    """The grant's predicate includes the APP gate, and the test host has no
    installed app -- so pin it on, the way ``test_watch`` does, and let the tests
    that exercise a disable flip it themselves."""
    # ``create=True`` so the pin is inert against a build that predates the gate:
    # then the falsification run (production reverted, tests kept) reports the
    # regression tests as FAILED rather than erroring in fixture setup.
    with mock.patch.object(cr, "is_app_enabled", return_value=True, create=True):
        if hasattr(cr, "_disabling"):
            cr._disabling = False
        yield
        if hasattr(cr, "_disabling"):
            cr._disabling = False


# ── fakes ───────────────────────────────────────────────────────────────────


class _FakeSlot:
    """Stand-in for _ChatSlot: records the prompt a turn would have run with."""

    def __init__(
        self, key: str = "crew-c_1", agent: str = "", model: str = "", workspace: str = ""
    ):
        self.key = key
        self.title = ""
        self._titled = False
        self._trust = False
        self._trust_scope = ""
        self.agent = agent
        self.model = model
        self.workspace = workspace
        self.messages: list[dict[str, Any]] = []
        self.running = False
        self.prompts: list[str] = []
        self.runners: list[Any] = []

    def append(self, role: str, content: str, cls: str = "", **kw: Any) -> None:
        self.messages.append({"role": role, "content": content, "cls": cls})

    def enqueue_or_run_prompt(self, prompt: str, run_chat_coro: Any, state: Any) -> bool:
        self.prompts.append(prompt)
        self.runners.append(run_chat_coro)
        self.append("user", prompt)
        return not self.running


class _FakeState:
    """Minimal DashboardState: slot registry plus the calls the runtime makes."""

    def __init__(self) -> None:
        self.slots: dict[str, _FakeSlot] = {}
        self.created: list[dict[str, Any]] = []
        self.pushes = 0
        #: Slot keys whose turns were charged against the background-turn cap.
        self.capped: list[str] = []
        #: When set, the cap never hands out a permit — the turn never runs.
        self.permit_timeout = False

    def get_slot(self, key: str) -> _FakeSlot | None:
        return self.slots.get(key)

    async def run_background_turn(self, slot: Any, coro: Any) -> Any:
        """The app-owned concurrency cap, recording what was charged against it.

        Present on the fake precisely because the runtime MUST route every crew
        turn through it: a fake without this method would let a dispatch that calls
        ``_run_chat`` itself pass unnoticed, which is the defect these tests pin.
        The real one queues at the cap and abandons the turn after its own wait
        budget, so ``permit_timeout`` closes the coroutine rather than running it.
        """
        self.capped.append(str(getattr(slot, "key", "")))
        if self.permit_timeout:
            coro.close()
            raise TimeoutError("queued behind the background-turn cap")
        return await coro

    def get_or_create_slot(
        self,
        name: str = "",
        agent: str = "",
        workspace: str = "default",
        model: str = "",
        app: str = "",
        **kw: Any,
    ) -> _FakeSlot:
        self.created.append(
            {"name": name, "agent": agent, "workspace": workspace, "model": model, "app": app}
        )
        slot = self.slots.get(name)
        if slot is None:
            slot = _FakeSlot(name, agent=agent, model=model, workspace=workspace)
            self.slots[name] = slot
        return slot

    def push_slots_update(self) -> None:
        self.pushes += 1

    def push_slot_title(self, key: str, title: str) -> None:
        self.pushes += 1


class _FakeLoop:
    """One armed autonudge loop — the part of ``NudgeLoop`` the runtime reads."""

    def __init__(self, loop_id: str, slot_key: str, active: bool = True):
        self.id = loop_id
        self.slot_key = slot_key
        self.active = active


class _FakeNudge:
    """Minimal AutoNudgeService: the loop registry plus the three calls made here.

    Needed because the real ``get_instance()`` returns ``None`` outside a running
    gateway, so an unstubbed test only ever exercises the no-service branch — and
    the restart bug lives in the branch where a loop DOES exist.
    """

    def __init__(self, loops: list[_FakeLoop] | None = None) -> None:
        self.loops: list[_FakeLoop] = list(loops or [])
        self.added: list[str] = []
        self.updates: list[tuple[str, dict[str, Any]]] = []

    def get_by_slot(self, slot_key: str) -> _FakeLoop | None:
        return next((lp for lp in self.loops if lp.slot_key == slot_key), None)

    def list_all(self) -> list[_FakeLoop]:
        return list(self.loops)

    async def add(self, slot_key: str = "", message: str = "", **kw: Any) -> _FakeLoop:
        self.added.append(slot_key)
        loop = _FakeLoop(f"nl_{len(self.loops)}", slot_key)
        self.loops.append(loop)
        return loop

    async def update(self, loop_id: str, **kw: Any) -> None:
        self.updates.append((loop_id, dict(kw)))
        for lp in self.loops:
            if lp.id == loop_id and "active" in kw:
                lp.active = bool(kw["active"])


def _app(state: _FakeState | None) -> Any:
    return cast(Any, {"state": state})


def _crew(root, name="Andromeda", **spec) -> dict[str, Any]:
    # Off the loop for the reason ``_item`` gives: most callers are async tests.
    return off_loop(cs.create_crew, OWNER, REPO, {"name": name, **spec}, root)


#: crew id -> the live crew log unit its slot runs on. A crew's ledger is the fold of
#: that unit, so seeding a work item means recording into it as the write route does.
_UNITS: dict[str, str] = {}


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- the autouse crew-log isolation of test_issue_radar_crew_runtime.py and the modules split out of it, moved verbatim; none of them calls monkeypatch.undo()
def _isolated_crew_log(monkeypatch):
    """Crew log on, in this test's own data home, and no warm state carried over.

    FUNCTION-scoped: the data home is the rootdir conftest's per-test
    ``KIROCREW_HOME`` pin, so the crew log a test writes lands in a directory that
    test alone owns, and ``KIROCREW_CREW_LOG`` is set through ``monkeypatch`` so it
    is gone the moment the test is. A module-scoped ``patch.dict`` here set both
    keys for the whole worker between tests: every other suite the worker ran
    after this file's first test saw a crew log switched on that none of them
    asked for, and a data home none of them pinned. The seeding helpers below are
    called from unittest classes that manage their own store roots; a crew's id is
    minted fresh per test, so ``_UNITS`` is cleared with the rest.
    """
    from kiro_crew.crew_log import emit as crew_log_emit

    monkeypatch.setenv("KIROCREW_CREW_LOG", "1")
    crew_log_emit.reset_caches()
    crew_log_projection.forget_slot_folds()
    _UNITS.clear()
    yield
    crew_log_emit.drain_for_shutdown(timeout=2.0)
    crew_log_emit.reset_caches()
    crew_log_projection.forget_slot_folds()
    _UNITS.clear()


def _unit_for(crew_id: str) -> str:
    """The crew's live unit, created on first use."""
    if crew_id not in _UNITS:
        from kiro_crew.crew_log.schema import KIND_SESSION
        from kiro_crew.crew_log.store import CrewLog

        sid = f"acp-{crew_id}"
        CrewLog.create(
            KIND_SESSION, sid, owner="owner", agent="kirocrew", slot=cs.slot_key_for(crew_id)
        )
        _UNITS[crew_id] = sid
    return _UNITS[crew_id]


def _item(root, crew_id, number, **patch) -> dict[str, Any]:
    """Seed one work item through the real write path: one entry in the crew's log.

    The write runs on a worker thread (``off_loop``), as every product caller runs
    it (``asyncio.to_thread``). Most callers here are async tests, so a direct call
    would run on the event-loop thread, where ``file_lock`` takes ONE attempt and
    never waits: if the crew log's writer thread is still inside its brief critical
    section, the read-back after the append is refused and the write raises
    ``CrewLedgerNotRecorded``. On a worker thread the same contention is an ordinary
    short wait.
    """
    return off_loop(
        cs.commit_work_progress,
        OWNER,
        REPO,
        crew_id,
        number,
        patch,
        "claim",
        "seeded",
        root=root,
        session_id=_unit_for(crew_id),
    )["item"]
