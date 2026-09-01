"""Tests for the session storage endpoints.

These cover the wire contract and the guards, not the filesystem mechanics —
those live in ``test_session_storage.py``. Two properties are load-bearing here:
every mutation is refused for a restricted session and audited when it succeeds,
and the payload never splits a session's size across the two stores it occupies.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from aiohttp.test_utils import make_mocked_request

from kiro_crew import session_storage as session_storage_module
from kiro_crew.dashboard.handlers import session_storage as handler

_DAY = 86400.0


@pytest.fixture(autouse=True)
def _fresh_scan_cache() -> None:
    """No cached filesystem pass leaks between tests. See test_session_storage.py."""
    session_storage_module.invalidate_scan_cache()


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
    @pytest.fixture(autouse=True)
    def _no_job_leaks(self, request: pytest.FixtureRequest) -> Iterator[None]:
        """The job slot is a module global, so a test must not inherit one.

        Without this a finished job from an earlier test makes the next one's POST
        look like a second empty, or its status read report a run it never started.

        Teardown WAITS for a still-running job rather than cancelling it. Cancelling
        the asyncio task only abandons the `to_thread` wrapper - the worker thread
        keeps running `empty_trash` and keeps deleting files, straight through
        `tmp_path` teardown, so a failed assertion turned into a real delete racing the
        fixture that was removing the same tree. Waiting is bounded and, on timeout,
        fails loudly with the test's name instead of leaving a thread mutating storage.
        """
        handler._empty_job = None
        yield
        job = handler._empty_job
        if job is not None and job.task is not None and not job.task.done():
            loop = asyncio.get_event_loop()
            try:
                loop.run_until_complete(asyncio.wait_for(job.task, timeout=30))
            except asyncio.TimeoutError:  # pragma: no cover - a wedged test, not a path
                pytest.fail(
                    f"{request.node.name} left an empty job running for 30s; it holds a "
                    "worker thread that is still deleting files"
                )
        handler._empty_job = None

    @pytest.mark.asyncio
    async def test_a_job_left_running_is_awaited_by_teardown_not_abandoned(
        self, stores: tuple[Path, Path]
    ) -> None:
        """Covers the guard itself: this test deliberately does NOT await its job.

        Cancelling the task would abandon a worker thread that is still deleting files,
        into `tmp_path` teardown. So the fixture waits, and this is the test that makes
        it wait - without it the wait path never runs and could rot.
        """
        done = threading.Event()

        def slow_empty(batch_ids, on_progress=None, on_skip=None, expect=None):  # type: ignore[no-untyped-def]
            time.sleep(0.2)
            done.set()
            return 0

        with (
            patch.object(handler, "_sel", return_value=_sel_stub()),
            patch.object(handler, "staged_targets", return_value=([], 0, {})),
            patch.object(handler, "empty_trash", slow_empty),
        ):
            resp = await handler.api_session_storage_empty(
                _request("POST", "/api/system/session-storage/empty", {"all": True})
            )

        assert resp.status == 202
        job = handler._empty_job
        assert job is not None and job.task is not None and not job.task.done()
        # Deliberately returning with the job in flight. If teardown cancelled instead
        # of waiting, `done` would still be unset when the next test starts and the
        # thread would outlive this one.
        assert not done.is_set()

    @pytest.mark.asyncio
    async def test_an_explicit_delete_refused_by_a_failed_snapshot_is_still_audited(
        self,
    ) -> None:
        """Refusing an irreversible operation must still record that it was attempted.

        Every other outcome of this endpoint reaches the audit inside ``_run_empty_job``,
        and before this change an explicit selection reached it even on a failed snapshot,
        because it DISPATCHED anyway. Failing closed is right -- dispatching without the
        snapshot deletes whatever answers to the names by the time the worker runs -- but it
        moved the request off the audited path, and an unrecorded attempt at the one
        irreversible operation in this surface is a worse hole than the one it closed.
        """

        def unreadable(*_args, **_kwargs):  # type: ignore[no-untyped-def]
            raise OSError("too many open files")

        sel = _sel_stub()
        with (
            patch.object(handler, "_sel", return_value=sel),
            patch.object(handler, "staged_targets", unreadable),
        ):
            resp = await handler.api_session_storage_empty(
                _request(
                    "POST",
                    "/api/system/session-storage/empty",
                    {"batch_ids": ["20260101T000000Z-aaaa"]},
                )
            )
        body = json.loads(resp.body)

        assert resp.status == 202
        assert body["error"], "the refusal must be visible on the job"
        audited = [
            c.kwargs
            for c in sel.log_api_access.call_args_list
            if c.kwargs["operation"] == "session_storage.empty"
        ]
        assert audited, "the refused empty must be audited"
        assert audited[0]["outcome"] == "refused"
        assert audited[0]["resources"] == "snapshot_unreadable"

    @pytest.mark.asyncio
    async def test_accepts_the_work_then_frees_the_space_and_audits_it(
        self, stores: tuple[Path, Path]
    ) -> None:
        """202 says "accepted", not "done" — the bytes land when the job finishes."""
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
            accepted = json.loads(resp.body)

            assert resp.status == 202
            assert accepted["running"] is True
            # The denominator comes from the staged manifest, so the screen can draw
            # progress from the first frame instead of after the first callback.
            assert accepted["total_bytes"] >= size

            job = handler._empty_job
            assert job is not None and job.task is not None
            await job.task

            status = json.loads(
                (
                    await handler.api_session_storage_empty_status(
                        _request("GET", "/api/system/session-storage/empty", None)
                    )
                ).body
            )

        assert status["job"]["running"] is False
        assert status["job"]["error"] == ""
        assert status["job"]["freed_bytes"] >= size
        assert status["job"]["job_id"] == accepted["job_id"]
        assert session_storage_module.list_trash() == []
        resources = [c.kwargs["resources"] for c in sel.log_api_access.call_args_list]
        assert any(r.startswith("freed:") for r in resources)

    @pytest.mark.asyncio
    async def test_no_job_reads_as_no_job(self, stores: tuple[Path, Path]) -> None:
        resp = await handler.api_session_storage_empty_status(
            _request("GET", "/api/system/session-storage/empty", None)
        )
        assert json.loads(resp.body) == {"job": None}

    @pytest.mark.asyncio
    async def test_progress_is_visible_while_the_delete_is_still_running(
        self, stores: tuple[Path, Path]
    ) -> None:
        """The point of the whole change: a partial figure, before it is finished.

        The delete is held open on an event so the status read happens mid-run,
        which is the only moment the old blocking endpoint could say nothing at all.
        """
        _, kiro_home = stores
        _retired(kiro_home, "aaaa1111")
        release = asyncio.Event()
        loop = asyncio.get_running_loop()

        def _slow_empty(batch_ids, on_progress=None, on_skip=None, expect=None):  # type: ignore[no-untyped-def]
            if on_progress is not None:
                on_progress(512)
            asyncio.run_coroutine_threadsafe(_wait(), loop).result()
            return 1024

        async def _wait() -> None:
            await release.wait()

        with patch.object(handler, "_sel", return_value=_sel_stub()):
            await handler.api_session_storage_cleanup(
                _request("POST", "/api/system/session-storage/cleanup", {"older_than_days": 30})
            )
            with patch.object(handler, "empty_trash", _slow_empty):
                await handler.api_session_storage_empty(
                    _request("POST", "/api/system/session-storage/empty", {"all": True})
                )
                job = handler._empty_job
                assert job is not None and job.task is not None
                # try/finally, not a trailing release: the worker is parked in
                # `.result()` on a thread, and an assertion failing before the set
                # would leave that thread blocked forever. Cancelling the asyncio task
                # (which the fixture does) does not reach it, so pytest could not shut
                # its executor down and a failed test became a hung run.
                try:
                    # Let the worker thread report once.
                    for _ in range(200):
                        if job.freed_bytes:
                            break
                        await asyncio.sleep(0.01)

                    mid = json.loads(
                        (
                            await handler.api_session_storage_empty_status(
                                _request("GET", "/api/system/session-storage/empty", None)
                            )
                        ).body
                    )
                    assert mid["job"]["running"] is True
                    assert mid["job"]["freed_bytes"] == 512

                    # A second empty is refused while this one holds the slot, and the
                    # refusal carries the running job so a second tab shows progress
                    # rather than an error it cannot act on.
                    busy = await handler.api_session_storage_empty(
                        _request("POST", "/api/system/session-storage/empty", {"all": True})
                    )
                    assert busy.status == 409
                    assert json.loads(busy.body)["code"] == "empty_in_progress"
                    assert json.loads(busy.body)["job"]["job_id"] == mid["job"]["job_id"]
                finally:
                    release.set()
                    await job.task

        assert job.done is True
        assert job.freed_bytes == 1024

    @pytest.mark.asyncio
    async def test_a_refusal_lands_on_the_job_not_on_a_lost_request(
        self, stores: tuple[Path, Path]
    ) -> None:
        """The request is already answered, so a refusal has to be readable later."""

        def _refuse(batch_ids, on_progress=None, on_skip=None, expect=None):  # type: ignore[no-untyped-def]
            raise session_storage_module.SessionStorageError("the trash root moved")

        sel = _sel_stub()
        with (
            patch.object(handler, "_sel", return_value=sel),
            patch.object(handler, "staged_targets", return_value=([], 0, {})),
            patch.object(handler, "empty_trash", _refuse),
        ):
            await handler.api_session_storage_empty(
                _request("POST", "/api/system/session-storage/empty", {"all": True})
            )
            job = handler._empty_job
            assert job is not None and job.task is not None
            await job.task

        assert job.done is True
        assert "trash root moved" in job.error
        outcomes = [c.kwargs["outcome"] for c in sel.log_api_access.call_args_list]
        assert "refused" in outcomes

    @pytest.mark.asyncio
    async def test_an_unexpected_error_finishes_the_job_instead_of_hanging_it(
        self, stores: tuple[Path, Path]
    ) -> None:
        """A job left flagged running is a screen polling a delete that stopped."""

        def _boom(batch_ids, on_progress=None, on_skip=None, expect=None):  # type: ignore[no-untyped-def]
            raise RuntimeError("disk went away")

        sel = _sel_stub()
        with (
            patch.object(handler, "_sel", return_value=sel),
            patch.object(handler, "staged_targets", return_value=([], 0, {})),
            patch.object(handler, "empty_trash", _boom),
        ):
            await handler.api_session_storage_empty(
                _request("POST", "/api/system/session-storage/empty", {"all": True})
            )
            job = handler._empty_job
            assert job is not None and job.task is not None
            await job.task

        assert job.done is True
        assert job.error != ""
        # The client is told something stopped, never the exception text.
        assert "disk went away" not in job.error
        outcomes = [c.kwargs["outcome"] for c in sel.log_api_access.call_args_list]
        assert "error" in outcomes

    @pytest.mark.asyncio
    async def test_a_kept_batch_is_reported_as_a_refusal_not_a_success(
        self, stores: tuple[Path, Path]
    ) -> None:
        """A batch held back is not "0 bytes freed, success".

        `_empty_trash_locked` keeps a batch holding files no manifest lists, because
        those are the only copy -- and nothing raises. Read only from the exception,
        the job settled clean and the screen said "Freed 0 B." above a batch that was
        still there, with the reason in a log the user cannot read.
        """
        _, kiro_home = stores
        _retired(kiro_home, "aaaa1111")
        sel = _sel_stub()

        with patch.object(handler, "_sel", return_value=sel):
            await handler.api_session_storage_cleanup(
                _request("POST", "/api/system/session-storage/cleanup", {"older_than_days": 30})
            )
            batch = session_storage_module.list_trash()[0]
            staged = session_storage_module.trash_root() / batch.batch_id / "cli"
            staged.mkdir(parents=True, exist_ok=True)
            (staged / "cccc3333.jsonl").write_bytes(b"ONLY COPY")

            await handler.api_session_storage_empty(
                _request("POST", "/api/system/session-storage/empty", {"all": True})
            )
            job = handler._empty_job
            assert job is not None and job.task is not None
            await job.task

            status = json.loads(
                (
                    await handler.api_session_storage_empty_status(
                        _request("GET", "/api/system/session-storage/empty", None)
                    )
                ).body
            )

        assert status["job"]["skipped"] == [session_storage_module.SKIP_UNLISTED_FILES]
        assert status["job"]["error"] == ""
        assert len(session_storage_module.list_trash()) == 1, "the batch was kept"
        outcomes = [c.kwargs["outcome"] for c in sel.log_api_access.call_args_list]
        assert "refused" in outcomes

    @pytest.mark.asyncio
    async def test_a_stale_finished_job_stops_being_reported(
        self, stores: tuple[Path, Path]
    ) -> None:
        """An outcome must not sit on the screen as current for days.

        The slot holds the last job for the life of the process, so without a cutoff
        "Freed 18GB." rendered above the Trash on every visit until a restart.
        """
        handler._empty_job = handler._EmptyJob(
            job_id="empty-old",
            total_bytes=10,
            freed_bytes=10,
            done=True,
            finished_at=time.time() - handler._JOB_TTL_SECONDS - 60,
        )

        resp = await handler.api_session_storage_empty_status(
            _request("GET", "/api/system/session-storage/empty", None)
        )
        assert json.loads(resp.body) == {"job": None}

        # A job that finished just now is still the answer.
        handler._empty_job.finished_at = time.time()
        resp = await handler.api_session_storage_empty_status(
            _request("GET", "/api/system/session-storage/empty", None)
        )
        assert json.loads(resp.body)["job"]["job_id"] == "empty-old"

    @pytest.mark.asyncio
    async def test_two_simultaneous_posts_produce_one_job(self, stores: tuple[Path, Path]) -> None:
        """The slot is claimed in the same synchronous step as the check.

        Reading the staged totals first put a suspension point between the guard and
        the claim: both requests passed, both got a 202, and the second overwrote the
        first -- so the status endpoint reported a job that (serialized behind the
        mutation lock) found the trash already gone and said "freed 0", while the
        delete the user was waiting on had no record at all.
        """

        def _slow_totals(batch_ids):  # type: ignore[no-untyped-def]
            time.sleep(0.05)
            return [], 0, {}

        release = asyncio.Event()
        loop = asyncio.get_running_loop()

        def _held_empty(batch_ids, on_progress=None, on_skip=None, expect=None):  # type: ignore[no-untyped-def]
            asyncio.run_coroutine_threadsafe(release.wait(), loop).result()
            return 0

        with (
            patch.object(handler, "_sel", return_value=_sel_stub()),
            patch.object(handler, "staged_targets", _slow_totals),
            patch.object(handler, "empty_trash", _held_empty),
        ):
            first, second = await asyncio.gather(
                handler.api_session_storage_empty(
                    _request("POST", "/api/system/session-storage/empty", {"all": True})
                ),
                handler.api_session_storage_empty(
                    _request("POST", "/api/system/session-storage/empty", {"all": True})
                ),
            )
            job = handler._empty_job
            # Released in `finally` for the same reason as the mid-run test: the worker
            # parks in `.result()` on a thread that only this event frees, and an
            # assertion failing first would hang the run rather than fail it.
            try:
                statuses = sorted([first.status, second.status])
                assert job is not None and job.task is not None
            finally:
                release.set()
                if job is not None and job.task is not None:
                    await job.task

        assert statuses == [202, 409]

    @pytest.mark.asyncio
    async def test_a_batch_id_that_is_not_a_batch_is_refused_not_dropped(
        self, stores: tuple[Path, Path]
    ) -> None:
        """Naming a bad id must refuse, not report success for a delete that never ran.

        Resolving the target set by FILTERING the staged list took over from
        `_batch_dir`, which every other caller resolves ids through: an id that is not
        a batch - a typo, or one already emptied - was silently dropped, the worker got
        an empty list, and the user was told the delete succeeded.
        """
        seen: list[object] = []

        with (
            patch.object(handler, "_sel", return_value=_sel_stub()),
            patch.object(handler, "empty_trash", lambda ids, *a, **k: seen.append(ids) or 0),
        ):
            resp = await handler.api_session_storage_empty(
                _request(
                    "POST",
                    "/api/system/session-storage/empty",
                    {"batch_ids": ["../../sessions"]},
                )
            )

        assert resp.status == 400
        assert json.loads(resp.body)["code"] == "empty_refused"
        assert seen == [], "nothing was dispatched"
        # The slot is not left holding a job for a request that never ran.
        assert handler._empty_job is None

    @pytest.mark.asyncio
    async def test_an_unreadable_trash_fails_an_empty_all_closed_and_says_so(
        self, stores: tuple[Path, Path]
    ) -> None:
        """ "Everything staged" that cannot be resolved must delete NOTHING.

        Two properties pull against each other here and both come from real findings:
        a failure must not wedge the slot forever, and it must not hand the worker
        "all" to re-enumerate later, which would destroy batches staged after the
        click. So the job settles with a reason instead of running, and the slot is
        free for a retry.
        """

        def _explode(batch_ids):  # type: ignore[no-untyped-def]
            raise ValueError("a staged manifest is malformed")

        deleted: list[object] = []

        with (
            patch.object(handler, "_sel", return_value=_sel_stub()),
            patch.object(handler, "staged_targets", _explode),
            patch.object(handler, "empty_trash", lambda ids, *a, **k: deleted.append(ids) or 0),
        ):
            resp = await handler.api_session_storage_empty(
                _request("POST", "/api/system/session-storage/empty", {"all": True})
            )

        job = handler._empty_job
        assert resp.status == 202
        assert job is not None and job.done is True
        assert job.task is None, "nothing was dispatched"
        assert deleted == [], "no delete ran"
        assert "could not be read" in json.loads(resp.body)["error"]

        # And the slot is free again, rather than answering 409 forever.
        with (
            patch.object(handler, "_sel", return_value=_sel_stub()),
            patch.object(handler, "staged_targets", return_value=([], 0, {})),
            patch.object(handler, "empty_trash", lambda *a, **k: 0),
        ):
            again = await handler.api_session_storage_empty(
                _request("POST", "/api/system/session-storage/empty", {"all": True})
            )
            second = handler._empty_job
            assert second is not None and second.task is not None
            await second.task
        assert again.status == 202

    @pytest.mark.asyncio
    async def test_a_refusal_text_is_scrubbed_before_a_client_reads_it(
        self, stores: tuple[Path, Path]
    ) -> None:
        """The one part of a job a browser renders goes through the same scrubbers.

        A refusal quotes what caused it, which can be the caller's own argument or a
        string the module was resolving, and this endpoint is pollable - so the text
        gets the module's `_redact` and a length cap rather than being passed through.
        Asserted with a credential shape the scrubber recognises (the AWS
        documentation example key), because that is what "redacted" means here: a
        pattern match, not a filter over every possible sentence.
        """
        leak = "refused near AKIAIOSFODNN7EXAMPLE " + ("y" * 900)

        def _refuse(batch_ids, on_progress=None, on_skip=None, expect=None):  # type: ignore[no-untyped-def]
            raise session_storage_module.SessionStorageError(leak)

        with (
            patch.object(handler, "_sel", return_value=_sel_stub()),
            patch.object(handler, "staged_targets", return_value=([], 0, {})),
            patch.object(handler, "empty_trash", _refuse),
        ):
            await handler.api_session_storage_empty(
                _request("POST", "/api/system/session-storage/empty", {"all": True})
            )
            job = handler._empty_job
            assert job is not None and job.task is not None
            await job.task

            body = json.loads(
                (
                    await handler.api_session_storage_empty_status(
                        _request("GET", "/api/system/session-storage/empty", None)
                    )
                ).body
            )

        reported = body["job"]["error"]
        assert len(reported) <= handler._ERROR_TEXT_LIMIT
        assert "AKIAIOSFODNN7EXAMPLE" not in reported
        assert "refused" in reported

    @pytest.mark.asyncio
    async def test_a_named_selection_fails_closed_when_the_snapshot_cannot_be_read(
        self, stores: tuple[Path, Path]
    ) -> None:
        """A list of names is not approval, so a failed snapshot cancels the delete.

        This case used to dispatch anyway, on the reasoning that the caller had already
        said WHICH batches and a missing snapshot only cost the progress bar its
        denominator. That reading was wrong: the snapshot is what turns those names into
        approval of the DIRECTORIES they pointed at, so dispatching without it deletes
        whatever answers to the names by the time the worker runs. The failure is not
        always benign either - a staged tree deep enough to exhaust descriptors arrives
        here as an exception, and writing into the trash is how it gets there.

        Answered as a settled job carrying the reason, not a 500, so the screen has one
        shape to read.
        """

        def _explode(batch_ids):  # type: ignore[no-untyped-def]
            raise ValueError("a staged manifest is malformed")

        seen: list[object] = []

        with (
            patch.object(handler, "_sel", return_value=_sel_stub()),
            patch.object(handler, "staged_targets", _explode),
            patch.object(handler, "empty_trash", lambda ids, *a, **k: seen.append(ids) or 0),
        ):
            resp = await handler.api_session_storage_empty(
                _request(
                    "POST",
                    "/api/system/session-storage/empty",
                    {"batch_ids": ["b-1", "b-2"]},
                )
            )
            job = handler._empty_job
            assert job is not None

        assert resp.status == 202
        body = json.loads(resp.body)
        assert body["running"] is False, "the job is settled, not left polling"
        assert body["error"], "the user has to be told why nothing moved"
        assert seen == [], "nothing may be deleted without an identity to check against"
        assert job.task is None, "no worker was dispatched"

    @pytest.mark.asyncio
    async def test_empty_all_hands_the_worker_the_batches_it_resolved(
        self, stores: tuple[Path, Path]
    ) -> None:
        """A batch staged AFTER the click must survive an "empty everything".

        `empty_trash(None)` enumerates in the worker, which now runs well after the
        request and can queue behind another mutation - so a batch staged in between
        was destroyed although the user never saw it, and a staged batch is the only
        copy of those sessions.
        """
        _, kiro_home = stores
        _retired(kiro_home, "aaaa1111")
        seen: list[object] = []

        with patch.object(handler, "_sel", return_value=_sel_stub()):
            await handler.api_session_storage_cleanup(
                _request("POST", "/api/system/session-storage/cleanup", {"older_than_days": 30})
            )
            staged = [b.batch_id for b in session_storage_module.list_trash()]
            assert len(staged) == 1

            with patch.object(handler, "empty_trash", lambda ids, *a, **k: seen.append(ids) or 0):
                await handler.api_session_storage_empty(
                    _request("POST", "/api/system/session-storage/empty", {"all": True})
                )
                job = handler._empty_job
                assert job is not None and job.task is not None
                await job.task

        assert seen == [staged], "the worker gets ids, never None"

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


class TestTheReclaimRefreshIsCheapToCallPerSession:
    """``move_to_trash`` calls ``refresh`` once per selected session (#7118).

    It has to, because a resume that only READS an old transcript writes nothing
    and is invisible to the mtime guard inside the move loop — the index is where
    it shows up. Rebuilding the index that often is what has to be made affordable:
    the rebuild reads and parses the whole session map, and the selection is capped
    at six figures.
    """

    def _write_map(self, payload: dict[str, str]) -> Path:
        """Land a session map the way ``SessionMap`` does: replace, never edit.

        The token includes ``st_ino`` precisely because every real write arrives
        through ``os.replace``, so an in-place rewrite is not the case under test.
        """
        target = handler.config_dir() / handler.SESSION_MAP_FILENAME
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, target)
        return target

    def test_the_same_index_is_returned_while_the_map_has_not_moved(
        self, stores: tuple[Path, Path]
    ) -> None:
        """One stat, not one parse — and the SAME object, which is the signal.

        ``move_to_trash`` reads an unchanged index by identity, so returning an
        equal-but-new object would still pay for re-deriving its sets per session.
        """
        self._write_map({"dashboard:chat-1": "aaaa1111"})
        built = []

        def _spy() -> handler.SessionIndex:
            built.append(1)
            return handler.SessionIndex()

        refresh = handler._MapBackedRefresh()
        with patch.object(handler, "_build_index", _spy):
            first = refresh()
            second = refresh()
            third = refresh()

        assert first is second is third
        assert len(built) == 1

    def test_a_mapping_write_forces_a_rebuild(self, stores: tuple[Path, Path]) -> None:
        """A resume maps the session, which replaces the file. That must be seen."""
        self._write_map({"dashboard:chat-1": "aaaa1111"})

        def _spy() -> handler.SessionIndex:
            return handler.SessionIndex()

        refresh = handler._MapBackedRefresh()
        with patch.object(handler, "_build_index", _spy):
            before = refresh()
            self._write_map({"dashboard:chat-1": "aaaa1111", "dashboard:chat-2": "bbbb2222"})
            after = refresh()

        assert before is not after

    def test_an_unreadable_map_is_never_treated_as_unchanged(
        self, stores: tuple[Path, Path]
    ) -> None:
        """Fails toward rebuilding: a missing map costs a re-read, not a skip.

        Skipping on an unreadable token would turn "I cannot tell" into "nothing
        has changed", which is exactly the wrong direction for a guard.
        """
        assert not (handler.config_dir() / handler.SESSION_MAP_FILENAME).exists()
        built = []

        def _spy() -> handler.SessionIndex:
            built.append(1)
            return handler.SessionIndex()

        refresh = handler._MapBackedRefresh()
        with patch.object(handler, "_build_index", _spy):
            first = refresh()
            second = refresh()

        assert first is not second
        assert len(built) == 2

    def test_a_write_landing_during_a_rebuild_is_not_missed(
        self, stores: tuple[Path, Path]
    ) -> None:
        """The token is read BEFORE the rebuild, and that one is stored.

        Stamping the rebuilt index with a token read afterwards would record a write
        the rebuild had already raced past, and the next call would report the map
        unchanged — losing the very resume this exists to catch.
        """
        self._write_map({"dashboard:chat-1": "aaaa1111"})
        raced = {"yet": False}

        def _spy_that_races() -> handler.SessionIndex:
            if not raced["yet"]:
                raced["yet"] = True
                self._write_map({"dashboard:chat-2": "bbbb2222"})
            return handler.SessionIndex()

        refresh = handler._MapBackedRefresh()
        with patch.object(handler, "_build_index", _spy_that_races):
            first = refresh()
            second = refresh()

        assert first is not second


class TestWhyAReclaimIsRefused:
    """A refusal must name the real reason.

    Both states are refused today, so these tests do not assert new power — they
    assert that the product stops telling a user a month-old idle conversation is
    "in use", which is a claim the user can disprove by reading the date next to it.
    """

    def test_the_pre_classification_never_reads_a_cached_cotenant_pass(
        self, stores: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_classify derives per-row refusals, so it must enumerate uncached.

        A pre-pass fed a 30s-stale co-tenant set would classify a just-claimed
        session as eligible; a stale store scan fails independently the same way
        (a stale-older mtime reads eligible here and too_fresh inside the move).
        Either way the authority still refuses, but as the all-or-nothing batch
        failure ``_classify`` exists to prevent. Both halves are counted, so a
        future change that splits the flag cannot re-cache either half here.
        """
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        index = session_storage_module.SessionIndex(stem_to_sid={}, active_sids=frozenset())
        # Prime both caches through the ordinary cached read path.
        session_storage_module.list_units(index)

        calls = {"cotenant": 0, "scan": 0}
        real_cotenants = session_storage_module._replay_store_cotenants
        real_scan = session_storage_module._scan_raw_uncached

        def counted_cotenants() -> list[str]:
            calls["cotenant"] += 1
            return real_cotenants()

        def counted_scan(sid_for_stem):
            calls["scan"] += 1
            return real_scan(sid_for_stem)

        monkeypatch.setattr(session_storage_module, "_replay_store_cotenants", counted_cotenants)
        monkeypatch.setattr(session_storage_module, "_scan_raw_uncached", counted_scan)
        handler._classify([], index, now=time.time())

        assert calls["cotenant"] == 1, "the trash pre-pass must re-read co-tenants, not the cache"
        assert calls["scan"] == 1, "the trash pre-pass must re-enumerate the stores, not the cache"

    @staticmethod
    def _recorded_session(crew_home: Path, kiro_home: Path, sid: str, key: str) -> None:
        """A session that IS in the map: a transcript, a replay log, and the entry."""
        stem = key.replace(":", "_")
        transcript = crew_home / "sessions" / f"{stem}.jsonl"
        transcript.write_text('{"_type": "metadata"}\n')
        mtime = time.time() - 30 * _DAY
        os.utime(transcript, (mtime, mtime))
        _retired(kiro_home, sid, age_days=30)
        for directory in (crew_home, crew_home / "crew"):
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "session_map.json").write_text(json.dumps({key: {"sid": sid}}))

    def _request_with_running(self, uids: list[str], running: frozenset[str]):
        req = _request("POST", "/api/system/session-storage/trash", {"uids": uids})
        state = req.app["state"]
        # Set explicitly. A MagicMock would answer `in` with False and make this
        # pass for the wrong reason, asserting against an interface nobody has.
        state.running_session_keys.return_value = running
        state.conversation_log.list_sessions.return_value = []
        return req

    @pytest.mark.asyncio
    async def test_an_idle_recorded_session_is_refused_as_resumable_not_in_use(
        self, stores: tuple[Path, Path]
    ) -> None:
        crew_home, kiro_home = stores
        sid = "aaaaaaaa-0000-4000-8000-000000000001"
        self._recorded_session(crew_home, kiro_home, sid, "dashboard:chat-9")

        req = self._request_with_running([sid], frozenset())
        with patch.object(handler, "_sel", _sel_stub):
            resp = await handler.api_session_inventory_trash(req)

        body = json.loads(resp.body)
        assert body["sessions"] == 0, "still refused — this test asserts the reason, not new power"
        assert body["refused"] == [{"uid": sid, "reason": "resumable"}]

    @pytest.mark.asyncio
    async def test_a_running_session_is_refused_as_in_use(self, stores: tuple[Path, Path]) -> None:
        crew_home, kiro_home = stores
        sid = "aaaaaaaa-0000-4000-8000-000000000002"
        key = "dashboard:chat-10"
        self._recorded_session(crew_home, kiro_home, sid, key)

        req = self._request_with_running([sid], frozenset({key}))
        with patch.object(handler, "_sel", _sel_stub):
            resp = await handler.api_session_inventory_trash(req)

        body = json.loads(resp.body)
        assert body["sessions"] == 0
        assert body["refused"] == [{"uid": sid, "reason": "in_use"}]

    @pytest.mark.asyncio
    async def test_the_row_reports_running_separately_from_resumable(
        self, stores: tuple[Path, Path]
    ) -> None:
        crew_home, kiro_home = stores
        sid = "aaaaaaaa-0000-4000-8000-000000000003"
        key = "dashboard:chat-11"
        self._recorded_session(crew_home, kiro_home, sid, key)

        req = _request("GET", "/api/system/session-storage/sessions")
        state = req.app["state"]
        state.running_session_keys.return_value = frozenset()
        state.conversation_log.list_sessions.return_value = []
        resp = await handler.api_session_inventory(req)

        rows = {row["uid"]: row for row in json.loads(resp.body)["sessions"]}
        assert rows[sid]["active"] is True, "recorded, so still refused"
        assert rows[sid]["live"] is False, "nothing is running, so it is not in use"


class TestTheListDoesNotShipTheWholeStore:
    """Six figures of replay-only rows render as one collapsed line.

    Sending them all was 35MB of JSON on the measured machine and most of why the
    screen took tens of seconds to open. The cap is only safe because the group's
    real size and total still travel, and because the sessions below the cut stay
    reachable by age — so these tests pin both halves of that bargain.
    """

    @pytest.mark.asyncio
    async def test_only_the_largest_replay_only_sessions_are_listed(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, kiro_home = stores
        monkeypatch.setattr(handler, "_BACKGROUND_ROW_LIMIT", 2)
        for i, size in enumerate((10, 5000, 200, 900)):
            _retired(kiro_home, f"aaaaaaaa-0000-4000-8000-00000000000{i}", log_bytes=size)

        req = _request("GET", "/api/system/session-storage/sessions")
        state = req.app["state"]
        state.running_session_keys.return_value = frozenset()
        state.conversation_log.list_sessions.return_value = []
        body = json.loads((await handler.api_session_inventory(req)).body)

        listed = [row["bytes"] for row in body["sessions"]]
        assert len(listed) == 2, "the cap must bound the response, not just the display"
        assert listed == sorted(listed, reverse=True), "the largest are the ones worth listing"
        assert listed[0] > 5000, "the biggest session must survive the cut"

    @pytest.mark.asyncio
    async def test_the_group_reports_its_true_size_not_the_listed_sample(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A client that filtered the rows would under-report by six figures."""
        _, kiro_home = stores
        monkeypatch.setattr(handler, "_BACKGROUND_ROW_LIMIT", 1)
        total = sum(
            _retired(kiro_home, f"aaaaaaaa-0000-4000-8000-00000000000{i}", log_bytes=100 * (i + 1))
            for i in range(4)
        )

        req = _request("GET", "/api/system/session-storage/sessions")
        state = req.app["state"]
        state.running_session_keys.return_value = frozenset()
        state.conversation_log.list_sessions.return_value = []
        body = json.loads((await handler.api_session_inventory(req)).body)

        assert body["background"]["sessions"] == 4
        assert body["background"]["bytes"] == total
        assert body["background"]["listed"] == 1
        assert len(body["sessions"]) == 1, "the sample the summary is describing"

    @pytest.mark.asyncio
    async def test_age_options_are_cumulative_and_exclude_sessions_in_use(
        self, stores: tuple[Path, Path]
    ) -> None:
        """The options label a sweep, and a sweep takes everything OLDER than N.

        Disjoint bands would make a client sum them and infer the boundaries from
        their labels, and a label it mis-parsed would understate what is about to
        move.
        """
        crew_home, kiro_home = stores
        _retired(kiro_home, "aaaaaaaa-0000-4000-8000-000000000001", age_days=10)
        _retired(kiro_home, "aaaaaaaa-0000-4000-8000-000000000002", age_days=200)
        # Mapped, so refused however old it is — and therefore not offered.
        held = "aaaaaaaa-0000-4000-8000-000000000003"
        _retired(kiro_home, held, age_days=500)
        (crew_home / "session_map.json").write_text(
            json.dumps({"dashboard:chat-1": {"sid": held}}), encoding="utf-8"
        )

        req = _request("GET", "/api/system/session-storage/sessions")
        state = req.app["state"]
        state.running_session_keys.return_value = frozenset()
        state.conversation_log.list_sessions.return_value = []
        body = json.loads((await handler.api_session_inventory(req)).body)

        counts = {opt["days"]: opt["sessions"] for opt in body["age_options"]}
        assert counts[7] == 2, "both retired sessions are older than a week"
        assert counts[30] == 1, "only the 200-day one is older than a month"
        assert counts[90] == 1
        assert all(
            opt["sessions"] <= 2 for opt in body["age_options"]
        ), "a session the server would refuse must never be counted into an offer"


class TestTranscriptContentIsRedacted:
    """A title and a first message are conversation content.

    Either can carry a key someone pasted into a chat, so both are scrubbed
    before they reach the dashboard — the same rule the artifact surface follows.
    """

    SECRET = "AKIAIOSFODNN7EXAMPLE"

    @pytest.mark.asyncio
    async def test_a_credential_in_a_title_never_reaches_the_list(
        self, stores: tuple[Path, Path]
    ) -> None:
        crew_home, kiro_home = stores
        sid = "cccccccc-0000-4000-8000-000000000001"
        key = "dashboard:chat-20"
        TestWhyAReclaimIsRefused._recorded_session(crew_home, kiro_home, sid, key)

        req = _request("GET", "/api/system/session-storage/sessions")
        state = req.app["state"]
        state.running_session_keys.return_value = frozenset()
        state.conversation_log.list_sessions.return_value = [
            {"key": key.replace(":", "_"), "title": f"deploy with {self.SECRET} today"}
        ]
        resp = await handler.api_session_inventory(req)

        body = resp.body.decode()
        assert self.SECRET not in body
        rows = {r["uid"]: r for r in json.loads(body)["sessions"]}
        assert "deploy with" in rows[sid]["title"], "only the secret is scrubbed, not the title"

    @pytest.mark.asyncio
    async def test_a_credential_in_a_first_message_never_reaches_the_detail(
        self, stores: tuple[Path, Path]
    ) -> None:
        crew_home, kiro_home = stores
        sid = "cccccccc-0000-4000-8000-000000000002"
        TestWhyAReclaimIsRefused._recorded_session(crew_home, kiro_home, sid, "dashboard:chat-21")

        secret = self.SECRET

        class _Digest:
            first_message = f"here is my key {secret}"
            turns = 3
            images = 0

        with patch.object(handler, "digest", lambda *a, **k: _Digest()):
            req = _request("GET", f"/api/system/session-storage/sessions/{sid}")
            req.match_info["uid"] = sid
            resp = await handler.api_session_inventory_detail(req)

        body = resp.body.decode()
        assert self.SECRET not in body
        assert "here is my key" in body


class TestRefusalsAreAudited:
    """Being told "no" is an outcome the audit log has to carry.

    Someone asked to remove specific conversations and did not get to. Recording
    only the successes would leave the protection invisible.
    """

    @pytest.mark.asyncio
    async def test_a_fully_refused_selection_is_audited_as_denied(
        self, stores: tuple[Path, Path]
    ) -> None:
        crew_home, kiro_home = stores
        sid = "dddddddd-0000-4000-8000-000000000001"
        TestWhyAReclaimIsRefused._recorded_session(crew_home, kiro_home, sid, "dashboard:chat-30")

        req = _request("POST", "/api/system/session-storage/trash", {"uids": [sid]})
        req.app["state"].running_session_keys.return_value = frozenset()
        sel = _sel_stub()
        with patch.object(handler, "_sel", lambda: sel):
            resp = await handler.api_session_inventory_trash(req)

        assert json.loads(resp.body)["sessions"] == 0
        outcomes = [c.kwargs["outcome"] for c in sel.log_api_access.call_args_list]
        assert "denied" in outcomes, "a refusal must leave a denied event"
        denial = next(
            c for c in sel.log_api_access.call_args_list if c.kwargs["outcome"] == "denied"
        )
        assert sid in denial.kwargs["resources"]
        assert "resumable" in denial.kwargs["resources"], "the reason belongs in the record"

    @pytest.mark.asyncio
    async def test_a_refusal_raised_from_inside_the_move_is_audited_too(
        self, stores: tuple[Path, Path]
    ) -> None:
        """The only record for a batch refused mid-move is this one.

        A selection where every session is protected DURING staging never reaches
        the success path, so without an audit on the exception branch the
        protection decision would leave no trace at all.
        """
        crew_home, kiro_home = stores
        # Unmapped and old, so it passes pre-flight and the move is attempted --
        # which is the only way to reach the branch under test.
        sid = "dddddddd-0000-4000-8000-000000000009"
        _retired(kiro_home, sid, age_days=45)

        req = _request("POST", "/api/system/session-storage/trash", {"uids": [sid]})
        req.app["state"].running_session_keys.return_value = frozenset()
        sel = _sel_stub()

        def _raise(*_args, **_kwargs):
            raise handler.SessionStorageError(
                "all 1 selected session(s) were resumed while being staged; nothing was moved"
            )

        with (
            patch.object(handler, "_sel", lambda: sel),
            patch.object(handler, "move_to_trash", _raise),
        ):
            resp = await handler.api_session_inventory_trash(req)

        assert resp.status == 400
        assert json.loads(resp.body)["code"] == "trash_refused"
        outcomes = [c.kwargs["outcome"] for c in sel.log_api_access.call_args_list]
        assert "denied" in outcomes, "a mid-move refusal must leave a denied event"
        assert "success" not in outcomes, "nothing was taken, so nothing succeeded"

    @pytest.mark.asyncio
    async def test_a_partial_refusal_is_audited_alongside_the_success(
        self, stores: tuple[Path, Path]
    ) -> None:
        """Nine taken and one protected must record BOTH, not just the nine."""
        crew_home, kiro_home = stores
        protected = "dddddddd-0000-4000-8000-000000000002"
        TestWhyAReclaimIsRefused._recorded_session(
            crew_home, kiro_home, protected, "dashboard:chat-31"
        )
        # An unmapped, old session: eligible, so the request partially succeeds.
        takeable = "dddddddd-0000-4000-8000-000000000003"
        _retired(kiro_home, takeable, age_days=45)

        req = _request("POST", "/api/system/session-storage/trash", {"uids": [protected, takeable]})
        req.app["state"].running_session_keys.return_value = frozenset()
        sel = _sel_stub()
        with patch.object(handler, "_sel", lambda: sel):
            resp = await handler.api_session_inventory_trash(req)

        body = json.loads(resp.body)
        assert body["sessions"] == 1, "the eligible one still moves"
        assert [r["uid"] for r in body["refused"]] == [protected]
        outcomes = [c.kwargs["outcome"] for c in sel.log_api_access.call_args_list]
        assert "denied" in outcomes and "success" in outcomes, outcomes


class TestAMalformedTitleCannotCrashTheList:
    """A title is not guaranteed to be a string.

    The resume path assigns a client-supplied ``body["title"]`` to the slot with no
    type check of its own, so a number reaches the persisted metadata. A number is
    truthy, so a plain presence check would hand it to the scrubbers and turn a read
    into a 500.
    """

    @pytest.mark.asyncio
    async def test_a_numeric_title_is_skipped_rather_than_raising(
        self, stores: tuple[Path, Path]
    ) -> None:
        crew_home, kiro_home = stores
        sid = "eeeeeeee-0000-4000-8000-000000000001"
        key = "dashboard:chat-40"
        TestWhyAReclaimIsRefused._recorded_session(crew_home, kiro_home, sid, key)

        req = _request("GET", "/api/system/session-storage/sessions")
        state = req.app["state"]
        state.running_session_keys.return_value = frozenset()
        state.conversation_log.list_sessions.return_value = [
            {"key": key.replace(":", "_"), "title": 12345},
            {"key": "other_stem", "title": None},
        ]

        resp = await handler.api_session_inventory(req)

        assert resp.status == 200
        rows = {r["uid"]: r for r in json.loads(resp.body)["sessions"]}
        # The row still lists — it just falls back to its origin for a label.
        assert rows[sid]["title"] == ""
        assert rows[sid]["origin"], "the row is still identifiable without a title"


class TestACredentialInASessionIdIsScrubbed:
    """A session id is only loosely constrained, and ``origin`` is rendered.

    ``_UNIT_ID_RE`` admits ``[A-Za-z0-9._-]``, which is exactly the shape of an
    access-key id — so a session whose key happens to be one would otherwise put it
    on screen as the row's provenance line.
    """

    @pytest.mark.asyncio
    async def test_an_access_key_shaped_session_id_is_not_rendered(
        self, stores: tuple[Path, Path]
    ) -> None:
        crew_home, kiro_home = stores
        secret = "AKIAIOSFODNN7EXAMPLE"
        # A replay-only unit keyed by the offending id, which is what `_origin`
        # falls back to when there is no transcript stem.
        _retired(kiro_home, secret, age_days=40)

        req = _request("GET", "/api/system/session-storage/sessions")
        state = req.app["state"]
        state.running_session_keys.return_value = frozenset()
        state.conversation_log.list_sessions.return_value = []
        resp = await handler.api_session_inventory(req)

        rows = {r["uid"]: r for r in json.loads(resp.body)["sessions"]}
        assert secret in rows, "the row must still be listed and actionable"
        assert secret not in rows[secret]["origin"], "the DISPLAYED string is scrubbed"
