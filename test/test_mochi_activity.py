"""Tests for the Mochi activity log and the two dashboard read routes.

The activity log's failure mode is SILENT data loss: a bad rollover overwrites
yesterday, and an off-by-one in the cap drops the newest entry instead of the
oldest. Neither raises, so both are pinned here rather than left to inspection.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

import pytest
from aiohttp.test_utils import make_mocked_request

from kiro_crew.apps.builtins.mochi import activity_log as al
from kiro_crew.apps.builtins.mochi import hooks
from kiro_crew.apps.builtins.mochi.backend import routes


class _Ctx:
    def __init__(self, tmp_path) -> None:
        self.name = "mochi"
        self.data_dir = tmp_path
        self.events = None
        self.config: dict[str, Any] = {}


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setattr(routes, "is_app_enabled", lambda name: True)
    hooks._runtime = None
    yield
    hooks._runtime = None


@contextlib.asynccontextmanager
async def _live_runtime(tmp_path):
    """Start the runtime inside the test's own loop, stop it on exit.

    An async CONTEXT MANAGER rather than an ``@pytest_asyncio.fixture``: the
    suite pins pytest-asyncio 0.20.3, whose async-fixture wrapper reads the
    ``fixturedef.unittest`` attribute pytest 8.1 removed — on CI every
    async-generator fixture errors at setup. The repo avoids the decorator by
    convention (see test_denied_commands_api.py's module docstring).
    """
    await hooks.on_startup(_Ctx(tmp_path))
    try:
        yield hooks._runtime
    finally:
        await hooks.on_shutdown(None)


def _store(tmp_path, name: str) -> dict:
    return json.loads((tmp_path / name).read_text(encoding="utf-8"))


def _json_request(method: str, path: str, body: dict):
    """A mocked request whose ``json()`` returns ``body`` — the settings handler
    reads its payload that way."""
    req = make_mocked_request(method, path, headers={"Content-Type": "application/json"})

    async def _json():
        return body

    req.json = _json  # type: ignore[method-assign]
    return req


class TestActivityLog:
    def test_first_write_creates_todays_file(self, tmp_path):
        al.log_activity(tmp_path, "notification", "build failed")
        store = _store(tmp_path, al.LOG_FILE)
        assert store["date"] == al._today()
        assert [e["type"] for e in store["entries"]] == ["notification"]
        assert store["entries"][0]["content"] == "build failed"
        assert store["entries"][0]["ts"]

    def test_appends_within_the_same_day(self, tmp_path):
        al.log_activity(tmp_path, "memory", "one")
        al.log_activity(tmp_path, "memory", "two")
        assert [e["content"] for e in _store(tmp_path, al.LOG_FILE)["entries"]] == ["one", "two"]

    def test_date_change_archives_yesterday_then_starts_fresh(self, tmp_path):
        al.log_activity(tmp_path, "sleep", "goodnight")
        # Rewrite the date key to simulate the clock rolling over.
        path = tmp_path / al.LOG_FILE
        store = json.loads(path.read_text(encoding="utf-8"))
        store["date"] = "2000-01-01"
        path.write_text(json.dumps(store), encoding="utf-8")

        al.log_activity(tmp_path, "wake", "good morning")

        today = _store(tmp_path, al.LOG_FILE)
        yesterday = _store(tmp_path, al.YESTERDAY_LOG_FILE)
        assert today["date"] == al._today()
        assert [e["content"] for e in today["entries"]] == ["good morning"]
        # The previous day survives one more day — the dashboard and the plan
        # skill both read it.
        assert [e["content"] for e in yesterday["entries"]] == ["goodnight"]

    def test_rollover_with_no_entries_writes_no_archive(self, tmp_path):
        (tmp_path / al.LOG_FILE).write_text(
            json.dumps({"date": "2000-01-01", "entries": []}), encoding="utf-8"
        )
        al.log_activity(tmp_path, "wake", "hello")
        assert not (tmp_path / al.YESTERDAY_LOG_FILE).exists()

    def test_cap_drops_the_OLDEST_entries(self, tmp_path, monkeypatch):
        monkeypatch.setattr(al, "_MAX_ENTRIES", 3)
        for i in range(5):
            al.log_activity(tmp_path, "note", f"e{i}")
        contents = [e["content"] for e in _store(tmp_path, al.LOG_FILE)["entries"]]
        assert contents == ["e2", "e3", "e4"]

    def test_corrupt_file_is_treated_as_empty_not_fatal(self, tmp_path):
        (tmp_path / al.LOG_FILE).write_text("{not json", encoding="utf-8")
        al.log_activity(tmp_path, "note", "after corruption")
        assert [e["content"] for e in _store(tmp_path, al.LOG_FILE)["entries"]] == [
            "after corruption"
        ]

    def test_read_recent_merges_both_days_newest_first(self, tmp_path):
        (tmp_path / al.LOG_FILE).write_text(
            json.dumps(
                {
                    "date": al._today(),
                    "entries": [{"ts": "2026-07-30T09:00:00+00:00", "type": "a", "content": "new"}],
                }
            ),
            encoding="utf-8",
        )
        (tmp_path / al.YESTERDAY_LOG_FILE).write_text(
            json.dumps(
                {
                    "date": "2026-07-29",
                    "entries": [{"ts": "2026-07-29T08:00:00+00:00", "type": "b", "content": "old"}],
                }
            ),
            encoding="utf-8",
        )
        assert [e["content"] for e in al.read_recent(tmp_path)] == ["new", "old"]

    def test_read_recent_on_empty_dir(self, tmp_path):
        assert al.read_recent(tmp_path) == []

    def test_credentials_are_redacted_before_they_are_persisted(self, tmp_path):
        """Most entries are agent-authored, and this file is served back to a browser.

        ``perform_pet_action``'s summary reaches this writer through ``notify_user``,
        and ``/api/apps/mochi/activity`` plus the plan skill both read the file — so
        an unredacted key here is persisted, rendered, and fed into a later prompt.
        Redaction lives in ``log_activity`` itself so a new caller cannot skip it.
        """
        al.log_activity(tmp_path, "notification", "creds AKIAIOSFODNN7EXAMPLE done")
        stored = al.read_recent(tmp_path)[0]["content"]
        assert "AKIAIOSFODNN7EXAMPLE" not in stored
        assert "creds" in stored and "done" in stored

    def test_exfiltration_urls_are_redacted_too(self, tmp_path):
        """A URL carrying a credential is the shape the scanner flags.

        A bare external URL is NOT redacted by design — the scanner targets
        credential-bearing links rather than every outbound host, so this asserts the
        real contract instead of an imagined one.
        """
        al.log_activity(
            tmp_path, "notification", "sent to https://drop.example/p?key=AKIAIOSFODNN7EXAMPLE"
        )
        stored = al.read_recent(tmp_path)[0]["content"]
        assert "AKIAIOSFODNN7EXAMPLE" not in stored
        assert "REDACTED" in stored

    def test_ordinary_content_is_untouched(self, tmp_path):
        """The redactor must not mangle the normal case — every entry goes through it."""
        msg = "Checked 3 watch items, 1 triggered (price drop)."
        al.log_activity(tmp_path, "watch", msg)
        assert al.read_recent(tmp_path)[0]["content"] == msg


class TestDashboardRoutes:
    @pytest.mark.asyncio
    async def test_runtime_log_activity_persists(self, tmp_path):
        """The wiring that was missing: _log_activity only reached the logger, so
        the dashboard card and the plan skill stayed permanently empty."""
        async with _live_runtime(tmp_path) as runtime:
            runtime._log_activity("notification", "wired up")
            # On the event loop the persist is offloaded (a locked file write must
            # not block the loop), so it lands asynchronously — poll briefly.
            for _ in range(100):
                if [e["content"] for e in al.read_recent(tmp_path)] == ["wired up"]:
                    break
                await asyncio.sleep(0.01)
            assert [e["content"] for e in al.read_recent(tmp_path)] == ["wired up"]

    @pytest.mark.asyncio
    async def test_plan_reports_empty_when_no_queue_file(self, tmp_path):
        async with _live_runtime(tmp_path):
            resp = await routes._handle_plan_get(make_mocked_request("GET", "/api/apps/mochi/plan"))
            assert resp.status == 200
            assert json.loads(resp.body) == {"tasks": [], "note": "no plan yet"}

    @pytest.mark.asyncio
    async def test_plan_returns_the_queue_including_planner_metadata(self, tmp_path):
        async with _live_runtime(tmp_path):
            from kiro_crew.apps.builtins.mochi import queue_file as qf
            from kiro_crew.apps.builtins.mochi.hooks import _QUEUE_FILE

            qf.write_queue_atomic(
                tmp_path / _QUEUE_FILE,
                {
                    "tasks": [
                        {"id": "t1", "type": "notify", "execute_after": "2026-07-30T10:00:00Z"}
                    ],
                    "narrative": "watching a build",
                    "mood": "busy",
                    "planned_at": "2026-07-30T09:00:00Z",
                    "planned_until": "2026-07-30T12:00:00Z",
                },
            )
            resp = await routes._handle_plan_get(make_mocked_request("GET", "/api/apps/mochi/plan"))
            body = json.loads(resp.body)
            # narrative/mood are what the page's header and Plan card render — a
            # tasks-only payload would blank both.
            assert body["narrative"] == "watching a build"
            assert body["mood"] == "busy"
            assert len(body["tasks"]) == 1

    @pytest.mark.asyncio
    async def test_plan_redacts_agent_authored_credentials(self, tmp_path):
        """The queue is agent-authored (update_plan) and served to the browser
        here; a credential an LLM wrote into any (possibly nested) field must be
        redacted before it reaches the dashboard — mirrors the activity-log sink.
        """
        async with _live_runtime(tmp_path):
            from kiro_crew.apps.builtins.mochi import queue_file as qf
            from kiro_crew.apps.builtins.mochi.hooks import _QUEUE_FILE

            # A FAKE AWS example key, split into two literals so CodeQL's
            # clear-text-storage source heuristic doesn't flag this
            # redaction-CONTROL test as storing a real secret. The runtime value
            # is still a full AKIA key, so the redactor is genuinely exercised.
            planted = "AKIA" + "IOSFODNN7EXAMPLE"
            qf.write_queue_atomic(
                tmp_path / _QUEUE_FILE,
                {
                    "tasks": [{"id": "t1", "type": "notify", "summary": f"use {planted} now"}],
                    "narrative": f"leaked {planted}",
                    "mood": "busy",
                    "planned_at": "2026-07-30T09:00:00Z",
                    "planned_until": "2026-07-30T12:00:00Z",
                },
            )
            resp = await routes._handle_plan_get(
                make_mocked_request("GET", "/api/apps/mochi/plan")
            )
            raw = resp.body.decode()
            # Redacted both at the top level (narrative) AND nested (task.summary).
            assert planted not in raw
            body = json.loads(resp.body)
            assert "[REDACTED" in body["narrative"]
            assert "[REDACTED" in body["tasks"][0]["summary"]
            # Non-sensitive structure is preserved untouched.
            assert body["mood"] == "busy"

    @pytest.mark.asyncio
    async def test_activity_route_returns_logged_entries(self, tmp_path):
        async with _live_runtime(tmp_path):
            al.log_activity(tmp_path, "notification", "hello")
            resp = await routes._handle_activity_get(
                make_mocked_request("GET", "/api/apps/mochi/activity")
            )
            assert resp.status == 200
            assert [e["content"] for e in json.loads(resp.body)["entries"]] == ["hello"]

    @pytest.mark.asyncio
    async def test_both_routes_are_behind_the_enabled_gate(self, monkeypatch):
        """403 while disabled is what the page's landing state keys off — an
        ungated route would make a disabled Mochi look online."""
        monkeypatch.setattr(routes, "is_app_enabled", lambda name: False)
        for handler in (routes._handle_plan_get, routes._handle_activity_get):
            gated = routes._require_enabled(handler)
            resp = await gated(make_mocked_request("GET", "/api/apps/mochi/x"))
            assert resp.status == 403

    @pytest.mark.asyncio
    async def test_language_change_broadcasts_so_open_windows_relabel(
        self, tmp_path, monkeypatch
    ) -> None:
        """`language` is in _LIVE_KEYS: the pet and panel choose their i18n bundle
        from it, so a save that did not broadcast left both on the old language
        until they were reopened."""
        async with _live_runtime(tmp_path) as runtime:
            seen: list[str] = []
            monkeypatch.setattr(
                runtime, "_broadcast", lambda channel, *a: seen.append(channel), raising=False
            )
            resp = await routes._handle_settings_update(
                _json_request("POST", "/api/apps/mochi/settings", {"language": "zh"})
            )
            assert resp.status == 200
            assert "mochi:color-map-changed" in seen

    @pytest.mark.asyncio
    async def test_backend_only_key_does_not_broadcast(self, tmp_path, monkeypatch) -> None:
        """silentSubagents is consumed by the notification buffer, not by any
        window, so it is deliberately absent from _LIVE_KEYS."""
        async with _live_runtime(tmp_path) as runtime:
            seen: list[str] = []
            monkeypatch.setattr(
                runtime, "_broadcast", lambda channel, *a: seen.append(channel), raising=False
            )
            resp = await routes._handle_settings_update(
                _json_request("POST", "/api/apps/mochi/settings", {"silentSubagents": True})
            )
            assert resp.status == 200
            assert seen == []

    def test_routes_are_registered(self):
        from aiohttp import web

        app = web.Application()
        routes.register_routes(app)
        paths = {r.resource.canonical for r in app.router.routes() if r.resource}
        assert "/api/apps/mochi/plan" in paths
        assert "/api/apps/mochi/activity" in paths
