"""The one upgrade notice a crewmate moved onto its worker gives its user.

Owed only by a crewmate the migration moved, delivered once as a hidden turn in
its own thread, never again on a later start, never for a crewmate created after
the upgrade.
"""

from __future__ import annotations

import asyncio

import pytest

from kiro_crew import mate_upgrade_notice as notice
from kiro_crew import members as members_mod
from kiro_crew.dashboard import mate_thread_wakes as wakes


class _Slot:
    def __init__(self, key: str, agent: str):
        self.key = key
        self.mode = members_mod.DM_SLOT_MODE
        self.agent = agent
        self.executor = ""
        self.task = None
        self._queue: list = []
        self._turn_admission_reserved = False

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()


class _State:
    def __init__(self, *slots: _Slot):
        self._slots = {s.key: s for s in slots}
        self._background_tasks: set = set()

    def get_slot(self, key: str):
        return self._slots.get(key)


@pytest.fixture
def dispatched(monkeypatch):
    calls: list[tuple[str, str]] = []

    def dispatch(state, slot, prompt, *, human_seen, heartbeat=False):
        calls.append((slot.key, prompt))

    monkeypatch.setattr("kiro_crew.dashboard.mate_welcome.dispatch_hidden_turn", dispatch)
    return calls


def _thread(member: str) -> _Slot:
    key = members_mod.member_slot_key(member)
    members_mod.write_dm_binding(member, member=member, slot_key=key)
    return _Slot(key, member)


async def _offer(state: _State) -> None:
    await wakes.offer_upgrade_notices(state)
    for task in list(getattr(state, "_mate_wake_tasks", {}).values()):
        await task


def test_a_moved_crewmate_tells_its_user_once_and_never_on_a_later_start(dispatched):
    slot = _thread("atlas")
    notice.mark_notice_owed("atlas")

    asyncio.run(_offer(_State(slot)))
    asyncio.run(_offer(_State(slot)))  # a later start: a fresh gateway state

    assert dispatched == [(slot.key, notice.NOTICE_PROMPT)]
    assert dispatched[0][1].startswith(notice.WORKER_UPGRADE_PREFIX)


def test_a_move_that_changed_the_kit_names_the_change_in_its_one_notice(dispatched):
    slot = _thread("atlas")
    notice.mark_notice_owed("atlas", "it no longer has tool 'execute_bash'")

    asyncio.run(_offer(_State(slot)))
    asyncio.run(_offer(_State(slot)))  # a later start: still one notice

    assert len(dispatched) == 1
    key, prompt = dispatched[0]
    assert key == slot.key and prompt.startswith(notice.NOTICE_PROMPT)
    assert "it no longer has tool 'execute_bash'" in prompt
    assert "request_capabilities" in prompt


def test_a_crewmate_created_after_the_upgrade_owes_no_notice(dispatched):
    slot = _thread("nova")
    # Its own directory exists, as any crewmate's does once it has been used.
    members_mod.member_dir(members_mod.member_slug("nova")).mkdir(parents=True, exist_ok=True)

    asyncio.run(_offer(_State(slot)))

    assert dispatched == []


def test_a_notice_owed_by_another_member_is_not_claimed(dispatched):
    slot = _thread("atlas")
    directory = members_mod.member_dir(members_mod.member_slug("atlas"))
    directory.mkdir(parents=True, exist_ok=True)
    # A record naming a member the thread's binding does not hold.
    (directory / notice.NOTICE_OWED_FILENAME).write_text(
        '{"member": "someone-else"}', encoding="utf-8"
    )
    imposter = _Slot(slot.key, "someone-else")

    asyncio.run(_offer(_State(imposter)))

    assert dispatched == []


def test_a_thread_not_open_at_startup_gets_it_at_its_next_turn_end(dispatched):
    slot = _thread("atlas")
    notice.mark_notice_owed("atlas")

    async def _drive():
        state = _State()
        await _offer(state)  # the thread was not open yet
        state._slots[slot.key] = slot
        wakes.note_turn_end(state, slot, "hello")
        for task in list(state._background_tasks):
            await task
        for task in list(getattr(state, "_mate_wake_tasks", {}).values()):
            await task

    asyncio.run(_drive())

    assert [key for key, _ in dispatched] == [slot.key]


def test_the_migration_owes_the_notice_only_for_crewmates_it_moved(monkeypatch, tmp_path):
    _migrate_atlas_and_orbit(monkeypatch, tmp_path)

    assert notice.claim_notice(members_mod.member_slug("atlas"), "atlas") is True
    assert notice.claim_notice(members_mod.member_slug("orbit"), "orbit") is False


def test_a_crewmate_the_migration_left_behind_tells_its_user_why_once(
    monkeypatch, tmp_path, dispatched
):
    """``orbit``'s template denies writes under ``/etc``, which its worker would not
    keep, so it stays on its template; its thread says so once, with the reason."""
    _migrate_atlas_and_orbit(monkeypatch, tmp_path)
    assert notice.claim_kept_notice(members_mod.member_slug("atlas"), "atlas") == ""
    slot = _thread("orbit")

    asyncio.run(_offer(_State(slot)))
    asyncio.run(_offer(_State(slot)))  # a later start, still left behind

    assert len(dispatched) == 1
    key, prompt = dispatched[0]
    assert key == slot.key and prompt.startswith(notice.WORKER_UPGRADE_PREFIX)
    assert "toolsSettings" in prompt and "left as it was" in prompt


def test_a_left_behind_crewmate_that_moves_later_never_says_it_was_left(
    monkeypatch, tmp_path, dispatched
):
    """Start 1 leaves ``orbit`` behind with its thread unopened; start 2 moves it.
    Its thread then gets the moved notice only, never the stale kept one."""
    import json

    import kiro_crew.config.paths as paths_mod
    from kiro_crew.mate_config_migration import migrate_existing_crewmates

    cfg = _migrate_atlas_and_orbit(monkeypatch, tmp_path)
    shell = paths_mod.kiro_agents_dir() / "shell.json"
    spec = json.loads(shell.read_text(encoding="utf-8"))
    spec.pop("toolsSettings")
    shell.write_text(json.dumps(spec), encoding="utf-8")
    assert migrate_existing_crewmates(cfg).migrated == ["orbit"]
    slot = _thread("orbit")

    asyncio.run(_offer(_State(slot)))
    asyncio.run(_offer(_State(slot)))

    assert dispatched == [(slot.key, notice.NOTICE_PROMPT)]


def test_a_kept_record_left_beside_a_claimed_moved_notice_is_not_delivered():
    """A kept record an older build never cleared, for a crewmate that has moved."""
    directory = members_mod.member_dir(members_mod.member_slug("orbit"))
    directory.mkdir(parents=True, exist_ok=True)
    notice.mark_kept_owed("orbit", "its agent could not be read")
    (directory / notice.NOTICE_CLAIMED_FILENAME).write_text('{"member": "orbit"}')

    assert notice.claim_kept_notice(members_mod.member_slug("orbit"), "orbit") == ""


def test_a_kept_notice_claimed_while_the_thread_is_busy_follows_its_next_turn_end(
    dispatched,
):
    slot = _thread("orbit")
    notice.mark_kept_owed("orbit", "the agent it runs on could not be read")

    async def _drive():
        state = _State(slot)
        slot._queue.append("a queued message")  # busy: the flush waits
        await _offer(state)
        assert dispatched == []
        slot._queue.clear()
        wakes.note_turn_end(state, slot, "done")
        for task in list(state._background_tasks):
            await task
        for task in list(getattr(state, "_mate_wake_tasks", {}).values()):
            await task

    asyncio.run(_drive())

    assert [key for key, _ in dispatched] == [slot.key]
    assert "could not be read" in dispatched[0][1]


def _migrate_atlas_and_orbit(monkeypatch, tmp_path):
    """``atlas`` moves; ``orbit``'s worker would not keep its write limit, so it stays."""
    import json

    import kiro_crew.config.paths as paths_mod
    from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
    from kiro_crew.mate_config_migration import migrate_existing_crewmates

    user = tmp_path / "home" / "agents"
    user.mkdir(parents=True)
    monkeypatch.setattr(paths_mod, "kiro_agents_dir", lambda: user)
    monkeypatch.setattr(paths_mod, "kiro_home", lambda: tmp_path / "home")
    base = {"name": "kirocrew-mate-worker", "prompt": "p", "tools": ["fs_read"], "mcpServers": {}}
    mates = tmp_path / "home" / "mates" / "agents"
    mates.mkdir(parents=True)
    (mates / "kirocrew-mate-worker.json").write_text(json.dumps(base), encoding="utf-8")
    (user / "plain.json").write_text(json.dumps({**base, "name": "plain"}), encoding="utf-8")
    (user / "shell.json").write_text(
        json.dumps(
            {
                **base,
                "name": "shell",
                "toolsSettings": {"fs_write": {"deniedPaths": ["/etc/**"]}},
            }
        ),
        encoding="utf-8",
    )
    cfg = KiroCrewConfig()
    cfg.agents = {
        "atlas": KiroCrewAgentConfig(member_id="atlas", kiro_agent="plain"),
        "orbit": KiroCrewAgentConfig(member_id="orbit", kiro_agent="shell"),
    }
    monkeypatch.setattr(
        "kiro_crew.mate_config_migration.KiroCrewConfig",
        type("Shim", (), {"load": staticmethod(lambda: cfg)}),
    )
    monkeypatch.setattr("kiro_crew.memory_stores.persist_member_config", lambda c, n, **kw: None)

    assert migrate_existing_crewmates(cfg).migrated == ["atlas"]
    return cfg


def test_the_notice_names_the_path_to_the_worker_tabs_as_the_dashboard_shows_it():
    """The tabs live in the crewmate editor's Built From section, opened by the pencil.

    Built From only shows the kit, so the notice also names where it is changed:
    the same editor's Advanced section, Worker's extra kit."""
    for words in ("pencil button (Edit mate)", "Built From", "Tools, MCP Servers and Skills"):
        assert words in notice.NOTICE_PROMPT
    assert "crewmate page" not in notice.NOTICE_PROMPT
    assert "under Advanced, Worker's extra kit" in notice.NOTICE_PROMPT


def test_the_notice_names_the_editor_labels_the_dashboard_shows():
    """The words the notice quotes are the English labels the editor renders."""
    import json
    from pathlib import Path

    labels = json.loads(
        (Path(__file__).resolve().parents[1] / "website/src/i18n/locales/en.manual.json").read_text(
            encoding="utf-8"
        )
    )
    kit = labels["crewWorkerKit"]
    assert (kit["section_advanced"], kit["title"]) == ("Advanced", "Worker's extra kit")
