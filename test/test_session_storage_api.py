"""Tests for the session storage endpoints.

These cover the wire contract and the guards, not the filesystem mechanics —
those live in ``test_session_storage.py``. Two properties are load-bearing here:
every mutation is refused for a restricted session and audited when it succeeds,
and the payload never splits a session's size across the two stores it occupies.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from aiohttp.test_utils import make_mocked_request

from kiro_crew import session_storage as session_storage_module
from kiro_crew.dashboard.handlers import session_storage as handler

_DAY = 86400.0


@pytest.fixture()
def stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    # Nested, not sibling: reclaim_block_reason() refuses an isolated data home
    # whose kiro store sits outside it, because such a store may be shared.
    crew_home = tmp_path / "crew"
    kiro_home = crew_home / "kiro"
    (crew_home / "sessions" / "archive").mkdir(parents=True)
    (kiro_home / "sessions" / "cli").mkdir(parents=True)
    monkeypatch.setenv("KIROCREW_HOME", str(crew_home))
    monkeypatch.setenv("KIRO_HOME", str(kiro_home))
    return crew_home, kiro_home


def _retired(kiro_home: Path, sid: str, *, log_bytes: int = 1024, age_days: float = 60) -> int:
    """Create a kiro-cli session old enough to be reclaimable; return its bytes.

    Aged against the real clock, because the handlers call ``measure`` without a
    ``now`` override — the injectable clock is the module's seam, not the API's.
    """
    root = kiro_home / "sessions" / "cli"
    mtime = time.time() - age_days * _DAY
    total = 0
    for suffix, payload in ((".json", b"{}"), (".jsonl", b"c" * log_bytes)):
        path = root / f"{sid}{suffix}"
        path.write_bytes(payload)
        os.utime(path, (mtime, mtime))
        total += len(payload)
    return total


def _sel_stub() -> MagicMock:
    sel = MagicMock()
    sel.log_api_access = MagicMock()
    return sel


def _raw_request(method: str, path: str, raw: bytes, *, restricted: bool = False):
    """A request whose body is arbitrary bytes, for malformed-payload cases."""
    headers = {"X-Session-Key": "dashboard:guest" if restricted else "dashboard:ui"}
    req = make_mocked_request(method, path, headers=headers, payload=None)
    state = MagicMock()
    state._restricted_keys = {"dashboard:guest"} if restricted else set()
    req.app["state"] = state

    async def _read():
        return raw

    req.read = _read  # type: ignore[method-assign]
    return req


def _request(method: str, path: str, body: dict | None = None, *, restricted: bool = False):
    headers = {"X-Session-Key": "dashboard:guest" if restricted else "dashboard:ui"}
    payload = json.dumps(body or {}).encode()
    req = make_mocked_request(method, path, headers=headers, payload=None)
    state = MagicMock()
    state._restricted_keys = {"dashboard:guest"} if restricted else set()
    req.app["state"] = state

    async def _read():
        return payload

    req.read = _read  # type: ignore[method-assign]
    return req


class TestReport:
    @pytest.mark.asyncio
    async def test_reports_one_size_per_session(self, stores: tuple[Path, Path]) -> None:
        _, kiro_home = stores
        size = _retired(kiro_home, "aaaa1111")

        resp = await handler.api_session_storage(_request("GET", "/api/system/session-storage"))
        body = json.loads(resp.body)

        assert resp.status == 200
        assert body["total_sessions"] == 1
        assert body["total_bytes"] == size
        assert body["reclaimable_sessions"] == 1

    @pytest.mark.asyncio
    async def test_payload_never_splits_the_two_stores(self, stores: tuple[Path, Path]) -> None:
        """The split is an implementation detail and must not reach a client."""
        _, kiro_home = stores
        _retired(kiro_home, "aaaa1111")

        resp = await handler.api_session_storage(_request("GET", "/api/system/session-storage"))
        flat = json.dumps(json.loads(resp.body))

        for leaked in ("cli_bytes", "crew_bytes", "kiro-cli", "transcript_bytes"):
            assert leaked not in flat

    @pytest.mark.asyncio
    async def test_trash_declares_that_staged_bytes_remain_on_disk(
        self, stores: tuple[Path, Path]
    ) -> None:
        resp = await handler.api_session_storage(_request("GET", "/api/system/session-storage"))
        body = json.loads(resp.body)

        assert body["trash"]["still_on_disk"] is True
        assert body["trash"]["bytes"] == 0
        assert body["trash"]["batches"] == []


class TestCleanup:
    @pytest.mark.asyncio
    async def test_dry_run_moves_nothing(self, stores: tuple[Path, Path]) -> None:
        _, kiro_home = stores
        size = _retired(kiro_home, "aaaa1111")
        req = _request(
            "POST",
            "/api/system/session-storage/cleanup",
            {"older_than_days": 30, "dry_run": True},
        )

        with patch.object(handler, "_sel", return_value=_sel_stub()):
            resp = await handler.api_session_storage_cleanup(req)
        body = json.loads(resp.body)

        assert body == {"dry_run": True, "sessions": 1, "bytes": size, "remaining": 0}
        assert (kiro_home / "sessions" / "cli" / "aaaa1111.jsonl").is_file()

    @pytest.mark.asyncio
    async def test_stages_and_audits(self, stores: tuple[Path, Path]) -> None:
        _, kiro_home = stores
        _retired(kiro_home, "aaaa1111")
        req = _request("POST", "/api/system/session-storage/cleanup", {"older_than_days": 30})
        sel = _sel_stub()

        with patch.object(handler, "_sel", return_value=sel):
            resp = await handler.api_session_storage_cleanup(req)
        body = json.loads(resp.body)

        assert resp.status == 200
        assert body["sessions"] == 1
        assert body["batch_id"]
        assert not (kiro_home / "sessions" / "cli" / "aaaa1111.jsonl").exists()
        operations = [c.kwargs["operation"] for c in sel.log_api_access.call_args_list]
        assert "session_storage.cleanup" in operations

    @pytest.mark.asyncio
    async def test_over_cap_stages_the_oldest_instead_of_refusing(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A refusal would dead-end the very install this exists for."""
        _, kiro_home = stores
        _retired(kiro_home, "oldest00", age_days=400)
        _retired(kiro_home, "middle00", age_days=200)
        _retired(kiro_home, "newest00", age_days=60)
        monkeypatch.setattr(handler, "_MAX_SELECTION", 2)
        req = _request("POST", "/api/system/session-storage/cleanup", {"older_than_days": 30})

        with patch.object(handler, "_sel", return_value=_sel_stub()):
            resp = await handler.api_session_storage_cleanup(req)
        body = json.loads(resp.body)

        assert resp.status == 200
        assert body["sessions"] == 2
        assert body["remaining"] == 1
        cli = kiro_home / "sessions" / "cli"
        # Oldest-first, so repeating the call makes monotonic progress.
        assert not (cli / "oldest00.jsonl").exists()
        assert not (cli / "middle00.jsonl").exists()
        assert (cli / "newest00.jsonl").is_file()

    @pytest.mark.asyncio
    async def test_the_index_is_built_off_the_event_loop(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reading session_map.json is filesystem work; it must not stall the loop."""
        _, kiro_home = stores
        _retired(kiro_home, "aaaa1111")
        offloaded: list[str] = []
        real_to_thread = handler.asyncio.to_thread

        async def spy(func, *args, **kwargs):
            offloaded.append(getattr(func, "__name__", repr(func)))
            return await real_to_thread(func, *args, **kwargs)

        monkeypatch.setattr(handler.asyncio, "to_thread", spy)
        req = _request("POST", "/api/system/session-storage/cleanup", {"older_than_days": 30})

        with patch.object(handler, "_sel", return_value=_sel_stub()):
            await handler.api_session_storage_cleanup(req)

        assert "_build_index" in offloaded

    @pytest.mark.asyncio
    async def test_an_unrepresentable_threshold_is_a_400_not_a_500(
        self, stores: tuple[Path, Path]
    ) -> None:
        """JSON bounds no integer, so float() can overflow on a valid payload."""
        req = _request(
            "POST",
            "/api/system/session-storage/cleanup",
            {"older_than_days": int("9" * 400)},
        )

        with patch.object(handler, "_sel", return_value=_sel_stub()):
            resp = await handler.api_session_storage_cleanup(req)

        assert resp.status == 400
        assert json.loads(resp.body)["code"] == "invalid_threshold"

    @pytest.mark.asyncio
    async def test_restricted_session_is_refused_and_audited(
        self, stores: tuple[Path, Path]
    ) -> None:
        _, kiro_home = stores
        _retired(kiro_home, "aaaa1111")
        req = _request(
            "POST",
            "/api/system/session-storage/cleanup",
            {"older_than_days": 30},
            restricted=True,
        )
        sel = _sel_stub()

        with patch.object(handler, "_sel", return_value=sel):
            resp = await handler.api_session_storage_cleanup(req)

        assert resp.status == 403
        assert json.loads(resp.body)["code"] == "restricted_session"
        assert (kiro_home / "sessions" / "cli" / "aaaa1111.jsonl").is_file()
        outcomes = [c.kwargs["outcome"] for c in sel.log_api_access.call_args_list]
        assert outcomes == ["denied"]

    @pytest.mark.asyncio
    async def test_missing_threshold_carries_a_machine_readable_code(
        self, stores: tuple[Path, Path]
    ) -> None:
        req = _request("POST", "/api/system/session-storage/cleanup", {})

        with patch.object(handler, "_sel", return_value=_sel_stub()):
            resp = await handler.api_session_storage_cleanup(req)

        assert resp.status == 400
        assert json.loads(resp.body)["code"] == "invalid_threshold"

    @pytest.mark.asyncio
    async def test_a_boolean_is_not_accepted_as_a_threshold(
        self, stores: tuple[Path, Path]
    ) -> None:
        """``True`` is an int in Python; a threshold of 1 day is not what was meant."""
        req = _request("POST", "/api/system/session-storage/cleanup", {"older_than_days": True})

        with patch.object(handler, "_sel", return_value=_sel_stub()):
            resp = await handler.api_session_storage_cleanup(req)

        assert resp.status == 400
        assert json.loads(resp.body)["code"] == "invalid_threshold"

    @pytest.mark.asyncio
    async def test_nothing_to_reclaim_is_success_not_an_error(
        self, stores: tuple[Path, Path]
    ) -> None:
        req = _request("POST", "/api/system/session-storage/cleanup", {"older_than_days": 30})

        with patch.object(handler, "_sel", return_value=_sel_stub()):
            resp = await handler.api_session_storage_cleanup(req)

        assert resp.status == 200
        assert json.loads(resp.body) == {
            "sessions": 0,
            "bytes": 0,
            "batch_id": "",
            "remaining": 0,
        }


class TestRestore:
    @pytest.mark.asyncio
    async def test_round_trip(self, stores: tuple[Path, Path]) -> None:
        _, kiro_home = stores
        _retired(kiro_home, "aaaa1111")
        with patch.object(handler, "_sel", return_value=_sel_stub()):
            staged = await handler.api_session_storage_cleanup(
                _request("POST", "/api/system/session-storage/cleanup", {"older_than_days": 30})
            )
            batch_id = json.loads(staged.body)["batch_id"]
            resp = await handler.api_session_storage_restore(
                _request("POST", "/api/system/session-storage/restore", {"batch_id": batch_id})
            )

        assert json.loads(resp.body)["restored"] == 1
        assert (kiro_home / "sessions" / "cli" / "aaaa1111.jsonl").is_file()

    @pytest.mark.asyncio
    async def test_missing_batch_id_is_rejected(self, stores: tuple[Path, Path]) -> None:
        with patch.object(handler, "_sel", return_value=_sel_stub()):
            resp = await handler.api_session_storage_restore(
                _request("POST", "/api/system/session-storage/restore", {})
            )

        assert resp.status == 400
        assert json.loads(resp.body)["code"] == "invalid_batch"

    @pytest.mark.asyncio
    async def test_unknown_batch_reports_a_refusal_code(self, stores: tuple[Path, Path]) -> None:
        with patch.object(handler, "_sel", return_value=_sel_stub()):
            resp = await handler.api_session_storage_restore(
                _request(
                    "POST",
                    "/api/system/session-storage/restore",
                    {"batch_id": "20240101T000000-deadbeef"},
                )
            )

        assert resp.status == 400
        assert json.loads(resp.body)["code"] == "restore_refused"

    @pytest.mark.asyncio
    async def test_restricted_session_is_refused(self, stores: tuple[Path, Path]) -> None:
        with patch.object(handler, "_sel", return_value=_sel_stub()):
            resp = await handler.api_session_storage_restore(
                _request(
                    "POST",
                    "/api/system/session-storage/restore",
                    {"batch_id": "x"},
                    restricted=True,
                )
            )

        assert resp.status == 403


class TestEmpty:
    @pytest.mark.asyncio
    async def test_frees_space_and_audits_the_bytes(self, stores: tuple[Path, Path]) -> None:
        _, kiro_home = stores
        size = _retired(kiro_home, "aaaa1111")
        sel = _sel_stub()

        with patch.object(handler, "_sel", return_value=sel):
            await handler.api_session_storage_cleanup(
                _request("POST", "/api/system/session-storage/cleanup", {"older_than_days": 30})
            )
            resp = await handler.api_session_storage_empty(
                _request("POST", "/api/system/session-storage/empty", {"all": True})
            )

        assert json.loads(resp.body)["freed_bytes"] >= size
        resources = [c.kwargs["resources"] for c in sel.log_api_access.call_args_list]
        assert any(r.startswith("freed:") for r in resources)

    @pytest.mark.asyncio
    async def test_an_empty_body_destroys_nothing(self, stores: tuple[Path, Path]) -> None:
        """The only irreversible endpoint takes explicit intent, never a default."""
        _, kiro_home = stores
        _retired(kiro_home, "aaaa1111")
        with patch.object(handler, "_sel", return_value=_sel_stub()):
            await handler.api_session_storage_cleanup(
                _request("POST", "/api/system/session-storage/cleanup", {"older_than_days": 30})
            )
            resp = await handler.api_session_storage_empty(
                _request("POST", "/api/system/session-storage/empty", {})
            )

        assert resp.status == 400
        assert json.loads(resp.body)["code"] == "nothing_specified"
        assert len(session_storage_module.list_trash()) == 1

    @pytest.mark.asyncio
    async def test_malformed_json_destroys_nothing(self, stores: tuple[Path, Path]) -> None:
        """A parse failure must not read as "no arguments" on a destructive path."""
        _, kiro_home = stores
        _retired(kiro_home, "aaaa1111")
        with patch.object(handler, "_sel", return_value=_sel_stub()):
            await handler.api_session_storage_cleanup(
                _request("POST", "/api/system/session-storage/cleanup", {"older_than_days": 30})
            )
            req = _raw_request("POST", "/api/system/session-storage/empty", b"not json at all")
            resp = await handler.api_session_storage_empty(req)

        assert resp.status == 400
        assert json.loads(resp.body)["code"] == "invalid_body"
        assert len(session_storage_module.list_trash()) == 1

    @pytest.mark.asyncio
    async def test_a_string_batch_ids_does_not_empty_everything(
        self, stores: tuple[Path, Path]
    ) -> None:
        """A bare string is not a list; collapsing it to None would delete all."""
        _, kiro_home = stores
        _retired(kiro_home, "aaaa1111")
        with patch.object(handler, "_sel", return_value=_sel_stub()):
            await handler.api_session_storage_cleanup(
                _request("POST", "/api/system/session-storage/cleanup", {"older_than_days": 30})
            )
            resp = await handler.api_session_storage_empty(
                _request("POST", "/api/system/session-storage/empty", {"batch_ids": "some-batch"})
            )

        assert resp.status == 400
        assert json.loads(resp.body)["code"] == "invalid_batch"
        # The batch it would have destroyed is still staged.
        assert len(session_storage_module.list_trash()) == 1

    @pytest.mark.asyncio
    async def test_a_string_uids_does_not_widen_a_restore(self, stores: tuple[Path, Path]) -> None:
        with patch.object(handler, "_sel", return_value=_sel_stub()):
            resp = await handler.api_session_storage_restore(
                _request(
                    "POST",
                    "/api/system/session-storage/restore",
                    {"batch_id": "x", "uids": "aaaa1111"},
                )
            )

        assert resp.status == 400
        assert json.loads(resp.body)["code"] == "invalid_batch"

    @pytest.mark.asyncio
    async def test_restricted_session_is_refused(self, stores: tuple[Path, Path]) -> None:
        with patch.object(handler, "_sel", return_value=_sel_stub()):
            resp = await handler.api_session_storage_empty(
                _request("POST", "/api/system/session-storage/empty", {}, restricted=True)
            )

        assert resp.status == 403
        assert json.loads(resp.body)["code"] == "restricted_session"


class TestIndexConstruction:
    def test_stems_come_from_the_history_resolver(self, stores: tuple[Path, Path]) -> None:
        """A mapped session must pair to the filename history actually writes."""
        with patch.object(handler, "SessionMap") as fake:
            fake.return_value.mapped_sids_by_key.return_value = {"dashboard:chat-1": "aaaa1111"}
            index = handler._build_index()

        assert index.stem_to_sid == {"dashboard_chat-1": "aaaa1111"}
        assert index.active_sids == frozenset({"aaaa1111"})

    def test_a_legacy_slack_stem_is_paired_too(self, stores: tuple[Path, Path]) -> None:
        """A thread predating the canonical key still logs under its bare ts.

        Pairing only the canonical stem would leave that transcript looking
        unowned, and therefore reclaimable while its session is still resumable.
        """
        key = "slack:1785861252.833429"
        with patch.object(handler, "SessionMap") as fake:
            fake.return_value.mapped_sids_by_key.return_value = {key: "bbbb2222"}
            index = handler._build_index()

        # Pinned literals, not transcript_stems() — comparing the resolver against
        # itself would pass even if it stopped returning the legacy stem at all.
        assert index.stem_to_sid == {
            "slack_1785861252.833429": "bbbb2222",
            "1785861252.833429": "bbbb2222",
        }
        assert index.active_stems == frozenset(index.stem_to_sid)
