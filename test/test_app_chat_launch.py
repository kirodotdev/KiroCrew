"""App chat launch: ``permissions.chatLaunch`` and the gate on ``POST /api/chat/slots``.

An app's page opens a person's chat it declares (a key prefix, the agents, an optional
folder, the memory modes) by sending the ordinary create with ``app_launch: <app>``,
without declaring ``/api/chat/slots`` in ``permissions.api``. Pinned here:

- the declaration parses fail-closed, never reaches a core key namespace, and round-trips
  through the manifest;
- the create is admitted only inside the declaration, from a person, for an enabled
  app, with no other create option, onto no app-owned or non-chat slot and no letter-case
  alias of a live slot (both checked again at the mint) and no transcript that is
  unreadable or another app owns or launched; each
  refusal is one uniform 403 that creates nothing and is audited under the app's name;
- a key prefix is one installed app's: while another installed app declares an
  overlapping one, neither launches, so no app reaches a chat under another's prefix;
- what is admitted is the ordinary create, field for field;
- adoption (E9) refuses a copy that declares more than the wheel copy does, and the
  trust disclosures name it.

Owning spec: docs/system-specs/modules/app-kit-platform.md, section 44.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_ready_kiro_prerequisite

from kiro_crew.apps import chat_launch
from kiro_crew.apps.chat_launch import ChatLaunch, parse_chat_launch
from kiro_crew.apps.manifest import AppManifest
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.history import ConversationLog

APP = "notes"
FOLDER_ID = "f-notes"
OTHER_FOLDER_ID = "f-other"
DECLARED: dict[str, Any] = {
    "keyPrefix": "notes-followup-",
    "agents": ["notes-reviewer", "kirocrew"],
    "folder": "Notes",
    "memoryModes": ["persistent"],
}
KEY = "notes-followup-abc123"
#: *KEY* in another letter case: one transcript file with it on a case-insensitive disk.
KEY_ALIAS = "notes-followup-ABC123"


def _launch_body(**over: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": KEY,
        "agent": "kirocrew",
        "memory_mode": "persistent",
        "title": "followup-pr#7",
        "folder_id": FOLDER_ID,
        "app_launch": APP,
    }
    body.update(over)
    return {k: v for k, v in body.items() if v is not None}


# --------------------------------------------------------------------------- parsing


class TestDeclaration:
    def test_a_full_declaration_parses_and_round_trips(self):
        launch = parse_chat_launch(DECLARED)
        assert launch == ChatLaunch(
            key_prefix="notes-followup-",
            agents=("notes-reviewer", "kirocrew"),
            folder="Notes",
            memory_modes=("persistent",),
        )
        assert parse_chat_launch(launch.to_dict()) == launch

    def test_folder_and_memory_modes_are_optional(self):
        launch = parse_chat_launch({"keyPrefix": "x-", "agents": ["kirocrew"]})
        assert launch is not None and launch.folder == "" and launch.memory_modes == ("persistent",)
        assert "folder" not in launch.to_dict()

    @pytest.mark.parametrize(
        "value",
        [
            None,
            True,
            "notes-",
            [],
            {},
            {**DECLARED, "keyPrefix": "notes"},  # no trailing separator
            {**DECLARED, "keyPrefix": "Notes-"},
            {**DECLARED, "keyPrefix": "notes_x-"},
            {**DECLARED, "keyPrefix": "-"},
            {**DECLARED, "keyPrefix": "a" * 64 + "-"},
            {**DECLARED, "agents": []},
            {**DECLARED, "agents": "kirocrew"},
            {**DECLARED, "agents": ["bad name"]},
            {**DECLARED, "agents": ["a"] * 17},
            {**DECLARED, "folder": 3},
            {**DECLARED, "folder": "  "},
            {**DECLARED, "folder": "a\nb"},
            {**DECLARED, "memoryModes": []},
            {**DECLARED, "memoryModes": ["forever"]},
            {**DECLARED, "memoryModes": "persistent"},
            {**DECLARED, "project": "/etc"},
        ],
    )
    def test_anything_malformed_declares_nothing(self, value):
        assert parse_chat_launch(value) is None

    @pytest.mark.parametrize(
        "prefix", ["chat-", "chat-1-", "cron-", "workflow-x-", "member-", "task-review-", "task-"]
    )
    def test_no_prefix_reaches_a_key_the_core_mints(self, prefix):
        assert parse_chat_launch({**DECLARED, "keyPrefix": prefix}) is None

    def test_the_core_prefixes_are_the_cores_own_spellings(self):
        from kiro_crew.dashboard import slot_ownership
        from kiro_crew.dashboard.session_control_parts.caller_access import OWNER_CHAT_SLOT_PREFIX
        from kiro_crew.members_parts.member_space import DM_SLOT_KEY_PREFIX

        assert chat_launch.CORE_KEY_PREFIXES == (
            OWNER_CHAT_SLOT_PREFIX,
            slot_ownership.CRON_SLOT_PREFIX,
            slot_ownership.WORKFLOW_SLOT_PREFIX,
            slot_ownership.TASK_REVIEW_SLOT_PREFIX,
            DM_SLOT_KEY_PREFIX,
        )

    def test_covers_key_is_the_prefix_plus_a_bounded_key_id(self):
        launch = parse_chat_launch(DECLARED)
        assert launch is not None
        assert launch.covers_key(KEY)
        for key in (
            "notes-followup-",
            "notes-other-1",
            "notes-followup-a b",
            "notes-followup-" + "a" * 179,
        ):
            assert not launch.covers_key(key), key

    def test_within_is_no_wider_on_every_axis(self):
        launch = parse_chat_launch(DECLARED)
        assert launch is not None and launch.within(launch)
        assert not launch.within(None)
        for wider in (
            {**DECLARED, "agents": [*DECLARED["agents"], "other"]},
            {**DECLARED, "memoryModes": ["persistent", "incognito"]},
            {**DECLARED, "folder": "Elsewhere"},
            {**DECLARED, "keyPrefix": "notes-"},
        ):
            other = parse_chat_launch(wider)
            assert other is not None and not other.within(launch), wider

    @pytest.mark.parametrize(
        ("prefix", "overlapping"),
        [
            ("notes-followup-", True),
            ("notes-", True),
            ("notes-followup-x-", True),
            ("notes-other-", False),
            ("followup-", False),
        ],
    )
    def test_two_prefixes_overlap_when_one_starts_the_other(self, prefix, overlapping):
        launch = parse_chat_launch(DECLARED)
        other = parse_chat_launch({**DECLARED, "keyPrefix": prefix})
        assert launch is not None and other is not None
        assert other.overlaps(launch) is overlapping
        assert launch.overlaps(other) is overlapping

    def test_the_manifest_carries_it_and_drops_a_malformed_one(self):
        base = {"name": APP, "version": "1.0.0", "description": "d", "author": "a"}
        manifest = AppManifest.from_dict({**base, "permissions": {"chatLaunch": DECLARED}})
        assert manifest.permissions.chatLaunch == parse_chat_launch(DECLARED)
        assert manifest.permissions.to_dict()["chatLaunch"] == parse_chat_launch(DECLARED).to_dict()
        bad = AppManifest.from_dict({**base, "permissions": {"chatLaunch": {"keyPrefix": "chat-"}}})
        assert bad.permissions.chatLaunch is None
        assert "chatLaunch" not in bad.permissions.to_dict()


# --------------------------------------------------------------------------- disclosure


def test_the_install_summary_and_the_catalog_name_it():
    from kiro_crew.apps.official_catalog import _disclosed_permissions_manifest
    from kiro_crew.apps.permissions import format_permissions_summary

    manifest = AppManifest.from_dict(
        {
            "name": APP,
            "version": "1.0.0",
            "description": "d",
            "author": "a",
            "permissions": {"chatLaunch": DECLARED},
        }
    )
    summary = format_permissions_summary(manifest)
    assert "may open chats for you with notes-reviewer, kirocrew in the 'Notes' folder" in summary
    assert "chat keys start with 'notes-followup-'; memory: persistent" in summary
    disclosed = _disclosed_permissions_manifest({"permissions": {"chatLaunch": DECLARED}})
    assert disclosed == {"permissions": {"chatLaunch": parse_chat_launch(DECLARED).to_dict()}}
    assert (
        _disclosed_permissions_manifest({"permissions": {"chatLaunch": {"keyPrefix": "chat-"}}})
        is None
    )


# --------------------------------------------------------------------------- the gate


def _make_state(tmp_path) -> DashboardState:
    sessions = MagicMock(count=0)
    sessions.remove = AsyncMock()
    sessions.recycle_background = AsyncMock()
    sessions.get_pid = MagicMock(return_value=None)
    state = DashboardState(
        sessions=sessions,
        crons=MagicMock(list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})),
        lessons=MagicMock(load_all=MagicMock(return_value=[])),
        start_time=0.0,
        conversation_log=ConversationLog(base_dir=tmp_path),
    )
    state.kiro_prerequisite_service = _make_ready_kiro_prerequisite()
    state._folders.append({"id": FOLDER_ID, "name": "Notes", "order": 0})
    state._folders.append({"id": OTHER_FOLDER_ID, "name": "Personal", "order": 1})
    return state


def _make_app(state, *, app_token: str = "") -> web.Application:
    from kiro_crew.dashboard.chat import api_chat_slot_create

    async def handler(request: web.Request) -> web.Response:
        if app_token:
            request["app"] = app_token
        return await api_chat_slot_create(request)

    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots", handler)
    return app


@pytest.fixture(autouse=True)
def _isolate(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    # Its own data home, so the installed apps a prefix check reads are this test's.
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))


def _install_tree(tmp_path, name: str, declared: dict[str, Any] | None = None) -> None:
    """An installed app tree under the test's data home, declaring *declared* if given."""
    tree = tmp_path / "home" / "apps" / name
    tree.mkdir(parents=True)
    manifest = {
        "name": name.lstrip("."),
        "version": "1.0.0",
        "description": "d",
        "author": "a",
        "permissions": {} if declared is None else {"chatLaunch": declared},
    }
    (tree / "app.json").write_text(json.dumps(manifest), encoding="utf-8")


@pytest.fixture
def declared(monkeypatch):
    """*APP* is enabled and declares :data:`DECLARED`; any other app declares nothing."""
    launch = parse_chat_launch(DECLARED)
    monkeypatch.setattr(
        chat_launch, "declared_chat_launch", lambda name: launch if name == APP else None
    )
    return launch


@pytest.fixture
def denials(monkeypatch):
    seen: list[tuple[str, str, str, str]] = []
    from kiro_crew.dashboard.handlers import sessions

    monkeypatch.setattr(sessions, "_audit_app_denial", lambda *a: seen.append(a))
    return seen


async def _post(state, body, *, app_token: str = ""):
    async with TestClient(TestServer(_make_app(state, app_token=app_token))) as client:
        resp = await client.post("/api/chat/slots", json=body)
        return resp.status, await resp.json()


def _seed_transcript(state, key: str, **meta: Any) -> None:
    path = state.conversation_log._path(f"dashboard:{key}")
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        {"_type": "metadata", **meta},
        {"role": "user", "content": "hello", "ts": "2026-01-01T00:00:00"},
    ]
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")


@pytest.mark.usefixtures("declared")
class TestAdmitted:
    @pytest.mark.asyncio
    async def test_a_declared_launch_is_the_ordinary_create(self, tmp_path):
        launched = _make_state(tmp_path / "a")
        plain = _make_state(tmp_path / "b")
        status, row = await _post(launched, _launch_body())
        assert status == 200, row
        plain_status, plain_row = await _post(
            plain, {k: v for k, v in _launch_body().items() if k != "app_launch"}
        )
        assert plain_status == 200
        fields = ("key", "agent", "title", "folder_id", "memory_mode", "mode")
        assert {f: row.get(f) for f in fields} == {f: plain_row.get(f) for f in fields}
        slot = launched._slots[KEY]
        # The person's chat: no owning app.
        assert slot._app == "" and slot.folder_id == FOLDER_ID and slot.agent == "kirocrew"

    @pytest.mark.asyncio
    async def test_a_reopen_of_a_persons_chat_under_the_prefix_is_admitted(self, tmp_path):
        state = _make_state(tmp_path)
        _seed_transcript(state, KEY, title="earlier")
        status, row = await _post(state, _launch_body(title=None, folder_id=None))
        assert status == 200, row
        assert row["key"] == KEY

    @pytest.mark.asyncio
    async def test_a_live_persons_chat_under_the_key_is_reopened(self, tmp_path):
        state = _make_state(tmp_path)
        mine = state.get_or_create_slot(KEY)
        status, row = await _post(state, _launch_body(title=None, folder_id=None))
        assert status == 200, row
        assert row["key"] == KEY and state._slots[KEY] is mine and mine._app == ""

    @pytest.mark.asyncio
    async def test_the_app_it_names_is_recorded_on_the_conversation_it_begins(self, tmp_path):
        state = _make_state(tmp_path)
        status, _ = await _post(state, _launch_body(embed_app=APP))
        assert status == 200
        assert state.conversation_log.get_metadata(f"dashboard:{KEY}").get("embed_app") == APP
        assert state._slots[KEY]._app == ""


@pytest.mark.usefixtures("declared")
class TestRefused:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("over", "reason"),
        [
            ({"app_launch": "other-app"}, "declares no chatLaunch"),
            ({"app_launch": "Bad App"}, "not a valid name"),
            ({"name": "chat-1-1700000000"}, "outside the declared prefix"),
            ({"name": "notes-other-1"}, "outside the declared prefix"),
            ({"name": "notes-followup-a b"}, "slot-key form"),
            ({"name": None}, "slot-key form"),
            ({"agent": "admin-agent"}, "agent is not declared"),
            ({"agent": None}, "agent is not declared"),
            ({"memory_mode": "incognito"}, "memory mode"),
            ({"folder_id": OTHER_FOLDER_ID}, "folder is not the declared one"),
            ({"folder_id": "missing"}, "folder is not the declared one"),
            ({"embed_app": "other-app"}, "embed_app names another app"),
            ({"project_from_slot": "chat-1-1"}, "undeclared create options: project_from_slot"),
            ({"mode": "worker"}, "undeclared create options: mode"),
            ({"model": "auto"}, "undeclared create options: model"),
            ({"agent_kind": "member"}, "undeclared create options: agent_kind"),
            ({"app_agent_owner": APP}, "undeclared create options: app_agent_owner"),
        ],
    )
    async def test_outside_the_declaration_nothing_is_created(
        self, tmp_path, denials, over, reason
    ):
        state = _make_state(tmp_path)
        status, body = await _post(state, _launch_body(**over))
        assert status == 403, body
        assert body["code"] == "app_chat_launch_refused"
        assert body["error"] == "this app may not open that chat"
        assert state._slots == {}
        assert len(denials) == 1 and reason in denials[0][3]
        assert denials[0][1] == "chat_launch"

    @pytest.mark.asyncio
    async def test_an_app_token_cannot_launch(self, tmp_path, denials):
        state = _make_state(tmp_path)
        status, _ = await _post(state, _launch_body(), app_token=APP)
        assert status == 403 and state._slots == {}
        assert "app token" in denials[0][3]

    @pytest.mark.asyncio
    async def test_another_apps_live_slot_is_left_untouched(self, tmp_path, denials):
        state = _make_state(tmp_path)
        other = state.get_or_create_slot(KEY, app="other-app")
        other.title = "theirs"
        status, _ = await _post(state, _launch_body())
        assert status == 403
        assert state._slots[KEY] is other and other.title == "theirs" and other.folder_id == ""
        assert "app-owned slot" in denials[0][3]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("meta", "reason"),
        [
            ({"app": "other-app"}, "belongs to an app"),
            ({"app": APP}, "belongs to an app"),
            ({"embed_app": "other-app"}, "opened by another app"),
        ],
    )
    async def test_a_transcript_another_owner_holds_is_refused(
        self, tmp_path, denials, meta, reason
    ):
        state = _make_state(tmp_path)
        _seed_transcript(state, KEY, **meta)
        status, _ = await _post(state, _launch_body())
        assert status == 403 and state._slots == {}
        assert reason in denials[0][3]

    @pytest.mark.asyncio
    async def test_a_live_slot_of_another_mode_is_left_untouched(self, tmp_path, denials):
        state = _make_state(tmp_path)
        worker = state.get_or_create_slot(KEY)
        worker.mode = "worker"
        title = worker.title
        status, _ = await _post(state, _launch_body())
        assert status == 403
        assert state._slots[KEY] is worker and worker.folder_id == "" and worker.title == title
        assert [d[3] for d in denials] == ["the key holds a non-chat slot"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("owner", [APP, "other-app", ""])
    async def test_a_live_slot_under_the_key_in_another_letter_case_is_left_untouched(
        self, tmp_path, denials, owner
    ):
        from kiro_crew.dashboard.slot_ownership import CASE_ALIAS_DENIED

        state = _make_state(tmp_path)
        alias = state.get_or_create_slot(KEY_ALIAS, app=owner)
        title = alias.title
        status, body = await _post(state, _launch_body())
        assert status == 403, body
        assert list(state._slots) == [KEY_ALIAS] and state._slots[KEY_ALIAS] is alias
        assert alias.title == title and alias.folder_id == "" and alias._app == owner
        assert [d[3] for d in denials] == [CASE_ALIAS_DENIED]

    @pytest.mark.asyncio
    async def test_a_transcript_whose_record_cannot_be_read_is_refused(self, tmp_path, denials):
        state = _make_state(tmp_path)
        path = state.conversation_log._path(f"dashboard:{KEY}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\xff\xfe not a record\n")
        status, _ = await _post(state, _launch_body())
        assert status == 403 and state._slots == {}
        assert [d[3] for d in denials] == ["the saved transcript's record is unreadable"]

    @pytest.mark.asyncio
    async def test_a_slot_that_takes_the_key_during_the_creates_awaits_is_refused_at_the_mint(
        self, tmp_path, monkeypatch, denials
    ):
        from kiro_crew.dashboard import chat_handlers

        state = _make_state(tmp_path)
        taken: list[Any] = []
        claim = chat_handlers._app_claim_refused

        async def another_app_takes_the_key(*args: Any, **kwargs: Any) -> bool:
            # Awaited after the gate passed and before the mint: the window the
            # recheck closes.
            slot = state.get_or_create_slot(KEY, app="other-app")
            taken.append((slot, slot.title))
            return await claim(*args, **kwargs)

        monkeypatch.setattr(chat_handlers, "_app_claim_refused", another_app_takes_the_key)
        status, body = await _post(state, _launch_body())
        assert status == 403, body
        assert body["code"] == "app_chat_launch_refused"
        [(slot, title)] = taken
        assert state._slots[KEY] is slot and slot._app == "other-app"
        assert slot.title == title and slot.folder_id == ""
        assert [d[3] for d in denials] == ["the key holds an app-owned slot"]

    @pytest.mark.asyncio
    async def test_a_letter_case_alias_taken_during_the_creates_awaits_is_refused_at_the_mint(
        self, tmp_path, monkeypatch, denials
    ):
        from kiro_crew.dashboard import chat_handlers
        from kiro_crew.dashboard.slot_ownership import CASE_ALIAS_DENIED

        state = _make_state(tmp_path)
        taken: list[Any] = []
        claim = chat_handlers._app_claim_refused

        async def the_app_takes_an_alias(*args: Any, **kwargs: Any) -> bool:
            taken.append(state.get_or_create_slot(KEY_ALIAS, app=APP))
            return await claim(*args, **kwargs)

        monkeypatch.setattr(chat_handlers, "_app_claim_refused", the_app_takes_an_alias)
        status, body = await _post(state, _launch_body())
        assert status == 403, body
        [alias] = taken
        assert list(state._slots) == [KEY_ALIAS] and state._slots[KEY_ALIAS] is alias
        assert [d[3] for d in denials] == [CASE_ALIAS_DENIED]


@pytest.mark.usefixtures("declared")
@pytest.mark.asyncio
async def test_a_bare_launch_defers_its_broadcast_as_the_plain_create_does(tmp_path, monkeypatch):
    """``app_launch`` alone does not move an otherwise plain create onto the synchronous path."""
    state = _make_state(tmp_path)
    scheduled: list[tuple[Any, ...]] = []
    loop = asyncio.get_running_loop()
    real_call_later = loop.call_later

    def capture_deferred_flush(delay, callback, *args, **kwargs):
        if callback == state._deferred_slots_flush:
            scheduled.append(args)
            return MagicMock()
        return real_call_later(delay, callback, *args, **kwargs)

    monkeypatch.setattr(loop, "call_later", capture_deferred_flush)
    status, row = await _post(state, _launch_body(title=None, folder_id=None))
    assert status == 200, row
    assert scheduled == [(f"create slot {KEY!r}", 1)]


class TestOnePrefixOneApp:
    """No app reaches a chat under a prefix another installed app declares."""

    def test_every_other_installed_tree_with_an_overlapping_prefix_holds_it(self, tmp_path):
        _install_tree(tmp_path, APP, DECLARED)
        _install_tree(tmp_path, "impostor-app", DECLARED)
        _install_tree(tmp_path, "wider", {**DECLARED, "keyPrefix": "notes-"})
        _install_tree(tmp_path, "narrower", {**DECLARED, "keyPrefix": "notes-followup-x-"})
        _install_tree(tmp_path, "separate", {**DECLARED, "keyPrefix": "notes-other-"})
        _install_tree(tmp_path, "plain")
        _install_tree(tmp_path, ".impostor-app-update-inflight", DECLARED)
        launch = parse_chat_launch(DECLARED)
        assert launch is not None
        # No installed record is read, so a disabled app's tree holds its prefix too.
        assert chat_launch.overlapping_declarers(APP, launch) == (
            "impostor-app",
            "narrower",
            "wider",
        )

    def test_no_installed_apps_hold_nothing(self, tmp_path):
        launch = parse_chat_launch(DECLARED)
        assert launch is not None
        assert chat_launch.overlapping_declarers(APP, launch) == ()

    @pytest.mark.asyncio
    async def test_an_app_declaring_another_apps_prefix_reaches_none_of_its_chats(
        self, tmp_path, monkeypatch, denials
    ):
        launch = parse_chat_launch(DECLARED)
        monkeypatch.setattr(chat_launch, "declared_chat_launch", lambda _name: launch)
        _install_tree(tmp_path, APP, DECLARED)
        _install_tree(tmp_path, "impostor-app", DECLARED)
        state = _make_state(tmp_path)
        # A chat the built-in page opened: its record names no app.
        _seed_transcript(state, KEY, title="followup-pr#7")

        status, _ = await _post(state, _launch_body(app_launch="impostor-app", title="renamed"))
        assert status == 403 and state._slots == {}
        assert state.conversation_log.get_metadata(f"dashboard:{KEY}")["title"] == "followup-pr#7"
        assert denials[-1][0] == "impostor-app"
        assert denials[-1][3].endswith("overlapping key prefix: notes")

        # Neither app launches while the two overlap ...
        status, _ = await _post(state, _launch_body(title=None))
        assert status == 403 and state._slots == {}
        assert denials[-1][3].endswith("overlapping key prefix: impostor-app")

        # ... and the prefix is the remaining app's once the other is uninstalled.
        shutil.rmtree(tmp_path / "home" / "apps" / "impostor-app")
        status, row = await _post(state, _launch_body(title=None))
        assert status == 200, row
        assert len(denials) == 2


@pytest.mark.asyncio
async def test_a_disabled_or_undeclaring_app_launches_nothing(tmp_path, denials):
    """The real lookup: an app with no enabled record declares nothing."""
    assert chat_launch.declared_chat_launch(APP) is None
    state = _make_state(tmp_path)
    status, _ = await _post(state, _launch_body())
    assert status == 403 and state._slots == {}
