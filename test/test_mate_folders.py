"""A mate keeps every chat and folder it creates in one home folder."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew.dashboard import mate_folders as mf

PRINCIPAL = "member:member-radar-0123"
OTHER = "member:member-fixer-4567"


class _FolderState:
    """The two folder-store calls the module makes, over a plain list."""

    def __init__(self, folders: list[dict[str, Any]] | None = None) -> None:
        self._folders: list[dict[str, Any]] = folders or []
        self._lock = asyncio.Lock()
        self.writes = 0

    async def mutate_folders(self, mutate):
        async with self._lock:
            # Yield inside the lock, so a second caller really waits on it.
            await asyncio.sleep(0)
            changed, value = mutate(self._folders)
            if changed:
                self.writes += 1
            return value

    async def read_folders(self, read):
        return read(self._folders)


def _run(coro):
    return asyncio.run(coro)


def test_principal_helpers():
    assert mf.mate_principal("member-radar-0123") == PRINCIPAL
    assert mf.mate_principal("") == ""
    assert mf.is_mate_principal(PRINCIPAL)
    assert not mf.is_mate_principal("member:")
    assert not mf.is_mate_principal("some-app")
    assert not mf.is_mate_principal("")


def test_home_is_created_once_named_with_the_display_name():
    state = _FolderState()
    first = _run(mf.ensure_mate_home(state, PRINCIPAL, "Radar"))
    again = _run(mf.ensure_mate_home(state, PRINCIPAL, "Radar"))
    assert first == again
    homes = [f for f in state._folders if f.get(mf.MATE_HOME_KEY)]
    assert len(homes) == 1
    assert homes[0]["name"] == "Radar"
    assert homes[0]["owner_app"] == PRINCIPAL
    assert homes[0]["parent_id"] == ""
    # The second call found the home and wrote nothing.
    assert state.writes == 1


def test_concurrent_first_use_creates_one_home():
    state = _FolderState()

    async def both():
        return await asyncio.gather(
            mf.ensure_mate_home(state, PRINCIPAL, "Radar"),
            mf.ensure_mate_home(state, PRINCIPAL, "Radar"),
        )

    a, b = _run(both())
    assert a == b
    assert sum(1 for f in state._folders if f.get(mf.MATE_HOME_KEY)) == 1


def test_each_mate_gets_its_own_home():
    state = _FolderState()
    radar = _run(mf.ensure_mate_home(state, PRINCIPAL, "Radar"))
    fixer = _run(mf.ensure_mate_home(state, OTHER, "Fixer"))
    assert radar != fixer
    assert mf.mate_home_id(state._folders, PRINCIPAL) == radar
    assert mf.mate_home_id(state._folders, OTHER) == fixer


def test_a_display_name_change_renames_the_home():
    state = _FolderState()
    home = _run(mf.ensure_mate_home(state, PRINCIPAL, "Radar"))
    _run(mf.rename_mate_home(state, PRINCIPAL, "Radar Bot"))
    assert next(f for f in state._folders if f["id"] == home)["name"] == "Radar Bot"
    # The next ensure carries a newer name the same way.
    _run(mf.ensure_mate_home(state, PRINCIPAL, "Scout"))
    assert next(f for f in state._folders if f["id"] == home)["name"] == "Scout"


def test_rename_without_a_home_writes_nothing():
    state = _FolderState()
    _run(mf.rename_mate_home(state, PRINCIPAL, "Radar"))
    _run(mf.rename_mate_home(state, "", "Radar"))
    assert state._folders == []
    assert state.writes == 0


def test_an_empty_display_name_falls_back_to_the_store():
    state = _FolderState()
    _run(mf.ensure_mate_home(state, PRINCIPAL, "  "))
    assert state._folders[0]["name"] == "member-radar-0123"


def test_a_person_folder_that_looks_like_a_home_is_not_one():
    # Same owner, but nested: only a TOP-LEVEL marked folder is the home.
    folders = [
        {"id": "p", "name": "Ops", "parent_id": ""},
        {
            "id": "x",
            "name": "Radar",
            "parent_id": "p",
            "owner_app": PRINCIPAL,
            mf.MATE_HOME_KEY: True,
        },
        {"id": "y", "name": "Radar", "parent_id": "", "owner_app": PRINCIPAL},
    ]
    assert mf.mate_home_id(folders, PRINCIPAL) == ""


def test_in_mate_home_walks_the_whole_subtree():
    state = _FolderState()
    home = _run(mf.ensure_mate_home(state, PRINCIPAL, "Radar"))
    state._folders += [
        {"id": "a", "name": "PR review", "parent_id": home, "owner_app": PRINCIPAL},
        {"id": "b", "name": "kirocrew-worker", "parent_id": "a", "owner_app": PRINCIPAL},
        {"id": "p", "name": "Finance", "parent_id": ""},
        # A cycle must not hang the walk.
        {"id": "c1", "name": "c1", "parent_id": "c2"},
        {"id": "c2", "name": "c2", "parent_id": "c1"},
    ]
    assert mf.in_mate_home(state._folders, home, PRINCIPAL)
    assert mf.in_mate_home(state._folders, "b", PRINCIPAL)
    assert not mf.in_mate_home(state._folders, "p", PRINCIPAL)
    assert not mf.in_mate_home(state._folders, "", PRINCIPAL)
    assert not mf.in_mate_home(state._folders, "c1", PRINCIPAL)
    assert not mf.in_mate_home(state._folders, "b", OTHER)


@pytest.fixture
def _display(monkeypatch):
    monkeypatch.setattr(mf, "display_name_for_principal", lambda principal: "Radar")


def test_a_create_inside_the_home_keeps_its_parent(_display):
    state = _FolderState()
    home = _run(mf.ensure_mate_home(state, PRINCIPAL, "Radar"))
    state._folders.append(
        {"id": "a", "name": "PR review", "parent_id": home, "owner_app": PRINCIPAL}
    )
    assert _run(mf.home_parent_for_create(state, PRINCIPAL, "a")) == "a"
    assert _run(mf.home_parent_for_create(state, PRINCIPAL, home)) == home


@pytest.mark.parametrize("parent", ["", "p", "missing"])
def test_a_create_outside_the_home_is_filed_under_it(_display, parent):
    state = _FolderState([{"id": "p", "name": "Finance", "parent_id": ""}])
    got = _run(mf.home_parent_for_create(state, PRINCIPAL, parent))
    assert got == mf.mate_home_id(state._folders, PRINCIPAL)
    assert got and got != parent


def test_a_create_filed_home_keeps_the_person_folders_project(_display):
    state = _FolderState(
        [
            {"id": "p", "name": "Ops", "parent_id": "", "project_dir": "/repo/x"},
            {"id": "q", "name": "Runbooks", "parent_id": "p"},
        ]
    )
    # The project comes from the named folder's ancestry, not only the folder.
    for named in ("p", "q"):
        parent, project = _run(mf.home_create_target(state, PRINCIPAL, named, ""))
        assert parent == mf.mate_home_id(state._folders, PRINCIPAL)
        assert project == "/repo/x"
    # A project the caller gives wins.
    assert _run(mf.home_create_target(state, PRINCIPAL, "p", "/repo/y"))[1] == "/repo/y"
    # The top level and a missing folder supply nothing.
    assert _run(mf.home_create_target(state, PRINCIPAL, "", ""))[1] == ""
    assert _run(mf.home_create_target(state, PRINCIPAL, "missing", ""))[1] == ""
    # A create inside the home keeps its parent and its own project.
    home = mf.mate_home_id(state._folders, PRINCIPAL)
    assert _run(mf.home_create_target(state, PRINCIPAL, home, "")) == (home, "")


def test_the_cap_refuses_a_new_home(monkeypatch):
    monkeypatch.setattr(mf, "MAX_CHAT_FOLDERS", 1)
    state = _FolderState([{"id": "p", "name": "Finance", "parent_id": ""}])
    with pytest.raises(mf.MateFolderError):
        _run(mf.ensure_mate_home(state, PRINCIPAL, "Radar"))


def test_ensure_refuses_a_non_mate_principal():
    with pytest.raises(ValueError):
        _run(mf.ensure_mate_home(_FolderState(), "some-app", "x"))


def _execution(store: str, member_id: str | None = None) -> Any:
    return SimpleNamespace(member_id=member_id, store=SimpleNamespace(store_id=store))


def test_filing_leaves_a_non_mate_caller_alone(_display):
    state = _FolderState([{"id": "p", "name": "Finance", "parent_id": ""}])
    assert _run(
        mf.mate_home_filing(state, _execution("member-radar-0123"), "p", conductor=False)
    ) == ("p", "p")
    assert _run(mf.mate_home_filing(state, None, "", conductor=False)) == ("", "")
    assert state._folders == [{"id": "p", "name": "Finance", "parent_id": ""}]


def test_filing_puts_a_mate_chat_in_its_home(_display):
    state = _FolderState([{"id": "p", "name": "Finance", "parent_id": ""}])
    execution = _execution("member-radar-0123", "radar")
    home, project = _run(mf.mate_home_filing(state, execution, "", conductor=True))
    assert home == mf.mate_home_id(state._folders, PRINCIPAL)
    assert project == home
    # A person's folder is not where a mate's chat goes, but it still supplies
    # the project directory.
    assert _run(mf.mate_home_filing(state, execution, "p", conductor=True)) == (home, "p")
    # A folder that does not exist supplies nothing: the home is used for both.
    assert _run(mf.mate_home_filing(state, execution, "missing", conductor=True)) == (home, home)
    # A folder inside the home is kept.
    state._folders.append(
        {"id": "a", "name": "PR review", "parent_id": home, "owner_app": PRINCIPAL}
    )
    assert _run(mf.mate_home_filing(state, execution, "a", conductor=True)) == ("a", "a")


def test_a_legacy_conductor_without_a_member_id_gets_no_home(_display):
    state = _FolderState([{"id": "p", "name": "Finance", "parent_id": ""}])
    legacy = _execution("member-radar-0123")
    assert _run(mf.mate_home_filing(state, legacy, "p", conductor=True)) == ("p", "p")
    assert _run(mf.mate_home_filing(state, legacy, "", conductor=True)) == ("", "")
    assert mf.mate_home_id(state._folders, PRINCIPAL) == ""
    assert state._folders == [{"id": "p", "name": "Finance", "parent_id": ""}]


def test_only_the_owning_mate_may_write_into_its_home():
    state = _FolderState([{"id": "p", "name": "Finance", "parent_id": ""}])
    home = _run(mf.ensure_mate_home(state, PRINCIPAL, "Radar"))
    state._folders.append(
        {"id": "a", "name": "PR review", "parent_id": home, "owner_app": PRINCIPAL}
    )
    folders = state._folders
    assert mf.is_mate_home(next(f for f in folders if f["id"] == home))
    assert not mf.is_mate_home(next(f for f in folders if f["id"] == "a"))
    assert mf.home_owner_of(folders, home) == PRINCIPAL
    assert mf.home_owner_of(folders, "a") == PRINCIPAL
    assert mf.home_owner_of(folders, "p") == ""
    assert mf.home_owner_of(folders, "") == ""
    # The person (""), an app and another mate are all kept out; the mate is not.
    for outsider in ("", "some-app", OTHER):
        assert mf.refuses_write_into(folders, home, outsider)
        assert mf.refuses_write_into(folders, "a", outsider)
    assert not mf.refuses_write_into(folders, home, PRINCIPAL)
    assert not mf.refuses_write_into(folders, "a", PRINCIPAL)
    # Outside any home nothing is refused here.
    assert not mf.refuses_write_into(folders, "p", "")
    assert not mf.refuses_write_into(folders, "", "")


def test_the_home_takes_only_layout_changes():
    from kiro_crew.dashboard.chat_folders_records import LAYOUT_ONLY_FIELDS

    assert LAYOUT_ONLY_FIELDS == {"collapsed", "order"}


async def _build_state_with_home() -> tuple[_FolderState, str]:
    state = _FolderState([{"id": "p", "name": "Finance", "parent_id": ""}])
    home = await mf.ensure_mate_home(state, PRINCIPAL, "Radar")
    state._folders.append(
        {"id": "a", "name": "PR review", "parent_id": home, "owner_app": PRINCIPAL}
    )
    return state, home


def _state_with_home() -> tuple[_FolderState, str]:
    """For a sync test; an ``async def`` test awaits :func:`_build_state_with_home`."""
    return _run(_build_state_with_home())


def test_filing_refuses_an_outsider_into_a_mate_home(_display):
    state, home = _state_with_home()
    before = [dict(f) for f in state._folders]
    # The person's chat (no execution or no member) and another mate's worker.
    outsiders = (None, _execution("default"), _execution("member-fixer-4567", "fixer"))
    for execution in outsiders:
        for target in (home, "a"):
            with pytest.raises(mf.MateHomeLockedError):
                _run(mf.mate_home_filing(state, execution, target, conductor=False))
        # Outside any home nothing changes.
        assert _run(mf.mate_home_filing(state, execution, "p", conductor=False)) == ("p", "p")
        assert _run(mf.mate_home_filing(state, execution, "", conductor=False)) == ("", "")
    assert state._folders == before


def test_filing_lets_the_mates_own_worker_into_its_home(_display):
    state, home = _state_with_home()
    worker = _execution("member-radar-0123", "radar")
    assert _run(mf.mate_home_filing(state, worker, home, conductor=False)) == (home, home)
    assert _run(mf.mate_home_filing(state, worker, "a", conductor=False)) == ("a", "a")
    # A worker is not re-filed: what it names outside the home is kept.
    assert _run(mf.mate_home_filing(state, worker, "p", conductor=False)) == ("p", "p")


def test_filing_lets_the_conductor_into_its_own_home_only(_display):
    state, home = _state_with_home()
    other_home = _run(mf.ensure_mate_home(state, OTHER, "Fixer"))
    conductor = _execution("member-radar-0123", "radar")
    assert _run(mf.mate_home_filing(state, conductor, "a", conductor=True)) == ("a", "a")
    # Another mate's home is outside this mate's home, so it is re-filed home,
    # and its project does not follow: only a person's folder supplies one.
    assert _run(mf.mate_home_filing(state, conductor, other_home, conductor=True)) == (home, home)
    state._folders.append({"id": "o", "name": "Theirs", "parent_id": other_home})
    assert _run(mf.mate_home_filing(state, conductor, "o", conductor=True)) == (home, home)
    # A person's folder nested anywhere keeps supplying the project.
    state._folders.append({"id": "q", "name": "Repo", "parent_id": "p"})
    assert _run(mf.mate_home_filing(state, conductor, "q", conductor=True)) == (home, "q")


def test_release_unlocks_only_that_mates_home():
    state, home = _state_with_home()
    other_home = _run(mf.ensure_mate_home(state, OTHER, "Fixer"))
    state._folders.append({"id": "in", "name": "Inside", "parent_id": home})
    assert mf.refuses_write_into(state._folders, "in", "")
    assert _run(mf.release_mate_home(state, PRINCIPAL))
    released = next(f for f in state._folders if f["id"] == home)
    assert mf.MATE_HOME_KEY not in released
    assert released["name"] == "Radar"
    assert not mf.is_mate_home(released)
    assert mf.mate_home_id(state._folders, PRINCIPAL) == ""
    # The person may write into the released subtree; the other mate's home stays locked.
    assert not mf.refuses_write_into(state._folders, home, "")
    assert not mf.refuses_write_into(state._folders, "in", "")
    assert mf.mate_home_id(state._folders, OTHER) == other_home
    assert mf.refuses_write_into(state._folders, other_home, "")


def test_release_without_a_home_is_a_no_op():
    state = _FolderState([{"id": "p", "name": "Mine", "parent_id": ""}])
    assert not _run(mf.release_mate_home(state, PRINCIPAL))
    assert not _run(mf.release_mate_home(state, ""))
    assert not _run(mf.release_mate_home(state, "some-app"))
    assert state.writes == 0
    assert state._folders == [{"id": "p", "name": "Mine", "parent_id": ""}]


def test_subtree_ids_are_what_home_owner_of_locks():
    state, home = _state_with_home()
    other_home = _run(mf.ensure_mate_home(state, OTHER, "Fixer"))
    state._folders.extend(
        [
            {"id": "deep", "name": "Deep", "parent_id": "a"},
            {"id": "q", "name": "Repo", "parent_id": "p"},
            {"id": "dangling", "name": "Lost", "parent_id": "gone"},
            {"id": "c1", "name": "Loop 1", "parent_id": "c2"},
            {"id": "c2", "name": "Loop 2", "parent_id": "c1"},
        ]
    )
    locked = mf.mate_home_subtree_ids(state._folders)
    assert locked == {home, "a", "deep", other_home}
    assert locked == {
        str(f["id"]) for f in state._folders if mf.home_owner_of(state._folders, str(f["id"]))
    }


def test_folder_cleanup_never_removes_a_mate_home_or_its_subtree():
    from kiro_crew.dashboard.chat_folder_cleanup import removable_folder_ids

    state, home = _state_with_home()
    state._folders.append({"id": "q", "name": "Empty", "parent_id": "p"})
    removable = removable_folder_ids(state._folders, (), include_top_level=True)
    assert home not in removable
    assert "a" not in removable
    # The person's empty folders still go.
    assert set(removable) == {"p", "q"}
    # Once the mate is deleted its home is an ordinary folder again.
    _run(mf.release_mate_home(state, PRINCIPAL))
    removable = removable_folder_ids(state._folders, (), include_top_level=True)
    assert {home, "a"} <= set(removable)


def test_create_folder_record_keeps_outsiders_out_of_a_mate_home():
    from kiro_crew.dashboard.chat_folders_records import (
        FolderMateHomeLockedError,
        FolderOwnershipError,
        create_folder_record,
    )

    state, home = _state_with_home()
    for outsider in ("", "some-app", OTHER):
        for parent in (home, "a"):
            with pytest.raises(FolderMateHomeLockedError) as refused:
                _run(
                    create_folder_record(state, name="Mine", parent_id=parent, request_app=outsider)
                )
            assert refused.value.code == "mate_home_locked"
            # Callers that audit an ownership refusal as a denial keep doing so.
            assert isinstance(refused.value, FolderOwnershipError)
    assert [f["id"] for f in state._folders] == ["p", home, "a"]
    made = _run(create_folder_record(state, name="Mine", parent_id="a", request_app=PRINCIPAL))
    assert made["parent_id"] == "a"
    assert _run(create_folder_record(state, name="Mine", parent_id="p"))["parent_id"] == "p"


def test_execution_principal():
    assert mf.execution_principal(None) == ""
    assert mf.execution_principal(_execution("member-radar-0123")) == ""
    assert mf.execution_principal(_execution("member-radar-0123", "radar")) == PRINCIPAL


def test_fork_and_revive_filing_refuses_an_outsider(monkeypatch):
    import kiro_crew.execution_context as ec

    state, home = _state_with_home()
    records = {
        "person": None,
        "worker": _execution("member-radar-0123", "radar"),
        "other": _execution("member-fixer-4567", "fixer"),
    }
    monkeypatch.setattr(ec, "read_session_execution", lambda key: records[key])
    for caller in ("person", "other"):
        for target in (home, "a"):
            with pytest.raises(mf.MateHomeLockedError):
                _run(mf.refuse_foreign_home_filing(state, caller, target))
        _run(mf.refuse_foreign_home_filing(state, caller, "p"))
        _run(mf.refuse_foreign_home_filing(state, caller, ""))
    _run(mf.refuse_foreign_home_filing(state, "worker", home))
    _run(mf.refuse_foreign_home_filing(state, "worker", "a"))


def test_a_refile_checks_both_ends_of_the_move():
    state, home = _state_with_home()
    folders = state._folders
    for outsider in ("", "some-app", OTHER):
        for current in (home, "a"):
            assert mf.refuses_refile(folders, current, "p", outsider)
            assert mf.refuses_refile(folders, current, "", outsider)
        # Outside any home a chat moves freely.
        assert not mf.refuses_refile(folders, "p", "", outsider)
        assert not mf.refuses_refile(folders, "", "p", outsider)
        assert not mf.refuses_refile(folders, "p", "p", outsider)
    # The mate moves its own chats in, around and out of its home.
    assert not mf.refuses_refile(folders, home, "a", PRINCIPAL)
    assert not mf.refuses_refile(folders, "p", home, PRINCIPAL)
    assert not mf.refuses_refile(folders, "a", "p", PRINCIPAL)


def test_revive_filing_refuses_an_outsider_moving_a_session_out_of_a_home(monkeypatch):
    import kiro_crew.execution_context as ec

    state, home = _state_with_home()
    records = {
        "person": None,
        "worker": _execution("member-radar-0123", "radar"),
        "other": _execution("member-fixer-4567", "fixer"),
    }
    monkeypatch.setattr(ec, "read_session_execution", lambda key: records[key])
    for caller in ("person", "other"):
        for current in (home, "a"):
            with pytest.raises(mf.MateHomeLockedError):
                _run(mf.refuse_foreign_home_filing(state, caller, "p", current))
        # A session archived outside any home is refiled as asked.
        _run(mf.refuse_foreign_home_filing(state, caller, "p", ""))
        _run(mf.refuse_foreign_home_filing(state, caller, "p", "p"))
    _run(mf.refuse_foreign_home_filing(state, "worker", "p", home))


def test_name_lookups_never_adopt_a_folder_in_a_mate_home():
    from kiro_crew.dashboard.arrival_folders import _find
    from kiro_crew.dashboard.channel_folders import _find_folder

    state, home = _state_with_home()
    folders = state._folders
    assert _find_folder(folders, "Radar", "slack") is None
    assert _find_folder(folders, "PR review", "slack") is None
    assert _find_folder(folders, "Finance", "slack")["id"] == "p"
    assert _find(folders, "Radar", "") is None
    assert _find(folders, "PR review", home) is None
    assert _find(folders, "Finance", "")["id"] == "p"


def test_a_fork_keeps_a_mate_home_folder_only_for_that_mate():
    state, home = _state_with_home()
    folders = state._folders
    # The person, an app and another mate forking a chat in the home get an unfiled child.
    for outsider in ("", "some-app", OTHER):
        assert mf.inherited_folder(folders, home, outsider) == ""
        assert mf.inherited_folder(folders, "a", outsider) == ""
    # The mate keeps it; a person's folder and no folder are inherited as they are.
    assert mf.inherited_folder(folders, home, PRINCIPAL) == home
    assert mf.inherited_folder(folders, "a", PRINCIPAL) == "a"
    assert mf.inherited_folder(folders, "p", "") == "p"
    assert mf.inherited_folder(folders, "", "") == ""


def test_session_folder_principal(monkeypatch):
    import kiro_crew.execution_context as ec

    records = {"person": None, "worker": _execution("member-radar-0123", "radar")}

    def _read(key):
        if key == "broken":
            raise OSError("unreadable")
        return records[key]

    monkeypatch.setattr(ec, "read_session_execution", _read)
    assert _run(mf.session_folder_principal("worker")) == PRINCIPAL
    assert _run(mf.session_folder_principal("person")) == ""
    assert _run(mf.session_folder_principal("broken")) == ""


def test_a_cron_tab_is_kept_out_of_a_foreign_mate_home():
    from kiro_crew.dashboard.cron_inject import chat_folder_refuses_store

    state, home = _state_with_home()
    for target in (home, "a"):
        assert chat_folder_refuses_store(state, target, "")
        assert chat_folder_refuses_store(state, target, "member-fixer-4567")
        assert not chat_folder_refuses_store(state, target, "member-radar-0123")
    assert not chat_folder_refuses_store(state, "p", "")
    assert not chat_folder_refuses_store(state, "", "")


def _cron_app(handler, route: str, state: _FolderState, **store):
    from unittest.mock import AsyncMock, MagicMock

    from aiohttp import web
    from dashboard_owner_helpers import as_owner

    async def hold_folders(section):
        return await section([dict(f) for f in state._folders])

    app = web.Application()
    app["state"] = SimpleNamespace(
        crons=SimpleNamespace(**{"raise_if_store_unreadable": lambda: None, **store}),
        _folders=state._folders,
        hold_folders=hold_folders,
        push_refresh=MagicMock(),
        ack_notification=AsyncMock(),
        has_slot=MagicMock(return_value=False),
        get_slot=lambda name: None,
        push_slots_update=MagicMock(),
    )
    app.router.add_route("*", route, handler)
    return as_owner(app)


_CRON_BODY = {"name": "brief", "message": "Write it.", "every": 3600}


@pytest.mark.asyncio
class TestCronSaveIntoAMateHome:
    @staticmethod
    def _sel(monkeypatch):
        from unittest.mock import MagicMock

        import kiro_crew.dashboard.handlers as handlers_pkg

        audit = MagicMock()
        monkeypatch.setattr(handlers_pkg, "sel", lambda: audit)
        return audit

    @staticmethod
    def _denials(audit) -> list[dict[str, Any]]:
        return [
            c.kwargs
            for c in audit.log_api_access.call_args_list
            if c.kwargs.get("outcome") == "denied"
        ]

    async def test_a_create_on_another_store_is_refused_and_audited(self, monkeypatch):
        from unittest.mock import AsyncMock

        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.dashboard.handlers.cron import api_crons_create

        audit = self._sel(monkeypatch)
        state, home = await _build_state_with_home()
        add = AsyncMock()
        app = _cron_app(api_crons_create, "/api/crons", state, add_job_async=add)
        async with TestClient(TestServer(app)) as client:
            for target in (home, "a"):
                resp = await client.post(
                    "/api/crons", json={**_CRON_BODY, "chat_folder_id": target}
                )
                assert resp.status == 403
                assert (await resp.json())["code"] == "mate_home_locked"
        add.assert_not_awaited()
        denials = self._denials(audit)
        assert [d["operation"] for d in denials] == ["crons.create", "crons.create"]
        assert all(d["error"] == "mate_home_locked" for d in denials)

    async def test_a_create_for_the_mate_itself_is_kept(self, monkeypatch):
        from unittest.mock import AsyncMock

        from aiohttp.test_utils import TestClient, TestServer

        import kiro_crew.dashboard.handlers.cron as handler_mod
        from kiro_crew.dashboard.handlers.cron import api_crons_create

        self._sel(monkeypatch)
        monkeypatch.setattr(handler_mod, "_member_store", lambda member: "member-radar-0123")
        state, home = await _build_state_with_home()
        add = AsyncMock(return_value=SimpleNamespace(id="job42"))
        monkeypatch.setattr("kiro_crew.dashboard.change_cards.cron_job_revision", lambda job: "rev")
        app = _cron_app(api_crons_create, "/api/crons", state, add_job_async=add)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/crons", json={**_CRON_BODY, "member_id": "radar", "chat_folder_id": home}
            )
            assert resp.status == 200
        assert add.await_args.kwargs["chat_folder_id"] == home

    async def test_an_update_follows_the_store_the_job_runs_on(self, monkeypatch):
        from unittest.mock import AsyncMock

        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.dashboard.handlers.cron import api_cron_update

        audit = self._sel(monkeypatch)
        state, home = await _build_state_with_home()
        update = AsyncMock(return_value=SimpleNamespace(id="job42", chat_folder_id=home))
        for store, status in (("", 403), ("member-fixer-4567", 403), ("member-radar-0123", 200)):
            update.reset_mock()
            job = SimpleNamespace(id="job42", memory_store=store, chat_folder_id="")
            app = _cron_app(
                api_cron_update,
                "/api/crons/{job_id}",
                state,
                update_job_async=update,
                get_job_async=AsyncMock(return_value=job),
            )
            async with TestClient(TestServer(app)) as client:
                resp = await client.patch("/api/crons/job42", json={"chat_folder_id": home})
                assert resp.status == status
                if status == 403:
                    assert (await resp.json())["code"] == "mate_home_locked"
            assert update.await_count == (1 if status == 200 else 0)
        assert [d["operation"] for d in self._denials(audit)] == ["crons.update"] * 2

    async def test_an_update_may_not_move_a_tab_out_of_a_foreign_home(self, monkeypatch):
        from unittest.mock import AsyncMock

        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.dashboard.handlers.cron import api_cron_update

        audit = self._sel(monkeypatch)
        state, home = await _build_state_with_home()
        update = AsyncMock(return_value=SimpleNamespace(id="job42", chat_folder_id="p"))
        cases = (
            ("", "a", {"chat_folder_id": "p"}, 403),
            ("", home, {"chat_folder_id": ""}, 403),
            ("", "a", {"persistent_session": False}, 403),
            ("member-fixer-4567", "a", {"chat_folder_id": "p"}, 403),
            # The mate's own job leaves its home; a job outside any home refiles freely.
            ("member-radar-0123", "a", {"chat_folder_id": "p"}, 200),
            ("", "p", {"chat_folder_id": ""}, 200),
            ("", "", {"chat_folder_id": "p"}, 200),
        )
        for store, current, body, status in cases:
            update.reset_mock()
            job = SimpleNamespace(id="job42", memory_store=store, chat_folder_id=current)
            app = _cron_app(
                api_cron_update,
                "/api/crons/{job_id}",
                state,
                update_job_async=update,
                get_job_async=AsyncMock(return_value=job),
            )
            async with TestClient(TestServer(app)) as client:
                resp = await client.patch("/api/crons/job42", json=body)
                assert resp.status == status, (store, current, body)
                if status == 403:
                    assert (await resp.json())["code"] == "mate_home_locked"
            assert update.await_count == (1 if status == 200 else 0)
        denials = self._denials(audit)
        assert [d["operation"] for d in denials] == ["crons.update"] * 4
        assert all("current_chat_folder_id=" in d["resources"] for d in denials)

    async def test_moving_an_existing_tab_rechecks_its_current_folder(self, monkeypatch):
        from unittest.mock import AsyncMock

        from kiro_crew.dashboard import cron_inject

        state, home = await _build_state_with_home()
        slot = SimpleNamespace(key="cron-job42", folder_id="a", memory_store="")
        state.get_slot = lambda name: slot  # type: ignore[attr-defined]
        commit = AsyncMock(return_value=True)
        monkeypatch.setattr(cron_inject, "_commit_cron_tab_placement", commit)
        job = SimpleNamespace(id="job42", name="brief")
        await cron_inject.move_cron_job_tab(state, job, "a", "p")
        commit.assert_not_awaited()
        slot.memory_store = "member-radar-0123"
        await cron_inject.move_cron_job_tab(state, job, "a", "p")
        commit.assert_awaited_once()
