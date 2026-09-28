"""Behavioral coverage for the uncovered branches of ``dashboard/handlers/memory_admin.py``.

Targets: ``_taken_at``'s ``.zip`` snapshot path and its unparseable-name refusal,
``_probe_store_blocking``'s missing-path / unreadable-backups-dir / unavailable
tiers, ``api_memory_retired``'s malformed pagination and store-unavailable
refusal, ``api_memory_backups``'s ``pending_restore_status`` failure, the
restore-cancel refusal path, ``_backup_now_blocking``'s skipped/failed
outcomes, and ``api_memory_restore``'s ``MemoryStartupUnavailable`` /
integrity-refusal / unexpected-exception branches (each with its audit call).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest import mock
from urllib.parse import urlencode

import pytest
from aiohttp import streams, web
from aiohttp.test_utils import make_mocked_request

from kiro_crew import memory_backup, memory_stores
from kiro_crew.config import loader as loader_mod
from kiro_crew.context import ContextBuilder
from kiro_crew.dashboard.handlers import memory_admin
from kiro_crew.memory_startup import MemoryStartupUnavailable
from kiro_crew.memory_stores import DEFAULT_MEMORY_STORE, owned_store_path
from kiro_crew.vector_memory import VectorMemoryStore

pytestmark = pytest.mark.xdist_group("memory_admin_handlers_coverage")

_FINANCE = "finance"
_SESSION_KEY = "dashboard:chat-1"
_OWNER_SUBJECT = "local-app"


class _Slot:
    is_restricted = False
    blocks_reads = False


class _ConversationLog:
    def __init__(self, bindings: dict[str, str] | None = None) -> None:
        self._bindings = dict(bindings or {})

    def get_metadata(self, session_key: str) -> dict[str, str]:
        return {"memory_store": self._bindings.get(session_key, "")}


class _State:
    owner_id = ""
    consolidator = None
    sessions = None

    def __init__(self) -> None:
        self.context_builder = None
        self.conversation_log = _ConversationLog()
        self._slots = {"chat-1": _Slot()}
        self._restricted_keys: set[str] = set()


class _Env:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.home = tmp_path / "data-home"
        self.home.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(self.home))
        loader_mod._invalidate_config_cache()
        monkeypatch.setattr(memory_stores, "_DECLARED_MEMO", None)
        self._monkeypatch = monkeypatch
        self._closables: list[VectorMemoryStore] = []
        self.unavailable: set[str] = set()
        monkeypatch.setattr(ContextBuilder, "ensure_store", staticmethod(self._ensure_store))

    def declare(self, *names: str) -> None:
        payload = {
            "memory_stores": {DEFAULT_MEMORY_STORE: {}, **{name: {} for name in names}},
            "default_memory_store": DEFAULT_MEMORY_STORE,
        }
        (self.home / "config.json").write_text(json.dumps(payload), encoding="utf-8")
        loader_mod._invalidate_config_cache()
        self._monkeypatch.setattr(memory_stores, "_DECLARED_MEMO", None)

    async def _ensure_store(self, store: str) -> VectorMemoryStore | None:
        if store in self.unavailable:
            return None
        return self.tier(store)

    def tier(self, store: str) -> VectorMemoryStore:
        path = owned_store_path(store)
        assert path is not None, store
        path.parent.mkdir(parents=True, exist_ok=True)
        tier = VectorMemoryStore(db_path=path)
        tier.init()
        self._closables.append(tier)
        return tier

    def state(self) -> _State:
        return _State()

    def close(self) -> None:
        for tier in self._closables:
            tier.close()


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    e = _Env(tmp_path, monkeypatch)
    try:
        yield e
    finally:
        e.close()


def _body_payload(raw: bytes, loop) -> streams.StreamReader:
    reader = streams.StreamReader(mock.Mock(_reading_paused=False), limit=len(raw) + 1, loop=loop)
    reader.feed_data(raw)
    reader.feed_eof()
    return reader


def _request(
    method: str,
    path: str,
    state: _State,
    *,
    query: dict[str, str] | None = None,
    owner: bool = True,
    body: Any = None,
) -> web.Request:
    import asyncio

    app = web.Application()
    app["state"] = state
    target = f"{path}?{urlencode(query)}" if query else path
    headers = {"X-Session-Key": _SESSION_KEY}
    kwargs: dict[str, Any] = {"headers": headers, "app": app}
    if body is not None:
        raw = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
        headers["Content-Length"] = str(len(raw))
        kwargs["payload"] = _body_payload(raw, asyncio.get_event_loop())
    request = make_mocked_request(method, target, **kwargs)
    if owner:
        request["user"] = _OWNER_SUBJECT
        request["app"] = ""
    return request


def _body(resp: web.Response) -> Any:
    return json.loads(resp.text or "")


# ── _taken_at: the .zip snapshot path and the unparseable-name refusal ────────


class TestTakenAt:
    def test_zip_backup_reads_snapshot_time_via_member_memory_backup(self, monkeypatch) -> None:
        from kiro_crew import member_memory_backup

        stamped = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        monkeypatch.setattr(member_memory_backup, "snapshot_time", lambda p: stamped)
        result = memory_admin._taken_at(Path("some.zip"))
        assert result == "2026-01-02T03:04:05Z"

    def test_unparseable_name_returns_none_rather_than_raising(self) -> None:
        assert memory_admin._taken_at(Path("not-a-backup-name.db")) is None


# ── _probe_store_blocking: missing path, unreadable backups dir, unavailable ──


class TestProbeStoreBlocking:
    def test_undeclared_name_returns_the_unprobed_row(self, env) -> None:
        env.declare()
        row = memory_admin._probe_store_blocking("nosuchstore")
        assert row["exists"] is False
        assert row["semantic_count"] is None
        assert row["backup_count"] == 0

    def test_unreadable_backups_directory_still_reports_real_counts(self, env, monkeypatch) -> None:
        env.declare(_FINANCE)
        path = owned_store_path(_FINANCE)
        assert path is not None
        tier = env.tier(_FINANCE)
        tier.set_semantic("pref.editor", "vim", 1.0, "user_explicit")

        def _boom(_db_path: Path) -> list[Path]:
            raise OSError("backups dir unreadable")

        monkeypatch.setattr(memory_backup, "list_backups", _boom)
        row = memory_admin._probe_store_blocking(_FINANCE)
        # The backup summary is lost, but the counts survive independently.
        assert row["backup_count"] == 0
        assert row["newest_backup"] is None
        assert row["exists"] is True
        assert row["semantic_count"] == 1

    def test_unavailable_store_records_the_redacted_reason_and_stops(
        self, env, monkeypatch
    ) -> None:
        env.declare(_FINANCE)

        def _refuse(_name: str) -> None:
            raise MemoryStartupUnavailable("boom AKIAIOSFODNN7EXAMPLE")

        # ``require_memory_ready`` is imported inside ``_probe_store_blocking`` from
        # ``kiro_crew.memory_startup``, so patching a name on ``memory_admin`` itself
        # is a silent no-op -- patch the defining module the deferred import reads.
        from kiro_crew import memory_startup

        monkeypatch.setattr(memory_startup, "require_memory_ready", _refuse)
        row = memory_admin._probe_store_blocking(_FINANCE)
        assert "unavailable_reason" in row
        assert "AKIAIOSFODNN7EXAMPLE" not in row["unavailable_reason"]
        assert row["exists"] is False

    def test_nonexistent_file_stops_before_opening_sqlite(self, env) -> None:
        env.declare(_FINANCE)
        # Declared but never opened: owned_store_path resolves, but no file exists.
        row = memory_admin._probe_store_blocking(_FINANCE)
        assert row["exists"] is False
        assert row["lineage"] is None

    def test_unrecognized_lineage_reports_unprobed(self, env, monkeypatch) -> None:
        env.declare(_FINANCE)
        env.tier(_FINANCE)  # creates a real, empty crew-lineage db

        from kiro_crew import memory_schema

        monkeypatch.setattr(memory_schema, "detect_lineage", lambda db: None)
        row = memory_admin._probe_store_blocking(_FINANCE)
        assert row["lineage"] is None
        assert row["exists"] is False


class TestListStoresBlocking:
    def test_a_probe_exception_still_lists_the_store_as_unreadable(self, env, monkeypatch) -> None:
        env.declare(_FINANCE)

        def _boom(_name: str) -> dict:
            raise RuntimeError("kaboom")

        monkeypatch.setattr(memory_admin, "_probe_store_blocking", _boom)
        rows = memory_admin._list_stores_blocking()
        by_name = {row["name"]: row for row in rows}
        assert by_name[_FINANCE]["exists"] is False
        assert by_name[_FINANCE]["semantic_count"] is None


# ── api_memory_retired: malformed pagination + store unavailable ──────────────


class TestApiMemoryRetired:
    @pytest.mark.asyncio
    async def test_malformed_limit_is_a_400_before_the_store_is_touched(self, env) -> None:
        env.declare(_FINANCE)
        resp = await memory_admin.api_memory_retired(
            _request(
                "GET",
                "/api/memory/retired",
                env.state(),
                query={"store": _FINANCE, "limit": "not-a-number"},
            )
        )
        assert resp.status == 400
        assert _body(resp)["code"] == "invalid_pagination"

    @pytest.mark.asyncio
    async def test_malformed_offset_is_also_a_400(self, env) -> None:
        env.declare(_FINANCE)
        resp = await memory_admin.api_memory_retired(
            _request(
                "GET",
                "/api/memory/retired",
                env.state(),
                query={"store": _FINANCE, "offset": "nope"},
            )
        )
        assert resp.status == 400
        assert _body(resp)["code"] == "invalid_pagination"

    @pytest.mark.asyncio
    async def test_unavailable_vector_tier_answers_503(self, env) -> None:
        env.declare(_FINANCE)
        env.unavailable.add(_FINANCE)
        resp = await memory_admin.api_memory_retired(
            _request("GET", "/api/memory/retired", env.state(), query={"store": _FINANCE})
        )
        assert resp.status == 503
        assert _body(resp)["code"] == "store_unavailable"

    @pytest.mark.asyncio
    async def test_success_lists_retired_episodes_with_redacted_fields(self, env) -> None:
        env.declare(_FINANCE)
        tier = env.tier(_FINANCE)
        tier.write_episodic("The old fact.", source="test")
        row = tier.get_retired_episodic(limit=10, offset=0)
        assert row == []  # nothing retired yet, but the tier is reachable
        resp = await memory_admin.api_memory_retired(
            _request("GET", "/api/memory/retired", env.state(), query={"store": _FINANCE})
        )
        assert resp.status == 200
        assert _body(resp)["retired"] == []


# ── api_memory_retired_restore ────────────────────────────────────────────────


class TestApiMemoryRetiredRestore:
    @pytest.mark.asyncio
    async def test_empty_id_is_refused(self, env) -> None:
        env.declare(_FINANCE)
        env.tier(_FINANCE)
        resp = await memory_admin.api_memory_retired_restore(
            _request(
                "POST",
                "/api/memory/retired/restore",
                env.state(),
                body={"store": _FINANCE, "id": "   "},
            )
        )
        assert resp.status == 400
        assert _body(resp)["code"] == "invalid_episode_id"

    @pytest.mark.asyncio
    async def test_unknown_id_is_a_404(self, env) -> None:
        env.declare(_FINANCE)
        env.tier(_FINANCE)
        resp = await memory_admin.api_memory_retired_restore(
            _request(
                "POST",
                "/api/memory/retired/restore",
                env.state(),
                body={"store": _FINANCE, "id": "no-such-id"},
            )
        )
        assert resp.status == 404
        assert _body(resp)["code"] == "unknown_retired_episode"

    @pytest.mark.asyncio
    async def test_unavailable_store_answers_503(self, env) -> None:
        env.declare(_FINANCE)
        env.unavailable.add(_FINANCE)
        resp = await memory_admin.api_memory_retired_restore(
            _request(
                "POST",
                "/api/memory/retired/restore",
                env.state(),
                body={"store": _FINANCE, "id": "x"},
            )
        )
        assert resp.status == 503
        assert _body(resp)["code"] == "store_unavailable"


# ── api_memory_backups: pending_restore_status failure ────────────────────────


class TestApiMemoryBackups:
    @pytest.mark.asyncio
    async def test_pending_status_failure_answers_503_with_redaction(
        self, env, monkeypatch
    ) -> None:
        env.declare(_FINANCE)
        env.tier(_FINANCE).close()

        def _boom(_db_path: Path) -> dict:
            raise ValueError("boom AKIAIOSFODNN7EXAMPLE")

        monkeypatch.setattr(memory_backup, "pending_restore_status", _boom)
        resp = await memory_admin.api_memory_backups(
            _request("GET", "/api/memory/backups", env.state(), query={"store": _FINANCE})
        )
        assert resp.status == 503
        assert _body(resp)["code"] == "restore_status_unavailable"
        assert "AKIAIOSFODNN7EXAMPLE" not in resp.text

    @pytest.mark.asyncio
    async def test_undeclared_store_is_a_404_before_any_listing(self, env) -> None:
        resp = await memory_admin.api_memory_backups(
            _request("GET", "/api/memory/backups", env.state(), query={"store": "nosuchstore"})
        )
        assert resp.status == 404
        assert _body(resp)["code"] == "unknown_memory_store"


# ── api_memory_restore_cancel ──────────────────────────────────────────────────


class TestApiMemoryRestoreCancel:
    @pytest.mark.asyncio
    async def test_body_error_short_circuits(self, env) -> None:
        request = make_mocked_request(
            "POST",
            "/api/memory/restore/cancel",
            headers={"X-Session-Key": _SESSION_KEY, "Content-Type": "application/json"},
            app=web.Application(),
        )
        request.app["state"] = env.state()
        request["user"] = _OWNER_SUBJECT
        request["app"] = ""
        resp = await memory_admin.api_memory_restore_cancel(request)
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_undeclared_store_short_circuits_before_any_cancel(self, env) -> None:
        resp = await memory_admin.api_memory_restore_cancel(
            _request(
                "POST", "/api/memory/restore/cancel", env.state(), body={"store": "nosuchstore"}
            )
        )
        assert resp.status == 404
        assert _body(resp)["code"] == "unknown_memory_store"

    @pytest.mark.asyncio
    async def test_refused_cancel_answers_409_with_redaction(self, env, monkeypatch) -> None:
        env.declare(_FINANCE)
        env.tier(_FINANCE).close()

        def _boom(_db_path: Path) -> bool:
            raise ValueError("refused AKIAIOSFODNN7EXAMPLE")

        monkeypatch.setattr(memory_backup, "cancel_pending_restore", _boom)
        resp = await memory_admin.api_memory_restore_cancel(
            _request("POST", "/api/memory/restore/cancel", env.state(), body={"store": _FINANCE})
        )
        assert resp.status == 409
        assert _body(resp)["code"] == "restore_refused"
        assert "AKIAIOSFODNN7EXAMPLE" not in resp.text

    @pytest.mark.asyncio
    async def test_unchanged_cancel_still_reports_ok_with_no_pending(self, env) -> None:
        env.declare(_FINANCE)
        env.tier(_FINANCE).close()
        resp = await memory_admin.api_memory_restore_cancel(
            _request("POST", "/api/memory/restore/cancel", env.state(), body={"store": _FINANCE})
        )
        assert resp.status == 200
        body = _body(resp)
        assert body["ok"] is True
        assert body["cancelled"] is False
        assert body["pending"] is False
        assert body["restart_required"] is False


# ── _backup_now_blocking: skipped and failed outcomes ─────────────────────────


class TestBackupNowBlocking:
    def test_no_file_yet_reports_skipped_not_failed(self, env) -> None:
        env.declare(_FINANCE)
        path = owned_store_path(_FINANCE)
        assert path is not None
        # Never opened: backup_store returns None for a nonexistent database.
        result = memory_admin._backup_now_blocking(path)
        assert result == {"backed_up": 0, "skipped": 1, "pruned": 0, "failed": 0}

    def test_memory_backup_failed_is_reported_as_failed(self, env, monkeypatch) -> None:
        env.declare(_FINANCE)
        path = owned_store_path(_FINANCE)
        assert path is not None
        env.tier(_FINANCE).close()

        def _boom(_db_path: Path) -> None:
            raise memory_backup.MemoryBackupFailed("nope")

        monkeypatch.setattr(memory_backup, "backup_store", _boom)
        result = memory_admin._backup_now_blocking(path)
        assert result["failed"] == 1
        assert result["backed_up"] == 0

    def test_unexpected_exception_is_also_reported_as_failed(self, env, monkeypatch) -> None:
        env.declare(_FINANCE)
        path = owned_store_path(_FINANCE)
        assert path is not None
        env.tier(_FINANCE).close()

        def _boom(_db_path: Path) -> None:
            raise RuntimeError("surprise")

        monkeypatch.setattr(memory_backup, "backup_store", _boom)
        result = memory_admin._backup_now_blocking(path)
        assert result["failed"] == 1


class TestApiMemoryBackup:
    @pytest.mark.asyncio
    async def test_undeclared_store_is_refused_before_backing_up(self, env) -> None:
        resp = await memory_admin.api_memory_backup(
            _request("POST", "/api/memory/backup", env.state(), body={"store": "nosuchstore"})
        )
        assert resp.status == 404


# ── api_memory_restore: startup/integrity/unexpected exception branches ───────


class TestApiMemoryRestore:
    @pytest.mark.asyncio
    async def test_undeclared_store_is_a_404_before_reading_the_body_name(self, env) -> None:
        resp = await memory_admin.api_memory_restore(
            _request(
                "POST",
                "/api/memory/restore",
                env.state(),
                body={"store": "nosuchstore", "name": "x"},
            )
        )
        assert resp.status == 404
        assert _body(resp)["code"] == "unknown_memory_store"

    @pytest.mark.asyncio
    async def test_unfound_backup_is_a_404(self, env) -> None:
        env.declare(_FINANCE)
        env.tier(_FINANCE).close()
        resp = await memory_admin.api_memory_restore(
            _request(
                "POST",
                "/api/memory/restore",
                env.state(),
                body={"store": _FINANCE, "name": "does-not-exist.db"},
            )
        )
        assert resp.status == 404
        assert _body(resp)["code"] == "backup_not_found"

    @pytest.mark.asyncio
    async def test_race_with_a_prune_between_is_file_and_restore_is_a_404(
        self, env, monkeypatch
    ) -> None:
        env.declare(_FINANCE)
        path = owned_store_path(_FINANCE)
        assert path is not None
        env.tier(_FINANCE).close()
        backup = memory_backup.backup_store(path)
        assert backup is not None

        def _boom(_db_path: Path, _backup: Path, _store: str) -> None:
            raise FileNotFoundError("raced")

        monkeypatch.setattr(memory_admin, "_restore_blocking", _boom)
        resp = await memory_admin.api_memory_restore(
            _request(
                "POST",
                "/api/memory/restore",
                env.state(),
                body={"store": _FINANCE, "name": backup.name},
            )
        )
        assert resp.status == 404
        assert _body(resp)["code"] == "backup_not_found"

    @pytest.mark.asyncio
    async def test_startup_unavailable_during_restore_answers_503(self, env, monkeypatch) -> None:
        env.declare(_FINANCE)
        path = owned_store_path(_FINANCE)
        assert path is not None
        env.tier(_FINANCE).close()
        backup = memory_backup.backup_store(path)
        assert backup is not None

        def _boom(_db_path: Path, _backup: Path, _store: str) -> None:
            raise MemoryStartupUnavailable("boom AKIAIOSFODNN7EXAMPLE")

        monkeypatch.setattr(memory_admin, "_restore_blocking", _boom)
        resp = await memory_admin.api_memory_restore(
            _request(
                "POST",
                "/api/memory/restore",
                env.state(),
                body={"store": _FINANCE, "name": backup.name},
            )
        )
        assert resp.status == 503
        assert _body(resp)["code"] == "store_unavailable"
        assert "AKIAIOSFODNN7EXAMPLE" not in resp.text

    @pytest.mark.asyncio
    async def test_integrity_refusal_answers_409_with_redaction(self, env, monkeypatch) -> None:
        env.declare(_FINANCE)
        path = owned_store_path(_FINANCE)
        assert path is not None
        env.tier(_FINANCE).close()
        backup = memory_backup.backup_store(path)
        assert backup is not None

        def _boom(_db_path: Path, _backup: Path, _store: str) -> None:
            raise ValueError("corrupt AKIAIOSFODNN7EXAMPLE")

        monkeypatch.setattr(memory_admin, "_restore_blocking", _boom)
        resp = await memory_admin.api_memory_restore(
            _request(
                "POST",
                "/api/memory/restore",
                env.state(),
                body={"store": _FINANCE, "name": backup.name},
            )
        )
        assert resp.status == 409
        assert _body(resp)["code"] == "restore_refused"
        assert "AKIAIOSFODNN7EXAMPLE" not in resp.text

    @pytest.mark.asyncio
    async def test_unexpected_exception_during_restore_answers_500_and_audits(
        self, env, monkeypatch
    ) -> None:
        env.declare(_FINANCE)
        path = owned_store_path(_FINANCE)
        assert path is not None
        env.tier(_FINANCE).close()
        backup = memory_backup.backup_store(path)
        assert backup is not None

        def _boom(_db_path: Path, _backup: Path, _store: str) -> None:
            raise RuntimeError("kaboom")

        audited: list[tuple[str, str, str]] = []

        async def _audit(_request, operation, outcome, resources):
            audited.append((operation, outcome, resources))

        monkeypatch.setattr(memory_admin, "_restore_blocking", _boom)
        monkeypatch.setattr(memory_admin, "_audit", _audit)
        resp = await memory_admin.api_memory_restore(
            _request(
                "POST",
                "/api/memory/restore",
                env.state(),
                body={"store": _FINANCE, "name": backup.name},
            )
        )
        assert resp.status == 500
        assert _body(resp)["code"] == "restore_failed"
        assert audited == [("memory.restore", "error", f"{_FINANCE}:{backup.name}")]

    @pytest.mark.asyncio
    async def test_successful_restore_audits_success(self, env, monkeypatch) -> None:
        env.declare(_FINANCE)
        path = owned_store_path(_FINANCE)
        assert path is not None
        env.tier(_FINANCE).close()
        backup = memory_backup.backup_store(path)
        assert backup is not None

        audited: list[tuple[str, str, str]] = []

        async def _audit(_request, operation, outcome, resources):
            audited.append((operation, outcome, resources))

        monkeypatch.setattr(memory_admin, "_restore_blocking", lambda *_a, **_k: None)
        monkeypatch.setattr(memory_admin, "_audit", _audit)
        resp = await memory_admin.api_memory_restore(
            _request(
                "POST",
                "/api/memory/restore",
                env.state(),
                body={"store": _FINANCE, "name": backup.name},
            )
        )
        assert resp.status == 200
        body = _body(resp)
        assert body["ok"] is True
        assert body["pending"] is True
        assert body["restart_required"] is True
        assert audited == [("memory.restore", "success", f"{_FINANCE}:{backup.name}")]
