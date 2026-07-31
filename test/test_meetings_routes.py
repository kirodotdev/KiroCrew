"""HTTP route tests: the full meeting lifecycle over the real aiohttp router.

Lives in the repo-level ``test/`` tree (not the app's in-package ``tests/``)
because ``setup.cfg`` sets ``testpaths = test transfer`` — a test under
``src/kiro_crew/apps/builtins/...`` is never collected by CI.

Covers the request contract every frontend call depends on, plus the input
validation and redaction the AUTOSDE ``backend-security-controls`` rule requires.
Agent dispatch always goes through the fake session manager; nothing spawns a
process or opens a socket.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from meetings_helpers import (  # noqa: F401
    app_fixture,
    client_for,
    enabled_fixture,
    fake_sessions_fixture,
    make_app,
    reset_module_state_fixture,
    root_fixture,
)

from kiro_crew.apps.builtins.meetings.backend import constants as k
from kiro_crew.apps.builtins.meetings.backend import store
from kiro_crew.apps.builtins.meetings.backend.routes import _common

BASE = k.API_BASE


async def _start(client, meeting_id: str = "standup", **body) -> dict:
    await client.post(f"{BASE}/meetings/{meeting_id}/init", json={"title": "Standup"})
    resp = await client.post(f"{BASE}/meetings/{meeting_id}/start", json=body)
    assert resp.status == 200, await resp.text()
    return await resp.json()


class TestAuthorizationGate:
    @pytest.mark.asyncio
    async def test_disabled_app_denies_every_route(self, root: Path, monkeypatch):
        """Deny-by-default: routes are registered at startup, so a
        default-disabled app must refuse at request time."""
        monkeypatch.setattr(_common, "is_app_enabled", lambda _name: False)
        async with client_for(make_app(root)) as client:
            for method, path in (
                ("get", f"{BASE}/config"),
                ("get", f"{BASE}/meetings"),
                ("get", f"{BASE}/status"),
                ("post", f"{BASE}/meetings/x/init"),
                ("post", f"{BASE}/calendar/sync"),
            ):
                resp = await getattr(client, method)(path, json={})
                assert resp.status == 403, f"{method} {path} was not denied"
                assert "disabled" in (await resp.json())["error"]


class TestConfigRoutes:
    @pytest.mark.asyncio
    async def test_get_config_includes_provider_catalogs(self, app: web.Application):
        async with client_for(app) as client:
            resp = await client.get(f"{BASE}/config")
            assert resp.status == 200
            body = await resp.json()
            assert body["config"]["task_provider"] == k.TASK_PROVIDER_LOCAL
            assert {r["id"] for r in body["task_providers"]} >= {k.TASK_PROVIDER_LOCAL}
            assert {r["id"] for r in body["calendar_providers"]} >= {k.CALENDAR_PROVIDER_ICS}

    @pytest.mark.asyncio
    async def test_put_config_roundtrips_allowed_fields(self, app, root: Path):
        async with client_for(app) as client:
            resp = await client.put(
                f"{BASE}/config",
                json={
                    "config": {
                        "task_provider": k.TASK_PROVIDER_LOCAL,
                        "calendar": {"provider": k.CALENDAR_PROVIDER_ICS, "source": "/tmp/c.ics"},
                        "poll_interval_active": 2500,
                        "meeting_agents": [
                            {"id": "note-taker", "name": "Notes", "widget_type": "markdown"}
                        ],
                    }
                },
            )
            assert resp.status == 200
            saved = (await resp.json())["config"]
            assert saved["calendar"]["provider"] == k.CALENDAR_PROVIDER_ICS
            assert saved["poll_interval_active"] == 2500
        assert store.read_config(root)["calendar"]["source"] == "/tmp/c.ics"

    @pytest.mark.asyncio
    async def test_put_config_rejects_unknown_providers(self, app):
        async with client_for(app) as client:
            resp = await client.put(
                f"{BASE}/config",
                json={
                    "config": {
                        "task_provider": "corporate-tracker",
                        "calendar": {"provider": "corporate-calendar"},
                    }
                },
            )
            saved = (await resp.json())["config"]
            # An unregistered id would name a provider that cannot resolve; it is
            # collapsed to the default rather than persisted.
            assert saved["task_provider"] == k.TASK_PROVIDER_LOCAL
            assert saved["calendar"]["provider"] == k.CALENDAR_PROVIDER_NONE

    @pytest.mark.asyncio
    async def test_put_config_drops_agent_with_unsafe_id(self, app):
        async with client_for(app) as client:
            resp = await client.put(
                f"{BASE}/config",
                json={
                    "config": {
                        "meeting_agents": [
                            {"id": "../../evil", "name": "Evil"},
                            {"id": "note-taker", "name": "Notes"},
                        ]
                    }
                },
            )
            ids = [a["id"] for a in (await resp.json())["config"]["meeting_agents"]]
            assert ids == ["note-taker"]

    @pytest.mark.asyncio
    async def test_put_config_sanitizes_agent_reference(self, app):
        async with client_for(app) as client:
            resp = await client.put(
                f"{BASE}/config",
                json={
                    "config": {
                        "meeting_agents": [
                            {"id": "note-taker", "agent": "../../../etc/passwd"},
                        ]
                    }
                },
            )
            assert (await resp.json())["config"]["meeting_agents"][0]["agent"] == ""

    @pytest.mark.asyncio
    async def test_put_config_empty_agents_falls_back_to_defaults(self, app):
        async with client_for(app) as client:
            resp = await client.put(f"{BASE}/config", json={"config": {"meeting_agents": []}})
            ids = [a["id"] for a in (await resp.json())["config"]["meeting_agents"]]
            assert ids == ["note-taker", "sketch-artist"]

    @pytest.mark.asyncio
    async def test_put_config_rejects_non_object(self, app):
        async with client_for(app) as client:
            resp = await client.put(f"{BASE}/config", json={"config": "nope"})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_put_config_clamps_poll_intervals(self, app):
        async with client_for(app) as client:
            resp = await client.put(
                f"{BASE}/config",
                json={"config": {"poll_interval_active": 1, "poll_interval_idle": 99_999_999}},
            )
            saved = (await resp.json())["config"]
            assert saved["poll_interval_active"] == 1000
            assert saved["poll_interval_idle"] == 600_000

    @pytest.mark.asyncio
    async def test_put_config_drops_default_preset_that_does_not_exist(self, app):
        async with client_for(app) as client:
            resp = await client.put(
                f"{BASE}/config", json={"config": {"default_preset": "ghost"}}
            )
            assert (await resp.json())["config"]["default_preset"] == ""

    @pytest.mark.asyncio
    async def test_put_config_invalid_json_is_400(self, app):
        async with client_for(app) as client:
            resp = await client.put(
                f"{BASE}/config",
                data="{not json",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400


class TestDictionaryRoutes:
    @pytest.mark.asyncio
    async def test_add_list_remove(self, app, root: Path):
        async with client_for(app) as client:
            resp = await client.post(
                f"{BASE}/dictionary", json={"correct": "DynamoDB", "aliases": ["dynamo db"]}
            )
            assert resp.status == 200
            # Added alongside the seeded starter terms, not replacing them.
            assert "DynamoDB" in {t["correct"] for t in (await resp.json())["terms"]}

            resp = await client.get(f"{BASE}/dictionary")
            assert any(t["correct"] == "DynamoDB" for t in (await resp.json())["terms"])

            resp = await client.post(f"{BASE}/dictionary/remove", json={"correct": "DynamoDB"})
            assert resp.status == 200
            assert "DynamoDB" not in {t["correct"] for t in (await resp.json())["terms"]}

        assert "DynamoDB" not in store.dictionary_path(root).read_text()

    @pytest.mark.asyncio
    async def test_add_requires_correct_and_aliases(self, app):
        async with client_for(app) as client:
            assert (await client.post(f"{BASE}/dictionary", json={"aliases": ["x"]})).status == 400
            assert (
                await client.post(f"{BASE}/dictionary", json={"correct": "X", "aliases": []})
            ).status == 400

    @pytest.mark.asyncio
    async def test_remove_unknown_is_404(self, app):
        async with client_for(app) as client:
            resp = await client.post(f"{BASE}/dictionary/remove", json={"correct": "Ghost"})
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_reload_reports_count(self, app, root: Path):
        store.dictionary_path(root).write_text(
            '[[term]]\ncorrect = "X"\naliases = ["ex"]\n'
        )
        async with client_for(app) as client:
            resp = await client.post(f"{BASE}/dictionary/reload")
            assert (await resp.json())["count"] == 1


class TestMeetingLifecycleRoutes:
    @pytest.mark.asyncio
    async def test_init_creates_the_folder_and_files(self, app, root: Path):
        async with client_for(app) as client:
            resp = await client.post(
                f"{BASE}/meetings/standup/init", json={"title": "My Standup"}
            )
            assert resp.status == 200
        mdir = store.meeting_dir("standup", root)
        assert (mdir / k.SESSION_META_FILE).is_file()
        assert (mdir / k.TASKS_FILE).is_file()
        assert (mdir / "note-taker.md").is_file()
        assert json.loads((mdir / k.SESSION_META_FILE).read_text())["title"] == "My Standup"

    @pytest.mark.asyncio
    async def test_init_is_idempotent(self, app, root: Path):
        async with client_for(app) as client:
            await client.post(f"{BASE}/meetings/standup/init", json={"title": "First"})
            await client.post(f"{BASE}/meetings/standup/init", json={"title": "Second"})
        meta = store.read_meeting_meta("standup", root)
        assert meta is not None and meta["title"] == "First"

    @pytest.mark.asyncio
    async def test_init_rejects_a_traversal_id(self, app):
        async with client_for(app) as client:
            resp = await client.post(f"{BASE}/meetings/..%2F..%2Fetc/init", json={})
            assert resp.status in (400, 403, 404)

    @pytest.mark.asyncio
    async def test_init_accepts_a_colon_id(self, app, root: Path):
        async with client_for(app) as client:
            resp = await client.post(f"{BASE}/meetings/evt%3A123/init", json={})
            assert resp.status == 200
            assert (await resp.json())["meeting_id"] == "evt_123"

    @pytest.mark.asyncio
    async def test_start_activates_and_inits_agents(self, app, root: Path, fake_sessions):
        async with client_for(app) as client:
            body = await _start(client)
            assert body["status"] == k.STATUS_ACTIVE
            assert set(body["agents"]) == {"note-taker", "sketch-artist"}
        # One kickoff prompt per agent plus the always-on task extractor.
        assert len(fake_sessions.calls) == 3
        assert all("OUTPUT_FILE:" in msg for _k, _a, msg in fake_sessions.calls)

    @pytest.mark.asyncio
    async def test_start_with_agent_filter(self, app, fake_sessions):
        async with client_for(app) as client:
            body = await _start(client, agents_enabled=["note-taker"])
            assert body["agents"] == ["note-taker"]
        assert len(fake_sessions.calls) == 2  # note-taker + task extractor

    @pytest.mark.asyncio
    async def test_start_refuses_a_second_concurrent_meeting(self, app, fake_sessions):
        async with client_for(app) as client:
            await _start(client, "first")
            await client.post(f"{BASE}/meetings/second/init", json={})
            resp = await client.post(f"{BASE}/meetings/second/start", json={})
            assert resp.status == 409

    @pytest.mark.asyncio
    async def test_restart_sends_the_restart_notice_not_a_fresh_init(self, app, fake_sessions):
        async with client_for(app) as client:
            await _start(client)
            fake_sessions.calls.clear()
            resp = await client.post(
                f"{BASE}/meetings/standup/start", json={"restart": True}
            )
            assert resp.status == 200
        assert all(
            k.SYSTEM_MEETING_RESTARTED in msg for _k, _a, msg in fake_sessions.calls
        )

    @pytest.mark.asyncio
    async def test_start_redacts_the_title(self, app, root: Path, fake_sessions):
        async with client_for(app) as client:
            await client.post(f"{BASE}/meetings/standup/init", json={})
            await client.post(
                f"{BASE}/meetings/standup/start",
                json={"title": "Rotate AKIAIOSFODNN7EXAMPLE"},
            )
        meta = store.read_meeting_meta("standup", root)
        assert meta is not None
        assert "AKIAIOSFODNN7EXAMPLE" not in meta["title"]

    @pytest.mark.asyncio
    async def test_status_transitions(self, app, fake_sessions):
        async with client_for(app) as client:
            await _start(client)
            for state in (k.STATUS_PAUSED, k.STATUS_ACTIVE, k.STATUS_REVIEWING):
                resp = await client.post(
                    f"{BASE}/meetings/standup/status", json={"status": state}
                )
                assert resp.status == 200
                assert (await resp.json())["status"] == state

    @pytest.mark.asyncio
    async def test_status_rejects_an_unknown_state(self, app, fake_sessions):
        async with client_for(app) as client:
            await _start(client)
            resp = await client.post(
                f"{BASE}/meetings/standup/status", json={"status": "banana"}
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_status_on_unknown_meeting_is_404(self, app):
        async with client_for(app) as client:
            resp = await client.post(
                f"{BASE}/meetings/ghost/status", json={"status": k.STATUS_PAUSED}
            )
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_stop_flushes_and_marks_ended(self, app, root: Path, fake_sessions):
        async with client_for(app) as client:
            await _start(client)
            fake_sessions.calls.clear()
            resp = await client.post(f"{BASE}/meetings/standup/stop")
            assert resp.status == 200
            assert (await resp.json())["status"] == k.STATUS_ENDED
        assert any(k.SYSTEM_MEETING_ENDED in msg for _k, _a, msg in fake_sessions.calls)
        meta = store.read_meeting_meta("standup", root)
        assert meta is not None and meta["status"] == k.STATUS_ENDED
        assert _common.ACTIVE.get() is None

    @pytest.mark.asyncio
    async def test_list_and_get(self, app):
        async with client_for(app) as client:
            await client.post(f"{BASE}/meetings/one/init", json={"title": "One"})
            await client.post(f"{BASE}/meetings/two/init", json={"title": "Two"})
            resp = await client.get(f"{BASE}/meetings")
            titles = {m["title"] for m in (await resp.json())["meetings"]}
            assert titles == {"One", "Two"}

            resp = await client.get(f"{BASE}/meetings/one")
            body = await resp.json()
            assert body["meta"]["title"] == "One"
            assert body["live"] is None

    @pytest.mark.asyncio
    async def test_get_unknown_meeting_is_404(self, app):
        async with client_for(app) as client:
            assert (await client.get(f"{BASE}/meetings/ghost")).status == 404

    @pytest.mark.asyncio
    async def test_outputs_are_batched_and_redacted(self, app, root: Path):
        async with client_for(app) as client:
            await client.post(f"{BASE}/meetings/standup/init", json={"title": "Standup"})
            store.write_agent_output(
                "standup",
                {"id": "note-taker", "widget_type": "markdown"},
                "# Notes\n\nkey AKIAIOSFODNN7EXAMPLE here",
                root,
            )
            resp = await client.get(f"{BASE}/meetings/standup/outputs")
            body = await resp.json()
            assert "AKIAIOSFODNN7EXAMPLE" not in body["outputs"]["note-taker"]
            assert "# Notes" in body["outputs"]["note-taker"]
            assert body["tasks"] == []


class TestAttachmentRoutes:
    @pytest.mark.asyncio
    async def test_add_and_remove(self, app):
        async with client_for(app) as client:
            await client.post(f"{BASE}/meetings/standup/init", json={})
            resp = await client.post(
                f"{BASE}/meetings/standup/attachments",
                json={
                    "action": "add",
                    "attachments": [
                        {"type": "url", "url": "https://example.test/doc", "label": "Doc"}
                    ],
                },
            )
            assert len((await resp.json())["attachments"]) == 1
            resp = await client.post(
                f"{BASE}/meetings/standup/attachments", json={"action": "remove", "index": 0}
            )
            assert (await resp.json())["attachments"] == []

    @pytest.mark.asyncio
    async def test_drops_a_dangerous_url_scheme(self, app):
        async with client_for(app) as client:
            await client.post(f"{BASE}/meetings/standup/init", json={})
            resp = await client.post(
                f"{BASE}/meetings/standup/attachments",
                json={
                    "action": "add",
                    "attachments": [
                        {"type": "url", "url": "file:///etc/passwd", "label": "Bad"},
                        {"type": "url", "url": "javascript:alert(1)", "label": "Worse"},
                        {"type": "url", "url": "https://example.test/ok", "label": "Fine"},
                    ],
                },
            )
            attachments = (await resp.json())["attachments"]
            assert [a["label"] for a in attachments] == ["Fine"]

    @pytest.mark.asyncio
    async def test_drops_an_unknown_type(self, app):
        async with client_for(app) as client:
            await client.post(f"{BASE}/meetings/standup/init", json={})
            resp = await client.post(
                f"{BASE}/meetings/standup/attachments",
                json={"action": "add", "attachments": [{"type": "artifact", "slug": "x"}]},
            )
            assert (await resp.json())["attachments"] == []

    @pytest.mark.asyncio
    async def test_enforces_the_cap(self, app):
        async with client_for(app) as client:
            await client.post(f"{BASE}/meetings/standup/init", json={})
            resp = await client.post(
                f"{BASE}/meetings/standup/attachments",
                json={
                    "action": "add",
                    "attachments": [
                        {"type": "url", "url": f"https://example.test/{i}", "label": str(i)}
                        for i in range(k.MAX_ATTACHMENTS + 20)
                    ],
                },
            )
            assert len((await resp.json())["attachments"]) == k.MAX_ATTACHMENTS

    @pytest.mark.asyncio
    async def test_rejects_a_bad_action_and_index(self, app):
        async with client_for(app) as client:
            await client.post(f"{BASE}/meetings/standup/init", json={})
            assert (
                await client.post(
                    f"{BASE}/meetings/standup/attachments", json={"action": "nuke"}
                )
            ).status == 400
            assert (
                await client.post(
                    f"{BASE}/meetings/standup/attachments",
                    json={"action": "remove", "index": "one"},
                )
            ).status == 400

    @pytest.mark.asyncio
    async def test_unknown_meeting_is_404(self, app):
        async with client_for(app) as client:
            resp = await client.post(
                f"{BASE}/meetings/ghost/attachments", json={"action": "add", "attachments": []}
            )
            assert resp.status == 404


class TestAgentRoutes:
    @pytest.mark.asyncio
    async def test_get_agents(self, app):
        async with client_for(app) as client:
            body = await (await client.get(f"{BASE}/agents")).json()
            assert [a["id"] for a in body["agents"]] == ["note-taker", "sketch-artist"]
            assert body["task_extractor_id"] == k.TASK_EXTRACTOR_ID

    @pytest.mark.asyncio
    async def test_status_idle_shape(self, app):
        async with client_for(app) as client:
            body = await (await client.get(f"{BASE}/status")).json()
            assert body == {
                "active_meeting": None,
                "muted_agents": [],
                "agents": {},
                "agents_paused": False,
                "expired": False,
            }

    @pytest.mark.asyncio
    async def test_status_live_shape(self, app, fake_sessions):
        async with client_for(app) as client:
            await _start(client)
            body = await (await client.get(f"{BASE}/status")).json()
            assert body["active_meeting"] == "standup"
            assert set(body["agents"]) == {"note-taker", "sketch-artist", k.TASK_EXTRACTOR_ID}

    @pytest.mark.asyncio
    async def test_dispatch_broadcasts_and_redacts(self, app, fake_sessions):
        async with client_for(app) as client:
            await _start(client)
            resp = await client.post(
                f"{BASE}/meetings/standup/dispatch",
                json={"text": "rotate AKIAIOSFODNN7EXAMPLE today"},
            )
            assert resp.status == 200
            body = await resp.json()
            assert body["dispatched"] == 3
            assert "AKIAIOSFODNN7EXAMPLE" not in body["text"]

    @pytest.mark.asyncio
    async def test_dispatch_marks_a_chat_line(self, app, fake_sessions):
        async with client_for(app) as client:
            await _start(client)
            resp = await client.post(
                f"{BASE}/meetings/standup/dispatch",
                json={"text": "actually the owner is Bob", "chat": True},
            )
            assert (await resp.json())["text"].startswith(k.CHAT_PREFIX)

    @pytest.mark.asyncio
    async def test_dispatch_without_an_active_meeting_is_409(self, app):
        async with client_for(app) as client:
            resp = await client.post(
                f"{BASE}/meetings/standup/dispatch", json={"text": "hello"}
            )
            assert resp.status == 409

    @pytest.mark.asyncio
    async def test_dispatch_requires_text(self, app, fake_sessions):
        async with client_for(app) as client:
            await _start(client)
            assert (
                await client.post(f"{BASE}/meetings/standup/dispatch", json={})
            ).status == 400

    @pytest.mark.asyncio
    async def test_dispatch_on_an_expired_session_is_410(self, app, fake_sessions):
        import time

        async with client_for(app) as client:
            await _start(client)
            session = _common.ACTIVE.get()
            assert session is not None
            session.started_at = time.time() - (k.MAX_SESSION_DURATION + 1)
            resp = await client.post(
                f"{BASE}/meetings/standup/dispatch", json={"text": "still talking"}
            )
            assert resp.status == 410

    @pytest.mark.asyncio
    async def test_dispatch_drops_noise_without_erroring(self, app, fake_sessions):
        async with client_for(app) as client:
            await _start(client)
            resp = await client.post(f"{BASE}/meetings/standup/dispatch", json={"text": "I I"})
            assert resp.status == 200
            assert (await resp.json())["dispatched"] == 0

    @pytest.mark.asyncio
    async def test_mute_persists_without_a_live_session(self, app, root: Path):
        async with client_for(app) as client:
            await client.post(f"{BASE}/meetings/standup/init", json={})
            resp = await client.post(
                f"{BASE}/meetings/standup/mute",
                json={"agent_id": "note-taker", "muted": True},
            )
            assert (await resp.json())["muted_agents"] == ["note-taker"]
        meta = store.read_meeting_meta("standup", root)
        assert meta is not None and meta["muted_agents"] == ["note-taker"]

    @pytest.mark.asyncio
    async def test_mute_applies_to_the_live_session(self, app, fake_sessions):
        async with client_for(app) as client:
            await _start(client)
            await client.post(
                f"{BASE}/meetings/standup/mute",
                json={"agent_id": "note-taker", "muted": True},
            )
            session = _common.ACTIVE.get()
            assert session is not None and "note-taker" in session.muted_agents
            resp = await client.post(
                f"{BASE}/meetings/standup/dispatch", json={"text": "the build is green"}
            )
            assert (await resp.json())["dispatched"] == 2

    @pytest.mark.asyncio
    async def test_mute_string_false_is_not_treated_as_true(self, app):
        """A type slip must not silently invert a mute decision: bool("false")
        is True, so the field reader is strict and falls back to the default."""
        async with client_for(app) as client:
            await client.post(f"{BASE}/meetings/standup/init", json={})
            resp = await client.post(
                f"{BASE}/meetings/standup/mute",
                json={"agent_id": "note-taker", "muted": "false"},
            )
            # Non-bool → the documented default (True), never a coerced truthy.
            assert (await resp.json())["muted_agents"] == ["note-taker"]

    @pytest.mark.asyncio
    async def test_mute_rejects_a_bad_agent_id(self, app):
        async with client_for(app) as client:
            await client.post(f"{BASE}/meetings/standup/init", json={})
            resp = await client.post(
                f"{BASE}/meetings/standup/mute", json={"agent_id": "../../etc"}
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_toggle_agent_on_and_off(self, app, root: Path, fake_sessions):
        async with client_for(app) as client:
            await _start(client, agents_enabled=["note-taker"])
            fake_sessions.calls.clear()
            resp = await client.post(
                f"{BASE}/meetings/standup/agents",
                json={"agent_id": "sketch-artist", "enable": True},
            )
            assert resp.status == 200
            assert "sketch-artist" in (await resp.json())["agents_enabled"]
            assert store.agent_output_path("standup", "sketch-artist.html", root).is_file()
            assert any("mid-meeting" in msg for _k, _a, msg in fake_sessions.calls)

            resp = await client.post(
                f"{BASE}/meetings/standup/agents",
                json={"agent_id": "sketch-artist", "enable": False},
            )
            assert "sketch-artist" not in (await resp.json())["agents_enabled"]

    @pytest.mark.asyncio
    async def test_toggle_unknown_agent_is_404(self, app, fake_sessions):
        async with client_for(app) as client:
            await _start(client)
            resp = await client.post(
                f"{BASE}/meetings/standup/agents", json={"agent_id": "ghost"}
            )
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_reset_resumes_paused_queues(self, app, fake_sessions):
        async with client_for(app) as client:
            await _start(client)
            session = _common.ACTIVE.get()
            assert session is not None
            session.agents["note-taker"]._fail_count = k.MAX_DISPATCH_FAILURES
            resp = await client.post(f"{BASE}/meetings/standup/reset")
            assert (await resp.json())["resumed"] == ["note-taker"]
            assert session.agents_paused is False

    @pytest.mark.asyncio
    async def test_reset_without_an_active_meeting_is_409(self, app):
        async with client_for(app) as client:
            assert (await client.post(f"{BASE}/meetings/standup/reset")).status == 409

    @pytest.mark.asyncio
    async def test_agent_message_flushes_immediately(self, app, fake_sessions):
        async with client_for(app) as client:
            await _start(client)
            fake_sessions.calls.clear()
            resp = await client.post(
                f"{BASE}/meetings/standup/message",
                json={"agent_id": "note-taker", "text": "please add the decision log"},
            )
            assert resp.status == 200
        prompts = fake_sessions.prompts_for("note-taker")
        assert prompts and prompts[-1].startswith(k.CHAT_PREFIX)

    @pytest.mark.asyncio
    async def test_agent_message_to_an_absent_agent_is_404(self, app, fake_sessions):
        async with client_for(app) as client:
            await _start(client, agents_enabled=["note-taker"])
            resp = await client.post(
                f"{BASE}/meetings/standup/message",
                json={"agent_id": "sketch-artist", "text": "hi"},
            )
            assert resp.status == 404


class TestTaskRoutes:
    @pytest.mark.asyncio
    async def test_add_list_update_delete(self, app):
        async with client_for(app) as client:
            await client.post(f"{BASE}/meetings/standup/init", json={})
            resp = await client.post(
                f"{BASE}/meetings/standup/tasks",
                json={"description": "ship the seam", "assignee": "Alice", "priority": "high"},
            )
            assert resp.status == 200
            task_id = (await resp.json())["task"]["id"]

            resp = await client.get(f"{BASE}/meetings/standup/tasks")
            assert [t["id"] for t in (await resp.json())["tasks"]] == [task_id]

            resp = await client.patch(
                f"{BASE}/meetings/standup/tasks",
                json={"id": task_id, "fields": {"assignee": "Bob", "priority": "low"}},
            )
            updated = (await resp.json())["task"]
            assert updated["assignee"] == "Bob" and updated["priority"] == "low"

            resp = await client.delete(
                f"{BASE}/meetings/standup/tasks", json={"id": task_id}
            )
            assert (await resp.json())["tasks"] == []

    @pytest.mark.asyncio
    async def test_add_requires_a_description(self, app):
        async with client_for(app) as client:
            await client.post(f"{BASE}/meetings/standup/init", json={})
            assert (
                await client.post(f"{BASE}/meetings/standup/tasks", json={})
            ).status == 400

    @pytest.mark.asyncio
    async def test_add_normalizes_an_invalid_priority(self, app):
        async with client_for(app) as client:
            await client.post(f"{BASE}/meetings/standup/init", json={})
            resp = await client.post(
                f"{BASE}/meetings/standup/tasks",
                json={"description": "d", "priority": "URGENT!!!"},
            )
            assert (await resp.json())["task"]["priority"] == k.DEFAULT_TASK_PRIORITY

    @pytest.mark.asyncio
    async def test_agent_written_tasks_are_normalized_and_redacted(self, app, root: Path):
        """``tasks.json`` is written by an LLM agent, so the file's shape is
        untrusted even though the app owns the path."""
        hostile: list[Any] = [
            {"description": "rotate AKIAIOSFODNN7EXAMPLE", "priority": "critical"},
            {"text": "legacy field name"},
            "not a dict",
            {"assignee": "no description"},
        ]
        store.write_tasks("standup", hostile, root)
        async with client_for(app) as client:
            tasks = (await (await client.get(f"{BASE}/meetings/standup/tasks")).json())["tasks"]
        assert len(tasks) == 2
        assert "AKIAIOSFODNN7EXAMPLE" not in tasks[0]["description"]
        assert tasks[0]["priority"] == k.DEFAULT_TASK_PRIORITY
        assert tasks[1]["description"] == "legacy field name"

    @pytest.mark.asyncio
    async def test_update_unknown_task_is_404(self, app):
        async with client_for(app) as client:
            await client.post(f"{BASE}/meetings/standup/init", json={})
            resp = await client.patch(
                f"{BASE}/meetings/standup/tasks", json={"id": "ghost", "fields": {}}
            )
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_update_requires_a_fields_object(self, app):
        async with client_for(app) as client:
            await client.post(f"{BASE}/meetings/standup/init", json={})
            resp = await client.patch(
                f"{BASE}/meetings/standup/tasks", json={"id": "x", "fields": "nope"}
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_update_cannot_blank_the_description(self, app):
        async with client_for(app) as client:
            await client.post(f"{BASE}/meetings/standup/init", json={})
            resp = await client.post(
                f"{BASE}/meetings/standup/tasks", json={"description": "keep me"}
            )
            task_id = (await resp.json())["task"]["id"]
            resp = await client.patch(
                f"{BASE}/meetings/standup/tasks",
                json={"id": task_id, "fields": {"description": "   "}},
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_delete_unknown_is_404(self, app):
        async with client_for(app) as client:
            await client.post(f"{BASE}/meetings/standup/init", json={})
            resp = await client.delete(f"{BASE}/meetings/standup/tasks", json={"id": "ghost"})
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_review_state_roundtrip(self, app):
        async with client_for(app) as client:
            await client.post(f"{BASE}/meetings/standup/init", json={})
            resp = await client.post(
                f"{BASE}/meetings/standup/tasks", json={"description": "noise"}
            )
            task_id = (await resp.json())["task"]["id"]
            resp = await client.post(
                f"{BASE}/meetings/standup/tasks/review",
                json={"id": task_id, "review_status": k.REVIEW_ARCHIVED},
            )
            assert (await resp.json())["tasks"][0]["review_status"] == k.REVIEW_ARCHIVED

    @pytest.mark.asyncio
    async def test_review_rejects_pushed_as_a_client_state(self, app):
        """``pushed`` is set by the filing path, never by the client — otherwise a
        task could be marked filed without a provider ever being called."""
        async with client_for(app) as client:
            await client.post(f"{BASE}/meetings/standup/init", json={})
            resp = await client.post(
                f"{BASE}/meetings/standup/tasks", json={"description": "d"}
            )
            task_id = (await resp.json())["task"]["id"]
            resp = await client.post(
                f"{BASE}/meetings/standup/tasks/review",
                json={"id": task_id, "review_status": k.REVIEW_PUSHED},
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_file_task_writes_the_ledger_and_marks_pushed(self, app, root: Path):
        async with client_for(app) as client:
            await client.post(f"{BASE}/meetings/standup/init", json={"title": "Standup"})
            resp = await client.post(
                f"{BASE}/meetings/standup/tasks",
                json={"description": "ship the seam", "assignee": "Alice"},
            )
            task_id = (await resp.json())["task"]["id"]
            resp = await client.post(
                f"{BASE}/meetings/standup/tasks/file", json={"id": task_id}
            )
            assert resp.status == 200
            body = await resp.json()
            assert body["ref"]["provider"] == k.TASK_PROVIDER_LOCAL
            assert body["tasks"][0]["review_status"] == k.REVIEW_PUSHED
        ledger = json.loads((root / "task-ledger.json").read_text())
        assert ledger["tasks"][0]["description"] == "ship the seam"
        assert ledger["tasks"][0]["meeting_title"] == "Standup"

    @pytest.mark.asyncio
    async def test_file_unknown_task_is_404(self, app):
        async with client_for(app) as client:
            await client.post(f"{BASE}/meetings/standup/init", json={})
            resp = await client.post(
                f"{BASE}/meetings/standup/tasks/file", json={"id": "ghost"}
            )
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_file_task_provider_failure_is_502(self, app, root: Path, monkeypatch):
        from kiro_crew.apps.builtins.meetings.backend.providers import tasks as taskprov

        class Failing(taskprov.TaskProvider):
            @property
            def provider_id(self) -> str:
                return "failing"

            @property
            def display_name(self) -> str:
                return "Failing"

            def create(self, draft):
                raise RuntimeError("tracker down")

        monkeypatch.setattr(
            "kiro_crew.apps.builtins.meetings.backend.routes.tasks."
            "taskprov.get_task_provider",
            lambda *_a, **_kw: Failing(),
        )
        async with client_for(app) as client:
            await client.post(f"{BASE}/meetings/standup/init", json={})
            resp = await client.post(
                f"{BASE}/meetings/standup/tasks", json={"description": "d"}
            )
            task_id = (await resp.json())["task"]["id"]
            resp = await client.post(
                f"{BASE}/meetings/standup/tasks/file", json={"id": task_id}
            )
            assert resp.status == 502
            assert (await resp.json())["ok"] is False
        # The task must NOT be marked pushed when nothing was filed.
        assert store.read_tasks("standup", root)["tasks"][0]["review_status"] == (
            k.REVIEW_PENDING
        )

    @pytest.mark.asyncio
    async def test_task_providers_endpoint(self, app):
        async with client_for(app) as client:
            body = await (await client.get(f"{BASE}/task-providers")).json()
            assert body["active"] == k.TASK_PROVIDER_LOCAL
            assert {r["id"] for r in body["providers"]} >= {k.TASK_PROVIDER_LOCAL}


class TestCalendarRoutes:
    @pytest.mark.asyncio
    async def test_get_calendar_empty(self, app):
        async with client_for(app) as client:
            body = await (await client.get(f"{BASE}/calendar")).json()
            assert body == {
                "events": [],
                "provider": k.CALENDAR_PROVIDER_NONE,
                "configured": False,
            }

    @pytest.mark.asyncio
    async def test_providers_endpoint(self, app):
        async with client_for(app) as client:
            body = await (await client.get(f"{BASE}/calendar/providers")).json()
            assert {r["id"] for r in body["providers"]} == {
                k.CALENDAR_PROVIDER_NONE,
                k.CALENDAR_PROVIDER_ICS,
            }

    @pytest.mark.asyncio
    async def test_sync_without_a_calendar_is_502_with_guidance(self, app):
        async with client_for(app) as client:
            resp = await client.post(f"{BASE}/calendar/sync")
            assert resp.status == 502
            assert "Settings" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_sync_reads_a_local_ics_and_caches_it(self, app, root: Path, tmp_path: Path):
        from datetime import datetime, timedelta, timezone

        soon = (datetime.now(timezone.utc) + timedelta(hours=2)).strftime("%Y%m%dT%H%M%SZ")
        ics = tmp_path / "cal.ics"
        ics.write_text(
            "BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:evt-1\nSUMMARY:Design Review\n"
            f"DTSTART:{soon}\nEND:VEVENT\nEND:VCALENDAR\n"
        )
        store.write_config(
            {
                **store.read_config(root),
                "calendar": {"provider": k.CALENDAR_PROVIDER_ICS, "source": str(ics)},
            },
            root,
        )
        async with client_for(app) as client:
            resp = await client.post(f"{BASE}/calendar/sync")
            assert resp.status == 200
            body = await resp.json()
            assert body["count"] == 1
            assert body["events"][0]["title"] == "Design Review"

            resp = await client.get(f"{BASE}/calendar")
            cached = await resp.json()
            assert cached["events"][0]["event_id"] == "evt-1"
            assert cached["configured"] is True

    @pytest.mark.asyncio
    async def test_sync_refuses_a_non_https_url_source(self, app, root: Path):
        store.write_config(
            {
                **store.read_config(root),
                "calendar": {
                    "provider": k.CALENDAR_PROVIDER_ICS,
                    "source": "http://example.test/cal.ics",
                },
            },
            root,
        )
        async with client_for(app) as client:
            resp = await client.post(f"{BASE}/calendar/sync")
            assert resp.status == 502
            assert "https" in (await resp.json())["error"]


class TestBodyLimits:
    @pytest.mark.asyncio
    async def test_oversized_body_is_413(self, app):
        async with client_for(app) as client:
            resp = await client.post(
                f"{BASE}/meetings/standup/tasks",
                data=json.dumps({"description": "x" * (_common.MAX_BODY_BYTES + 100)}),
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 413

    @pytest.mark.asyncio
    async def test_non_object_body_is_400(self, app):
        async with client_for(app) as client:
            resp = await client.post(
                f"{BASE}/meetings/standup/tasks",
                data=json.dumps([1, 2, 3]),
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400


class TestStartupHook:
    @pytest.mark.asyncio
    async def test_startup_seeds_the_data_dir_and_loads_the_dictionary(self, tmp_path: Path):
        from kiro_crew.apps.builtins.meetings.backend.domain import session as sess

        fresh = tmp_path / "unseeded"
        app = make_app(fresh)
        app["state"] = None
        with pytest.MonkeyPatch.context() as patcher:
            patcher.setattr(_common, "is_app_enabled", lambda _n: True)
            async with client_for(app) as client:
                assert (await client.get(f"{BASE}/config")).status == 200
        assert (fresh / k.DICTIONARY_FILE).is_file()
        assert (fresh / "meetings").is_dir()
        # The seeded dictionary carries at least one term, so a fresh install
        # already corrects the product's own name.
        assert sess.shared_dictionary().terms


class TestActiveMeetingHolder:
    def test_set_cancels_the_previous_session(self, root: Path):
        from kiro_crew.apps.builtins.meetings.backend.domain import session as sess

        first = sess.MeetingSession(meeting_id="a", config=store.read_config(root))
        second = sess.MeetingSession(meeting_id="b", config=store.read_config(root))
        _common.ACTIVE.set(first)
        _common.ACTIVE.set(second)
        assert _common.ACTIVE.get() is second
        assert _common.ACTIVE.get("a") is None
        assert _common.ACTIVE.get("b") is second

    def test_clear_returns_the_previous(self, root: Path):
        from kiro_crew.apps.builtins.meetings.backend.domain import session as sess

        session = sess.MeetingSession(meeting_id="a", config=store.read_config(root))
        _common.ACTIVE.set(session)
        assert _common.ACTIVE.clear() is session
        assert _common.ACTIVE.get() is None


class TestFiledRefIsSanitized:
    """``filed_ref`` is agent-written, and its ``url`` becomes an ``href``.

    The dashboard renders the filed-task reference as a link, so a
    ``javascript:`` url written into ``tasks.json`` would execute on the
    dashboard origin when the user clicked it (React only warns, and the
    dashboard CSP permits inline script). The normalizer is the authoritative
    gate; the UI has a matching guard.
    """

    def test_a_javascript_url_is_dropped_but_the_id_survives(self) -> None:
        from kiro_crew.apps.builtins.meetings.backend.routes import tasks as task_routes

        ref = task_routes._normalize_filed_ref(
            {"id": "KC-1", "url": "javascript:alert(document.cookie)"}
        )
        assert ref == {"id": "KC-1"}

    @pytest.mark.parametrize(
        "url",
        [
            "javascript:alert(1)",
            "JavaScript:alert(1)",
            "data:text/html;base64,PHNjcmlwdD4=",
            "vbscript:msgbox",
            "file:///etc/passwd",
            "//evil.example",
            "/relative/path",
            " javascript:alert(1)",
        ],
    )
    def test_every_non_http_scheme_is_refused(self, url: str) -> None:
        from kiro_crew.apps.builtins.meetings.backend.routes import tasks as task_routes

        ref = task_routes._normalize_filed_ref({"id": "KC-2", "url": url})
        assert ref is not None
        assert "url" not in ref, f"{url!r} should not be rendered as a link"

    @pytest.mark.parametrize("url", ["https://tracker.example/t/1", "http://tracker.example/t/1"])
    def test_absolute_http_urls_are_kept(self, url: str) -> None:
        from kiro_crew.apps.builtins.meetings.backend.routes import tasks as task_routes

        ref = task_routes._normalize_filed_ref({"id": "KC-3", "url": url})
        assert ref == {"id": "KC-3", "url": url}

    def test_a_non_dict_ref_is_dropped(self) -> None:
        from kiro_crew.apps.builtins.meetings.backend.routes import tasks as task_routes

        assert task_routes._normalize_filed_ref("KC-4") is None
        assert task_routes._normalize_filed_ref(None) is None

    def test_normalize_task_routes_filed_ref_through_the_gate(self) -> None:
        from kiro_crew.apps.builtins.meetings.backend.routes import tasks as task_routes

        task = task_routes._normalize_task(
            {"description": "do the thing", "filed_ref": {"id": "X", "url": "javascript:1"}}
        )
        assert task is not None
        assert task["filed_ref"] == {"id": "X"}


class TestAgentAndPresetSanitizers:
    """`settings.py`'s coercion layer, exercised directly.

    `agents.json` and the presets map are agent-writable AND user-editable, so
    every field is untrusted even though the app owns the path. These are the
    branches that drop a hostile or malformed record rather than letting it reach
    a dispatch (an agent ref is used to resolve WHICH agent runs, and a preset id
    becomes a filesystem path segment).
    """

    @staticmethod
    def _mod():
        from kiro_crew.apps.builtins.meetings.backend.routes import settings as mod

        return mod

    @pytest.mark.parametrize(
        "ref",
        [
            "../escape",          # traversal
            "/absolute/agent",    # absolute
            ".hidden",            # leading dot
            "has space",          # illegal char
            "semi;colon",
            "x" * 300,            # over the length cap
            "",
            None,
            123,                  # not a string at all
        ],
    )
    def test_an_unsafe_agent_ref_is_dropped(self, ref):
        assert self._mod()._clean_agent_ref(ref) == ""

    @pytest.mark.parametrize("ref", ["note-taker", "meetings/note-taker", "a_b-c/d"])
    def test_a_safe_agent_ref_survives(self, ref):
        assert self._mod()._clean_agent_ref(ref) == ref

    def test_a_non_dict_agent_def_is_dropped(self):
        mod = self._mod()
        assert mod._clean_agent_def("not a dict") is None
        assert mod._clean_agent_def(None) is None

    def test_an_agent_def_with_an_unsafe_id_is_dropped(self):
        assert self._mod()._clean_agent_def({"id": "../boom", "name": "x"}) is None

    def test_an_agent_def_is_coerced_field_by_field(self):
        cleaned = self._mod()._clean_agent_def(
            {
                "id": "note-taker",
                "name": "  Note Taker  ",
                "agent": "meetings/note-taker",
                "widget_type": "not-a-widget",
                "prompt": "  do the thing  ",
                "enabled_by_default": "yes",
                "listening_by_default": 0,
            }
        )
        assert cleaned is not None
        assert cleaned["name"] == "Note Taker"
        assert cleaned["prompt"] == "do the thing"
        # An unknown widget type falls back rather than reaching the renderer.
        assert cleaned["widget_type"] == k.DEFAULT_WIDGET_TYPE
        # Truthiness is coerced to a real bool, so "yes"/0 cannot leak through.
        assert cleaned["enabled_by_default"] is True
        assert cleaned["listening_by_default"] is False

    def test_a_missing_name_falls_back_to_the_id(self):
        cleaned = self._mod()._clean_agent_def({"id": "sketch-artist"})
        assert cleaned is not None
        assert cleaned["name"] == "sketch-artist"

    def test_a_non_dict_preset_is_dropped(self):
        mod = self._mod()
        assert mod._clean_preset([]) is None
        assert mod._clean_preset(None) is None

    def test_a_preset_keeps_only_safe_agent_ids(self):
        cleaned = self._mod()._clean_preset(
            {"enabled_agents": ["note-taker", "../escape", "sketch-artist", 7]}
        )
        assert cleaned == {"enabled_agents": ["note-taker", "sketch-artist"]}

    def test_a_preset_with_no_agent_list_becomes_empty(self):
        assert self._mod()._clean_preset({"enabled_agents": "all of them"}) == {
            "enabled_agents": []
        }


class TestOutputsPollIsRedactedAndOffLoop:
    """`GET /outputs` is polled every few seconds for a whole meeting.

    Two properties it must hold, both of which were briefly missing:

    1. BOTH halves of the payload are redacted. The agent outputs always were;
       the task list was forwarded straight off `store.read_tasks`, which returns
       the raw agent-written `tasks.json`.
    2. The reads and the `redact()` passes happen on a WORKER THREAD. The
       note-taker is prompted to rewrite its whole file after every transcription
       batch, so the reads are unbounded and redacting a large file measures in
       tens of milliseconds — inline, on a repeating poll, that stalls every other
       task on the gateway loop including the liveness heartbeat.
    """

    _FAKE_SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"

    @pytest.mark.asyncio
    async def test_a_credential_in_the_task_list_is_redacted(self, app, root):
        import json as _json

        async with client_for(app) as client:
            await client.post(f"{BASE}/meetings/standup/init", json={})
            # Write tasks.json the way an AGENT would — straight to disk, bypassing
            # the API's normalization on the write path, so only the READ path can
            # save us.
            (root / "meetings" / "standup" / "tasks.json").write_text(
                _json.dumps(
                    {
                        "meeting_id": "standup",
                        "tasks": [
                            {"description": f"rotate {self._FAKE_SECRET}", "priority": "high"}
                        ],
                    }
                ),
                encoding="utf-8",
            )
            resp = await client.get(f"{BASE}/meetings/standup/outputs")
            assert resp.status == 200
            body = json.dumps(await resp.json())
        assert self._FAKE_SECRET not in body
        assert "REDACTED" in body

    @pytest.mark.asyncio
    async def test_a_malformed_task_record_is_dropped_not_forwarded(self, app, root):
        import json as _json

        async with client_for(app) as client:
            await client.post(f"{BASE}/meetings/standup/init", json={})
            (root / "meetings" / "standup" / "tasks.json").write_text(
                _json.dumps(
                    {"meeting_id": "standup", "tasks": ["not a dict", {"description": "  "}, {}]}
                ),
                encoding="utf-8",
            )
            resp = await client.get(f"{BASE}/meetings/standup/outputs")
            payload = await resp.json()
        # Each of those three is unusable, so none should reach the dashboard.
        assert payload["tasks"] == []

    def test_the_handler_reads_off_the_event_loop(self):
        """A blocking read on the loop is the defect; pin the offload."""
        import inspect

        from kiro_crew.apps.builtins.meetings.backend.routes import meeting_lifecycle as ml

        src = inspect.getsource(ml.handle_get_outputs)
        assert "asyncio.to_thread" in src, "the poll must not read on the event loop"
        # The blocking work lives in the helper the thread runs, not the handler.
        assert "read_agent_outputs" not in src

    def test_the_collector_redacts_both_halves(self):
        import inspect

        from kiro_crew.apps.builtins.meetings.backend.routes import meeting_lifecycle as ml

        src = inspect.getsource(ml._collect_outputs)
        assert "redact(" in src, "agent outputs must be redacted"
        # The task list must go through the normalizer, never straight off the store.
        assert "read_normalized" in src
        assert "store.read_tasks" not in src


class TestNoStoreCallRunsOnTheEventLoop:
    """No route handler may touch the filesystem inline.

    The gateway runs every task on ONE asyncio loop, so a synchronous store call in
    an `async def` freezes the user's chat turn AND the liveness heartbeat until the
    watchdog kills the process — the wedge the AUTOSDE
    `no-blocking-call-on-event-loop` rule exists to prevent. `GET /meetings` was the
    reported instance (`list_meetings` globs `*/session.json` and JSON-parses every
    hit), but roughly thirty call sites across these five modules had the same shape.

    This is an AST assertion rather than a per-handler source grep so a NEW handler
    that reads inline fails too, without anyone remembering to extend a list.
    """

    #: Every `store` function that opens, walks, stats, or writes a file. A handler
    #: may name one (handing it to `asyncio.to_thread`) but never CALL one.
    _BLOCKING_STORE_FNS = frozenset(
        {
            "data_dir",
            "ensure_data_dirs",
            "meetings_root",
            "meeting_dir",
            "agent_output_path",
            "read_config",
            "write_config",
            "read_meeting_meta",
            "write_meeting_meta",
            "list_meetings",
            "tasks_path",
            "read_tasks",
            "write_tasks",
            "ensure_agent_files",
            "read_agent_outputs",
            "write_agent_output",
            "read_calendar_cache",
            "write_calendar_cache",
        }
    )

    #: Domain helpers that are themselves stacks of blocking store calls.
    _BLOCKING_DOMAIN_FNS = frozenset(
        {"start_meeting_meta", "end_meeting_meta", "reload_dictionary"}
    )

    def _route_modules(self) -> list:
        from kiro_crew.apps.builtins.meetings.backend.routes import (
            agents,
            calendar,
            meeting_lifecycle,
            settings,
            tasks,
        )

        return [agents, calendar, meeting_lifecycle, settings, tasks]

    def _inline_blocking_calls(self, module) -> list[str]:
        """`file:line handler -> callee()` for every blocking call in an `async def`.

        Nested plain `def`s are skipped: a sync closure inside an `async def` is what
        `run_in_executor` runs, so its body is already off the loop.
        """
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(module))
        offenders: list[str] = []
        for handler in [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)]:
            nested = {
                id(n)
                for outer in ast.walk(handler)
                if isinstance(outer, ast.FunctionDef)
                for n in ast.walk(outer)
            }
            for node in ast.walk(handler):
                if id(node) in nested or not isinstance(node, ast.Call):
                    continue
                callee = node.func
                if not isinstance(callee, ast.Attribute):
                    continue
                owner = callee.value
                if not isinstance(owner, ast.Name):
                    continue
                blocking = (
                    owner.id == "store" and callee.attr in self._BLOCKING_STORE_FNS
                ) or (owner.id == "sess" and callee.attr in self._BLOCKING_DOMAIN_FNS)
                if blocking:
                    offenders.append(
                        f"{module.__name__}:{node.lineno} "
                        f"{handler.name} -> {owner.id}.{callee.attr}()"
                    )
        return offenders

    def test_no_handler_calls_the_store_inline(self):
        offenders: list[str] = []
        for module in self._route_modules():
            offenders.extend(self._inline_blocking_calls(module))
        assert offenders == [], (
            "these run blocking filesystem IO on the gateway event loop; wrap them in "
            "asyncio.to_thread (grouped into one sync helper per handler):\n  "
            + "\n  ".join(offenders)
        )

    def test_the_reported_handler_lists_meetings_off_the_loop(self):
        """The exact call site the CI reviewer flagged."""
        import inspect

        from kiro_crew.apps.builtins.meetings.backend.routes import meeting_lifecycle as ml

        src = inspect.getsource(ml.handle_list_meetings)
        assert "asyncio.to_thread(store.list_meetings" in src

    def test_every_read_modify_write_handler_uses_one_thread_hop(self):
        """A read and the write derived from it must not straddle two hops.

        Two `to_thread` awaits with the mutation between them lets another request
        run in the gap and have its write overwritten by this one's stale list. The
        two handlers that legitimately cannot do this — `handle_toggle_agent` and
        `handle_file_task`, whose writes must follow an `await` (an agent dispatch
        and a provider call) — are excluded and documented in place.
        """
        import inspect

        from kiro_crew.apps.builtins.meetings.backend.routes import agents as ag
        from kiro_crew.apps.builtins.meetings.backend.routes import meeting_lifecycle as ml
        from kiro_crew.apps.builtins.meetings.backend.routes import settings as st
        from kiro_crew.apps.builtins.meetings.backend.routes import tasks as tk

        for handler in (
            ml.handle_meeting_init,
            ml.handle_meeting_status,
            ml.handle_attachments,
            ag.handle_mute_agent,
            st.handle_add_dictionary_term,
            st.handle_remove_dictionary_term,
            tk.handle_add_task,
            tk.handle_update_task,
            tk.handle_delete_task,
            tk.handle_review_task,
        ):
            src = inspect.getsource(handler)
            hops = src.count("asyncio.to_thread")
            assert hops == 1, (
                f"{handler.__name__} makes {hops} thread hops; group its "
                "read-modify-write into ONE sync helper"
            )

    def test_each_grouped_helper_is_documented_as_blocking(self):
        """The helpers a worker thread runs say so, per the repo convention."""
        import inspect

        from kiro_crew.apps.builtins.meetings.backend.routes import agents as ag
        from kiro_crew.apps.builtins.meetings.backend.routes import calendar as cl
        from kiro_crew.apps.builtins.meetings.backend.routes import meeting_lifecycle as ml
        from kiro_crew.apps.builtins.meetings.backend.routes import settings as st
        from kiro_crew.apps.builtins.meetings.backend.routes import tasks as tk

        for helper in (
            ml._init_meeting,
            ml._begin_meeting,
            ml._apply_status,
            ml._apply_attachments,
            ml._collect_outputs,
            ag._read_toggle_state,
            ag._apply_mute,
            cl._read_cached_calendar,
            st._reload_terms,
            st._add_term,
            st._remove_term,
            tk._append_task,
            tk._patch_task,
            tk._drop_task,
            tk._prepare_filing,
            tk._set_review_state,
        ):
            doc = inspect.getdoc(helper) or ""
            assert "BLOCKING" in doc, f"{helper.__name__} must document that it blocks"


class TestTaskWritesAreSerialized:
    """Concurrent task mutations must not silently overwrite each other.

    Each helper reads the whole list, changes one entry, and writes it back — on a
    worker thread, so two requests genuinely run at once. "Archive all" fires one
    POST per task, which is the easy way to lose all but one. `atomic_write` never
    helped: the write was atomic, the read-modify-write around it was not.
    """

    def test_concurrent_review_updates_all_survive(self, root: Path) -> None:
        import threading
        from concurrent import futures

        from kiro_crew.apps.builtins.meetings.backend.routes import tasks as task_routes

        meeting_id = "m1"
        count = 16
        store.write_tasks(
            meeting_id,
            [{"id": f"t{i}", "description": f"task {i}"} for i in range(count)],
            root,
        )
        barrier = threading.Barrier(count)

        def archive(index: int) -> None:
            barrier.wait()  # maximize overlap on the read-modify-write
            task_routes._set_review_state(
                meeting_id, f"t{index}", k.REVIEW_ARCHIVED, root
            )

        with futures.ThreadPoolExecutor(max_workers=count) as pool:
            list(pool.map(archive, range(count)))

        final = task_routes.read_normalized(meeting_id, root)
        archived = {t["id"] for t in final if t["review_status"] == k.REVIEW_ARCHIVED}
        assert archived == {f"t{i}" for i in range(count)}

    def test_concurrent_adds_all_survive(self, root: Path) -> None:
        import threading
        from concurrent import futures

        from kiro_crew.apps.builtins.meetings.backend.routes import tasks as task_routes

        meeting_id = "m2"
        count = 16
        barrier = threading.Barrier(count)

        def add(index: int) -> None:
            barrier.wait()
            task_routes._append_task(meeting_id, {"description": f"added {index}"}, root)

        with futures.ThreadPoolExecutor(max_workers=count) as pool:
            list(pool.map(add, range(count)))

        described = {t["description"] for t in task_routes.read_normalized(meeting_id, root)}
        assert described == {f"added {i}" for i in range(count)}

    def test_recording_a_filing_does_not_revert_concurrent_edits(self, root: Path) -> None:
        """The one helper that writes after an await must re-read, not replay.

        `handle_file_task` captures the list, awaits the provider, then records the
        result. Writing that pre-await snapshot would roll back anything changed in
        between — e.g. the task extractor agent adding a task.
        """
        from kiro_crew.apps.builtins.meetings.backend.providers import tasks as taskprov
        from kiro_crew.apps.builtins.meetings.backend.routes import tasks as task_routes

        meeting_id = "m3"
        store.write_tasks(meeting_id, [{"id": "t1", "description": "file me"}], root)
        # Something else writes while the "provider call" is in flight.
        store.write_tasks(
            meeting_id,
            [
                {"id": "t1", "description": "file me"},
                {"id": "t2", "description": "added during the filing"},
            ],
            root,
        )
        ref = taskprov.TaskRef(provider="local", id="mt-abc", created_at="now")

        final = task_routes._record_filing(meeting_id, "t1", ref, root)

        ids = {t["id"] for t in final}
        assert ids == {"t1", "t2"}, "the concurrently-added task must survive"
        filed = next(t for t in final if t["id"] == "t1")
        assert filed["review_status"] == k.REVIEW_PUSHED
        assert filed["filed_ref"]["id"] == "mt-abc"


class TestTeardownDrainsBeforeClearing:
    """Tearing a session down must not cancel transcript that never got sent.

    `ACTIVE.clear()` calls `cancel_all()`, which drops the pending flush timers —
    so a meeting torn down with a half-batch queued lost that text, and its final
    notes silently omitted whatever had not yet been dispatched. Every teardown path
    now goes through `drain_and_clear()`.
    """

    @pytest.mark.asyncio
    async def test_drain_and_clear_flushes_first(self, root: Path) -> None:
        from kiro_crew.apps.builtins.meetings.backend.routes import _common

        flushed: list[str] = []

        class _FakeSession:
            meeting_id = "m1"

            async def flush_all(self) -> None:
                flushed.append("flushed")

            def cancel_all(self) -> None:
                flushed.append("cancelled")

        active = _common._ActiveMeeting()
        active.set(_FakeSession())  # type: ignore[arg-type]

        previous = await active.drain_and_clear()

        # Flush strictly BEFORE the cancelling teardown, and the session is gone.
        assert flushed == ["flushed", "cancelled"]
        assert active.get() is None
        assert previous is not None

    @pytest.mark.asyncio
    async def test_a_failed_flush_still_tears_down(self, root: Path) -> None:
        """A stuck agent must not wedge shutdown — the session goes away regardless."""
        from kiro_crew.apps.builtins.meetings.backend.routes import _common

        class _BrokenSession:
            meeting_id = "m2"

            async def flush_all(self) -> None:
                raise RuntimeError("agent is wedged")

            def cancel_all(self) -> None:
                pass

        active = _common._ActiveMeeting()
        active.set(_BrokenSession())  # type: ignore[arg-type]

        await active.drain_and_clear()

        assert active.get() is None

    @pytest.mark.asyncio
    async def test_starting_a_meeting_drains_the_one_it_replaces(self) -> None:
        """`set()` cancels the outgoing session's queues, so the replace path is a
        teardown too — starting a second meeting while an earlier (typically
        expired) one still held a half-batch discarded that transcript."""
        import ast
        import inspect

        from kiro_crew.apps.builtins.meetings.backend.routes import meeting_lifecycle

        tree = ast.parse(inspect.getsource(meeting_lifecycle))
        for fn in [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)]:
            body = ast.dump(fn)
            if "attr='set'" not in body:
                continue
            assert "attr='drain_and_clear'" in body, (
                f"{fn.name} calls ACTIVE.set() without draining the session it "
                "replaces; queued transcript would be cancelled"
            )

    @pytest.mark.asyncio
    async def test_set_warns_rather_than_silently_dropping_a_queue(self, caplog) -> None:
        """A leftover queue at replace time means transcript is about to be lost, so
        it is logged with the count rather than vanishing."""
        import logging

        from kiro_crew.apps.builtins.meetings.backend.domain import session as sess
        from kiro_crew.apps.builtins.meetings.backend.routes import _common

        class _Session:
            meeting_id = "stale"

            def __init__(self) -> None:
                queue = sess.AgentQueue(name="n", key="k")
                queue.queue = ["a line nobody dispatched"]
                self.agents = {"n": queue}

            def cancel_all(self) -> None:
                pass

        active = _common._ActiveMeeting()
        active.set(_Session())  # type: ignore[arg-type]
        with caplog.at_level(logging.WARNING, logger="kirocrew.app.meetings"):
            active.set(None)
        assert "1 queued line(s)" in caplog.text
        assert "drain_and_clear" in caplog.text

    @pytest.mark.asyncio
    async def test_no_teardown_path_still_uses_the_lossy_clear(self) -> None:
        """`clear()` is lossy; only `set()` may use it. An AST check, so a NEW
        teardown path added later cannot quietly reintroduce the transcript loss."""
        import ast
        import importlib
        import inspect

        from kiro_crew.apps.builtins.meetings.backend.routes import (
            agents,
            meeting_lifecycle,
        )

        # `from ... import __init__` binds the dunder attribute, not the package —
        # import the package itself so `inspect.getsource` gets a module.
        routes_init = importlib.import_module(
            "kiro_crew.apps.builtins.meetings.backend.routes"
        )

        offenders: list[str] = []
        for module in (routes_init, agents, meeting_lifecycle):
            tree = ast.parse(inspect.getsource(module))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if not isinstance(func, ast.Attribute) or func.attr != "clear":
                    continue
                target = func.value
                if isinstance(target, ast.Name) and target.id == "ACTIVE":
                    offenders.append(f"{module.__name__}:{node.lineno} ACTIVE.clear()")

        assert offenders == [], (
            "these teardown paths cancel queued transcript instead of draining it; "
            "use `await ACTIVE.drain_and_clear()`:\n  " + "\n  ".join(offenders)
        )


class TestConcurrentStartsAreSerialized:
    """The single-active-meeting check and the install must be one critical section.

    `handle_start_meeting` reads `ACTIVE.get()`, then awaits (metadata IO, then the
    drain) before calling `set()`. Two starts interleaving in that gap both pass the
    check, and the second replaces the first — whose transcript then fails to
    dispatch with a confusing 409.
    """

    def test_the_check_and_the_install_are_under_one_lock(self) -> None:
        """AST assertion: everything from the `ACTIVE.get()` guard through
        `ACTIVE.set()` sits inside an `async with START_LOCK`, so a future edit
        cannot reopen the window by adding an await between them."""
        import ast
        import inspect

        from kiro_crew.apps.builtins.meetings.backend.routes import meeting_lifecycle

        tree = ast.parse(inspect.getsource(meeting_lifecycle))
        starts = [
            fn
            for fn in ast.walk(tree)
            if isinstance(fn, ast.AsyncFunctionDef) and fn.name == "handle_start_meeting"
        ]
        assert starts, "handle_start_meeting not found — did it move?"

        guarded: list[str] = []
        for node in ast.walk(starts[0]):
            if not isinstance(node, ast.AsyncWith):
                continue
            if "START_LOCK" not in ast.dump(node.items[0].context_expr):
                continue
            body = ast.dump(ast.Module(body=node.body, type_ignores=[]))
            if "attr='get'" in body and "attr='set'" in body:
                guarded.append("ok")

        assert guarded, (
            "handle_start_meeting's ACTIVE.get() check and ACTIVE.set() install are "
            "not both inside `async with START_LOCK` — two concurrent starts can "
            "each pass the check and one will silently replace the other"
        )
