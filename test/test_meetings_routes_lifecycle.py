"""HTTP route tests for a meeting's lifecycle, and the start/teardown invariants.

Init, start, status, stop and delete over the real aiohttp router, the active-meeting
holder and the outputs poll, plus the races around a live session: draining before
clearing, one critical section for start, start ordered against stop, no meeting left
falsely ``active``, and a teardown that never clears its replacement. The shared
fixtures and the fake session manager are in ``meetings_helpers``.
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
from meetings_routes_helpers import BASE, _start, _start_and_get_session

from kiro_crew.apps.builtins.meetings.backend import constants as k
from kiro_crew.apps.builtins.meetings.backend import store
from kiro_crew.apps.builtins.meetings.backend.routes import _common
from kiro_crew.testing.wait import await_until


class TestMeetingLifecycleRoutes:
    @pytest.mark.asyncio
    async def test_init_creates_the_folder_and_files(self, app, root: Path):
        async with client_for(app) as client:
            resp = await client.post(f"{BASE}/meetings/standup/init", json={"title": "My Standup"})
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
        """400, and it is worth saying WHICH of the two barriers answers.

        ``safe_meeting_id`` refuses this before ``contain`` ever sees it — the id is
        not ``[A-Za-z0-9._-]``, so it never becomes a path segment at all. That makes
        400 the right status here: the request is malformed, not forbidden.

        This assertion must not read ``in (400, 403, 404)``: that looseness is how
        a real bug survived: ``contain``'s own violation carries 403 and was being
        reported as 400, and no HTTP-level test could tell. The pair is now split —
        this one pins 400, and ``TestContainmentIsReportedAsForbidden`` pins 403.
        """
        async with client_for(app) as client:
            resp = await client.post(f"{BASE}/meetings/..%2F..%2Fetc/init", json={})
            assert resp.status == 400
            assert (await resp.json())["code"] == "invalid_path"

    @pytest.mark.asyncio
    async def test_a_symlinked_meeting_dir_is_403_not_400(self, app, root: Path, tmp_path: Path):
        """The regression test for the bug the loose assertion above was hiding.

        ``contain`` is the SECOND barrier, and the one that fires here: the id
        ``standup`` is perfectly legal, so ``safe_meeting_id`` passes it, and the
        violation only appears once ``resolve()`` follows a symlink planted inside the
        data dir and lands outside the root. That is the realistic reachable case —
        traversal in the id itself never gets this far.

        It carries 403 (``MeetingsPathError(status=403)``), but ``error_response`` had
        no ``FORBIDDEN`` branch, so every containment violation answered **400** for as
        long as this app has existed. A containment violation reported as "bad request"
        reads like a typo the caller can fix by retrying, when in fact the server just
        refused to leave its own data directory.

        Asserted at the HTTP boundary on purpose: ``test_meetings_store.py``'s
        ``test_outside_root_raises_403`` already proves the exception carries 403, and
        that test passed throughout — the status was lost in the route layer, which is
        the only place this can be caught.
        """
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        planted = store.meetings_root(root) / "standup"
        planted.parent.mkdir(parents=True, exist_ok=True)
        planted.symlink_to(outside, target_is_directory=True)

        async with client_for(app) as client:
            resp = await client.get(f"{BASE}/meetings/standup")
            assert resp.status == 403, "a containment violation must not read as 400"
            assert (await resp.json())["code"] == "invalid_path"

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
    @pytest.mark.parametrize(
        "blocker_status", [k.STATUS_ACTIVE, k.STATUS_PAUSED, k.STATUS_REVIEWING]
    )
    async def test_start_409_names_the_blocking_meeting(self, app, fake_sessions, blocker_status):
        """The 409 body names the blocking meeting and its status.

        Before this fix the body was ``{error, code}`` only, so the UI could not
        say which meeting held the slot or that it was a review still open.
        """
        async with client_for(app) as client:
            await _start(client, "first")
            await client.patch(f"{BASE}/meetings/first", json={"title": "Weekly sync"})
            if blocker_status != k.STATUS_ACTIVE:
                resp = await client.post(
                    f"{BASE}/meetings/first/status", json={"status": blocker_status}
                )
                assert resp.status == 200
            await client.post(f"{BASE}/meetings/second/init", json={})
            resp = await client.post(f"{BASE}/meetings/second/start", json={})
            assert resp.status == 409
            body = await resp.json()
            assert body["code"] == "meeting_already_active"
            assert body["blocking_meeting_title"] == "Weekly sync"
            assert body["blocking_meeting_status"] == blocker_status

    @pytest.mark.asyncio
    async def test_start_releases_an_abandoned_meeting_holding_the_latch(
        self, app, root: Path, fake_sessions
    ):
        """A meeting whose agent slots were retired must not block a fresh start.

        A gateway-wide session sweep (the dashboard's
        "Kiro identity changed" reconcile) retires a meeting's agent sessions
        while it is still INITIALIZING. The session object survives, never became
        dispatch-ready, and is not yet expired, so the single-active-meeting latch
        stayed held and every later start answered 409 -- the meeting was wedged
        with no live session.
        """
        async with client_for(app) as client:
            first = await _start_and_get_session(client, "first")
            # Model the sweep hitting mid-init: slots gone AND never became
            # dispatch-ready (an established meeting whose idle slots were reaped
            # keeps became_ready=True and stays recoverable, so it is NOT this).
            fake_sessions.live_keys = set()
            # The completed start above must have marked the session ready --
            # asserting it before the reset means deleting the readiness
            # assignment in the start path cannot survive this test.
            assert first.became_ready is True
            first.became_ready = False
            assert first.abandoned is True

            await client.post(f"{BASE}/meetings/second/init", json={})
            resp = await client.post(f"{BASE}/meetings/second/start", json={})
            assert resp.status == 200, await resp.text()
            # The new meeting took over the latch (checked while the app is up;
            # ACTIVE is cleared on shutdown when the client context exits).
            second = _common.ACTIVE.get("second")
            assert second is not None and second.meeting_id == "second"

        # The abandoned meeting is torn down terminal, not left persisted active
        # (two `active` meetings at once breaks the single-active invariant).
        first_meta = store.read_meeting_meta("first", root)
        assert first_meta is not None
        assert first_meta["status"] != k.STATUS_ACTIVE

    @pytest.mark.asyncio
    async def test_status_active_does_not_mark_a_never_ready_session_ready(
        self, app, root: Path, fake_sessions
    ):
        """The /status unpause path must not fake readiness.

        ``resume_dispatches`` is shared between the post-init start path and the
        ``/status active`` unpause path, and same-status ``active`` is accepted
        as an idempotent retry. If the unpause path set ``became_ready``, a
        routine status call against a retired-mid-init session would falsely
        mark it ready: ``abandoned`` goes False, the single-active latch
        re-wedges, and agents receive transcript with no output setup. Only the
        start path (after ``init_agents``) may mark readiness.
        """
        async with client_for(app) as client:
            first = await _start_and_get_session(client, "first")
            # Model the sweep hitting mid-init: slots gone, never dispatch-ready.
            fake_sessions.live_keys = set()
            first.became_ready = False
            assert first.abandoned is True

            # Idempotent same-status retry: reopens ingress via
            # resume_dispatches but must NOT mark the session ready.
            resp = await client.post(f"{BASE}/meetings/first/status", json={"status": "active"})
            assert resp.status == 200, await resp.text()
            assert first.became_ready is False
            assert first.abandoned is True

    @pytest.mark.asyncio
    async def test_restart_re_initializes_agents_and_then_notices(self, app, fake_sessions):
        """A restart must re-state OUTPUT_FILE, not just say "carry on".

        This assertion must not require that ONLY the restart notice was sent —
        that would encode the bug it was meant to describe. The agent slots are ordinary
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
            resp = await client.post(f"{BASE}/meetings/standup/start", json={"restart": True})
            assert resp.status == 200
        messages = [msg for _k, _a, msg in fake_sessions.calls]
        assert messages, "a restart dispatched nothing at all"
        # Every agent is told where to write...
        assert any(
            "OUTPUT_FILE:" in msg for msg in messages
        ), "a restarted meeting's agents were never given their OUTPUT_FILE"
        # ...and the notice still goes out, AFTER the instructions it qualifies.
        assert any(k.SYSTEM_MEETING_RESTARTED in msg for msg in messages)
        first_notice = next(
            i for i, msg in enumerate(messages) if k.SYSTEM_MEETING_RESTARTED in msg
        )
        assert any(
            "OUTPUT_FILE:" in msg for msg in messages[:first_notice]
        ), "the restart notice must follow the init messages it refers to"

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
                resp = await client.post(f"{BASE}/meetings/standup/status", json={"status": state})
                assert resp.status == 200
                assert (await resp.json())["status"] == state

    @pytest.mark.asyncio
    async def test_pausing_closes_ingress_and_resuming_reopens_it(self, app, fake_sessions):
        """Pause closes the server-side dispatch gate, not just the dashboard mic.

        A paused meeting refuses fan-out from every ingress (a second tab, the
        broadcast bar, a direct API call), so the holder's admission flag has to
        follow the status rather than being enforced only by the client. The
        ``active`` transition is the unpause path and reopens the gate.
        """
        async with client_for(app) as client:
            await _start(client)
            resp = await client.post(
                f"{BASE}/meetings/standup/status", json={"status": k.STATUS_PAUSED}
            )
            assert resp.status == 200
            live = (await (await client.get(f"{BASE}/meetings/standup")).json())["live"]
            assert live["accepting_dispatches"] is False
            assert live["buffering_dispatches"] is False

            resp = await client.post(f"{BASE}/meetings/standup/dispatch", json={"text": "held"})
            assert resp.status == 409
            assert (await resp.json())["code"] == "no_active_meeting"

            resp = await client.post(
                f"{BASE}/meetings/standup/status", json={"status": k.STATUS_ACTIVE}
            )
            assert resp.status == 200
            live = (await (await client.get(f"{BASE}/meetings/standup")).json())["live"]
            assert live["accepting_dispatches"] is True

    @pytest.mark.asyncio
    async def test_status_rejects_an_unknown_state(self, app, fake_sessions):
        async with client_for(app) as client:
            await _start(client)
            resp = await client.post(f"{BASE}/meetings/standup/status", json={"status": "banana"})
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
    async def test_delete_waits_for_in_flight_initialization(self, app, root: Path, monkeypatch):
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
            await await_until(observed_lock.waiter)
            release_init.set()

            assert (await init_request).status == 200
            assert (await delete_request).status == 204

        assert not store.meeting_dir("standup", root).exists()

    @pytest.mark.asyncio
    async def test_delete_waits_for_in_flight_agent_toggle(self, app, root: Path, monkeypatch):
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
            await await_until(toggle_entered)

            delete_request = asyncio.create_task(client.delete(f"{BASE}/meetings/standup"))
            await await_until(observed_lock.waiter)
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
            [
                {
                    "id": f"t-{marker}",
                    "description": f"rotate {marker}",
                    "assignee": marker,
                    "context": marker,
                    "labels": [marker],
                }
            ],
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
            (1, []),  # not iterable
            ("urgent", []),  # iterable, but per-CHARACTER -> junk labels
            ({"a": 1}, []),  # iterable over keys
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
        routes_init = importlib.import_module("kiro_crew.apps.builtins.meetings.backend.routes")

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

            # Both halves: the session is gone AND the disk does not claim otherwise.
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

        Calling `ACTIVE.set()` is no exemption: `handle_start_meeting` replaces the
        session of a DIFFERENT (expired) meeting, which is a teardown of that meeting,
        and it is the function where the pairing is easiest to miss.

        So every `drain_and_clear` caller must persist a terminal status, `set()` or
        not.
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
        the evicted one dispatches into a session that does not exist.
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
            assert (
                store.read_meeting_meta(meeting_id, root)["status"] == k.STATUS_ENDED
            ), f"{meeting_id} was left orphaned at boot"

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


class TestStartAndStopAreSerialized:
    """Agent initialization must not interleave with a teardown.

    `init_agents` is a long sequence of awaited dispatches and must run INSIDE
    `START_LOCK`. A stale Close in another tab could tear the session down midway, so
    the remaining agents would be initialized into a session that is gone while the
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
            start = asyncio.create_task(client.post(f"{BASE}/meetings/racer/start", json={}))
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
        drained = await await_until(teardown)

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
