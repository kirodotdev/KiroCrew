"""Pending keystone changes: a hand edit to a sealed file is shown, never silently applied.

Where the namespace sandbox seals the four keystone files, an on-disk file that differs
from its endorsed copy keeps loading as the endorsed copy and is PENDING. The owner
applies or discards it from Settings (``dashboard/handlers/config_pending.py``); one
bell note announces each pending version. These tests pin: pending detection and the
redacted key diff, apply and discard through each leaf's writer, the owner gate and
CSRF barrier, the absence of any agent/MCP path, the boot behaviour, and the note.

The conftest pins ``KIROCREW_HOME`` per test, so ``config_dir()`` is a fresh temp dir.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dashboard_owner_helpers import as_owner

from kiro_crew.config import loosening_provenance as lp
from kiro_crew.config.paths import config_dir
from kiro_crew.dashboard.handlers import config_pending
from kiro_crew.notifications.bus import NotificationBus

pytestmark = pytest.mark.asyncio


def _doc(**keys: object) -> bytes:
    return (json.dumps(keys, indent=2) + "\n").encode("utf-8")


@pytest.fixture(autouse=True)
def enforcing(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    _floor_monkeypatch.setattr(lp, "enforcement_active", lambda: True)


@pytest.fixture(autouse=True)
def quiet_sel():
    with patch("kiro_crew.dashboard.handlers.security._sel") as m:
        m.return_value = MagicMock()
        yield


@pytest.fixture(autouse=True)
def enable_followups(_floor_monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record the spec rebuild and session reset a computer-use enable flip runs,
    without touching the real agent spec or sessions."""
    import kiro_crew.agent as agent_mod
    import kiro_crew.dashboard.handlers.sessions as sessions_mod

    calls: list[str] = []

    async def _reset(_request) -> int:
        calls.append("reset")
        return 0

    _floor_monkeypatch.setattr(
        agent_mod,
        "rebuild_agent_config_reporting",
        lambda **_: (calls.append("rebuild"), (Path("/dev/null"), True))[1],
    )
    _floor_monkeypatch.setattr(sessions_mod, "_reset_all_sessions", _reset)
    return calls


def _seal(leaf: str, endorsed: bytes, disk: bytes) -> Path:
    """An endorsed copy plus a (possibly different) on-disk file for *leaf*."""
    lp.record(leaf, endorsed, strict=True)
    path = config_dir() / leaf
    path.write_bytes(disk)
    return path


class _State:
    """What the routes and the note sync read off the dashboard state."""

    owner_id = ""
    context_builder = None

    def __init__(self) -> None:
        self._notification_log: list[dict] = []
        self.notification_bus = NotificationBus(sink=self._notification_log.append)
        self.ws: list[tuple[str, dict]] = []
        self.rewrites = 0

    async def _rewrite_notifications_async(self) -> None:
        self.rewrites += 1

    async def ack_notification(self, ts: str) -> bool:
        for note in self._notification_log:
            if note.get("ts") == ts:
                note["acked"] = True
                return True
        return False

    def broadcast_ws_owners(self, msg_type: str, data: dict) -> None:
        self.ws.append((msg_type, data))


def _app(state: _State, *middlewares) -> web.Application:
    app = web.Application(middlewares=list(middlewares))
    app["state"] = state
    app.router.add_get("/api/security/config-pending", config_pending.api_config_pending_list)
    app.router.add_post(
        "/api/security/config-pending/{leaf}/apply", config_pending.api_config_pending_apply
    )
    app.router.add_post(
        "/api/security/config-pending/{leaf}/discard", config_pending.api_config_pending_discard
    )
    return as_owner(app)


def _live_notes(state: _State) -> list[dict]:
    return [n for n in state._notification_log if not n.get("acked")]


# ── detection ──


async def test_a_hand_edit_is_pending_and_not_applied() -> None:
    from kiro_crew.config.loader import read_config_for_update

    path = _seal("config.json", _doc(agent={"sandbox": "auto"}), _doc(agent={"sandbox": "off"}))
    [entry] = lp.pending_changes()
    assert entry["leaf"] == "config.json"
    assert entry["disk_valid"] is True
    assert entry["signature"] == lp.disk_signature(path.read_bytes())
    assert entry["changes"] == [
        {"path": ["agent", "sandbox"], "change": "changed", "before": "auto", "after": "off"}
    ]
    # Still loads as the endorsed copy.
    assert read_config_for_update(path)["agent"]["sandbox"] == "auto"


async def test_secret_values_are_withheld_from_the_diff() -> None:
    _seal(
        "config.json",
        _doc(slack={"bot_token": "old"}, agent={"env": {"X": "zero"}}),
        _doc(slack={"bot_token": "new"}, agent={"env": {"X": "one"}}),
    )
    changes = lp.pending_changes()[0]["changes"]
    by_path = {tuple(c["path"]): c for c in changes}
    token = by_path[("slack", "bot_token")]
    assert token["change"] == "changed" and token["redacted"] is True
    assert "before" not in token and "after" not in token
    env = by_path[("agent", "env", "X")]
    assert env["redacted"] is True and "after" not in env
    for value in ("old", "new", "zero", "one"):
        assert value not in json.dumps(changes)


async def test_nothing_is_pending_when_the_copy_matches_or_is_absent() -> None:
    _seal("config.json", _doc(a=1), _doc(a=1))
    (config_dir() / "denied_commands.json").write_bytes(_doc(disable_all=True))  # no copy
    assert lp.pending_changes() == []


async def test_a_differing_endorsed_file_stays_pending() -> None:
    endorsed = _doc(disable_all=False)
    _seal("denied_commands.json", endorsed, _doc(disable_all=True))
    assert lp.endorsed_bytes("denied_commands.json") == endorsed
    state = _State()
    await config_pending.sync_pending_notes(state, 0.0)
    assert [p["leaf"] for p in lp.pending_changes()] == ["denied_commands.json"]
    assert len(_live_notes(state)) == 1


# ── apply / discard through each leaf's writer ──


@pytest.mark.parametrize("leaf", ["config.json", "denied_commands.json", "computer_use.json"])
async def test_apply_endorses_the_reviewed_version(leaf: str) -> None:
    path = _seal(leaf, _doc(enabled=False), _doc(enabled=True))
    sig = lp.disk_signature(path.read_bytes())
    state = _State()
    await config_pending.sync_pending_notes(state, 0.0)
    async with TestClient(TestServer(_app(state))) as client:
        resp = await client.post(
            f"/api/security/config-pending/{leaf}/apply", json={"signature": sig}
        )
        assert resp.status == 200, await resp.text()
        assert (await resp.json())["count"] == 0
    assert json.loads(path.read_bytes())["enabled"] is True
    assert lp.reconcile(leaf, path.read_bytes()) == path.read_bytes()  # takes effect
    assert lp.pending_changes() == []
    assert _live_notes(state) == []
    assert state.ws[-1] == (config_pending.WS_EVENT, {"count": 0})


@pytest.mark.parametrize("leaf", ["config.json", "denied_commands.json", "computer_use.json"])
async def test_discard_restores_the_endorsed_copy(leaf: str) -> None:
    path = _seal(leaf, _doc(enabled=False), _doc(enabled=True))
    sig = lp.disk_signature(path.read_bytes())
    state = _State()
    await config_pending.sync_pending_notes(state, 0.0)
    async with TestClient(TestServer(_app(state))) as client:
        resp = await client.post(
            f"/api/security/config-pending/{leaf}/discard", json={"signature": sig}
        )
        assert resp.status == 200, await resp.text()
    assert json.loads(path.read_bytes())["enabled"] is False
    assert lp.pending_changes() == []
    assert _live_notes(state) == []


async def _apply(leaf: str, path: Path):
    """POST Apply for the on-disk version of *leaf*: ``(status, body)``."""
    async with TestClient(TestServer(_app(_State()))) as client:
        resp = await client.post(
            f"/api/security/config-pending/{leaf}/apply",
            json={"signature": lp.disk_signature(path.read_bytes())},
        )
        return resp.status, await resp.json()


async def test_applying_config_local_writes_exactly_the_reviewed_document() -> None:
    """Only config.json carries the meta block; the applied overlay is the reviewed one."""
    reviewed = {"agent": {"model": "next"}}
    path = _seal("config.local.json", _doc(agent={"model": "prev"}), _doc(**reviewed))
    status, body = await _apply("config.local.json", path)
    assert status == 200, body
    assert json.loads(path.read_bytes()) == reviewed


async def _assert_refused(leaf: str, endorsed: bytes, disk: bytes, reason: str) -> None:
    """Apply answers 409 ``write_refused`` naming *reason*; nothing moves."""
    path = _seal(leaf, endorsed, disk)
    status, body = await _apply(leaf, path)
    assert status == 409, body
    assert body["code"] == "write_refused"
    assert reason in body["error"]
    assert path.read_bytes() == disk
    assert lp.endorsed_bytes(leaf) == endorsed
    assert [p["leaf"] for p in lp.pending_changes()] == [leaf]


@pytest.mark.parametrize("leaf", ["config.json", "config.local.json"])
@pytest.mark.parametrize(
    "agent,reason",
    [
        ({"deepseek_env": {"DEEPSEEK_API_KEY": "sk-plain"}}, "agent.deepseek_env"),
        ({"env": {"PATH": "/x"}}, "agent.env refuses"),
    ],
    ids=["plaintext_provider_key", "env_entry_no_agent_receives"],
)
async def test_apply_runs_the_config_publish_floor(leaf: str, agent: dict, reason: str) -> None:
    """The floor every config write lands through judges the edit against the version
    in force, so a value only the pending file holds is one this Apply introduces."""
    await _assert_refused(leaf, _doc(agent={}), _doc(agent=agent), reason)


@pytest.mark.parametrize(
    "edited,reason",
    [
        ({"user_added": [{"id": "user-1", "pattern": "[", "enabled": True}]}, "invalid regex"),
        ({"user_added": [{"id": "user-1", "pattern": "(a+)+$"}]}, "catastrophic-backtracking"),
        ({"user_added": [{"pattern": "rm"}]}, "non-empty string id"),
        ({"user_added": [{"id": "user-1", "pattern": "rm", "note": 3}]}, "note must be a string"),
        ({"disable_all": "yes"}, "disable_all must be a boolean"),
        ({"disabled_ids": ["git-publish-push-brace-expansion-refspec"]}, "always-on"),
    ],
    ids=["bad_regex", "redos", "no_id", "note_type", "disable_all_type", "floor_rule"],
)
async def test_apply_runs_the_denied_command_save_rules(edited: dict, reason: str) -> None:
    await _assert_refused(
        "denied_commands.json",
        _doc(disable_all=False),
        _doc(**{"disable_all": False, **edited}),
        reason,
    )


async def test_a_valid_denied_command_edit_applies_with_its_note_collapsed() -> None:
    rule = {"id": "user-1", "pattern": "^rm -rf", "enabled": True, "note": "no\nwipes"}
    path = _seal("denied_commands.json", _doc(disable_all=False), _doc(user_added=[rule]))
    status, body = await _apply("denied_commands.json", path)
    assert status == 200, body
    [stored] = json.loads(path.read_bytes())["user_added"]
    assert stored == {**rule, "note": "no wipes"}
    assert lp.pending_changes() == []


@pytest.mark.parametrize(
    "edited,reason",
    [
        ({"allowed_apps": "Safari"}, "allowed_apps must be a list"),
        ({"extra_denied_apps": [1]}, "extra_denied_apps must be a list"),
        ({"enabled": "true"}, "enabled must be a boolean"),
    ],
    ids=["allowed_apps_not_list", "denied_apps_entry", "enabled_type"],
)
async def test_apply_runs_the_computer_use_put_validation(edited: dict, reason: str) -> None:
    await _assert_refused("computer_use.json", _doc(enabled=False), _doc(**edited), reason)


async def test_a_valid_computer_use_edit_applies_as_the_put_stores_it() -> None:
    path = _seal("computer_use.json", _doc(enabled=False), _doc(allowed_apps=[" Safari "]))
    status, body = await _apply("computer_use.json", path)
    assert status == 200, body
    assert json.loads(path.read_bytes()) == {"allowed_apps": ["safari"]}
    assert lp.pending_changes() == []


async def test_apply_refuses_a_version_the_owner_did_not_review() -> None:
    path = _seal("config.json", _doc(a=1), _doc(a=2))
    state = _State()
    async with TestClient(TestServer(_app(state))) as client:
        resp = await client.post(
            "/api/security/config-pending/config.json/apply",
            json={"signature": lp.disk_signature(_doc(a=3))},
        )
        assert resp.status == 409
        assert (await resp.json())["code"] == "pending_changed"
    assert path.read_bytes() == _doc(a=2)
    assert lp.endorsed_bytes("config.json") == _doc(a=1)


# ── who may call it ──


@pytest.mark.parametrize(
    "method,route",
    [
        ("GET", "/api/security/config-pending"),
        ("POST", "/api/security/config-pending/config.json/apply"),
        ("POST", "/api/security/config-pending/config.json/discard"),
    ],
)
@pytest.mark.parametrize(
    "headers",
    [{"X-Test-User": "U0NONOWNER"}, {"X-Test-App": "some-app"}],
    ids=["non_owner", "app_token"],
)
async def test_routes_refuse_a_caller_who_is_not_the_owner(method, route, headers) -> None:
    path = _seal("config.json", _doc(a=1), _doc(a=2))
    sig = lp.disk_signature(path.read_bytes())
    async with TestClient(TestServer(_app(_State()))) as client:
        resp = await client.request(method, route, json={"signature": sig}, headers=headers)
        assert resp.status == 403
        assert (await resp.json())["code"] == "owner_only"
    assert lp.endorsed_bytes("config.json") == _doc(a=1)


async def test_an_internal_secret_caller_is_refused() -> None:
    """An agent's MCP tools reach the API with ``X-Internal-Secret``: ``app`` is absent."""

    @web.middleware
    async def _internal(request, handler):
        request["internal_auth"] = True
        request["user"] = "local-app"
        return await handler(request)

    path = _seal("config.json", _doc(a=1), _doc(a=2))
    app = web.Application(middlewares=[_internal])
    app["state"] = _State()
    app.router.add_post(
        "/api/security/config-pending/{leaf}/apply", config_pending.api_config_pending_apply
    )
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/api/security/config-pending/config.json/apply",
            json={"signature": lp.disk_signature(path.read_bytes())},
        )
        assert resp.status == 403
    assert lp.endorsed_bytes("config.json") == _doc(a=1)


async def test_the_writes_sit_behind_the_csrf_barrier() -> None:
    from kiro_crew.dashboard import server as server_mod
    from kiro_crew.dashboard.token_auth_paths import is_csrf_exempt

    for verb in ("apply", "discard"):
        assert not is_csrf_exempt(f"/api/security/config-pending/config.json/{verb}", "POST")
    path = _seal("config.json", _doc(a=1), _doc(a=2))
    app = _app(_State(), server_mod._make_csrf_middleware("dashboard_user"))
    app["allowed_origins"] = {"http://127.0.0.1"}
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/api/security/config-pending/config.json/apply",
            json={"signature": lp.disk_signature(path.read_bytes())},
            headers={"Origin": "https://evil.example"},
        )
        assert resp.status == 403
    assert lp.endorsed_bytes("config.json") == _doc(a=1)


async def test_no_agent_or_mcp_tool_names_these_routes() -> None:
    """The routes and the handler are referenced only by the dashboard's own wiring."""
    src = Path(config_pending.__file__).resolve().parents[2]
    allowed = {
        "dashboard/handlers/config_pending.py",
        "dashboard/handlers/__init__.py",
        "dashboard/routes/system.py",
        "dashboard/server_runtime/heartbeat.py",
        "config/loosening_provenance.py",
        "slack/gateway_runtime/service_boot.py",  # a comment naming the module
    }
    referencing = set()
    for file in src.rglob("*.py"):
        text = file.read_text(encoding="utf-8", errors="replace")
        if "config-pending" in text or "config_pending" in text:
            referencing.add(file.relative_to(src).as_posix())
    assert referencing <= allowed, sorted(referencing - allowed)
    assert {"dashboard/handlers/config_pending.py", "dashboard/routes/system.py"} <= referencing

    from kiro_crew.dashboard import server_internal_paths as paths

    for name in ("_STRICT_INTERNAL_API_PATHS", "_MIXED_INTERNAL_API_PATHS"):
        assert not any("config-pending" in p for p in getattr(paths, name, ()))


# ── bell note + nav count ──


async def test_going_pending_notifies_once_per_version() -> None:
    path = _seal("config.json", _doc(a=1), _doc(a=2))
    state = _State()
    await config_pending.sync_pending_notes(state, 0.0)
    await config_pending.sync_pending_notes(state, 100.0)
    [note] = _live_notes(state)
    assert note["url"] == config_pending.SETTINGS_URL
    assert note["config_pending_leaf"] == "config.json"
    assert state.ws == [(config_pending.WS_EVENT, {"count": 1})]

    path.write_bytes(_doc(a=3))  # a new edit notifies again and supersedes the old note
    await config_pending.sync_pending_notes(state, 200.0)
    [newer] = _live_notes(state)
    assert newer is not note and len(state._notification_log) == 2


async def test_a_deleted_note_is_not_sent_again_for_the_same_version() -> None:
    """The owner deleting the note (or the log cap dropping it) is not a new edit."""
    _seal("config.json", _doc(a=1), _doc(a=2))
    state = _State()
    pushed: list[dict] = []
    # Delivered but never kept in the log: as if the owner deleted each note at once.
    state.notification_bus = NotificationBus(sink=pushed.append)
    await config_pending.sync_pending_notes(state, 0.0)
    assert len(pushed) == 1
    await config_pending.sync_pending_notes(state, 100.0)
    await config_pending.sync_pending_notes(state, 200.0)
    assert len(pushed) == 1


async def test_the_same_version_going_pending_again_after_a_resolve_is_notified() -> None:
    """A resolved note answers for a version that stopped being pending; the same bytes
    going pending again later (a file removed twice, one edit re-applied) is new."""
    path = _seal("config.json", _doc(a=1), _doc(a=2))
    state = _State()
    await config_pending.sync_pending_notes(state, 0.0)
    [first] = _live_notes(state)

    path.write_bytes(_doc(a=1))  # put back by hand: nothing is pending
    await config_pending.sync_pending_notes(state, 100.0)
    assert _live_notes(state) == []

    path.write_bytes(_doc(a=2))  # the very same edit, made again
    await config_pending.sync_pending_notes(state, 200.0)
    [again] = _live_notes(state)
    assert again is not first
    assert again["config_pending_leaf"] == "config.json"


async def test_a_failed_resolve_write_is_retried_on_the_next_sync() -> None:
    path = _seal("config.json", _doc(a=1), _doc(a=2))
    state = _State()
    await config_pending.sync_pending_notes(state, 0.0)
    writes: list[bool] = []

    async def disk_full_once() -> bool:
        writes.append(True)
        return len(writes) > 1  # the first rewrite fails, later ones land

    state._rewrite_notifications_async = disk_full_once  # type: ignore[method-assign]
    path.write_bytes(_doc(a=1))  # resolved: its mark must reach the log
    await config_pending.sync_pending_notes(state, 100.0)
    assert writes == [True]
    await config_pending.sync_pending_notes(state, 200.0)
    assert writes == [True, True], "the failed write is tried again"
    await config_pending.sync_pending_notes(state, 300.0)
    assert writes == [True, True], "and not after it landed"


async def test_a_read_note_still_pending_is_not_sent_again() -> None:
    _seal("config.json", _doc(a=1), _doc(a=2))
    state = _State()
    await config_pending.sync_pending_notes(state, 0.0)
    [note] = _live_notes(state)
    note["acked"] = True  # the owner read it; the edit is still pending
    await config_pending.sync_pending_notes(state, 100.0)
    assert len(state._notification_log) == 1


async def test_a_stalled_note_write_does_not_hold_the_heartbeat(monkeypatch) -> None:
    """The bound covers the note writes, not only the scan; a stalled sync is not doubled."""
    import asyncio

    from kiro_crew.testing.wait import await_until, finished

    _seal("config.json", _doc(a=1), _doc(a=2))
    release = asyncio.Event()
    publishing = asyncio.Event()
    entered: list[int] = []

    async def stalled_publish(state: object, pending: list[dict]) -> None:
        entered.append(1)
        publishing.set()
        await release.wait()

    monkeypatch.setattr(config_pending, "_publish", stalled_publish)
    # The publish never returns, so the bound always expires: its length decides nothing.
    monkeypatch.setattr(config_pending, "_SYNC_TIMEOUT_SECS", 0.05)  # flake-ok: always expires
    state = _State()
    try:
        await finished(config_pending.sync_pending_notes(state, 0.0), "the first sync")
        # The first sync may return before its task reaches the publish (the scan runs in
        # a thread first); wait for the publish itself, so the second sync meets it stalled.
        await await_until(publishing, what="the stalled publish")
        stalled = getattr(state, config_pending._SYNC_TASK_ATTR)
        await finished(config_pending.sync_pending_notes(state, 100.0), "the second sync")
        # The second beat started no sync of its own while the first is stalled.
        assert getattr(state, config_pending._SYNC_TASK_ATTR) is stalled
        assert entered == [1]
    finally:
        release.set()
        task = getattr(state, config_pending._SYNC_TASK_ATTR, None)
        if task is not None:
            await await_until(task, what="the sync task")


async def test_muting_every_mutable_channel_does_not_silence_the_note() -> None:
    """The edit is not in effect until the owner decides, so the note is never muted."""
    from kiro_crew.notifications.bus import SYSTEM_CHANNELS
    from kiro_crew.notifications.settings import PROTECTED_CHANNELS, ChannelSettings

    settings = ChannelSettings()
    for channel in set(SYSTEM_CHANNELS) - PROTECTED_CHANNELS:
        settings.update(channel, muted=True)
    _seal("config.json", _doc(a=1), _doc(a=2))
    state = _State()
    # The delivery sink applies the user's channel settings, as the dashboard's does.
    state.notification_bus = NotificationBus(
        sink=lambda note: state._notification_log.append(settings.apply(note))
    )
    await config_pending.sync_pending_notes(state, 0.0)
    [note] = _live_notes(state)
    assert note["channel"] in PROTECTED_CHANNELS
    assert not note.get("silenced") and note["priority"] != "passive"


async def test_a_meta_forged_note_does_not_suppress_the_real_one() -> None:
    path = _seal("config.json", _doc(a=1), _doc(a=2))
    state = _State()
    from kiro_crew.notifications.bus import NotificationPayload

    state.notification_bus.push(
        NotificationPayload(
            source="system",
            channel="system.agent",
            title="x",
            body="y",
            meta={
                config_pending.NOTE_KEY: {
                    "leaf": "config.json",
                    "signature": lp.disk_signature(path.read_bytes()),
                }
            },
        )
    )
    await config_pending.sync_pending_notes(state, 0.0)
    assert len(state._notification_log) == 2


async def test_a_resolved_leaf_clears_its_note_on_the_next_sync() -> None:
    path = _seal("config.json", _doc(a=1), _doc(a=2))
    state = _State()
    await config_pending.sync_pending_notes(state, 0.0)
    path.write_bytes(_doc(a=1))  # reverted by hand
    await config_pending.sync_pending_notes(state, 100.0)
    assert _live_notes(state) == []
    assert state.ws[-1] == (config_pending.WS_EVENT, {"count": 0})


async def test_editing_a_pending_file_again_tells_the_dashboard() -> None:
    """Same count, new version: the card must re-read, or it keeps the old diff."""
    path = _seal("config.json", _doc(a=1), _doc(a=2))
    state = _State()
    await config_pending.sync_pending_notes(state, 0.0)
    path.write_bytes(_doc(a=3))
    await config_pending.sync_pending_notes(state, 100.0)
    assert state.ws == [(config_pending.WS_EVENT, {"count": 1})] * 2
    await config_pending.sync_pending_notes(state, 200.0)  # nothing new: no event
    assert len(state.ws) == 2


# ── a file that is not valid JSON, or was deleted, can always be discarded ──


async def _resolve(verb: str, leaf: str, signature: str):
    async with TestClient(TestServer(_app(_State()))) as client:
        resp = await client.post(
            f"/api/security/config-pending/{leaf}/{verb}", json={"signature": signature}
        )
        return resp.status, await resp.json()


@pytest.mark.parametrize("leaf", ["config.json", "denied_commands.json", "computer_use.json"])
async def test_discard_restores_an_edit_that_is_not_valid_json(leaf: str) -> None:
    path = _seal(leaf, _doc(enabled=False), b"{not json")
    [entry] = lp.pending_changes()
    assert entry["disk_valid"] is False
    status, body = await _resolve("discard", leaf, entry["signature"])
    assert status == 200, body
    assert json.loads(path.read_bytes())["enabled"] is False
    assert lp.pending_changes() == []


@pytest.mark.parametrize("leaf", ["config.json", "denied_commands.json", "computer_use.json"])
async def test_a_deleted_file_is_put_back_by_discard_and_cannot_be_applied(leaf: str) -> None:
    path = _seal(leaf, _doc(enabled=False), b"")
    path.unlink()
    [entry] = lp.pending_changes()
    assert entry["removed"] is True
    status, body = await _resolve("apply", leaf, entry["signature"])
    assert status == 409 and body["code"] == "pending_removed", body
    assert not path.exists()
    status, body = await _resolve("discard", leaf, lp.REMOVED_SIGNATURE)
    assert status == 200, body
    assert json.loads(path.read_bytes())["enabled"] is False
    assert lp.pending_changes() == []


async def test_a_refused_agent_env_edit_says_what_to_do_first() -> None:
    path = _seal("config.json", _doc(agent={}), _doc(agent={"env": {"LD_PRELOAD": "/x.so"}}))
    status, body = await _apply("config.json", path)
    assert status == 409 and body["code"] == "write_refused"
    assert body["error"].startswith("Remove or rename 'LD_PRELOAD' in agent.env"), body
    assert "/x.so" not in body["error"]


# ── type-strict comparison, deep nesting, off-loop snapshot ──


async def test_true_becoming_1_is_a_change_and_apply_judges_it() -> None:
    """``true`` -> ``1`` loads as a different value, so it is shown and validated."""
    path = _seal("computer_use.json", _doc(enabled=True), _doc(enabled=1))
    [entry] = lp.pending_changes()
    assert entry["changes"] == [
        {"path": ["enabled"], "change": "changed", "before": True, "after": 1}
    ]
    status, body = await _apply("computer_use.json", path)
    assert status == 409, body
    assert "enabled must be a boolean" in body["error"]


async def test_a_denied_disable_all_of_1_is_refused() -> None:
    await _assert_refused(
        "denied_commands.json",
        _doc(disable_all=True),
        _doc(disable_all=1),
        "disable_all must be a boolean",
    )


async def test_a_custom_rule_whose_enabled_became_1_is_validated_as_changed() -> None:
    rule = {"id": "user-1", "pattern": "rm", "enabled": True}
    await _assert_refused(
        "denied_commands.json",
        _doc(user_added=[rule]),
        _doc(user_added=[{**rule, "enabled": 1}]),
        "enabled must be a boolean",
    )


async def test_a_deeply_nested_file_is_listed_as_invalid_and_the_rest_still_lists() -> None:
    deep = ("[" * 10000 + "]" * 10000).encode()
    _seal("config.local.json", _doc(a=1), deep)
    _seal("config.json", _doc(a=1), _doc(a=2))
    async with TestClient(TestServer(_app(_State()))) as client:
        resp = await client.get("/api/security/config-pending")
        assert resp.status == 200, await resp.text()
        body = await resp.json()
    by_leaf = {p["leaf"]: p for p in body["pending"]}
    assert by_leaf["config.local.json"]["disk_valid"] is False
    assert by_leaf["config.local.json"]["changes"] == []
    assert by_leaf["config.json"]["disk_valid"] is True


async def test_a_document_past_the_nesting_bound_cannot_be_applied() -> None:
    from kiro_crew.user_json import MAX_DOCUMENT_NESTING

    nested: object = 1
    for _ in range(MAX_DOCUMENT_NESTING + 1):
        nested = {"k": nested}
    path = _seal("config.json", _doc(a=1), _doc(deep=nested))
    assert lp.pending_changes()[0]["disk_valid"] is False
    status, body = await _apply("config.json", path)
    assert (status, body["code"]) == (409, "pending_invalid")


async def test_the_snapshot_probes_the_host_off_the_event_loop(monkeypatch) -> None:
    import threading

    loop_thread = threading.get_ident()
    probed_on: list[int] = []

    def _enforcing() -> bool:
        probed_on.append(threading.get_ident())
        return True

    monkeypatch.setattr(lp, "enforcement_active", _enforcing)
    path = _seal("config.json", _doc(a=1), _doc(a=2))
    async with TestClient(TestServer(_app(_State()))) as client:
        assert (await client.get("/api/security/config-pending")).status == 200
        resp = await client.post(
            "/api/security/config-pending/config.json/discard",
            json={"signature": lp.disk_signature(path.read_bytes())},
        )
        assert resp.status == 200, await resp.text()
    assert probed_on and loop_thread not in probed_on


# ── bounds and ordering of the listed changes ──


async def test_a_flood_of_other_keys_cannot_push_a_sandbox_change_off_the_list() -> None:
    junk = {f"a{i:04d}": i for i in range(150)}  # all sort before "agent"
    _seal("config.json", _doc(agent={"sandbox": "auto"}), _doc(**junk, agent={"sandbox": "off"}))
    [entry] = lp.pending_changes()
    assert entry["truncated"] is True
    assert len(entry["changes"]) == lp._MAX_CHANGES
    assert entry["changes"][0] == {
        "path": ["agent", "sandbox"],
        "change": "changed",
        "before": "auto",
        "after": "off",
    }


@pytest.mark.parametrize(
    "key", ["dangerously_skip_permissions", "dangerouslySkipPermissions", "yolo"]
)
async def test_a_flood_of_other_keys_cannot_push_an_approve_every_tool_grant_off_the_list(
    key,
) -> None:
    junk = {f"a{i:04d}": i for i in range(150)}  # all sort before "agent"
    _seal("config.json", _doc(agent={}), _doc(**junk, agent={key: True}))
    [entry] = lp.pending_changes()
    assert entry["truncated"] is True
    assert entry["changes"][0]["path"] == ["agent", key]


async def test_a_flood_under_one_security_key_cannot_push_another_off_the_list() -> None:
    env = {f"V{i:04d}": "x" for i in range(150)}  # agent.env sorts before sandbox, yolo
    _seal(
        "config.json",
        _doc(agent={"env": {}, "sandbox": "auto"}),
        _doc(agent={"env": env, "sandbox": "off", "yolo": True}),
    )
    [entry] = lp.pending_changes()
    paths = [c["path"] for c in entry["changes"]]
    assert ["agent", "sandbox"] in paths and ["agent", "yolo"] in paths
    assert entry["truncated"] is True
    assert len(entry["changes"]) <= lp._MAX_CHANGES


async def test_huge_keys_are_listed_with_a_bounded_path_and_response() -> None:
    big = {f"{i:03d}" + "k" * (1 << 20): 1 for i in range(lp._MAX_CHANGES + 1)}
    _seal("config.json", _doc(a=1), _doc(a=1, **big))
    async with TestClient(TestServer(_app(_State()))) as client:
        resp = await client.get("/api/security/config-pending")
        assert resp.status == 200
        text = await resp.text()
    [entry] = json.loads(text)["pending"]
    assert entry["truncated"] is True
    assert len(entry["changes"]) == lp._MAX_CHANGES
    assert all(len("".join(c["path"])) <= lp._MAX_SHOWN_PATH for c in entry["changes"])
    assert len(text) < 64 * 1024


@pytest.mark.parametrize("number", ["1e400", "-1e400", "NaN", "Infinity"])
async def test_a_number_json_cannot_spell_lists_as_invalid_and_discards(number: str) -> None:
    disk = ('{"x": ' + number + "}").encode()
    path = _seal("config.json", _doc(x=1), disk)

    def _strict(name: str) -> object:
        raise ValueError(name)

    async with TestClient(TestServer(_app(_State()))) as client:
        resp = await client.get("/api/security/config-pending")
        assert resp.status == 200
        [entry] = json.loads(await resp.text(), parse_constant=_strict)["pending"]
        assert entry["disk_valid"] is False
        sig = lp.disk_signature(disk)
        resp = await client.post(
            "/api/security/config-pending/config.json/apply", json={"signature": sig}
        )
        assert (resp.status, (await resp.json())["code"]) == (409, "pending_invalid")
        resp = await client.post(
            "/api/security/config-pending/config.json/discard", json={"signature": sig}
        )
        assert resp.status == 200, await resp.text()
    assert json.loads(path.read_bytes())["x"] == 1
    assert lp.pending_changes() == []


# ── a computer-use enable flip reaches the agent spec ──


@pytest.mark.parametrize(
    "endorsed,disk,expected",
    [
        ({"enabled": False}, {"enabled": True}, ["rebuild", "reset"]),
        ({"enabled": False}, {"enabled": False, "allowed_apps": ["safari"]}, []),
    ],
    ids=["enable_flip", "no_flip"],
)
async def test_applying_an_enable_flip_rebuilds_the_spec_and_resets_sessions(
    enable_followups: list[str], endorsed: dict, disk: dict, expected: list[str]
) -> None:
    path = _seal("computer_use.json", _doc(**endorsed), _doc(**disk))
    status, body = await _apply("computer_use.json", path)
    assert status == 200, body
    assert enable_followups == expected


async def test_the_listed_first_keys_are_the_keys_the_settings_card_words() -> None:
    import re

    source = (
        Path(__file__).resolve().parents[1]
        / "website/src/pages/settings/SecurityPanelConfigPending.tsx"
    ).read_text(encoding="utf-8")
    kinds = {"config.json": "config", "denied_commands.json": "denied"}
    kinds["computer_use.json"] = "computerUse"
    rules = set(re.findall(r"file: '(\w+)',\s*key: '([\w.]+)'", source))
    assert rules, "SECURITY_RULES not found"
    server = {
        (kinds[leaf], key)
        for leaf, keys in lp._SECURITY_KEYS.items()
        if leaf in kinds
        for key in keys
    }
    assert server == rules
    assert lp._SECURITY_KEYS["config.local.json"] == lp._SECURITY_KEYS["config.json"]
