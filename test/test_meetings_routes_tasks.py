"""HTTP route tests for the task and calendar routes, and the store rules behind them.

The task list and its filing (a filed ref is sanitized, a filed task stays filed and
is never filed twice), the calendar sync, body limits, the startup seed, the agent and
preset coercion layer, every metadata write held inside one transaction, and no route
handler calling the store on the event loop. The shared fixtures are in
``meetings_helpers``.
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
from meetings_routes_helpers import BASE

from kiro_crew.apps.builtins.meetings.backend import constants as k
from kiro_crew.apps.builtins.meetings.backend import store
from kiro_crew.apps.builtins.meetings.backend.routes import _common


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

            resp = await client.delete(f"{BASE}/meetings/standup/tasks", json={"id": task_id})
            assert (await resp.json())["tasks"] == []

    @pytest.mark.asyncio
    async def test_add_requires_a_description(self, app):
        async with client_for(app) as client:
            await client.post(f"{BASE}/meetings/standup/init", json={})
            assert (await client.post(f"{BASE}/meetings/standup/tasks", json={})).status == 400

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
            resp = await client.post(f"{BASE}/meetings/standup/tasks", json={"description": "d"})
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
            resp = await client.post(f"{BASE}/meetings/standup/tasks/file", json={"id": task_id})
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
            resp = await client.post(f"{BASE}/meetings/standup/tasks/file", json={"id": "ghost"})
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
            "kiro_crew.apps.builtins.meetings.backend.routes.tasks." "taskprov.get_task_provider",
            lambda *_a, **_kw: Failing(),
        )
        async with client_for(app) as client:
            await client.post(f"{BASE}/meetings/standup/init", json={})
            resp = await client.post(f"{BASE}/meetings/standup/tasks", json={"description": "d"})
            task_id = (await resp.json())["task"]["id"]
            resp = await client.post(f"{BASE}/meetings/standup/tasks/file", json={"id": task_id})
            assert resp.status == 502
            assert (await resp.json())["ok"] is False
        # The task must NOT be marked pushed when nothing was filed.
        assert store.read_tasks("standup", root)["tasks"][0]["review_status"] == (k.REVIEW_PENDING)

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
    a dispatch (an agent ref resolves WHICH agent runs, and a preset id
    becomes a filesystem path segment).
    """

    @staticmethod
    def _mod():
        from kiro_crew.apps.builtins.meetings.backend.routes import settings as mod

        return mod

    @pytest.mark.parametrize(
        "ref",
        [
            "../escape",  # traversal
            "/absolute/agent",  # absolute
            ".hidden",  # leading dot
            "has space",  # illegal char
            "semi;colon",
            "x" * 300,  # over the length cap
            "",
            None,
            123,  # not a string at all
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
            audio_import,
            calendar,
            meeting_lifecycle,
            settings,
            tasks,
        )

        # The poller is not a route, but a periodic task is exactly as
        # loop-reachable as a handler, so it is held to the same rule.
        return [
            agents,
            audio_import,
            calendar,
            meeting_lifecycle,
            settings,
            tasks,
            calendar_sync,
            calendar_poller,
        ]

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
                blocking = (owner.id == "store" and callee.attr in self._BLOCKING_STORE_FNS) or (
                    owner.id == "sess" and callee.attr in self._BLOCKING_DOMAIN_FNS
                )
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
        """``handle_list_meetings`` lists meetings via ``asyncio.to_thread``."""
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
            task_routes._set_review_state(meeting_id, f"t{index}", k.REVIEW_ARCHIVED, root)

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

        This class of bug has four known call sites: the metadata routes, the agent
        toggle, the dictionary, and stop/start in the session domain. A fix per call
        site leaves the next one open, so the invariant is asserted over the whole
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
                n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            ]
            for fn in functions:
                writes = [
                    n
                    for n in ast.walk(fn)
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

        assert checked >= 6, f"only {checked} metadata writers found — the scan is probably broken"
        assert not offenders, (
            "these write meeting metadata without holding store.meta_transaction(), "
            "so a concurrent request can silently discard their update: " + ", ".join(offenders)
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
                    {
                        "action": "add",
                        "attachments": [
                            {"type": "url", "url": f"https://example.test/{index}"},
                        ],
                    },
                    root,
                )

        with futures.ThreadPoolExecutor(max_workers=count) as pool:
            list(pool.map(mutate, range(count)))

        meta = meetings_store.read_meeting_meta(meeting_id, root) or {}
        muted = set(meta.get("muted_agents") or [])
        urls = {a.get("url") for a in (meta.get("attachments") or [])}
        assert muted == {f"agent-{i}" for i in range(count) if i % 2}
        assert urls == {f"https://example.test/{i}" for i in range(count) if i % 2 == 0}

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
            agent_routes._commit_toggle(meeting_id, f"agent-{index}", True, "", [], root)

        with futures.ThreadPoolExecutor(max_workers=count) as pool:
            list(pool.map(toggle, range(count)))

        meta = meetings_store.read_meeting_meta(meeting_id, root) or {}
        assert set(meta.get("agents_enabled") or []) == {f"agent-{i}" for i in range(count)}

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

            # The archive that would clobber it — the Archive All / second-tab case.
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
    async def test_an_unfiled_task_still_archives(self, app: web.Application, root: Path) -> None:
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

        `_record_filing` must not `break` out of its loop when the id is absent, write
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
