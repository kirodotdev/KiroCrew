"""Per-slot per-turn usage rows: the app-facing read side.

``usage.slot_turn_usage(slot, days)`` returns one row per turn of one session —
tokens, credits, duration, context meter — from the same shards ``slot_spend``
aggregates; ``telemetry.api_usage_turns`` is the thin HTTP wrapper an app is
granted through its manifest's ``permissions.api``. Mirrors
test_context_trace's temp-shard fixture and drives the REAL reader over
synthetic rows.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers import usage as usage_mod
from kiro_crew.dashboard.handlers.telemetry import api_usage_turns
from kiro_crew.dashboard.handlers.usage import TURN_USAGE_FIELDS, slot_turn_usage


@pytest.fixture(autouse=True)
def _isolated_shards(tmp_path, monkeypatch):
    monkeypatch.setattr(usage_mod, "_TOKEN_USAGE_DIR", tmp_path)
    return tmp_path


def _row(slot, *, ts=None, **extra):
    row = {
        "_type": "tokens",
        "ts": ts or datetime.now(timezone.utc).isoformat(),
        "slot": slot,
        "model": "some-model",
        "input": 1000,
        "output": 200,
        "cache_create": 0,
        "cache_read": 5000,
        "credits": 1.25,
        "cost": 0.0,
        "duration_ms": 42000,
        "context_used": 74000,
        "context_window": 200000,
    }
    row.update(extra)
    return row


def _write(shard_dir, rows, day=None):
    day = day or datetime.now().astimezone().strftime("%Y-%m-%d")
    path = shard_dir / f"{day}.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    return path


class TestSlotTurnUsage:
    def test_returns_only_the_named_slots_rows(self, _isolated_shards):
        _write(
            _isolated_shards,
            [
                _row("endless-run-abc"),
                _row("chat-1-123"),
                _row("endless-run-abc", credits=2.5),
            ],
        )
        turns = slot_turn_usage("endless-run-abc")
        assert len(turns) == 2
        assert [t["credits"] for t in turns] == [1.25, 2.5]
        assert all(t["model"] == "some-model" for t in turns)

    def test_every_declared_field_rides_along(self, _isolated_shards):
        _write(_isolated_shards, [_row("s1")])
        (turn,) = slot_turn_usage("s1")
        for field in TURN_USAGE_FIELDS:
            assert field in turn, f"declared field {field} missing from the row"

    def test_an_oversized_integer_field_does_not_crash_the_reader(self, _isolated_shards):
        # math.isfinite(huge_int) raises OverflowError on the float conversion;
        # a corrupt row must degrade, never 500 the endpoint. bool is an int
        # subclass and is not a count.
        _write(_isolated_shards, [_row("s1", input=10**400, credits=True)])
        (turn,) = slot_turn_usage("s1")
        assert turn["input"] == 10**400, "a large integer is a value, not corruption"
        assert "credits" not in turn

    def test_non_numeric_and_non_finite_values_are_dropped(self, _isolated_shards):
        _write(
            _isolated_shards,
            [_row("s1", credits="oops", duration_ms=float("inf"))],
        )
        (turn,) = slot_turn_usage("s1")
        assert "credits" not in turn
        assert "duration_ms" not in turn
        assert turn["input"] == 1000, "the bad fields are dropped, not the row"

    def test_torn_tail_lines_and_foreign_types_are_skipped(self, _isolated_shards):
        path = _write(_isolated_shards, [_row("s1"), {"_type": "other", "slot": "s1"}])
        with path.open("a") as fh:
            fh.write('{"_type": "tokens", "slot": "s1", "trunc')
        assert len(slot_turn_usage("s1")) == 1

    def test_an_unknown_slot_is_an_empty_list(self, _isolated_shards):
        _write(_isolated_shards, [_row("s1")])
        assert slot_turn_usage("nobody") == []

    def test_rows_outside_the_window_are_not_read(self, _isolated_shards):
        old_day = (datetime.now().astimezone() - timedelta(days=30)).strftime("%Y-%m-%d")
        _write(_isolated_shards, [_row("s1")], day=old_day)
        _write(_isolated_shards, [_row("s1", credits=9.0)])
        turns = slot_turn_usage("s1", days=2)
        assert [t["credits"] for t in turns] == [9.0]


class TestApiUsageTurns:
    @staticmethod
    def _app() -> web.Application:
        app = web.Application()
        app.router.add_get("/api/usage/turns", api_usage_turns)
        return app

    @pytest.mark.asyncio
    async def test_slot_is_required(self):
        async with TestClient(TestServer(self._app())) as client:
            resp = await client.get("/api/usage/turns")
            assert resp.status == 400
            assert (await resp.json())["code"] == "slot_required"

    @pytest.mark.asyncio
    async def test_returns_the_slots_turns(self, _isolated_shards):
        _write(_isolated_shards, [_row("endless-run-abc")])
        async with TestClient(TestServer(self._app())) as client:
            resp = await client.get("/api/usage/turns", params={"slot": "endless-run-abc"})
            assert resp.status == 200
            body = await resp.json()
            assert body["slot"] == "endless-run-abc"
            assert len(body["turns"]) == 1
            assert body["turns"][0]["credits"] == 1.25

    @pytest.mark.asyncio
    async def test_days_is_clamped_not_refused(self, _isolated_shards):
        _write(_isolated_shards, [_row("s1")])
        async with TestClient(TestServer(self._app())) as client:
            for bad in ("9999", "0", "-3", "banana"):
                resp = await client.get("/api/usage/turns", params={"slot": "s1", "days": bad})
                assert resp.status == 200, f"days={bad} must clamp, not fail"
                body = await resp.json()
                assert 1 <= body["days"] <= usage_mod.SPEND_WINDOW_DAYS


class TestAppIsolation:
    """App Kit §5.2 on the usage read: ownership lives on the ROW.

    An app caller receives only rows stamped with its own app at write time —
    however the slot is named, and whether or not it is still live. A live-slot
    check was deliberately rejected: it leaks on slot-name reuse (a recreated
    slot would vouch for the previous owner's retained rows) and denies an app
    its own completed sessions, which are exactly what an audit reads. Rows
    that predate the stamp are invisible to app callers: deny-by-default for
    ownership that was never recorded.
    """

    @staticmethod
    def _app(request_app: str = "") -> web.Application:
        app = web.Application()

        @web.middleware
        async def stamp(request, handler):
            request["app"] = request_app
            return await handler(request)

        app.middlewares.append(stamp)
        app.router.add_get("/api/usage/turns", api_usage_turns)
        return app

    @pytest.fixture(autouse=True)
    def _quiet_sel(self, monkeypatch):
        import kiro_crew.sel as sel_mod
        from kiro_crew.dashboard.handlers import telemetry as tele_mod

        calls: list[dict] = []

        class _Sel:
            def log_api_access(self, **kw):
                calls.append(kw)

        monkeypatch.setattr(sel_mod, "sel", lambda: _Sel())
        # Enablement is a separate axis, gated ON here so ownership semantics
        # are what these tests exercise; the disabled-app test flips it off.
        monkeypatch.setattr(tele_mod, "_app_is_enabled", lambda name: True)
        self.sel_calls = calls

    @pytest.mark.asyncio
    async def test_an_app_reads_only_rows_stamped_with_its_own_app(self, _isolated_shards):
        _write(
            _isolated_shards,
            [
                _row("shared-name", app="acme-app", credits=1.0),
                _row("shared-name", app="other-app", credits=2.0),
                _row("shared-name", credits=3.0),  # pre-stamp row: no owner recorded
            ],
        )
        async with TestClient(TestServer(self._app("acme-app"))) as client:
            resp = await client.get("/api/usage/turns", params={"slot": "shared-name"})
            assert resp.status == 200
            turns = (await resp.json())["turns"]
            assert [t["credits"] for t in turns] == [1.0], (
                "an app sees its own stamped rows only — never another owner's, "
                "never pre-stamp rows"
            )

    @pytest.mark.asyncio
    async def test_a_dead_sessions_own_rows_remain_readable(self, _isolated_shards):
        # No live slot exists at all; ownership recorded on the row is enough.
        _write(_isolated_shards, [_row("finished-run", app="acme-app")])
        async with TestClient(TestServer(self._app("acme-app"))) as client:
            resp = await client.get("/api/usage/turns", params={"slot": "finished-run"})
            assert resp.status == 200
            assert len((await resp.json())["turns"]) == 1

    @pytest.mark.asyncio
    async def test_a_foreign_slot_is_indistinguishable_from_an_empty_one(self, _isolated_shards):
        _write(_isolated_shards, [_row("theirs", app="other-app")])
        async with TestClient(TestServer(self._app("acme-app"))) as client:
            for probe in ("theirs", "never-existed"):
                resp = await client.get("/api/usage/turns", params={"slot": probe})
                assert resp.status == 200
                assert (await resp.json())["turns"] == []

    @pytest.mark.asyncio
    async def test_a_dashboard_user_reads_everything(self, _isolated_shards):
        _write(
            _isolated_shards,
            [_row("any-slot", app="acme-app"), _row("any-slot")],
        )
        async with TestClient(TestServer(self._app(""))) as client:
            resp = await client.get("/api/usage/turns", params={"slot": "any-slot"})
            assert resp.status == 200
            assert len((await resp.json())["turns"]) == 2

    @pytest.mark.asyncio
    async def test_a_disabled_app_is_refused_outright(self, _isolated_shards, monkeypatch):
        from kiro_crew.dashboard.handlers import telemetry as tele_mod

        monkeypatch.setattr(tele_mod, "_app_is_enabled", lambda name: False)
        _write(_isolated_shards, [_row("mine", app="acme-app")])
        async with TestClient(TestServer(self._app("acme-app"))) as client:
            resp = await client.get("/api/usage/turns", params={"slot": "mine"})
            assert resp.status == 404, "disable must revoke read access, not only writes"
        assert any(c.get("error") == "app is disabled" for c in self.sel_calls)

    @pytest.mark.asyncio
    async def test_a_malformed_app_request_is_still_audited(self, _isolated_shards):
        async with TestClient(TestServer(self._app("acme-app"))) as client:
            resp = await client.get("/api/usage/turns")  # no slot
            assert resp.status == 400
        assert any(
            c.get("outcome") == "denied" and c.get("error") == "slot missing"
            for c in self.sel_calls
        ), "a probing app must leave a trail even when the request is malformed"

    @pytest.mark.asyncio
    async def test_grants_are_audited_off_the_denial_path_too(self, _isolated_shards):
        _write(_isolated_shards, [_row("mine", app="acme-app")])
        async with TestClient(TestServer(self._app("acme-app"))) as client:
            resp = await client.get("/api/usage/turns", params={"slot": "mine"})
            assert resp.status == 200
        assert any(c.get("outcome") == "allowed" for c in self.sel_calls)


class TestRowLevelWindow:
    def test_a_boundary_shard_rows_older_than_the_cutoff_are_excluded(self, _isolated_shards):
        # The oldest shard FILE in a 2-day window covers a whole day; a row in
        # it can still be older than now-2d. The cutoff is per row.
        old_ts = (datetime.now(timezone.utc) - timedelta(days=2, hours=3)).isoformat()
        fresh_ts = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        day = (datetime.now().astimezone() - timedelta(days=1)).strftime("%Y-%m-%d")
        _write(_isolated_shards, [_row("s1", ts=old_ts, credits=1.0)], day=day)
        _write(_isolated_shards, [_row("s1", ts=fresh_ts, credits=2.0)])
        turns = slot_turn_usage("s1", days=2)
        assert [t["credits"] for t in turns] == [2.0]

    def test_a_shard_with_invalid_bytes_is_skipped_not_a_500(self, _isolated_shards):
        # Same contract as slot_spend's reader: a corrupt shard (invalid UTF-8)
        # is skipped; the endpoint must never 500 over one bad file.
        from datetime import datetime as _dt

        day = _dt.now().astimezone().strftime("%Y-%m-%d")
        bad = _isolated_shards / f"{day}.jsonl"
        bad.write_bytes(b"\xff\xfe broken \xff\n")
        assert slot_turn_usage("s1", days=2) == []

    def test_an_unparseable_timestamp_is_excluded(self, _isolated_shards):
        _write(_isolated_shards, [_row("s1", ts="not-a-date", credits=1.0)])
        assert slot_turn_usage("s1", days=2) == [], "accounting excludes what it cannot date"


class TestWriteSiteStamping:
    """The write sites that can run app-owned work must stamp the row.

    Source-level pin on the two load-bearing sites (chat runner slot._app,
    subagent info.app): behavioural coverage lives in each surface's own
    suite, but a refactor that silently drops the kwarg would undercount an
    app's audit forever (old rows are unrecoverable by design), so the stamp's
    PRESENCE is pinned here where the API contract lives.
    """

    _ROOT = Path(__file__).resolve().parents[2]

    def test_chat_runner_stamps_the_slots_app(self):
        src = (self._ROOT / "src/kiro_crew/dashboard/chat_runner.py").read_text(encoding="utf-8")
        assert 'app=getattr(slot, "_app", "") or ""' in src

    def test_subagent_completion_stamps_the_dispatching_app(self):
        src = (self._ROOT / "src/kiro_crew/subagent.py").read_text(encoding="utf-8")
        assert 'app=info.app or ""' in src


class TestExtremeTimestamps:
    """Local-time conversion is guarded, not only parsing.

    ``fromisoformat`` happily parses a year-1 stamp, and it is ``timestamp()``
    / ``astimezone()`` that then raise ``ValueError`` on the local-time
    conversion. A corrupt row must be excluded, never a 500.
    """

    def test_parse_row_ts_answers_none_for_a_year_one_stamp(self):
        assert usage_mod._parse_row_ts("0001-01-01T00:00:00") is None

    def test_parse_row_day_answers_none_for_a_year_one_stamp(self):
        assert usage_mod._parse_row_day("0001-01-01T00:00:00") is None

    def test_reader_skips_an_extreme_row_instead_of_raising(self, _isolated_shards):
        _write(
            _isolated_shards,
            [_row("chat-1", ts="0001-01-01T00:00:00"), _row("chat-1")],
        )
        turns = slot_turn_usage("chat-1")
        assert len(turns) == 1
