"""Waking a Bot for its out-of-date Dashboard fields (preview ``dashboard_stale_wake``).

The out-of-date line (``dashboard_stale_fields``) reached a Bot only inside a turn, so a
Bot with neither Daily Goal Reminder nor Heartbeat on kept a stale field until someone
talked to it. The gateway's one stale-wake cron job now starts one hidden turn in that
Bot's own thread (:mod:`kiro_crew.dashboard.stale_wake`). Each test below fails when its
piece of the change is removed:

* crossing the limit wakes exactly once -- drop the book check in ``due_fields`` and the
  second tick wakes again;
* a new write re-arms the field -- compare names instead of stamps and it never re-arms;
* fields stale together share one wake -- wake per field and the count is two;
* the wake line names every stale field -- drop ``shown=len(stale)`` in
  ``member_stale_state`` and fields past the third are only counted;
* the 30-minute floor -- drop the ``FLOOR_S`` check and the third tick wakes;
* a mid-turn, paused or loop-driven Bot is not woken and is tried on the next tick;
* a Bot whose thread has a turn queued starts no turn and books nothing -- book it and
  the tick after the queue empties never wakes it;
* preview off: no wake, and the job is removed and never installed;
* a tick that woke no one writes no run-history row -- drop the ``quiet_run`` check in
  the run recorder and every ten-minute tick adds one to the shared, capped index.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew import crew_pause, dashboard_agentic
from kiro_crew import members as members_mod
from kiro_crew.dashboard import goal_reminder, mate_welcome, stale_wake
from kiro_crew.dashboard_templates.manifest import parse_manifest

SLUG = "scout"
MEMBER = "Scout"
SLOT_KEY = "member-scout"


class _State:
    def __init__(self, slot: Any) -> None:
        self.slot = slot

    def get_slot(self, key: str) -> Any:
        return self.slot if key == SLOT_KEY else None


def _slot(**extra: Any) -> Any:
    base = {
        "key": SLOT_KEY,
        "mode": members_mod.DM_SLOT_MODE,
        "agent": MEMBER,
        "executor": "",
        "running": False,
        "_queue": [],
    }
    base.update(extra)
    return SimpleNamespace(**base)


@pytest.fixture
def world(tmp_path, monkeypatch):
    """One Bot whose stale fields the test sets, with every wake recorded."""
    monkeypatch.setattr(stale_wake, "data_home", lambda: tmp_path)
    monkeypatch.setattr(stale_wake, "switch_state", lambda: True)
    monkeypatch.setattr(stale_wake, "bots", lambda: [(SLUG, MEMBER, SLOT_KEY)])
    w = SimpleNamespace(stale=[], now=1_000_000.0, turns=[], records=[], slot=_slot())
    monkeypatch.setattr(
        dashboard_agentic,
        "member_stale_state",
        lambda slug, member: (list(w.stale), "Out of date on your page: x." if w.stale else ""),
    )
    monkeypatch.setattr(stale_wake, "time", SimpleNamespace(time=lambda: w.now))
    monkeypatch.setattr(crew_pause, "holds_turn", lambda key, *a, **k: False)
    monkeypatch.setattr(goal_reminder, "_monitor_loop_drives", lambda key: False)
    monkeypatch.setattr(goal_reminder, "gateway_stopping", lambda: False)

    async def _no_load(state, key):
        return ""

    monkeypatch.setattr(goal_reminder, "_load_thread", _no_load)
    monkeypatch.setattr(mate_welcome, "hidden_turn_language", lambda: "")

    def _dispatch(state, slot, prompt, *, human_seen):
        w.turns.append(prompt)
        return True

    monkeypatch.setattr(mate_welcome, "dispatch_hidden_turn", _dispatch)
    monkeypatch.setattr(
        stale_wake, "_record", lambda slug, member, fields: w.records.append(fields)
    )
    return w


def _tick(w: Any) -> str:
    return asyncio.run(stale_wake.fire(_State(w.slot)))


def test_a_field_that_crosses_its_limit_wakes_the_bot_exactly_once(world) -> None:
    world.stale = [("headline", "2026-11-08T00:00:00+00:00")]
    assert _tick(world) == "asked 1 Bot(s) to update"
    assert len(world.turns) == 1
    assert "Out of date on your page" in world.turns[0]
    assert "dashboard_write" in world.turns[0]
    assert world.records == [["headline"]]
    # Still stale, hours later: the same stale period wakes nobody again.
    world.now += 6 * 3600
    assert _tick(world) == "nothing to wake"
    assert len(world.turns) == 1


def test_a_field_written_again_is_re_armed(world) -> None:
    world.stale = [("headline", "2026-11-08T00:00:00+00:00")]
    _tick(world)
    # The Bot wrote it (a new stamp) and a day later it went stale again.
    world.now += 25 * 3600
    world.stale = [("headline", "2026-11-09T00:00:00+00:00")]
    _tick(world)
    assert len(world.turns) == 2
    assert world.records == [["headline"], ["headline"]]


def test_two_fields_stale_together_share_one_wake(world) -> None:
    world.stale = [
        ("open_risks", "2026-11-08T00:00:00+00:00"),
        ("headline", "2026-11-08T00:00:00+00:00"),
    ]
    _tick(world)
    assert len(world.turns) == 1
    assert world.records == [["headline", "open_risks"]]


def test_no_bot_is_woken_twice_within_the_floor(world) -> None:
    world.stale = [("headline", "2026-11-08T00:00:00+00:00")]
    _tick(world)
    # A second field goes stale ten minutes later: inside the floor, no turn.
    world.now += 10 * 60
    world.stale.append(("open_risks", "2026-11-08T00:10:00+00:00"))
    _tick(world)
    assert len(world.turns) == 1
    # Past the floor the waiting field gets its wake, and the book keeps both.
    world.now += stale_wake.FLOOR_S
    _tick(world)
    assert len(world.turns) == 2
    assert world.records[-1] == ["headline", "open_risks"]


@pytest.mark.parametrize(
    "case",
    ["mid_turn", "paused", "monitor_loop"],
)
def test_a_busy_or_held_bot_is_not_woken(world, monkeypatch, case: str) -> None:
    world.stale = [("headline", "2026-11-08T00:00:00+00:00")]
    if case == "mid_turn":
        world.slot.running = True
    elif case == "paused":
        monkeypatch.setattr(crew_pause, "holds_turn", lambda key, *a, **k: key == SLOT_KEY)
    else:
        monkeypatch.setattr(goal_reminder, "_monitor_loop_drives", lambda key: key == SLOT_KEY)
    assert _tick(world) == "nothing to wake"
    assert world.turns == [] and world.records == []
    # Once it is free, the next tick wakes it: the skip did not use up the field.
    world.slot.running = False
    monkeypatch.setattr(crew_pause, "holds_turn", lambda key, *a, **k: False)
    monkeypatch.setattr(goal_reminder, "_monitor_loop_drives", lambda key: False)
    _tick(world)
    assert len(world.turns) == 1


def test_a_queued_thread_starts_no_turn_and_is_woken_once_its_queue_empties(
    world, tmp_path
) -> None:
    world.stale = [("headline", "2026-11-08T00:00:00+00:00")]
    world.slot._queue = [{"text": "hello"}]
    assert _tick(world) == "nothing to wake"
    assert world.turns == [] and world.records == []
    # Nothing is booked, so a cancelled queued turn cannot use up the field.
    assert not (tmp_path / stale_wake.BOOK_NAME).exists()
    world.slot._queue = []
    _tick(world)
    assert len(world.turns) == 1
    assert world.records == [["headline"]]


def test_with_the_preview_off_nothing_wakes_and_the_job_removes_itself(world, monkeypatch) -> None:
    monkeypatch.setattr(stale_wake, "switch_state", lambda: False)
    world.stale = [("headline", "2026-11-08T00:00:00+00:00")]
    crons = _Crons([SimpleNamespace(id="j1", managed_by=stale_wake.MANAGED_KEY, enabled=True)])
    assert asyncio.run(stale_wake.fire(_State(world.slot), crons)) == "skipped: the preview is off"
    assert world.turns == [] and world.records == []
    assert crons.removed == ["j1"] and crons.added == []


def test_a_config_that_cannot_be_read_wakes_no_one_and_keeps_the_job(world, monkeypatch) -> None:
    monkeypatch.setattr(stale_wake, "switch_state", lambda: None)
    world.stale = [("headline", "2026-11-08T00:00:00+00:00")]
    crons = _Crons([SimpleNamespace(id="j1", managed_by=stale_wake.MANAGED_KEY, enabled=True)])
    out = asyncio.run(stale_wake.fire(_State(world.slot), crons))
    assert out == "skipped: the config could not be read"
    assert world.turns == [] and crons.removed == []
    # The config watcher path keeps it too.
    asyncio.run(stale_wake.sync_job(crons))
    assert crons.removed == [] and crons.added == []


def test_the_row_is_saved_before_the_turn_starts(world, monkeypatch, tmp_path) -> None:
    world.stale = [("headline", "2026-11-08T00:00:00+00:00")]
    seen: list[Any] = []

    def _dispatch(state, slot, prompt, *, human_seen):
        seen.append(json.loads((tmp_path / stale_wake.BOOK_NAME).read_text()).get(SLUG))
        world.turns.append(prompt)
        return True

    monkeypatch.setattr(mate_welcome, "dispatch_hidden_turn", _dispatch)
    _tick(world)
    assert seen and seen[0]["fields"] == {"headline": "2026-11-08T00:00:00+00:00"}


def test_a_book_that_cannot_be_saved_starts_no_turn(world, monkeypatch) -> None:
    world.stale = [("headline", "2026-11-08T00:00:00+00:00")]

    def _refuse(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(stale_wake, "atomic_write", _refuse)
    assert _tick(world).startswith("stopped: the wake book could not be saved")
    assert world.turns == [] and world.records == []
    # Every later tick refuses the same way: no unrecorded wake ever starts.
    world.now += stale_wake.FLOOR_S + 1
    _tick(world)
    assert world.turns == []


def test_a_turn_that_does_not_start_leaves_the_book_as_it_was(world, monkeypatch, tmp_path) -> None:
    world.stale = [("headline", "2026-11-08T00:00:00+00:00")]
    monkeypatch.setattr(mate_welcome, "dispatch_hidden_turn", lambda *a, **k: False)
    _tick(world)
    assert json.loads((tmp_path / stale_wake.BOOK_NAME).read_text()) == {}
    assert world.records == []


def test_a_book_over_its_cap_is_not_saved_and_starts_no_turn(world, monkeypatch) -> None:
    world.stale = [("headline", "2026-11-08T00:00:00+00:00")]
    monkeypatch.setattr(stale_wake, "_BOOK_MAX_BYTES", 10)
    assert _tick(world).startswith("stopped: the wake book could not be saved")
    assert world.turns == []


def test_rows_of_bots_no_longer_in_the_crew_are_dropped(world, tmp_path) -> None:
    (tmp_path / stale_wake.BOOK_NAME).write_text(json.dumps({"gone": {"at": 1.0, "fields": {}}}))
    world.stale = [("headline", "2026-11-08T00:00:00+00:00")]
    _tick(world)
    assert set(json.loads((tmp_path / stale_wake.BOOK_NAME).read_text())) == {SLUG}


def test_the_book_survives_a_restart(world, tmp_path) -> None:
    world.stale = [("headline", "2026-11-08T00:00:00+00:00")]
    _tick(world)
    book = json.loads((tmp_path / stale_wake.BOOK_NAME).read_text())
    assert book[SLUG]["fields"] == {"headline": "2026-11-08T00:00:00+00:00"}
    # A new process reads the same book, so the same stale period stays quiet.
    world.now += stale_wake.FLOOR_S + 1
    _tick(world)
    assert len(world.turns) == 1


class _Crons:
    def __init__(self, jobs: list[Any]) -> None:
        self.jobs = list(jobs)
        self.removed: list[str] = []
        self.added: list[str] = []

    async def list_jobs_async(self, include_disabled: bool = False) -> list[Any]:
        return list(self.jobs)

    def raise_if_store_unreadable(self) -> None:
        return None

    async def remove_job_async(self, job_id: str, **_kw: Any) -> None:
        self.removed.append(job_id)
        self.jobs = [j for j in self.jobs if j.id != job_id]

    async def add_managed_job_async(self, key: str, **kw: Any) -> Any:
        self.added.append(kw["cron_expr"])
        job = SimpleNamespace(id="new", managed_by=key, enabled=True)
        self.jobs.append(job)
        return job


def test_the_job_exists_exactly_while_the_preview_is_on(monkeypatch) -> None:
    crons = _Crons([])
    monkeypatch.setattr(stale_wake, "switch_state", lambda: True)
    asyncio.run(stale_wake.sync_job(crons))
    asyncio.run(stale_wake.sync_job(crons))
    assert crons.added == [stale_wake.TICK_EXPR]
    assert [j.managed_by for j in crons.jobs] == [stale_wake.MANAGED_KEY]
    monkeypatch.setattr(stale_wake, "switch_state", lambda: False)
    asyncio.run(stale_wake.sync_job(crons))
    assert crons.jobs == []


def test_a_start_with_the_preview_off_removes_a_job_left_from_before(monkeypatch) -> None:
    # Its own tick may never reach the cleanup: the fire-time gate can refuse the run.
    crons = _Crons([SimpleNamespace(id="j1", managed_by=stale_wake.MANAGED_KEY, enabled=True)])
    monkeypatch.setattr(stale_wake, "switch_state", lambda: False)

    async def _start() -> None:
        stale_wake.schedule_sync(crons)
        await asyncio.gather(*stale_wake._SYNC_TASKS)

    asyncio.run(_start())
    assert crons.removed == ["j1"] and crons.added == []


def test_only_bots_in_the_crew_with_a_thread_are_listed(monkeypatch) -> None:
    from kiro_crew.config.loader import KiroCrewConfig

    names = ["Ann", "Bad", "NoThread", "Scout", "_hidden"]
    cfg = SimpleNamespace(agents={n: {} for n in names})
    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls, *a, **k: cfg))
    monkeypatch.setattr(members_mod, "is_dispatchable_member_name", lambda n: not n.startswith("_"))

    def _slug(name, _cfg):
        if name == "Bad":
            raise members_mod.MemberSlugError(name)
        return name.lower()

    bindings = {
        "ann": {"member": "Ann", "slot_key": "member-ann"},
        "scout": {"member": "Scout", "slot_key": ""},
    }
    monkeypatch.setattr(members_mod, "member_slug", _slug)
    monkeypatch.setattr(members_mod, "read_dm_binding", lambda slug: bindings.get(slug))
    # A Bot with no thread, a blank slot or a name with no slug is left out.
    assert stale_wake.bots() == [("ann", "Ann", "member-ann")]


def test_an_unreadable_crew_config_lists_no_bots(monkeypatch) -> None:
    from kiro_crew.config.loader import KiroCrewConfig

    def _fail(cls, *a, **k):
        raise OSError("gone")

    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(_fail))
    assert stale_wake.bots() == []


@pytest.mark.parametrize(
    "body", [b"not json", b"[1, 2]", b"x" * 300_000], ids=["not-json", "a-list", "over-cap"]
)
def test_a_damaged_book_reads_as_empty(monkeypatch, tmp_path, body) -> None:
    monkeypatch.setattr(stale_wake, "_book_path", lambda: tmp_path / "book.json")
    (tmp_path / "book.json").write_bytes(body)
    assert stale_wake.read_book() == {}


def _previews(monkeypatch, previews: dict[str, bool]) -> None:
    from kiro_crew.config import KiroCrewConfig

    cfg = SimpleNamespace(
        dashboard=SimpleNamespace(feature_previews=previews), degraded_sections=frozenset()
    )
    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls, *a, **k: cfg))


@pytest.mark.parametrize("section", ["*", "dashboard"])
def test_a_torn_config_reads_as_unknown_not_off(monkeypatch, section) -> None:
    from kiro_crew.config import KiroCrewConfig

    cfg = SimpleNamespace(
        dashboard=SimpleNamespace(feature_previews={}), degraded_sections=frozenset({section})
    )
    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls, *a, **k: cfg))
    assert stale_wake.switch_state() is None


@pytest.mark.parametrize(
    ("previews", "on"),
    [
        ({}, False),
        ({"dashboard_stale_wake": True}, False),
        ({"dashboard_stale_fields": True}, False),
        ({"dashboard_stale_fields": True, "dashboard_stale_wake": True}, True),
    ],
)
def test_the_wake_needs_both_previews(monkeypatch, previews, on) -> None:
    _previews(monkeypatch, previews)
    assert stale_wake.enabled() is on


def test_the_stale_state_carries_each_fields_write_stamp(monkeypatch) -> None:
    manifest = parse_manifest(
        {
            "id": "wake-probe",
            "version": 1,
            "title": "Wake probe",
            "description": "a page for these cases",
            "source": "user",
            "fields": {
                "headline": {"type": "string", "source": {"agentic": True}, "stale_after_s": 3600},
                "lane": {"type": "string", "source": {"agentic": True}},
                "never": {"type": "string", "source": {"agentic": True}},
            },
        }
    )
    monkeypatch.setattr(dashboard_agentic, "stale_fields_enabled", lambda: True)
    monkeypatch.setattr(dashboard_agentic, "_crew_panel_on", lambda: True)
    monkeypatch.setattr(dashboard_agentic, "_member_page_manifest", lambda slug: manifest)
    monkeypatch.setattr(dashboard_agentic, "_member_slot", lambda member, slug: SLOT_KEY)
    import time

    old = "2000-01-01T00:00:00+00:00"
    fresh = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
    monkeypatch.setattr(
        dashboard_agentic, "_agentic_written_at", lambda slot: {"headline": old, "lane": fresh}
    )
    stale, line = dashboard_agentic.member_stale_state(SLUG, MEMBER)
    assert stale == [("headline", old), ("never", "")]
    assert "headline" in line and "never" in line
    # A cold fold (nobody opened the Bot since a restart) wakes nothing now, and is
    # folded in the background so a later tick can read it.
    from kiro_crew.crew_log import slot_prime

    primed: list[tuple[str, str]] = []
    monkeypatch.setattr(dashboard_agentic, "_agentic_written_at", lambda slot: None)
    monkeypatch.setattr(
        slot_prime, "schedule", lambda slot, name, **_k: primed.append((slot, name))
    )
    assert dashboard_agentic.member_stale_state(SLUG, MEMBER) == ([], "")
    assert primed == [(SLOT_KEY, "agentic")]
    # Preview off: nothing, so the job has nothing to wake for.
    monkeypatch.setattr(dashboard_agentic, "stale_fields_enabled", lambda: False)
    assert dashboard_agentic.member_stale_state(SLUG, MEMBER) == ([], "")


def test_the_wake_line_names_every_stale_field(monkeypatch) -> None:
    names = ["alpha", "bravo", "charlie", "delta", "echo"]
    manifest = parse_manifest(
        {
            "id": "wake-five",
            "version": 1,
            "title": "Wake five",
            "description": "a page with five never-written fields",
            "source": "user",
            "fields": {n: {"type": "string", "source": {"agentic": True}} for n in names},
        }
    )
    monkeypatch.setattr(dashboard_agentic, "stale_fields_enabled", lambda: True)
    monkeypatch.setattr(dashboard_agentic, "_crew_panel_on", lambda: True)
    monkeypatch.setattr(dashboard_agentic, "_member_page_manifest", lambda slug: manifest)
    monkeypatch.setattr(dashboard_agentic, "_member_slot", lambda member, slug: SLOT_KEY)
    monkeypatch.setattr(dashboard_agentic, "_agentic_written_at", lambda slot: {})
    stale, line = dashboard_agentic.member_stale_state(SLUG, MEMBER)
    assert sorted(name for name, _stamp in stale) == names
    # The wake books all five, so its line names all five, none counted as "more".
    for name in names:
        assert name in line
    # The turn block keeps its cap and counts the rest.
    capped = dashboard_agentic.stale_line(manifest, {}, 0.0)
    assert sum(name in capped for name in names) == dashboard_agentic.UNRESOLVED_NAMES_SHOWN


def test_only_a_tick_that_woke_someone_is_loud() -> None:
    assert stale_wake.quiet_outcome("nothing to wake")
    assert stale_wake.quiet_outcome("skipped: the preview is off")
    assert not stale_wake.quiet_outcome("asked 1 Bot(s) to update")
    assert not stale_wake.quiet_outcome(
        "stopped: the wake book could not be saved (asked 0 Bot(s))"
    )


@pytest.mark.asyncio
async def test_a_quiet_run_writes_no_history_row(tmp_path) -> None:
    from unittest.mock import AsyncMock

    from kiro_crew.cron import CronService

    svc = CronService(base_dir=tmp_path)
    svc._history.append = AsyncMock()
    quiet = {"on": True}

    async def tick(job):
        job.quiet_run = quiet["on"]
        return "nothing to wake" if quiet["on"] else "asked 1 Bot(s) to update"

    svc._on_job = tick
    job = svc.add_job("stale wake", "go", every_secs=900)
    await svc._run_job_isolated(job, svc._claim_run(job.id, "scheduled"))
    assert svc._history.append.await_count == 0
    # The flag is this run's alone: the next run that wakes someone keeps its row.
    quiet["on"] = False
    await svc._run_job_isolated(job, svc._claim_run(job.id, "scheduled"))
    assert svc._history.append.await_count == 1


@pytest.mark.asyncio
async def test_a_quiet_run_that_failed_keeps_its_row(tmp_path) -> None:
    from unittest.mock import AsyncMock

    from kiro_crew.cron import CronService

    svc = CronService(base_dir=tmp_path)
    svc._history.append = AsyncMock()

    async def tick(job):
        job.quiet_run = True
        job.last_status = "error"
        job.last_error = "boom"
        return "boom"

    svc._on_job = tick
    job = svc.add_job("stale wake", "go", every_secs=900)
    await svc._run_job_isolated(job, svc._claim_run(job.id, "scheduled"))
    assert svc._history.append.await_count == 1
