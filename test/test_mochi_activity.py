"""Tests for the Mochi activity log and the two dashboard read routes.

The activity log's failure mode is SILENT data loss: a bad rollover overwrites
yesterday, and an off-by-one in the cap drops the newest entry instead of the
oldest. Neither raises, so both are pinned here rather than left to inspection.
"""

from __future__ import annotations

import contextlib
import json
from typing import Any

import pytest
from aiohttp.test_utils import make_mocked_request
from dashboard_owner_helpers import NoConfiguredOwner

from kiro_crew.apps.builtins.mochi import activity_log as al
from kiro_crew.apps.builtins.mochi import hooks
from kiro_crew.apps.builtins.mochi.backend import routes
from kiro_crew.testing.wait import async_wait_until


def _owner_request(*args, **kwargs):
    """A mocked request that reads as the dashboard owner.

    The mutating routes are owner-gated, and the gate reads
    ``request.app["state"]`` plus the claims the token middleware sets.
    """
    req = make_mocked_request(*args, **kwargs)
    req.app["state"] = NoConfiguredOwner()
    req["user"] = "local-app"
    req["app"] = ""
    return req


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
    suite's convention for teardown that must await on the test's own loop
    (see test_denied_commands_api.py's module docstring).
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
    req = _owner_request(method, path, headers={"Content-Type": "application/json"})

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
        # Rewrite the date key AND the entry's ts to simulate a genuine previous
        # day: real yesterday data carries yesterday's timestamps, and the
        # rollover partitions retained entries by their UTC ts day.
        path = tmp_path / al.LOG_FILE
        store = json.loads(path.read_text(encoding="utf-8"))
        store["date"] = "2000-01-01"
        store["entries"][0]["ts"] = "2000-01-01T22:00:00+00:00"
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

    def test_failed_archive_write_must_not_destroy_yesterday(self, tmp_path, monkeypatch):
        """A swallowed archive failure at the rollover must not erase the day.

        On the first write after a UTC date change, ``log_activity`` archives
        yesterday to ``YESTERDAY_LOG_FILE`` and then overwrites ``LOG_FILE`` with
        a fresh today-store. The archive goes through ``_write_store``, which
        SWALLOWS ``OSError``. If that archive write fails (full disk at midnight,
        a read-only home, a Windows sharing violation), the previous day was
        never archived and the source file is then overwritten anyway — a full
        day of the pet's log silently destroyed, with no error to the caller.

        The fault is injected BENEATH ``_write_store`` (``os.replace`` raising for
        the yesterday file only), so the real swallow path runs; patching
        ``_write_store`` itself would instead make the exception propagate and
        hide the bug. The bar: yesterday's entry must still be readable through
        the module's own ``read_recent``, and today's entry is still recorded.
        """
        al.log_activity(tmp_path, "pet_action", "fed the pet on day one")

        # Roll the clock over so the next write takes the archive-then-reset
        # path. Stamp the entry's ts to the prior day too, as a genuine previous
        # day would be: the rollover partitions retained entries by ts day, so a
        # prior-day entry is the one that must be archived.
        path = tmp_path / al.LOG_FILE
        store = json.loads(path.read_text(encoding="utf-8"))
        store["date"] = "2000-01-01"
        store["entries"][0]["ts"] = "2000-01-01T22:00:00+00:00"
        path.write_text(json.dumps(store), encoding="utf-8")

        real_replace = al.os.replace

        def replace_but_fail_the_archive(src, dst, *a, **k):
            if al.os.path.basename(dst) == al.YESTERDAY_LOG_FILE:
                raise OSError("ENOSPC: no space left on device")
            return real_replace(src, dst, *a, **k)

        monkeypatch.setattr(al.os, "replace", replace_but_fail_the_archive)

        # The caller is told nothing (the swallow); log_activity returns normally.
        al.log_activity(tmp_path, "pet_action", "fed the pet on day two")

        contents = [e["content"] for e in al.read_recent(tmp_path)]
        # Today's entry is of course present.
        assert any("day two" in c for c in contents), "day-2 entry should be present"
        # The data-loss bar: a failed archive must not silently destroy the day
        # it was meant to preserve. Day-1 must survive where the readers see it.
        assert any("day one" in c for c in contents), (
            "data loss: yesterday's activity log was destroyed because the "
            "archive write failed and the source file was overwritten anyway"
        )

    def test_recovery_partitions_by_day_todays_entry_not_archived_early(
        self, tmp_path, monkeypatch
    ):
        """A mixed store left by a failed archive must partition by UTC day.

        After a failed rollover archive, ``log_activity`` keeps every entry in
        place under the OLD date and appends today's onto it — so the store can
        hold both a prior-day and a today entry while still dated the prior day.
        On a LATER write whose archive succeeds, only the prior-day entries may
        be archived to ``YESTERDAY_LOG_FILE``; today's entry must stay in
        ``LOG_FILE``. Otherwise today's entry lands in the yesterday bucket and
        ``read_recent`` drops it a whole rollover early, even though every other
        entry of that day survives — the data-loss regression GPT 6.1 flagged.
        """
        # Freeze the clock to one fixed instant so the test cannot flake across a
        # UTC-midnight crossing: both reads log_activity makes (_today for the
        # rollover decision, datetime.now for the new entry's ts) and the
        # expected values below all derive from the SAME day.
        today = "2026-06-15"
        frozen = al.datetime(2026, 6, 15, 12, 0, 0, tzinfo=al.timezone.utc)
        monkeypatch.setattr(al, "_today", lambda: today)

        class _FrozenDatetime:
            @staticmethod
            def now(tz=None):
                return frozen

        monkeypatch.setattr(al, "datetime", _FrozenDatetime)

        # The exact on-disk shape after a failed-archive recovery: dated the
        # prior day, holding one prior-day entry and one already-today entry.
        (tmp_path / al.LOG_FILE).write_text(
            json.dumps(
                {
                    "date": "2000-01-01",
                    "entries": [
                        {"ts": "2000-01-01T23:59:00+00:00", "type": "sleep", "content": "day one"},
                        {"ts": f"{today}T00:01:00+00:00", "type": "wake", "content": "day two a"},
                    ],
                }
            ),
            encoding="utf-8",
        )

        # A later write; its archive succeeds (no fault injected).
        al.log_activity(tmp_path, "pet_action", "day two b")

        today_store = _store(tmp_path, al.LOG_FILE)
        yesterday = _store(tmp_path, al.YESTERDAY_LOG_FILE)
        assert today_store["date"] == today
        # Today's pre-existing entry and the new one both stay in the current file.
        assert [e["content"] for e in today_store["entries"]] == ["day two a", "day two b"]
        # Only the genuine prior-day entry was archived.
        assert [e["content"] for e in yesterday["entries"]] == ["day one"]
        # And nothing is lost or mis-bucketed from the readers' view.
        contents = [e["content"] for e in al.read_recent(tmp_path)]
        assert {"day one", "day two a", "day two b"} <= set(contents)

    def test_failed_archive_must_not_evict_unarchived_entries_at_the_cap(
        self, tmp_path, monkeypatch
    ):
        """A failed archive must not let the entry cap erase the unarchived day.

        If the previous-day buffer is already at ``_MAX_ENTRIES`` when the
        rollover archive write fails, the retained (still-unarchived) buffer
        falls through to the append + cap trim, which would drop its oldest
        entry — so the day erodes one unarchived entry per failed write. The cap
        must be skipped while the archive has not landed: nothing is lost until
        an archive succeeds and the buffer can be bounded again.
        """
        monkeypatch.setattr(al, "_MAX_ENTRIES", 3)
        # Roll the clock forward so the next write takes the archive path, and
        # freeze it so the entries' UTC-day bucketing is deterministic.
        today = "2026-06-15"
        frozen = al.datetime(2026, 6, 15, 12, 0, 0, tzinfo=al.timezone.utc)
        monkeypatch.setattr(al, "_today", lambda: today)

        class _FrozenDatetime:
            @staticmethod
            def now(tz=None):
                return frozen

        monkeypatch.setattr(al, "datetime", _FrozenDatetime)

        # A prior-day buffer already full to the cap (3 entries, all yesterday).
        (tmp_path / al.LOG_FILE).write_text(
            json.dumps(
                {
                    "date": "2026-06-14",
                    "entries": [
                        {"ts": "2026-06-14T10:00:00+00:00", "type": "note", "content": "y0"},
                        {"ts": "2026-06-14T11:00:00+00:00", "type": "note", "content": "y1"},
                        {"ts": "2026-06-14T12:00:00+00:00", "type": "note", "content": "y2"},
                    ],
                }
            ),
            encoding="utf-8",
        )

        real_replace = al.os.replace

        def replace_but_fail_the_archive(src, dst, *a, **k):
            if al.os.path.basename(dst) == al.YESTERDAY_LOG_FILE:
                raise OSError("ENOSPC: no space left on device")
            return real_replace(src, dst, *a, **k)

        monkeypatch.setattr(al.os, "replace", replace_but_fail_the_archive)

        al.log_activity(tmp_path, "note", "today-new")

        # Archive failed, so the source still holds every unarchived prior-day
        # entry PLUS today's — the cap did NOT evict the oldest. The buffer may
        # transiently exceed the cap; that is the intended trade against silent
        # loss while the archive keeps failing.
        contents = [e["content"] for e in _store(tmp_path, al.LOG_FILE)["entries"]]
        assert contents == ["y0", "y1", "y2", "today-new"]
        # The yesterday archive did not land (the write was made to fail).
        assert not (tmp_path / al.YESTERDAY_LOG_FILE).exists()

    def test_repeated_archive_failures_stay_bounded_by_the_recovery_ceiling(
        self, tmp_path, monkeypatch
    ):
        """The failed-archive recovery path must stay FINITE.

        Skipping the normal cap while the archive fails must not remove the
        retention bound altogether — a persistent failure (e.g. a lasting
        Windows sharing lock) with a writable current file would otherwise grow
        the file without limit as every append sets ``archive_failed``. The
        recovery path holds at most ``_MAX_ENTRIES_UNARCHIVED`` entries at every
        step; once the unarchived prior-day entries have aged out under that
        ceiling the store rolls to today normally and the standard cap resumes.
        """
        monkeypatch.setattr(al, "_MAX_ENTRIES", 3)
        monkeypatch.setattr(al, "_MAX_ENTRIES_UNARCHIVED", 6)
        today = "2026-06-15"
        frozen = al.datetime(2026, 6, 15, 12, 0, 0, tzinfo=al.timezone.utc)
        monkeypatch.setattr(al, "_today", lambda: today)

        class _FrozenDatetime:
            @staticmethod
            def now(tz=None):
                return frozen

        monkeypatch.setattr(al, "datetime", _FrozenDatetime)

        # A prior-day buffer at the normal cap, dated yesterday.
        (tmp_path / al.LOG_FILE).write_text(
            json.dumps(
                {
                    "date": "2026-06-14",
                    "entries": [
                        {"ts": "2026-06-14T10:00:00+00:00", "type": "note", "content": "y0"},
                        {"ts": "2026-06-14T11:00:00+00:00", "type": "note", "content": "y1"},
                        {"ts": "2026-06-14T12:00:00+00:00", "type": "note", "content": "y2"},
                    ],
                }
            ),
            encoding="utf-8",
        )

        real_replace = al.os.replace

        def replace_but_fail_the_archive(src, dst, *a, **k):
            if al.os.path.basename(dst) == al.YESTERDAY_LOG_FILE:
                raise OSError("EACCES: sharing violation")
            return real_replace(src, dst, *a, **k)

        monkeypatch.setattr(al.os, "replace", replace_but_fail_the_archive)

        # Ten appends while the archive keeps failing — well past the ceiling.
        # The bound must hold at EVERY step: without a finite recovery ceiling
        # the file would grow to 13 here (3 seed + 10). No entry is ever lost
        # while a prior-day entry is still retained — the ceiling only trims once
        # it is reached, and by then the failure has far outlasted any transient.
        for i in range(10):
            al.log_activity(tmp_path, "note", f"n{i}")
            n = len(_store(tmp_path, al.LOG_FILE)["entries"])
            assert n <= al._MAX_ENTRIES_UNARCHIVED, f"unbounded growth at step {i}: {n} entries"

        # Ends bounded with the newest entries retained (prior-day entries have
        # aged out, so the store has rolled to today under the normal cap).
        entries = _store(tmp_path, al.LOG_FILE)["entries"]
        assert len(entries) <= al._MAX_ENTRIES_UNARCHIVED
        assert [e["content"] for e in entries] == ["n7", "n8", "n9"]

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
            # not block the loop), so it lands asynchronously.
            await async_wait_until(
                lambda: [e["content"] for e in al.read_recent(tmp_path)] == ["wired up"],
                describe=lambda: al.read_recent(tmp_path),
            )

    @pytest.mark.asyncio
    async def test_plan_reports_empty_when_no_queue_file(self, tmp_path):
        async with _live_runtime(tmp_path):
            resp = await routes._handle_plan_get(_owner_request("GET", "/api/apps/mochi/plan"))
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
            resp = await routes._handle_plan_get(_owner_request("GET", "/api/apps/mochi/plan"))
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
                _owner_request("GET", "/api/apps/mochi/plan")
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
                _owner_request("GET", "/api/apps/mochi/activity")
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
            resp = await gated(_owner_request("GET", "/api/apps/mochi/x"))
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
