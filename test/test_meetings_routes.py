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

import asyncio
import json
import threading
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
from kiro_crew.apps.builtins.meetings.backend.domain import session as sess
from kiro_crew.apps.builtins.meetings.backend.routes import _common

BASE = k.API_BASE

# Repo-root anchor for tests that read a checked-in source file. A relative
# literal resolves against the process CWD, which no test owns: another test
# chdir-ing away makes the read fail, and under `-n auto` the loser is decided
# by worker scheduling rather than by anything in this file.
_REPO_ROOT = Path(__file__).resolve().parents[1]


async def _start(client, meeting_id: str = "standup", **body) -> dict:
    await client.post(f"{BASE}/meetings/{meeting_id}/init", json={"title": "Standup"})
    resp = await client.post(f"{BASE}/meetings/{meeting_id}/start", json=body)
    assert resp.status == 200, await resp.text()
    return await resp.json()


async def _start_and_get_session(client, meeting_id: str = "standup", **body):
    """Start *meeting_id* and hand back its installed live session."""
    await _start(client, meeting_id, **body)
    session = _common.ACTIVE.get(meeting_id)
    assert session is not None, f"{meeting_id} did not install a live session"
    return session


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
    async def test_restart_re_initializes_agents_and_then_notices(self, app, fake_sessions):
        """A restart must re-state OUTPUT_FILE, not just say "carry on".

        This assertion previously required that ONLY the restart notice was sent —
        encoding the bug it was meant to describe. The agent slots are ordinary
        kiro sessions and can be reclaimed between stop and restart (session
        cleanup, a gateway restart, an idle sweep); a fresh session then received
        "continue appending to your output" and nothing naming that output, so it
        had no OUTPUT_FILE and the notes and tasks silently stopped updating.

        Re-initializing a session that DOES remember is harmless — the init
        message re-states the path and says the file already exists, and
        `init_agents` writes no files.
        """
        async with client_for(app) as client:
            await _start(client)
            fake_sessions.calls.clear()
            resp = await client.post(
                f"{BASE}/meetings/standup/start", json={"restart": True}
            )
            assert resp.status == 200
        messages = [msg for _k, _a, msg in fake_sessions.calls]
        assert messages, "a restart dispatched nothing at all"
        # Every agent is told where to write...
        assert any("OUTPUT_FILE:" in msg for msg in messages), (
            "a restarted meeting's agents were never given their OUTPUT_FILE"
        )
        # ...and the notice still goes out, AFTER the instructions it qualifies.
        assert any(k.SYSTEM_MEETING_RESTARTED in msg for msg in messages)
        first_notice = next(
            i for i, msg in enumerate(messages) if k.SYSTEM_MEETING_RESTARTED in msg
        )
        assert any("OUTPUT_FILE:" in msg for msg in messages[:first_notice]), (
            "the restart notice must follow the init messages it refers to"
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
    async def test_get_meeting_reports_dispatch_admission(self, app, fake_sessions):
        """The poll payload carries the same admission flag dispatch checks.

        The frontend opens the microphone off this field, so it must track the
        holder's ``accepting_dispatches`` — not merely whether a session is
        installed. A completed start reports it open; suspending ingress (the
        state the whole ~46s initialization window is in, and what reviewing
        does) reports it closed while ``live`` itself is still present.
        """
        async with client_for(app) as client:
            await _start(client)
            body = await (await client.get(f"{BASE}/meetings/standup")).json()
            assert body["live"] is not None
            assert body["live"]["accepting_dispatches"] is True

            # Ingress closed but session installed — what a poll observes while
            # a start is mid-initialization, here reached via the same holder
            # call the start handler uses.
            _common.ACTIVE.suspend_dispatches(_common.ACTIVE.get("standup"))
            body = await (await client.get(f"{BASE}/meetings/standup")).json()
            assert body["live"] is not None
            assert body["live"]["accepting_dispatches"] is False

    @pytest.mark.asyncio
    async def test_delete_removes_the_meeting_and_all_outputs(self, app, root: Path):
        async with client_for(app) as client:
            await client.post(f"{BASE}/meetings/standup/init", json={"title": "Standup"})
            store.write_tasks("standup", [{"id": "t1", "description": "Ship it"}], root)
            store.write_agent_edit(
                "standup",
                {"id": "note-taker", "widget_type": "markdown"},
                "# Mine\n",
                root,
            )
            edit_dir = store.agent_edits_dir("standup", root)
            assert edit_dir.is_dir()

            resp = await client.delete(f"{BASE}/meetings/standup")
            assert resp.status == 204
            assert (await client.get(f"{BASE}/meetings/standup")).status == 404
            assert (await (await client.get(f"{BASE}/meetings")).json())["meetings"] == []

        assert not store.meeting_dir("standup", root).exists()
        assert not edit_dir.exists()

    @pytest.mark.asyncio
    async def test_delete_unknown_meeting_is_404_with_code(self, app):
        async with client_for(app) as client:
            resp = await client.delete(f"{BASE}/meetings/ghost")
            assert resp.status == 404
            assert (await resp.json())["code"] == "meeting_not_found"

    @pytest.mark.asyncio
    async def test_delete_waits_for_in_flight_initialization(
        self, app, root: Path, monkeypatch
    ):
        from kiro_crew.apps.builtins.meetings.backend.routes import meeting_lifecycle

        class ObservedLock:
            def __init__(self):
                self.lock = asyncio.Lock()
                self.waiter = asyncio.Event()

            async def __aenter__(self):
                if self.lock.locked():
                    self.waiter.set()
                await self.lock.acquire()
                return self

            async def __aexit__(self, *_args):
                self.lock.release()

        observed_lock = ObservedLock()
        real_init = meeting_lifecycle._init_meeting
        init_entered = threading.Event()
        release_init = threading.Event()

        def blocked_init(*args, **kwargs):
            init_entered.set()
            assert release_init.wait(timeout=5)
            return real_init(*args, **kwargs)

        monkeypatch.setattr(meeting_lifecycle, "START_LOCK", observed_lock)
        monkeypatch.setattr(meeting_lifecycle, "_init_meeting", blocked_init)

        async with client_for(app) as client:
            init_request = asyncio.create_task(
                client.post(f"{BASE}/meetings/standup/init", json={"title": "Standup"})
            )
            assert await asyncio.to_thread(init_entered.wait, 5)

            delete_request = asyncio.create_task(client.delete(f"{BASE}/meetings/standup"))
            await asyncio.wait_for(observed_lock.waiter.wait(), timeout=5)
            release_init.set()

            assert (await init_request).status == 200
            assert (await delete_request).status == 204

        assert not store.meeting_dir("standup", root).exists()

    @pytest.mark.asyncio
    async def test_delete_waits_for_in_flight_agent_toggle(
        self, app, root: Path, monkeypatch
    ):
        from kiro_crew.apps.builtins.meetings.backend.routes import agents, meeting_lifecycle

        class ObservedLock:
            def __init__(self):
                self.lock = asyncio.Lock()
                self.waiter = asyncio.Event()

            async def __aenter__(self):
                if self.lock.locked():
                    self.waiter.set()
                await self.lock.acquire()
                return self

            async def __aexit__(self, *_args):
                self.lock.release()

        async with client_for(app) as client:
            await client.post(f"{BASE}/meetings/standup/init", json={"title": "Standup"})

            observed_lock = ObservedLock()
            real_toggle = agents._toggle_agent_locked
            toggle_entered = asyncio.Event()
            release_toggle = asyncio.Event()

            async def blocked_toggle(*args, **kwargs):
                toggle_entered.set()
                await release_toggle.wait()
                return await real_toggle(*args, **kwargs)

            monkeypatch.setattr(agents, "START_LOCK", observed_lock)
            monkeypatch.setattr(meeting_lifecycle, "START_LOCK", observed_lock)
            monkeypatch.setattr(agents, "_toggle_agent_locked", blocked_toggle)

            toggle_request = asyncio.create_task(
                client.post(
                    f"{BASE}/meetings/standup/agents",
                    json={"agent_id": "sketch-artist", "enable": True},
                )
            )
            await asyncio.wait_for(toggle_entered.wait(), timeout=5)

            delete_request = asyncio.create_task(client.delete(f"{BASE}/meetings/standup"))
            await asyncio.wait_for(observed_lock.waiter.wait(), timeout=5)
            release_toggle.set()

            assert (await toggle_request).status == 200
            assert (await delete_request).status == 204

        assert not store.meeting_dir("standup", root).exists()

    @pytest.mark.asyncio
    async def test_task_mutation_cannot_resurrect_a_deleted_meeting(
        self, app, root: Path, monkeypatch
    ):
        from kiro_crew.apps.builtins.meetings.backend.routes import tasks as task_routes

        async with client_for(app) as client:
            await client.post(f"{BASE}/meetings/standup/init", json={"title": "Standup"})

            real_write_tasks = store.write_tasks
            write_entered = threading.Event()
            release_write = threading.Event()

            def blocked_write(meeting_id, tasks, data_root=None):
                write_entered.set()
                assert release_write.wait(timeout=5)
                return real_write_tasks(meeting_id, tasks, data_root)

            real_transaction = task_routes.task_mutation_transaction
            delete_attempted = threading.Event()

            def observed_transaction():
                delete_attempted.set()
                return real_transaction()

            monkeypatch.setattr(store, "write_tasks", blocked_write)
            monkeypatch.setattr(task_routes, "task_mutation_transaction", observed_transaction)

            add_request = asyncio.create_task(
                client.post(
                    f"{BASE}/meetings/standup/tasks",
                    json={"description": "Ship it"},
                )
            )
            assert await asyncio.to_thread(write_entered.wait, 5)

            delete_request = asyncio.create_task(client.delete(f"{BASE}/meetings/standup"))
            assert await asyncio.to_thread(delete_attempted.wait, 5)
            release_write.set()

            assert (await add_request).status == 200
            assert (await delete_request).status == 204

        assert not store.meeting_dir("standup", root).exists()

    @pytest.mark.asyncio
    async def test_add_task_after_delete_is_404_without_recreating_the_directory(
        self, app, root: Path
    ):
        async with client_for(app) as client:
            await client.post(f"{BASE}/meetings/standup/init", json={"title": "Standup"})
            assert (await client.delete(f"{BASE}/meetings/standup")).status == 204

            resp = await client.post(
                f"{BASE}/meetings/standup/tasks",
                json={"description": "Late task"},
            )
            assert resp.status == 404
            assert (await resp.json())["code"] == "meeting_not_found"

        assert not store.meeting_dir("standup", root).exists()

    @pytest.mark.asyncio
    async def test_delete_refuses_a_live_meeting(self, app, root: Path, fake_sessions):
        async with client_for(app) as client:
            await _start(client)
            resp = await client.delete(f"{BASE}/meetings/standup")
            assert resp.status == 409
            assert (await resp.json())["code"] == "meeting_active"
            assert _common.ACTIVE.get("standup") is not None

        assert store.read_meeting_meta("standup", root) is not None

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

    @pytest.mark.asyncio
    async def test_every_task_field_is_redacted_including_the_id(self, app, root: Path):
        """``tasks.json`` is AGENT-written, so the id is untrusted text too.

        It was the one field in ``_normalize_task`` that skipped the redaction pass
        while `description` beside it was scrubbed — so a credential-shaped id
        crossed to the dashboard unchanged. Asserted per field rather than on the
        whole blob, so a future field added without a `redact()` fails here.
        """
        marker = "AKIAIOSFODNN7EXAMPLE"
        store.write_tasks(
            "standup",
            [{
                "id": f"t-{marker}",
                "description": f"rotate {marker}",
                "assignee": marker,
                "context": marker,
                "labels": [marker],
            }],
            root,
        )
        async with client_for(app) as client:
            await client.post(f"{BASE}/meetings/standup/init", json={"title": "Standup"})
            body = await (await client.get(f"{BASE}/meetings/standup/tasks")).json()
        task = body["tasks"][0]
        for field in ("id", "description", "assignee", "context"):
            assert marker not in task[field], f"{field} reached the dashboard unredacted"
        assert marker not in "".join(task["labels"])
        # The id is still a usable handle — redaction replaces the secret, it does
        # not blank the field, or every later PATCH/DELETE would 404.
        assert task["id"]

    @pytest.mark.parametrize(
        ("labels", "expected"),
        [
            (1, []),                    # not iterable -> used to raise TypeError
            ("urgent", []),             # iterable, but per-CHARACTER -> junk labels
            ({"a": 1}, []),             # iterable over keys
            (None, []),
            (["a", 2, "b"], ["a", "b"]),  # a real list still keeps its strings
        ],
    )
    def test_malformed_labels_do_not_crash_or_smear(self, labels, expected) -> None:
        """`tasks.json` is agent-written, so `labels` is whatever the model emitted.

        `"labels": 1` is not iterable, so the comprehension raised `TypeError` — and
        `read_normalized` runs on EVERY outputs poll, so one such record made every
        poll answer 500 for the rest of the meeting.

        `"labels": "urgent"` is the quieter half: a string IS iterable, so it silently
        became `["u","r","g","e","n","t"]` — six junk labels instead of one. A
        truthiness check catches neither; the type check catches both.
        """
        from kiro_crew.apps.builtins.meetings.backend.routes import tasks as task_routes

        out = task_routes._normalize_task({"description": "d", "labels": labels})
        assert out is not None
        assert out["labels"] == expected


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
    async def test_disabling_one_default_agent_keeps_the_others(self, app):
        """An ABSENT `agents_enabled` means "the defaults", not "none".

        The toggle seeded its list with `meta.get("agents_enabled") or []`, so the
        FIRST toggle on a fresh meeting destroyed the roster: disabling one default
        agent persisted `[]`, which `get_enabled_agents` reads as an explicit empty
        roster rather than "use the defaults". The meeting then started with no
        agents at all and silently produced no notes and no diagrams.
        """
        async with client_for(app) as client:
            await client.post(f"{BASE}/meetings/standup/init", json={})
            resp = await client.post(
                f"{BASE}/meetings/standup/agents",
                json={"agent_id": "sketch-artist", "enable": False},
            )
            assert resp.status == 200
            remaining = (await resp.json())["agents_enabled"]
        # The one we turned off is gone; the other default survives.
        assert "sketch-artist" not in remaining
        assert "note-taker" in remaining, (
            "disabling one default agent must not disable the rest"
        )

    @pytest.mark.asyncio
    async def test_an_explicit_empty_roster_is_preserved(self, app):
        """Turning everything off is a real state, not a value to re-seed."""
        async with client_for(app) as client:
            await client.post(f"{BASE}/meetings/standup/init", json={})
            await client.post(
                f"{BASE}/meetings/standup/start", json={"agents_enabled": []}
            )
            resp = await client.post(
                f"{BASE}/meetings/standup/agents",
                json={"agent_id": "note-taker", "enable": True},
            )
            assert resp.status == 200
            # Only the agent just enabled — the defaults are NOT re-added.
            assert (await resp.json())["agents_enabled"] == ["note-taker"]

    def _default_output_names(self, root: Path) -> set[str]:
        """Output filenames the default roster would seed."""
        config = store.read_config(root)
        return {
            store.agent_output_filename(a) for a in sess.get_enabled_agents(config, None)
        }

    @pytest.mark.asyncio
    async def test_init_preserves_an_explicit_empty_roster(self, app, root):
        """On init too, `[]` means "no agents" — not "use the defaults".

        Init seeded its roster with ``field_str_list(...) or meta.get(...)``, and
        ``or`` is falsy on ``[]``, so an explicitly empty roster fell through to
        the default set: every default agent got an output file seeded and then
        ran on the meeting. ``field_str_list`` returns None for absent precisely
        so the two stay distinguishable.
        """
        expected_defaults = self._default_output_names(root)
        assert expected_defaults, "fixture config must define at least one default agent"

        async with client_for(app) as client:
            resp = await client.post(
                f"{BASE}/meetings/standup/init", json={"agents_enabled": []}
            )
            assert resp.status == 200

        seeded = {p.name for p in store.meeting_dir("standup", root).iterdir() if p.is_file()}
        assert not (seeded & expected_defaults), (
            "an explicitly empty roster must seed no agent output files"
        )

    @pytest.mark.asyncio
    async def test_init_without_a_roster_still_seeds_the_defaults(self, app, root):
        """The other direction: absent must keep meaning "use the defaults"."""
        expected_defaults = self._default_output_names(root)

        async with client_for(app) as client:
            resp = await client.post(f"{BASE}/meetings/standup/init", json={})
            assert resp.status == 200

        seeded = {p.name for p in store.meeting_dir("standup", root).iterdir() if p.is_file()}
        assert expected_defaults <= seeded

    @pytest.mark.asyncio
    async def test_an_illegal_status_transition_is_refused(self, app, fake_sessions):
        """The dashboard greys these out; the SERVER has to refuse them.

        The endpoint accepted any member of `VALID_STATUSES`, so an authenticated
        `POST status=idle` against an ACTIVE meeting persisted "idle" while the live
        session stayed installed — transcription stopped feeding a meeting the UI
        still showed as running, and starting another answered 409 because `ACTIVE`
        was still held. A state reachable through the API that the UI can neither
        produce nor explain.
        """
        async with client_for(app) as client:
            await _start(client)
            resp = await client.post(
                f"{BASE}/meetings/standup/status", json={"status": "idle"}
            )
            assert resp.status == 409
            body = await resp.json()
            assert body["code"] == "invalid_transition"
            # And the meeting is still active — the refusal changed nothing.
            meta = (await (await client.get(f"{BASE}/meetings/standup")).json())["meta"]
            assert meta["status"] == k.STATUS_ACTIVE

    @pytest.mark.asyncio
    async def test_a_legal_transition_still_works(self, app, fake_sessions):
        async with client_for(app) as client:
            await _start(client)
            resp = await client.post(
                f"{BASE}/meetings/standup/status", json={"status": "paused"}
            )
            assert resp.status == 200

    @pytest.mark.asyncio
    async def test_repeating_the_current_status_is_accepted(self, app, fake_sessions):
        """An idempotent retry of a request whose response was lost must not fail."""
        async with client_for(app) as client:
            await _start(client)
            resp = await client.post(
                f"{BASE}/meetings/standup/status", json={"status": "active"}
            )
            assert resp.status == 200

    def test_the_server_and_client_transition_tables_agree(self):
        """Two copies of one rule, so drift is the risk worth pinning.

        The client's is a UI affordance and the server's is enforcement; if they
        disagree the UI either offers a button the API refuses or hides one it
        allows. Parsed from the TS source rather than duplicated here, so the
        assertion breaks when either side changes alone.
        """
        import re

        source = (
            _REPO_ROOT / "website/src/apps/meetings/hooks/useMeetingSession.ts"
        ).read_text()
        block = re.search(
            r"ALLOWED_TRANSITIONS: Record<MeetingStatus, MeetingStatus\[\]> = \{(.*?)\n\}",
            source,
            re.S,
        )
        assert block is not None, "the client transition table was not found"
        client_rule = {
            row.group(1): set(re.findall(r"'([a-z]+)'", row.group(2)))
            for row in re.finditer(r"(\w+): \[([^\]]*)\]", block.group(1))
        }
        server_rule = {key: set(value) for key, value in k.ALLOWED_TRANSITIONS.items()}
        assert client_rule == server_rule

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
            assert body["segment"]["source"] == k.TRANSCRIPT_SOURCE_SPEECH
            assert "AKIAIOSFODNN7EXAMPLE" not in body["segment"]["text"]

            transcript = await client.get(f"{BASE}/meetings/standup/transcript")
            assert transcript.status == 200
            transcript_body = await transcript.json()
            assert transcript_body["segments"] == [body["segment"]]
            assert transcript_body["next_cursor"] > 0

            second = await client.post(
                f"{BASE}/meetings/standup/dispatch",
                json={"text": "second line"},
            )
            assert second.status == 200
            page = await client.get(
                f"{BASE}/meetings/standup/transcript",
                params={"cursor": transcript_body["next_cursor"]},
            )
            page_body = await page.json()
            assert [segment["text"] for segment in page_body["segments"]] == [
                "second line"
            ]
            assert page_body["next_cursor"] > transcript_body["next_cursor"]

    @pytest.mark.asyncio
    async def test_dispatch_marks_a_chat_line(self, app, fake_sessions):
        async with client_for(app) as client:
            await _start(client)
            resp = await client.post(
                f"{BASE}/meetings/standup/dispatch",
                json={"text": "actually the owner is Bob", "chat": True},
            )
            body = await resp.json()
            assert body["text"].startswith(k.CHAT_PREFIX)
            assert body["segment"]["source"] == k.TRANSCRIPT_SOURCE_TYPED
            assert body["segment"]["text"] == "actually the owner is Bob"

    @pytest.mark.asyncio
    async def test_transcript_for_a_legacy_meeting_is_empty(self, app):
        async with client_for(app) as client:
            await client.post(f"{BASE}/meetings/legacy/init", json={})
            resp = await client.get(f"{BASE}/meetings/legacy/transcript")
            assert resp.status == 200
            assert await resp.json() == {"segments": [], "next_cursor": 0}

    @pytest.mark.asyncio
    async def test_transcript_for_an_unknown_meeting_is_404(self, app):
        async with client_for(app) as client:
            resp = await client.get(f"{BASE}/meetings/missing/transcript")
            assert resp.status == 404
            assert (await resp.json())["code"] == "meeting_not_found"

    @pytest.mark.asyncio
    async def test_capacity_failure_happens_before_agent_fanout(
        self, app, fake_sessions, monkeypatch: pytest.MonkeyPatch
    ):
        async with client_for(app) as client:
            session = await _start_and_get_session(client)
            broadcasted: list[str] = []
            monkeypatch.setattr(session, "broadcast", lambda line: broadcasted.append(line))
            monkeypatch.setattr(store, "append_transcript", lambda *_args: None)

            resp = await client.post(
                f"{BASE}/meetings/standup/dispatch", json={"text": "must not fan out"}
            )
            assert resp.status == 413
            assert (await resp.json())["code"] == "transcript_too_large"
            assert broadcasted == []

    @pytest.mark.asyncio
    async def test_stop_waits_for_an_in_flight_transcript_append(
        self, app, root: Path, fake_sessions, monkeypatch: pytest.MonkeyPatch
    ):
        from kiro_crew.apps.builtins.meetings.backend.routes import (
            agents,
            meeting_lifecycle,
        )

        class ObservedLock:
            def __init__(self):
                self.lock = asyncio.Lock()
                self.waiter = asyncio.Event()

            async def __aenter__(self):
                if self.lock.locked():
                    self.waiter.set()
                await self.lock.acquire()
                return self

            async def __aexit__(self, *_args):
                self.lock.release()

        async with client_for(app) as client:
            await _start(client)
            observed_lock = ObservedLock()
            append_entered = threading.Event()
            release_append = threading.Event()
            real_append = store.append_transcript

            def blocked_append(*args, **kwargs):
                append_entered.set()
                assert release_append.wait(timeout=5)
                return real_append(*args, **kwargs)

            monkeypatch.setattr(agents, "DISPATCH_LOCK", observed_lock)
            monkeypatch.setattr(meeting_lifecycle, "DISPATCH_LOCK", observed_lock)
            monkeypatch.setattr(store, "append_transcript", blocked_append)

            dispatch_request = asyncio.create_task(
                client.post(f"{BASE}/meetings/standup/dispatch", json={"text": "kept"})
            )
            assert await asyncio.to_thread(append_entered.wait, 5)

            stop_request = asyncio.create_task(
                client.post(f"{BASE}/meetings/standup/stop")
            )
            await asyncio.wait_for(observed_lock.waiter.wait(), timeout=5)
            release_append.set()

            assert (await dispatch_request).status == 200
            assert (await stop_request).status == 200
            assert (await client.delete(f"{BASE}/meetings/standup")).status == 204

        assert not store.meeting_dir("standup", root).exists()

    @pytest.mark.asyncio
    async def test_review_status_waits_for_an_in_flight_transcript_append(
        self, app, fake_sessions, monkeypatch: pytest.MonkeyPatch
    ):
        from kiro_crew.apps.builtins.meetings.backend.routes import (
            agents,
            meeting_lifecycle,
        )

        class ObservedLock:
            def __init__(self):
                self.lock = asyncio.Lock()
                self.waiter = asyncio.Event()

            async def __aenter__(self):
                if self.lock.locked():
                    self.waiter.set()
                await self.lock.acquire()
                return self

            async def __aexit__(self, *_args):
                self.lock.release()

        async with client_for(app) as client:
            await _start(client)
            fake_sessions.calls.clear()

            observed_lock = ObservedLock()
            append_entered = threading.Event()
            release_append = threading.Event()
            real_append = store.append_transcript

            def blocked_append(*args, **kwargs):
                append_entered.set()
                assert release_append.wait(timeout=5)
                return real_append(*args, **kwargs)

            monkeypatch.setattr(agents, "DISPATCH_LOCK", observed_lock)
            monkeypatch.setattr(meeting_lifecycle, "DISPATCH_LOCK", observed_lock)
            monkeypatch.setattr(store, "append_transcript", blocked_append)

            dispatch_request = asyncio.create_task(
                client.post(f"{BASE}/meetings/standup/dispatch", json={"text": "kept"})
            )
            assert await asyncio.to_thread(append_entered.wait, 5)

            review_request = asyncio.create_task(
                client.post(
                    f"{BASE}/meetings/standup/status",
                    json={"status": k.STATUS_REVIEWING},
                )
            )
            await asyncio.wait_for(observed_lock.waiter.wait(), timeout=5)
            release_append.set()

            assert (await dispatch_request).status == 200
            assert (await review_request).status == 200

        assert any("kept" in message for _key, _agent, message in fake_sessions.calls)

    @pytest.mark.asyncio
    async def test_stop_closes_dispatch_admission_before_a_slow_agent_flush(
        self, app, fake_sessions, monkeypatch: pytest.MonkeyPatch
    ):
        async with client_for(app) as client:
            session = await _start_and_get_session(client)
            flush_entered = asyncio.Event()
            release_flush = asyncio.Event()

            async def slow_flush() -> None:
                flush_entered.set()
                await release_flush.wait()

            monkeypatch.setattr(session, "flush_all", slow_flush)
            stop_request = asyncio.create_task(
                client.post(f"{BASE}/meetings/standup/stop")
            )
            await asyncio.wait_for(flush_entered.wait(), timeout=5)

            dispatch = await asyncio.wait_for(
                client.post(
                    f"{BASE}/meetings/standup/dispatch",
                    json={"text": "too late"},
                ),
                timeout=1,
            )
            assert dispatch.status == 409
            assert (await dispatch.json())["code"] == "no_active_meeting"

            release_flush.set()
            assert (await stop_request).status == 200

        assert _common.ACTIVE.get() is None

    @pytest.mark.asyncio
    async def test_stopping_an_inactive_meeting_keeps_active_dispatch_open(
        self, app, fake_sessions
    ):
        async with client_for(app) as client:
            active = await _start_and_get_session(client, "active")
            await client.post(f"{BASE}/meetings/stale/init", json={})

            stopped = await client.post(f"{BASE}/meetings/stale/stop")
            assert stopped.status == 200
            assert _common.ACTIVE.get("active") is active

            dispatch = await client.post(
                f"{BASE}/meetings/active/dispatch",
                json={"text": "still accepted"},
            )
            assert dispatch.status == 200

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
    async def test_get_calendar_normalizes_all_day_on_legacy_cache_rows(self, app, root):
        """A cache written before `all_day` existed must still satisfy the wire type.

        The frontend declares `all_day: boolean` required, and the GET route
        serves the cache verbatim otherwise — so a pre-upgrade row must be
        normalized to `false` here, not left keyless for JSON.parse to pass
        through as `undefined`.
        """
        store.write_calendar_cache(
            [{"event_id": "legacy-1", "title": "Legacy", "start": "2026-08-20T00:00:00Z"}],
            root,
        )
        async with client_for(app) as client:
            body = await (await client.get(f"{BASE}/calendar")).json()
            assert body["events"][0]["all_day"] is False

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
            # Stem plus a digest of the original UID — sanitizing alone was not
            # injective, so two distinct UIDs could share a meeting folder.
            assert cached["events"][0]["event_id"].startswith("evt-1-")
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
            "agent_edits_root",
            "agent_edits_dir",
            "agent_edit_path",
            "read_agent_edit",
            "read_agent_edits",
            "write_agent_edit",
            "revert_agent_edit",
            "read_calendar_cache",
            "write_calendar_cache",
        }
    )

    #: Domain helpers that are themselves stacks of blocking store calls.
    _BLOCKING_DOMAIN_FNS = frozenset(
        {"start_meeting_meta", "end_meeting_meta", "reload_dictionary"}
    )

    def _route_modules(self) -> list:
        from kiro_crew.apps.builtins.meetings.backend import calendar_poller, calendar_sync
        from kiro_crew.apps.builtins.meetings.backend.routes import (
            agents,
            calendar,
            meeting_lifecycle,
            settings,
            tasks,
        )

        # The poller is not a route, but a periodic task is exactly as
        # loop-reachable as a handler, so it is held to the same rule.
        return [agents, calendar, meeting_lifecycle, settings, tasks, calendar_sync, calendar_poller]

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
        store.write_meeting_meta(
            meeting_id, store.new_meeting_meta(meeting_id, "Concurrent adds"), root
        )
        barrier = threading.Barrier(count)

        def add(index: int) -> None:
            barrier.wait()
            task_routes._append_task(meeting_id, {"description": f"added {index}"}, root)

        with futures.ThreadPoolExecutor(max_workers=count) as pool:
            list(pool.map(add, range(count)))

        described = {t["description"] for t in task_routes.read_normalized(meeting_id, root)}
        assert described == {f"added {i}" for i in range(count)}

    def test_duplicate_agent_written_task_ids_are_made_unique(self, root: Path) -> None:
        """`tasks.json` is agent-written, so nothing stops the model emitting `t1` twice.

        Every route keys on the id, so a duplicate made them act on the wrong rows:
        update edited only the first match while delete removed BOTH, and filing
        recorded the ref against one arbitrarily. The user sees two rows and can
        address neither reliably.
        """
        from kiro_crew.apps.builtins.meetings.backend import store as meetings_store
        from kiro_crew.apps.builtins.meetings.backend.routes import tasks as task_routes

        meeting_id = "m-dupe"
        meetings_store.write_tasks(
            meeting_id,
            [
                {"id": "t1", "description": "first"},
                {"id": "t1", "description": "second"},
                {"id": "t2", "description": "third"},
            ],
            root,
        )
        tasks = task_routes.read_normalized(meeting_id, root)

        ids = [t["id"] for t in tasks]
        assert len(ids) == len(set(ids)), f"duplicate ids survived: {ids}"
        # Renamed, NOT dropped — the second row is a real task the extractor found.
        assert [t["description"] for t in tasks] == ["first", "second", "third"]
        # Stable: the same file always normalizes to the same ids, so a client that
        # just read the list can still act on what it saw.
        assert [t["id"] for t in task_routes.read_normalized(meeting_id, root)] == ids

    def test_deleting_one_of_two_duplicate_ids_keeps_the_other(self, root: Path) -> None:
        """The consequence the rename prevents, asserted end to end."""
        from kiro_crew.apps.builtins.meetings.backend import store as meetings_store
        from kiro_crew.apps.builtins.meetings.backend.routes import tasks as task_routes

        meeting_id = "m-dupe-delete"
        meetings_store.write_tasks(
            meeting_id,
            [{"id": "t1", "description": "keep me"}, {"id": "t1", "description": "drop me"}],
            root,
        )
        tasks = task_routes.read_normalized(meeting_id, root)
        target = next(t for t in tasks if t["description"] == "drop me")

        remaining = task_routes._drop_task(meeting_id, target["id"], root)

        assert remaining is not None
        assert [t["description"] for t in remaining] == ["keep me"]

    def test_every_metadata_writer_holds_the_transaction(self) -> None:
        """Structural: no `write_meeting_meta` outside a `meta_transaction()`.

        This class of bug was reported FOUR times across four review rounds, each
        time at a different call site — the metadata routes, the agent toggle, the
        dictionary, then stop/start in the session domain. Fixing them one at a time
        is what let the next one through, so the invariant is asserted over the whole
        module instead: every function that writes meeting metadata must hold the
        lock across its read-modify-write.

        AST rather than grep so a call inside a comment or a docstring cannot satisfy
        it, and so the enclosing function is what gets checked.
        """
        import ast
        from pathlib import Path as _Path

        import kiro_crew.apps.builtins.meetings.backend as backend_pkg

        backend = _Path(backend_pkg.__file__).resolve().parent
        offenders: list[str] = []
        checked = 0
        for module in sorted(backend.rglob("*.py")):
            tree = ast.parse(module.read_text(encoding="utf-8"))
            functions = [
                n for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            ]
            for fn in functions:
                writes = [
                    n for n in ast.walk(fn)
                    if isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "write_meeting_meta"
                ]
                if not writes:
                    continue
                checked += 1
                holds = any(
                    isinstance(n, ast.With)
                    and any(
                        isinstance(item.context_expr, ast.Call)
                        and getattr(item.context_expr.func, "attr", "") == "meta_transaction"
                        for item in n.items
                    )
                    for n in ast.walk(fn)
                )
                # A `_locked` helper documents that its CALLER holds the lock; the
                # caller is itself covered by this same scan.
                if not holds and not fn.name.endswith("_locked"):
                    offenders.append(f"{module.relative_to(backend)}::{fn.name}")

        assert checked >= 6, (
            f"only {checked} metadata writers found — the scan is probably broken"
        )
        assert not offenders, (
            "these write meeting metadata without holding store.meta_transaction(), "
            "so a concurrent request can silently discard their update: "
            + ", ".join(offenders)
        )

    def test_concurrent_metadata_updates_all_survive(self, root: Path) -> None:
        """Same hazard as the task list, on `session.json`.

        Every mutating route reads the whole metadata dict, changes one key and
        writes it back — on worker threads, so two requests genuinely execute at
        once. Adding an attachment while another request muted an agent meant the
        second write clobbered the first, and BOTH reported success. `atomic_write`
        never helped: the write was atomic, the read-modify-write was not.
        """
        import threading
        from concurrent import futures

        from kiro_crew.apps.builtins.meetings.backend import store as meetings_store
        from kiro_crew.apps.builtins.meetings.backend.routes import agents as agent_routes
        from kiro_crew.apps.builtins.meetings.backend.routes import meeting_lifecycle as lifecycle

        meeting_id = "m-concurrent"
        meetings_store.write_meeting_meta(
            meeting_id, meetings_store.new_meeting_meta(meeting_id, "Concurrent"), root
        )

        count = 16
        barrier = threading.Barrier(count)

        def mutate(index: int) -> None:
            barrier.wait()  # maximize overlap on the read-modify-write
            if index % 2:
                agent_routes._apply_mute(meeting_id, f"agent-{index}", True, root)
            else:
                lifecycle._apply_attachments(
                    meeting_id,
                    {"action": "add", "attachments": [
                        {"type": "url", "url": f"https://example.test/{index}"},
                    ]},
                    root,
                )

        with futures.ThreadPoolExecutor(max_workers=count) as pool:
            list(pool.map(mutate, range(count)))

        meta = meetings_store.read_meeting_meta(meeting_id, root) or {}
        muted = set(meta.get("muted_agents") or [])
        urls = {a.get("url") for a in (meta.get("attachments") or [])}
        assert muted == {f"agent-{i}" for i in range(count) if i % 2}
        assert urls == {
            f"https://example.test/{i}" for i in range(count) if i % 2 == 0
        }

    def test_concurrent_agent_toggles_all_survive(self, root: Path) -> None:
        """Recomputing the roster inside the lock, not writing a stale list.

        An earlier fix re-read the metadata but still wrote the list its caller had
        derived BEFORE the dispatch await — so two rapid toggles both computed from
        the same stale roster and the later commit discarded the earlier one. The
        re-read only protected OTHER fields; the field being changed still lost.
        """
        import threading
        from concurrent import futures

        from kiro_crew.apps.builtins.meetings.backend import store as meetings_store
        from kiro_crew.apps.builtins.meetings.backend.routes import agents as agent_routes

        meeting_id = "m-toggles"
        meetings_store.write_meeting_meta(
            meeting_id, meetings_store.new_meeting_meta(meeting_id, "Toggles"), root
        )
        count = 12
        barrier = threading.Barrier(count)

        def toggle(index: int) -> None:
            barrier.wait()
            agent_routes._commit_toggle(
                meeting_id, f"agent-{index}", True, "", [], root
            )

        with futures.ThreadPoolExecutor(max_workers=count) as pool:
            list(pool.map(toggle, range(count)))

        meta = meetings_store.read_meeting_meta(meeting_id, root) or {}
        assert set(meta.get("agents_enabled") or []) == {
            f"agent-{i}" for i in range(count)
        }

    def test_concurrent_dictionary_edits_all_survive(self, root: Path) -> None:
        """One thread HOP is not one critical section.

        `asyncio.to_thread` hands each request to a different worker and they run
        concurrently, so grouping the reload/mutate/save into a single helper bounded
        the interleaving without preventing it: two adds both reloaded the same TOML,
        each appended a term, and the later save dropped the earlier one — while both
        reported success.
        """
        import threading
        from concurrent import futures

        from kiro_crew.apps.builtins.meetings.backend.routes import settings as settings_routes

        count = 12
        barrier = threading.Barrier(count)

        def add(index: int) -> None:
            barrier.wait()
            settings_routes._add_term(root, f"Term{index}", [f"alias {index}"])

        with futures.ThreadPoolExecutor(max_workers=count) as pool:
            list(pool.map(add, range(count)))

        # A superset check: the fixture seeds its own terms, and what matters is
        # that none of the concurrent adds was lost.
        terms = {t["correct"] for t in settings_routes._reload_terms(root)}
        assert {f"Term{i}" for i in range(count)} <= terms

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


class TestTeardownLeavesNoMeetingFalselyActive:
    """A dropped live session must not leave `active` behind on disk.

    The live session lives in this process's memory; the meeting's status lives in
    `session.json`. Dropping one without the other leaves them disagreeing, and that
    combination is worse than either half: the dashboard reads `active` and shows
    Live (its transcription binding keys off exactly that status, so the browser
    keeps recording), while every resulting dispatch answers 409 and the speech is
    DROPPED rather than queued. The notes just stop, mid-meeting, with the UI still
    claiming to listen — and `idle -> active` being the only way out of a fresh
    meeting means Start cannot recover it either.

    `ended` is both honest and recoverable: `ended -> active` is allowed, so Restart
    is exactly the affordance the user needs.
    """

    @pytest.mark.asyncio
    async def test_gateway_shutdown_marks_the_live_meeting_ended(
        self, app: web.Application, root: Path
    ) -> None:
        from kiro_crew.apps.builtins.meetings.backend import routes as routes_pkg

        async with client_for(app) as client:
            await _start(client, "shutdown-me")
            assert store.read_meeting_meta("shutdown-me", root)["status"] == k.STATUS_ACTIVE
            assert _common.ACTIVE.get("shutdown-me") is not None

            await routes_pkg._on_cleanup(app)

            # Both halves: the session is gone AND the disk no longer claims otherwise.
            assert _common.ACTIVE.get() is None
            assert store.read_meeting_meta("shutdown-me", root)["status"] == k.STATUS_ENDED

    @pytest.mark.asyncio
    async def test_shutdown_with_no_live_meeting_touches_nothing(
        self, app: web.Application, root: Path
    ) -> None:
        """A meeting that is merely ON DISK is not ours to end.

        Only the meeting whose live session we are dropping gets marked — an idle or
        paused meeting from an earlier run must keep its status, or a restart would
        silently end everything in the user's history.
        """
        from kiro_crew.apps.builtins.meetings.backend import routes as routes_pkg

        async with client_for(app) as client:
            await client.post(f"{BASE}/meetings/never-started/init", json={"title": "Later"})
        await routes_pkg._on_cleanup(app)
        assert store.read_meeting_meta("never-started", root)["status"] == k.STATUS_IDLE

    @pytest.mark.asyncio
    async def test_shutdown_flushes_before_it_marks_ended(
        self, app: web.Application, root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Order matters: the flush is what saves the queued transcript, and it can
        only run while the session still exists."""
        from kiro_crew.apps.builtins.meetings.backend import routes as routes_pkg

        order: list[str] = []

        async with client_for(app) as client:
            session = await _start_and_get_session(client, "ordered")

            original_flush = session.flush_all

            async def _tracked_flush() -> None:
                order.append("flush")
                await original_flush()

            monkeypatch.setattr(session, "flush_all", _tracked_flush)

            real_write = store.write_meeting_meta

            def _tracked_write(meeting_id: str, meta: Any, r: Any = None) -> None:
                if meta.get("status") == k.STATUS_ENDED:
                    order.append("ended")
                real_write(meeting_id, meta, r)

            monkeypatch.setattr(store, "write_meeting_meta", _tracked_write)
            await routes_pkg._on_cleanup(app)

        assert order == ["flush", "ended"], f"wrong teardown order: {order}"

    @pytest.mark.asyncio
    async def test_an_expired_session_is_also_marked_ended(
        self, app: web.Application, root: Path
    ) -> None:
        """The expiry path drops the session too, so it has the same obligation.

        Reported only against gateway shutdown, but a four-hour meeting whose next
        line arrives after the session lapsed reaches the identical state by a
        different route — and left `active` on disk in exactly the same way.
        """
        async with client_for(app) as client:
            session = await _start_and_get_session(client, "long-one")
            # Older than MAX_SESSION_DURATION, so `expired` is True.
            session.started_at -= k.MAX_SESSION_DURATION + 1

            resp = await client.post(
                f"{BASE}/meetings/long-one/dispatch", json={"text": "still talking"}
            )
            assert resp.status == 410

            assert _common.ACTIVE.get() is None
            assert store.read_meeting_meta("long-one", root)["status"] == k.STATUS_ENDED

    def test_every_teardown_path_persists_a_terminal_status(self) -> None:
        """Structural: dropping the live session and persisting the status must stay
        paired, so a NEW teardown path cannot reintroduce the divergence.

        The first version of this test exempted any function that also called
        `ACTIVE.set()`, reasoning that a replacement keeps the meeting live. That was
        wrong, and a reviewer caught what it let through: `handle_start_meeting`
        replaces the session of a DIFFERENT (expired) meeting, which is a teardown of
        that meeting — and the exemption made the check blind to exactly the one
        function where the pairing is easiest to miss.

        So there is no blanket exemption now. Every `drain_and_clear` caller must
        persist a terminal status, `set()` or not.
        """
        import ast
        import importlib
        import inspect

        from kiro_crew.apps.builtins.meetings.backend.routes import agents as agents_routes
        from kiro_crew.apps.builtins.meetings.backend.routes import (
            meeting_lifecycle,
        )

        routes_init = importlib.import_module("kiro_crew.apps.builtins.meetings.backend.routes")

        offenders: list[str] = []
        for module in (routes_init, agents_routes, meeting_lifecycle):
            tree = ast.parse(inspect.getsource(module))
            for fn in ast.walk(tree):
                if not isinstance(fn, (ast.AsyncFunctionDef, ast.FunctionDef)):
                    continue
                body = ast.dump(fn)
                if "attr='drain_and_clear'" not in body:
                    continue
                persists = "end_meeting_meta" in body or "STATUS_ENDED" in body
                if not persists:
                    offenders.append(f"{module.__name__}:{fn.name}")

        assert offenders == [], (
            "these paths drop the live session without persisting a terminal "
            "status, leaving the meeting falsely `active` on disk (dashboard shows "
            "Live, dispatches 409, speech is lost):\n  " + "\n  ".join(offenders)
        )

    @pytest.mark.asyncio
    async def test_replacing_an_expired_meeting_ends_the_one_it_evicts(
        self, app: web.Application, root: Path
    ) -> None:
        """Starting a meeting over an EXPIRED different one is a teardown of that one.

        Only an expired meeting can be replaced (the guard 409s otherwise), and it is
        gone for good — so leaving it `active` means two meetings persist as active at
        once, breaking the single-active invariant the list view reads, and reopening
        the evicted one dispatches into a session that no longer exists.
        """
        async with client_for(app) as client:
            first = await _start_and_get_session(client, "the-long-one")
            first.started_at -= k.MAX_SESSION_DURATION + 1  # expired, so replaceable

            await _start(client, "the-new-one")

            assert store.read_meeting_meta("the-long-one", root)["status"] == k.STATUS_ENDED
            assert store.read_meeting_meta("the-new-one", root)["status"] == k.STATUS_ACTIVE
            assert _common.ACTIVE.get("the-new-one") is not None

    @pytest.mark.asyncio
    async def test_restarting_the_same_meeting_keeps_it_active(
        self, app: web.Application, root: Path
    ) -> None:
        """The evict-and-end rule must not fire on a restart of the SAME meeting.

        `handle_start_meeting` drains and replaces the session on a restart too, so an
        unconditional "end the outgoing meeting" would mark the meeting that is being
        started as ended — writing `ended` over the `active` its own start just wrote.
        """
        async with client_for(app) as client:
            await _start(client, "same-one")
            await _start(client, "same-one", restart=True)

            assert store.read_meeting_meta("same-one", root)["status"] == k.STATUS_ACTIVE
            assert _common.ACTIVE.get("same-one") is not None

    @pytest.mark.asyncio
    async def test_startup_ends_meetings_orphaned_by_a_hard_kill(self, root: Path) -> None:
        """`_on_cleanup` cannot run when the process is SIGKILLed, and that is exactly
        when a meeting is most likely to be mid-flight — so the same false-`active`
        state is reachable by simply not letting the hook run.

        Startup is a sound place to repair it: `ACTIVE` is empty by construction in a
        fresh process, so any non-terminal status on disk is provably orphaned.
        """
        from kiro_crew.apps.builtins.meetings.backend import routes as routes_pkg

        # Three meetings a killed process could have left behind, one per
        # non-terminal status, written directly to stand in for "no cleanup ran".
        for meeting_id, status in (
            ("was-active", k.STATUS_ACTIVE),
            ("was-paused", k.STATUS_PAUSED),
            ("was-reviewing", k.STATUS_REVIEWING),
        ):
            meta = store.new_meeting_meta(meeting_id, meeting_id)
            meta["status"] = status
            store.write_meeting_meta(meeting_id, meta, root)

        app = make_app(root)
        await routes_pkg._on_startup(app)

        for meeting_id in ("was-active", "was-paused", "was-reviewing"):
            assert store.read_meeting_meta(meeting_id, root)["status"] == k.STATUS_ENDED, (
                f"{meeting_id} was left orphaned at boot"
            )

    @pytest.mark.asyncio
    async def test_startup_leaves_an_idle_meeting_alone(self, root: Path) -> None:
        """`idle` is not orphaned — a meeting can sit initialized-but-never-started
        across any number of restarts, and ending those would mark every meeting the
        user ever opened as finished. Same for one already `ended`."""
        from kiro_crew.apps.builtins.meetings.backend import routes as routes_pkg

        store.write_meeting_meta("fresh", store.new_meeting_meta("fresh", "Fresh"), root)
        ended = store.new_meeting_meta("done", "Done")
        ended["status"] = k.STATUS_ENDED
        ended["ended_at"] = "2026-01-01T00:00:00Z"
        store.write_meeting_meta("done", ended, root)

        await routes_pkg._on_startup(make_app(root))

        assert store.read_meeting_meta("fresh", root)["status"] == k.STATUS_IDLE
        # Untouched, not merely still terminal: re-ending it would rewrite `ended_at`
        # and lose when the meeting actually finished.
        assert store.read_meeting_meta("done", root)["ended_at"] == "2026-01-01T00:00:00Z"


class TestFiledTasksStayFiled:
    """`pushed` is terminal: it records that a task was really filed externally.

    It is not a third value the review endpoint may set — `_record_filing` writes it
    together with the `filed_ref` identifying the created item. Overwriting it dropped
    the task out of the filed set AND lost the ref, so the same action item could be
    filed a second time: two items in the tracker for one task.
    """

    @pytest.mark.asyncio
    async def test_archiving_a_filed_task_does_not_unfile_it(
        self, app: web.Application, root: Path
    ) -> None:
        async with client_for(app) as client:
            await client.post(f"{BASE}/meetings/m/init", json={"title": "M"})
            await client.post(f"{BASE}/meetings/m/tasks", json={"description": "ship it"})
            listed = (await (await client.get(f"{BASE}/meetings/m/tasks")).json())["tasks"]
            task_id = listed[0]["id"]

            filed = await client.post(f"{BASE}/meetings/m/tasks/file", json={"id": task_id})
            assert filed.status == 200, await filed.text()

            # The archive that used to clobber it — the Archive All / second-tab case.
            resp = await client.post(
                f"{BASE}/meetings/m/tasks/review",
                json={"id": task_id, "review_status": k.REVIEW_ARCHIVED},
            )
            assert resp.status == 200
            after = next(t for t in (await resp.json())["tasks"] if t["id"] == task_id)

        assert after["review_status"] == k.REVIEW_PUSHED, "a filed task was un-filed"
        # The ref is the half that makes a re-file a DUPLICATE rather than a retry.
        assert after.get("filed_ref"), "the filing reference was lost"

    @pytest.mark.asyncio
    async def test_an_unfiled_task_still_archives(
        self, app: web.Application, root: Path
    ) -> None:
        """The guard must be scoped to `pushed` — ordinary archiving still works."""
        async with client_for(app) as client:
            await client.post(f"{BASE}/meetings/m2/init", json={"title": "M2"})
            await client.post(f"{BASE}/meetings/m2/tasks", json={"description": "maybe"})
            listed = (await (await client.get(f"{BASE}/meetings/m2/tasks")).json())["tasks"]
            resp = await client.post(
                f"{BASE}/meetings/m2/tasks/review",
                json={"id": listed[0]["id"], "review_status": k.REVIEW_ARCHIVED},
            )
            body = await resp.json()
        assert body["tasks"][0]["review_status"] == k.REVIEW_ARCHIVED


class TestStartAndStopAreSerialized:
    """Agent initialization must not interleave with a teardown.

    `init_agents` is a long sequence of awaited dispatches and it used to run OUTSIDE
    `START_LOCK`. A stale Close in another tab could tear the session down midway, so
    the remaining agents were initialized into a session no longer installed while the
    start still answered `active` — a meeting the UI showed as running, with nothing
    live and `ended` on disk.
    """

    def test_initialization_happens_inside_the_start_lock(self) -> None:
        """AST: `init_agents` must sit inside `async with START_LOCK`, so a later edit
        cannot move it back out and reopen the window."""
        import ast
        import inspect

        from kiro_crew.apps.builtins.meetings.backend.routes import meeting_lifecycle

        tree = ast.parse(inspect.getsource(meeting_lifecycle))
        start = next(
            fn
            for fn in ast.walk(tree)
            if isinstance(fn, ast.AsyncFunctionDef) and fn.name == "handle_start_meeting"
        )
        guarded = [
            node
            for node in ast.walk(start)
            if isinstance(node, ast.AsyncWith)
            and "START_LOCK" in ast.dump(node.items[0].context_expr)
            and "init_agents" in ast.dump(ast.Module(body=node.body, type_ignores=[]))
        ]
        assert guarded, (
            "handle_start_meeting initializes agents outside START_LOCK; a concurrent "
            "stop can tear the session down mid-initialization"
        )

    def test_stop_takes_the_same_lock(self) -> None:
        """A one-sided lock is no lock: stop must contend for it too."""
        import ast
        import inspect

        from kiro_crew.apps.builtins.meetings.backend.routes import meeting_lifecycle

        tree = ast.parse(inspect.getsource(meeting_lifecycle))
        stop = next(
            fn
            for fn in ast.walk(tree)
            if isinstance(fn, ast.AsyncFunctionDef) and fn.name == "handle_stop_meeting"
        )
        locked = [
            node
            for node in ast.walk(stop)
            if isinstance(node, ast.AsyncWith)
            and "START_LOCK" in ast.dump(node.items[0].context_expr)
            and "attr='drain_and_clear'" in ast.dump(ast.Module(body=node.body, type_ignores=[]))
        ]
        assert locked, "handle_stop_meeting tears down without holding START_LOCK"

    @pytest.mark.asyncio
    async def test_a_stop_during_a_start_runs_after_it(
        self, app: web.Application, root: Path
    ) -> None:
        """End to end: the stop is ordered after initialization, so the meeting ends
        up consistently ENDED with no live session — not `active` with nothing live."""
        import asyncio

        async with client_for(app) as client:
            await client.post(f"{BASE}/meetings/racer/init", json={"title": "Racer"})
            start = asyncio.create_task(
                client.post(f"{BASE}/meetings/racer/start", json={})
            )
            # Let the start reach its first await, then race a stop against it.
            await asyncio.sleep(0)
            stop = asyncio.create_task(client.post(f"{BASE}/meetings/racer/stop", json={}))
            start_resp, stop_resp = await asyncio.gather(start, stop)
            assert start_resp.status == 200, await start_resp.text()
            assert stop_resp.status == 200, await stop_resp.text()

            # Whichever order they serialized in, the two halves agree.
            meta = store.read_meeting_meta("racer", root)
            live = _common.ACTIVE.get("racer")
        if meta["status"] == k.STATUS_ENDED:
            assert live is None, "ended on disk but a session is still installed"
        else:
            assert live is not None, "active on disk with no live session"


class TestATaskIsNeverFiledTwice:
    """The provider call is an AWAIT, so it needs an asyncio lock around it.

    `_TASKS_LOCK` is a `threading.Lock` held only across local file IO and never
    across an await — so two review tabs filing the same task both passed their
    `_prepare_filing` read, both created an item in the external tracker, and the
    second `_record_filing` overwrote the first's `filed_ref`. Two tracker items for
    one action item, with one reference lost and no way to find it again.
    """

    @pytest.mark.asyncio
    async def test_two_concurrent_filings_create_one_item(
        self, app: web.Application, root: Path
    ) -> None:
        import asyncio
        from unittest import mock

        from kiro_crew.apps.builtins.meetings.backend.providers import tasks as taskprov

        created: list[str] = []
        real_create = taskprov.LocalTaskProvider.create

        def _counting_create(self, draft):  # type: ignore[no-untyped-def]
            created.append(draft.description)
            return real_create(self, draft)

        async with client_for(app) as client:
            await client.post(f"{BASE}/meetings/dup/init", json={"title": "Dup"})
            await client.post(f"{BASE}/meetings/dup/tasks", json={"description": "file me once"})
            listed = (await (await client.get(f"{BASE}/meetings/dup/tasks")).json())["tasks"]
            task_id = listed[0]["id"]

            with mock.patch.object(taskprov.LocalTaskProvider, "create", _counting_create):
                first, second = await asyncio.gather(
                    client.post(f"{BASE}/meetings/dup/tasks/file", json={"id": task_id}),
                    client.post(f"{BASE}/meetings/dup/tasks/file", json={"id": task_id}),
                )
            # Both report success — the caller's intent is satisfied either way, and
            # failing the loser would make a double-click look like an error.
            assert first.status == 200, await first.text()
            assert second.status == 200, await second.text()
            final = (await (await client.get(f"{BASE}/meetings/dup/tasks")).json())["tasks"]

        assert len(created) == 1, f"the provider created {len(created)} items, not 1"
        filed = next(t for t in final if t["id"] == task_id)
        assert filed["review_status"] == k.REVIEW_PUSHED
        assert filed.get("filed_ref"), "the filing reference was lost"

    @pytest.mark.asyncio
    async def test_refiling_an_already_filed_task_returns_the_existing_ref(
        self, app: web.Application, root: Path
    ) -> None:
        """The sequential case: a retry of a request whose response was lost, or a
        double-click slow enough not to overlap. Same answer, no second item."""
        from unittest import mock

        from kiro_crew.apps.builtins.meetings.backend.providers import tasks as taskprov

        created: list[str] = []
        real_create = taskprov.LocalTaskProvider.create

        def _counting_create(self, draft):  # type: ignore[no-untyped-def]
            created.append(draft.description)
            return real_create(self, draft)

        async with client_for(app) as client:
            await client.post(f"{BASE}/meetings/retry/init", json={"title": "Retry"})
            await client.post(f"{BASE}/meetings/retry/tasks", json={"description": "once only"})
            listed = (await (await client.get(f"{BASE}/meetings/retry/tasks")).json())["tasks"]
            task_id = listed[0]["id"]

            with mock.patch.object(taskprov.LocalTaskProvider, "create", _counting_create):
                first = await client.post(f"{BASE}/meetings/retry/tasks/file", json={"id": task_id})
                second = await client.post(
                    f"{BASE}/meetings/retry/tasks/file", json={"id": task_id}
                )
            assert first.status == 200
            assert second.status == 200
            first_ref = (await first.json())["ref"]
            second_ref = (await second.json())["ref"]

        assert len(created) == 1, f"the provider created {len(created)} items, not 1"
        # The SAME ITEM, so a client that retried can still act on what it was told.
        # Compared on `id` rather than whole-dict: the second response echoes the
        # ref as PERSISTED (`_normalize_filed_ref` keeps only id and a linkable url),
        # while the first echoes the provider's live return with its `provider` and
        # `created_at`. The identity is what matters and is what a caller uses.
        assert second_ref["id"] == first_ref["id"]

    def test_the_whole_filing_sequence_is_under_one_lock(self) -> None:
        """AST: prepare, the provider call and the record must share a critical
        section, so a later edit cannot reopen the window by moving one out."""
        import ast
        import inspect

        from kiro_crew.apps.builtins.meetings.backend.routes import tasks as task_routes

        tree = ast.parse(inspect.getsource(task_routes))
        handler = next(
            fn
            for fn in ast.walk(tree)
            if isinstance(fn, ast.AsyncFunctionDef) and fn.name == "handle_file_task"
        )
        held = [
            node
            for node in ast.walk(handler)
            if isinstance(node, ast.AsyncWith)
            and "_FILING_LOCK" in ast.dump(node.items[0].context_expr)
        ]
        assert held, "handle_file_task does not hold _FILING_LOCK"

        # And the locked body must reach the create — either inline or via the helper
        # it delegates to, which is what actually runs the sequence.
        locked = ast.dump(ast.Module(body=held[0].body, type_ignores=[]))
        assert "_file_task_locked" in locked or "provider.create" in locked

    def test_deletion_holds_the_same_lock_as_filing(self) -> None:
        """AST: `handle_delete_task` must take `_FILING_LOCK` too.

        A filing is prepare -> provider-create -> record and the middle step is an
        AWAIT, which `_TASKS_LOCK` (a threading lock, held only across local file IO)
        cannot span. So a delete landing inside that gap removed the task while the
        external item had already been created — leaving an orphan nothing references,
        while the filing response reported success. Deleting is the other half of the
        critical section the filing already serializes, so it needs the same lock.
        """
        import ast
        import inspect

        from kiro_crew.apps.builtins.meetings.backend.routes import tasks as task_routes

        tree = ast.parse(inspect.getsource(task_routes))
        handler = next(
            fn
            for fn in ast.walk(tree)
            if isinstance(fn, ast.AsyncFunctionDef) and fn.name == "handle_delete_task"
        )
        held = [
            node
            for node in ast.walk(handler)
            if isinstance(node, ast.AsyncWith)
            and "_FILING_LOCK" in ast.dump(node.items[0].context_expr)
        ]
        assert held, "handle_delete_task does not hold _FILING_LOCK"
        # The DROP must be inside it — holding the lock around nothing is not the fix.
        locked = ast.dump(ast.Module(body=held[0].body, type_ignores=[]))
        assert "_drop_task" in locked, "the drop runs outside the lock"

    def test_recording_a_filing_for_a_vanished_task_fails_loudly(self, tmp_path) -> None:
        """The deeper flaw the lock alone does not fix.

        `_record_filing` used to `break` out of its loop when the id was absent, write
        the list unchanged, and RETURN — so a task removed by any path the lock does not
        order (the extractor agent rewriting `tasks.json`, a hand-edit, a future route)
        produced a real external item plus a success response and no reference to it.
        It must refuse instead, and must not rewrite the list a concurrent deleter just
        wrote.
        """
        from kiro_crew.apps.builtins.meetings.backend import store
        from kiro_crew.apps.builtins.meetings.backend.providers import tasks as taskprov
        from kiro_crew.apps.builtins.meetings.backend.routes import _common
        from kiro_crew.apps.builtins.meetings.backend.routes import tasks as task_routes

        root = tmp_path
        store.write_tasks("m1", [{"id": "survivor", "description": "kept"}], root)
        ref = taskprov.TaskRef(id="EXT-1", url="https://tracker.invalid/EXT-1", provider="p")

        with pytest.raises(_common.BadRequest) as excinfo:
            task_routes._record_filing("m1", "already-deleted", ref, root)
        assert excinfo.value.status == 409
        assert excinfo.value.code == "task_vanished_while_filing"

        # The surviving task is untouched — the failed record must not rewrite the list.
        # `read_tasks` returns the whole document, so index into `tasks`.
        remaining = store.read_tasks("m1", root)["tasks"]
        assert [t["id"] for t in remaining] == ["survivor"]


class TestATeardownNeverClearsAReplacement:
    """`drain_and_clear` must drop the session it DRAINED, not whatever is installed
    when the flush finishes.

    `flush_all` is an await, and not every caller holds `START_LOCK` — the
    expired-dispatch path in `agents.py` does not. So a concurrent start could install
    a new session during the flush, and the unconditional `clear()` removed THAT one:
    the meeting the user just started went live with nothing installed, and every
    subsequent line of its transcript was dropped with a 409. Exactly the loss this
    method exists to prevent, displaced by one meeting.
    """

    @pytest.mark.asyncio
    async def test_a_session_installed_during_the_flush_survives(self, root: Path) -> None:
        import asyncio

        from kiro_crew.apps.builtins.meetings.backend.routes import _common

        active = _common._ActiveMeeting()
        release = asyncio.Event()
        cancelled: list[str] = []

        class _SlowSession:
            def __init__(self, meeting_id: str) -> None:
                self.meeting_id = meeting_id
                self.agents: dict[str, object] = {}

            async def flush_all(self) -> None:
                # Park mid-teardown, which is the window the race needs.
                await release.wait()

            def cancel_all(self) -> None:
                cancelled.append(self.meeting_id)

        outgoing = _SlowSession("expired-one")
        replacement = _SlowSession("the-new-one")
        active.set(outgoing)  # type: ignore[arg-type]

        teardown = asyncio.create_task(active.drain_and_clear())
        await asyncio.sleep(0)  # let it reach the flush
        active.set(replacement)  # type: ignore[arg-type]
        release.set()
        drained = await asyncio.wait_for(teardown, timeout=5)

        # The replacement is still installed, and the drained session is what came back.
        assert active.get("the-new-one") is replacement
        assert drained is outgoing
        # The outgoing session's timers are still cancelled — it was flushed a moment
        # ago and must not fire against a session nobody holds.
        assert "expired-one" in cancelled
        assert "the-new-one" not in cancelled

    @pytest.mark.asyncio
    async def test_the_ordinary_teardown_still_clears(self, root: Path) -> None:
        """With no replacement, the session is dropped exactly as before."""
        from kiro_crew.apps.builtins.meetings.backend.routes import _common

        class _Session:
            meeting_id = "solo"
            agents: dict[str, object] = {}

            async def flush_all(self) -> None:
                pass

            def cancel_all(self) -> None:
                pass

        active = _common._ActiveMeeting()
        session = _Session()
        active.set(session)  # type: ignore[arg-type]

        drained = await active.drain_and_clear()
        assert drained is session
        assert active.get() is None


def _held_lines(session) -> list[str]:
    """Just the text of an init hold, dropping each entry's recipient snapshot."""
    return [line for line, _recipients in session.init_buffer]


class TestSpeechDuringAgentInitIsHeldNotRefused:
    """Issue #4610: the opening of a meeting must survive agent initialization.

    ``handle_start_meeting`` persists ``active``, installs the session, then awaits
    ``init_agents`` — a sequence of model turns measured at ~46s. Ingress is shut
    for that whole span, so every line spoken into it was answered 409 and lost:
    the notes and tasks began partway through the first topic with nothing to show
    a turn had been dropped.

    Speech in that window is now HELD and replayed in arrival order. The hold is
    bounded by line count, overflows by dropping the OLDEST, and announces a drop
    to both readers — the agents and the durable transcript.
    """

    @staticmethod
    async def _start_paused_in_init(
        client, monkeypatch: pytest.MonkeyPatch, meeting_id: str = "standup"
    ):
        """Begin a start that blocks INSIDE ``init_agents``.

        Returns ``(start_task, release)``: the in-flight request, and the event that
        lets initialization finish. Mirrors the real window — the session is
        installed and the meeting reads ``active``, but no agent knows its output
        file yet.
        """
        entered = asyncio.Event()
        release = asyncio.Event()
        real_init_agents = sess.init_agents

        async def blocking_init(session, meta, root=None):
            entered.set()
            await release.wait()
            await real_init_agents(session, meta, root)

        monkeypatch.setattr(sess, "init_agents", blocking_init)
        await client.post(f"{BASE}/meetings/{meeting_id}/init", json={"title": "Standup"})
        start = asyncio.create_task(
            client.post(f"{BASE}/meetings/{meeting_id}/start", json={})
        )
        await asyncio.wait_for(entered.wait(), timeout=5)
        return start, release

    @pytest.mark.asyncio
    async def test_speech_mid_init_is_buffered_and_delivered_in_order(
        self, app, fake_sessions, monkeypatch: pytest.MonkeyPatch
    ):
        """The core regression: three lines spoken during init reach the agents.

        RED on origin/main — each dispatch answers 409 ``no_active_meeting`` and the
        lines never enter a queue at all.
        """
        async with client_for(app) as client:
            start, release = await self._start_paused_in_init(client, monkeypatch)

            for line in ("first the agenda", "then the blockers", "and the owners"):
                resp = await client.post(
                    f"{BASE}/meetings/standup/dispatch", json={"text": line}
                )
                assert resp.status == 200, await resp.text()
                body = await resp.json()
                # Held, not fanned out — and already durable either way. The hold is
                # observed on the session below; the response says only that nothing
                # reached an agent yet.
                assert body["dispatched"] == 0
                assert body["segment"]["source"] == k.TRANSCRIPT_SOURCE_SPEECH

            session = _common.ACTIVE.get("standup")
            assert session is not None
            assert _held_lines(session) == [
                "first the agenda",
                "then the blockers",
                "and the owners",
            ]

            release.set()
            assert (await start).status == 200

            # Drained into every unmuted queue, in the order they were spoken.
            assert session.init_buffer == []
            for agent_id in ("note-taker", "sketch-artist", k.TASK_EXTRACTOR_ID):
                assert session.agents[agent_id].queue == [
                    "first the agenda",
                    "then the blockers",
                    "and the owners",
                ]

    @pytest.mark.asyncio
    async def test_the_transcript_holds_every_line_spoken_during_init(
        self, app, fake_sessions, monkeypatch: pytest.MonkeyPatch
    ):
        """The durable record is written at ARRIVAL, so a reader loses nothing."""
        async with client_for(app) as client:
            start, release = await self._start_paused_in_init(client, monkeypatch)
            await client.post(
                f"{BASE}/meetings/standup/dispatch", json={"text": "opening remarks"}
            )
            # Readable BEFORE initialization finishes — the user sees their own words.
            body = await (await client.get(f"{BASE}/meetings/standup/transcript")).json()
            assert [s["text"] for s in body["segments"]] == ["opening remarks"]

            release.set()
            assert (await start).status == 200

    @pytest.mark.asyncio
    async def test_a_typed_line_mid_init_keeps_its_chat_marking(
        self, app, fake_sessions, monkeypatch: pytest.MonkeyPatch
    ):
        """A held line goes through the same recorder as a live one."""
        async with client_for(app) as client:
            start, release = await self._start_paused_in_init(client, monkeypatch)
            body = await (
                await client.post(
                    f"{BASE}/meetings/standup/dispatch",
                    json={"text": "the owner is Bob", "chat": True},
                )
            ).json()
            assert body["dispatched"] == 0
            assert body["text"].startswith(k.CHAT_PREFIX)
            assert body["segment"]["source"] == k.TRANSCRIPT_SOURCE_TYPED

            release.set()
            assert (await start).status == 200

    @pytest.mark.asyncio
    async def test_a_held_line_is_still_redacted(
        self, app, fake_sessions, monkeypatch: pytest.MonkeyPatch
    ):
        """The hold must not become a way for a credential to reach an agent unredacted."""
        async with client_for(app) as client:
            start, release = await self._start_paused_in_init(client, monkeypatch)
            body = await (
                await client.post(
                    f"{BASE}/meetings/standup/dispatch",
                    json={"text": "rotate AKIAIOSFODNN7EXAMPLE today"},
                )
            ).json()
            assert "AKIAIOSFODNN7EXAMPLE" not in body["text"]
            assert "AKIAIOSFODNN7EXAMPLE" not in body["segment"]["text"]

            session = _common.ACTIVE.get("standup")
            assert session is not None
            assert not any("AKIAIOSFODNN7EXAMPLE" in line for line in _held_lines(session))

            release.set()
            assert (await start).status == 200

    @pytest.mark.asyncio
    async def test_the_hold_is_bounded_and_drops_the_oldest(
        self, app, fake_sessions, monkeypatch: pytest.MonkeyPatch
    ):
        """The cap holds, and overflow discards the OLDEST lines — never the newest.

        The bound is the security property: ``POST .../dispatch`` takes untrusted
        text, so an unbounded hold on that path would be a memory-exhaustion lever.
        """
        monkeypatch.setattr(k, "MAX_INIT_BUFFER_LINES", 3)
        async with client_for(app) as client:
            start, release = await self._start_paused_in_init(client, monkeypatch)

            for index in range(5):
                resp = await client.post(
                    f"{BASE}/meetings/standup/dispatch", json={"text": f"line number {index}"}
                )
                assert resp.status == 200

            session = _common.ACTIVE.get("standup")
            assert session is not None
            # Never more than the cap, and it is the TAIL that survived.
            assert len(session.init_buffer) == 3
            assert _held_lines(session) == ["line number 2", "line number 3", "line number 4"]
            assert session.init_dropped == 2

            release.set()
            assert (await start).status == 200

    @pytest.mark.asyncio
    async def test_an_overflow_marks_the_gap_for_the_agents_and_the_transcript(
        self, app, fake_sessions, monkeypatch: pytest.MonkeyPatch
    ):
        """A drop is announced to BOTH readers — a silent truncation is the real bug.

        The marker leads the drained lines, because drop-oldest puts the gap at the
        head of what survived.
        """
        monkeypatch.setattr(k, "MAX_INIT_BUFFER_LINES", 2)
        async with client_for(app) as client:
            start, release = await self._start_paused_in_init(client, monkeypatch)
            for index in range(4):
                await client.post(
                    f"{BASE}/meetings/standup/dispatch", json={"text": f"line number {index}"}
                )

            session = _common.ACTIVE.get("standup")
            assert session is not None
            release.set()
            assert (await start).status == 200

            expected_marker = k.SYSTEM_INIT_BUFFER_OVERFLOW.format(count=2, limit=2)
            # The agents are told first, then given what survived.
            assert session.agents["note-taker"].queue == [
                expected_marker,
                "line number 2",
                "line number 3",
            ]
            assert session.init_dropped == 0  # the tally is consumed by the drain

            # And the human transcript states it too, under a source the reader
            # keeps: `read_transcript_page` drops unrecognized sources, so a marker
            # written outside `VALID_TRANSCRIPT_SOURCES` would vanish on read.
            body = await (await client.get(f"{BASE}/meetings/standup/transcript")).json()
            markers = [
                s for s in body["segments"] if s["source"] == k.TRANSCRIPT_SOURCE_SYSTEM
            ]
            assert len(markers) == 1
            assert markers[0]["text"] == expected_marker
            # Every spoken line is still in the transcript — only the AGENTS lost two.
            assert [s["text"] for s in body["segments"] if s["source"] != "system"] == [
                "line number 0",
                "line number 1",
                "line number 2",
                "line number 3",
            ]

    @pytest.mark.asyncio
    async def test_no_marker_when_the_hold_did_not_overflow(
        self, app, fake_sessions, monkeypatch: pytest.MonkeyPatch
    ):
        """A meeting that fits inside the bound gets no system noise at all."""
        async with client_for(app) as client:
            start, release = await self._start_paused_in_init(client, monkeypatch)
            await client.post(
                f"{BASE}/meetings/standup/dispatch", json={"text": "just the one line"}
            )
            release.set()
            assert (await start).status == 200

            body = await (await client.get(f"{BASE}/meetings/standup/transcript")).json()
            assert [s["source"] for s in body["segments"]] == [k.TRANSCRIPT_SOURCE_SPEECH]

    @pytest.mark.asyncio
    async def test_the_poll_reports_the_hold_so_the_microphone_can_open(
        self, app, fake_sessions, monkeypatch: pytest.MonkeyPatch
    ):
        """``buffering_dispatches`` is the flag the dashboard's mic gate needs.

        Without it the client keeps the microphone shut for the whole window (its
        gate reads ``accepting_dispatches``), so nothing would ever reach the hold
        and the server-side fix would be unreachable.
        """
        async with client_for(app) as client:
            start, release = await self._start_paused_in_init(client, monkeypatch)
            live = (await (await client.get(f"{BASE}/meetings/standup")).json())["live"]
            # Direct fan-out is shut, but speech LANDS — held.
            assert live["accepting_dispatches"] is False
            assert live["buffering_dispatches"] is True

            release.set()
            assert (await start).status == 200

            live = (await (await client.get(f"{BASE}/meetings/standup")).json())["live"]
            assert live["accepting_dispatches"] is True
            assert live["buffering_dispatches"] is False

    @pytest.mark.asyncio
    async def test_a_reviewing_meeting_still_refuses_instead_of_buffering(
        self, app, fake_sessions
    ):
        """The #1981 gate is untouched: only INITIALIZATION holds a line.

        A reviewing meeting has nowhere to put the line — its agents were told to
        finalize — so it must keep answering 409 rather than quietly accumulating
        speech nothing will drain.
        """
        async with client_for(app) as client:
            await _start(client)
            status = await client.post(
                f"{BASE}/meetings/standup/status", json={"status": k.STATUS_REVIEWING}
            )
            assert status.status == 200

            resp = await client.post(
                f"{BASE}/meetings/standup/dispatch", json={"text": "too late"}
            )
            assert resp.status == 409
            assert (await resp.json())["code"] == "no_active_meeting"

            live = (await (await client.get(f"{BASE}/meetings/standup")).json())["live"]
            assert live["accepting_dispatches"] is False
            assert live["buffering_dispatches"] is False

    @pytest.mark.asyncio
    async def test_an_outgoing_session_being_replaced_does_not_buffer(
        self, app, fake_sessions, monkeypatch: pytest.MonkeyPatch
    ):
        """Replacing a lapsed meeting suspends the OUTGOING session to refuse, not hold.

        Its transcript is being flushed and its session dropped; a line held against
        it would be drained by nobody.
        """
        async with client_for(app) as client:
            await _start(client, "the-old-one")
            outgoing = _common.ACTIVE.get("the-old-one")
            assert outgoing is not None
            monkeypatch.setattr(
                type(outgoing), "expired", property(lambda _self: True)
            )

            start, release = await self._start_paused_in_init(
                client, monkeypatch, "the-new-one"
            )
            # The replaced meeting refuses; only the starting one holds.
            stale = await client.post(
                f"{BASE}/meetings/the-old-one/dispatch", json={"text": "orphan line"}
            )
            assert stale.status == 409
            held = await client.post(
                f"{BASE}/meetings/the-new-one/dispatch", json={"text": "live line"}
            )
            assert held.status == 200
            starting = _common.ACTIVE.get("the-new-one")
            assert starting is not None
            assert _held_lines(starting) == ["live line"]

            release.set()
            assert (await start).status == 200

    @pytest.mark.asyncio
    async def test_filler_does_not_consume_a_slot_and_evict_real_speech(
        self, app, fake_sessions, monkeypatch: pytest.MonkeyPatch
    ):
        """Recognizer filler must not push the opening out of a bounded hold.

        Filtering ran at DRAIN, so ``"uh"`` occupied a slot until initialization
        finished and only then was discarded — meaning a burst of filler could
        evict the genuine speech the cap was supposed to be protecting. A live
        line is filtered the instant it arrives; a held one must be too.
        """
        monkeypatch.setattr(k, "MAX_INIT_BUFFER_LINES", 3)
        async with client_for(app) as client:
            start, release = await self._start_paused_in_init(client, monkeypatch)

            await client.post(
                f"{BASE}/meetings/standup/dispatch", json={"text": "the real agenda"}
            )
            for filler in ("uh", "um", "ok so uh", "hmm"):
                resp = await client.post(
                    f"{BASE}/meetings/standup/dispatch", json={"text": filler}
                )
                assert resp.status == 200

            session = _common.ACTIVE.get("standup")
            assert session is not None
            # Four filler lines went in and none of them displaced anything.
            assert _held_lines(session) == ["the real agenda"]
            assert session.init_dropped == 0

            release.set()
            assert (await start).status == 200
            assert session.agents["note-taker"].queue == ["the real agenda"]

    @pytest.mark.asyncio
    async def test_a_mute_during_init_does_not_rob_earlier_speech(
        self, app, fake_sessions, monkeypatch: pytest.MonkeyPatch
    ):
        """Recipients are the ones the line HAD, not the ones it finds at drain.

        Resolving recipients at drain let a mute applied mid-initialization reach
        backwards: a line spoken while the agent was listening silently vanished
        from its output. The live path cannot produce that, because it fans out
        before the mute exists.
        """
        async with client_for(app) as client:
            start, release = await self._start_paused_in_init(client, monkeypatch)
            await client.post(
                f"{BASE}/meetings/standup/dispatch", json={"text": "said while listening"}
            )

            mute = await client.post(
                f"{BASE}/meetings/standup/mute",
                json={"agent_id": "note-taker", "muted": True},
            )
            assert mute.status == 200

            await client.post(
                f"{BASE}/meetings/standup/dispatch", json={"text": "said after the mute"}
            )

            session = _common.ACTIVE.get("standup")
            assert session is not None
            release.set()
            assert (await start).status == 200

            # The note-taker keeps what it was addressed, and gains nothing after.
            assert session.agents["note-taker"].queue == ["said while listening"]
            # An agent unmuted throughout got both lines.
            assert session.agents["sketch-artist"].queue == [
                "said while listening",
                "said after the mute",
            ]


class TestMuteCannotLandInsideADispatch:
    """A mute may not change the audience of a line already being recorded.

    `handle_mute_agent` wrote `session.muted_agents` with no lock, while both
    dispatch paths resolve their recipients from that set AFTER an awaited
    transcript write. A mute arriving inside that window re-addressed a line to
    the mute state of a moment after it was spoken — delivered to the wrong agent
    set on the live path, and recorded-then-replayed to the wrong set on the
    initialization hold (issue #4610).
    """

    @pytest.mark.asyncio
    async def test_a_mute_racing_the_transcript_write_keeps_the_line_s_audience(
        self, app, fake_sessions, monkeypatch: pytest.MonkeyPatch
    ):
        async with client_for(app) as client:
            start, release = await (
                TestSpeechDuringAgentInitIsHeldNotRefused._start_paused_in_init(
                    client, monkeypatch
                )
            )
            session = _common.ACTIVE.get("standup")
            assert session is not None

            entered_append = threading.Event()
            allow_append = threading.Event()
            real_append = store.append_transcript

            def blocking_append(*args, **kwargs):
                """Hold the worker thread inside the transcript write."""
                entered_append.set()
                allow_append.wait(5)
                return real_append(*args, **kwargs)

            monkeypatch.setattr(store, "append_transcript", blocking_append)

            dispatch = asyncio.create_task(
                client.post(
                    f"{BASE}/meetings/standup/dispatch",
                    json={"text": "said while the note-taker was listening"},
                )
            )
            # The dispatch now holds DISPATCH_LOCK and is parked in the append.
            await asyncio.to_thread(entered_append.wait, 5)

            mute = asyncio.create_task(
                client.post(
                    f"{BASE}/meetings/standup/mute",
                    json={"agent_id": "note-taker", "muted": True},
                )
            )
            # It must not be able to apply while the dispatch holds admission.
            await asyncio.sleep(0.05)
            assert not mute.done(), "the mute applied inside the dispatch's window"

            allow_append.set()
            assert (await dispatch).status == 200
            assert (await mute).status == 200

            # The held line kept the audience it had when it was spoken.
            recipients = [names for _line, names in session.init_buffer]
            assert len(recipients) == 1
            assert "note-taker" in recipients[0]

            release.set()
            assert (await start).status == 200
