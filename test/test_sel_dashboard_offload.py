"""Regression guard: SEL dashboard handlers must not block the event loop.

``recent()`` and ``verify_integrity()`` both read the WHOLE audit-log file with
blocking IO (``sel.py`` reads it via ``read_text()`` in both). The log is a single
JSONL file pruned by age, so ``limit`` bounds the rows ``recent()`` returns, not
the bytes it reads. Called inline from an aiohttp handler either one stalls the
entire event loop for the duration of the read.
These tests assert both handlers dispatch through ``run_in_executor`` on the
bounded DISCOVERY pool -- not ``maintenance_executor``. Both handlers are
browser-triggerable, so routing them through the maintenance pool would let
dashboard tabs occupy the workers the orphan-reaping sweeps need. They FAIL
against an inline ``_sel().recent(...)`` / ``_sel().verify_integrity()`` call,
because ``run_in_executor`` is then never reached.

They also pin WHERE ``_sel()`` is called. Constructing the singleton reads or
creates the HMAC key and scans the log tail, so evaluating ``_sel()`` while
building the callable would leave that IO on the event loop even though the
read itself is offloaded. This is checked at two strengths: the ordering tests
record "callable submitted" against "``_sel()`` called" and require submission
first, and the thread-identity tests submit to a real executor through the real
running loop and require the constructing thread to differ from the loop's.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import kiro_crew.dashboard.handlers.core as core_mod
from kiro_crew import sel as sel_mod


class _FakeLoop:
    """Records the pool + callable handed to run_in_executor, then runs it."""

    def __init__(self, captured: dict, order: list[str]) -> None:
        self._captured = captured
        self._order = order

    async def run_in_executor(self, pool, fn, *args):  # type: ignore[no-untyped-def]
        self._captured["pool"] = pool
        self._captured["fn"] = fn
        self._order.append("submitted")
        return fn(*args)


def _request(query: dict | None = None) -> MagicMock:
    """An owner-authenticated fake request.

    ``/api/sel/events`` serves the security audit trail and is owner-gated, so a
    request that carries no owner identity is refused before the offload this
    module is about. The identity is shaped the way
    ``is_owner_dashboard_request`` reads it: a dashboard (empty-``app``) session
    whose user matches the configured owner.
    """
    req = MagicMock()
    req.query = query or {}
    req.app = {"state": SimpleNamespace(owner_id="owner-1")}
    req.__contains__.side_effect = lambda key: key == "app"
    req.__getitem__.side_effect = lambda key: "" if key == "app" else None
    req.get.side_effect = lambda key, default=None: "owner-1" if key == "user" else default
    return req


def _tracking_sel(fake_sel: MagicMock, order: list[str]):
    """Stand-in for ``_sel`` that records when construction happens."""

    def _call():
        order.append("sel")
        return fake_sel

    return _call


class TestSelHandlerOffload:
    @pytest.mark.asyncio
    async def test_events_handler_offloads_recent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict = {}
        order: list[str] = []
        fake_sel = MagicMock()
        fake_sel.recent.return_value = [{"event_id": "e1"}]
        sentinel_pool = object()
        monkeypatch.setattr(core_mod, "_sel", _tracking_sel(fake_sel, order))
        monkeypatch.setattr(core_mod, "discovery_executor", lambda: sentinel_pool)
        monkeypatch.setattr(
            core_mod.asyncio, "get_running_loop", lambda: _FakeLoop(captured, order)
        )

        resp = await core_mod.api_sel_events(_request({"limit": "7"}))

        assert captured["pool"] is sentinel_pool  # bounded discovery pool
        assert order == ["submitted", "sel"]  # singleton built off the loop
        fake_sel.recent.assert_called_once_with(limit=7)
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_the_allow_audit_shares_the_read_s_hop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The authorized read's own audit row must not be written on the loop.

        It is the same constraint as the read: the first ``_sel()`` call constructs
        the singleton, reading the HMAC key and scanning the log tail. Writing the
        allow row outside the hop would put that construction back on the loop, so it
        rides inside the submitted callable instead of paying a second hop.
        """
        captured: dict = {}
        order: list[str] = []
        fake_sel = MagicMock()
        fake_sel.recent.return_value = []
        fake_sel.log_api_access.side_effect = lambda **kw: order.append("audit")
        monkeypatch.setattr(core_mod, "_sel", _tracking_sel(fake_sel, order))
        monkeypatch.setattr(core_mod, "discovery_executor", lambda: object())
        monkeypatch.setattr(
            core_mod.asyncio, "get_running_loop", lambda: _FakeLoop(captured, order)
        )

        resp = await core_mod.api_sel_events(_request())

        assert resp.status == 200
        # Submitted first, then the singleton, then the audit: nothing before the hop.
        assert order == ["submitted", "sel", "audit"]
        assert fake_sel.log_api_access.call_args.kwargs["outcome"] == "allowed"

    @pytest.mark.asyncio
    async def test_verify_handler_offloads_verify_integrity(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict = {}
        order: list[str] = []
        fake_sel = MagicMock()
        fake_sel.verify_integrity.return_value = sel_mod.SelVerification(5, 5, True, "")
        sentinel_pool = object()
        monkeypatch.setattr(core_mod, "_sel", _tracking_sel(fake_sel, order))
        monkeypatch.setattr(core_mod, "discovery_executor", lambda: sentinel_pool)
        monkeypatch.setattr(
            core_mod.asyncio, "get_running_loop", lambda: _FakeLoop(captured, order)
        )

        resp = await core_mod.api_sel_verify(_request())

        assert captured["pool"] is sentinel_pool
        assert order == ["submitted", "sel"]
        fake_sel.verify_integrity.assert_called_once_with(detailed=True)
        assert resp.status == 200


class TestSelConstructionRunsOffTheLoop:
    """Thread-identity guards, run against a REAL loop and a REAL executor.

    ``TestSelHandlerOffload`` substitutes the loop, so its ordering assertion
    shows only that ``_sel()`` is called after submission -- the callable still
    runs inline in the test's own thread. These two tests submit to a real
    ``ThreadPoolExecutor`` through the real running loop and compare the thread
    that calls ``_sel()`` against the test's thread, which is the loop's thread.
    That is the property the offload exists for: singleton construction, which
    reads or creates the HMAC key and scans the log tail, must not run on the
    event loop. They fail if ``_sel()`` is evaluated while building the callable.
    """

    @staticmethod
    def _recording_sel(fake_sel: MagicMock, seen: dict):
        def _call():
            seen["ident"] = threading.get_ident()
            return fake_sel

        return _call

    @pytest.mark.asyncio
    async def test_events_handler_builds_sel_on_a_worker_thread(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict = {}
        fake_sel = MagicMock()
        fake_sel.recent.return_value = []
        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="test-disc")
        monkeypatch.setattr(core_mod, "_sel", self._recording_sel(fake_sel, seen))
        monkeypatch.setattr(core_mod, "discovery_executor", lambda: pool)
        try:
            resp = await core_mod.api_sel_events(_request({"limit": "3"}))
        finally:
            pool.shutdown(wait=True)

        assert seen["ident"] != threading.get_ident()  # off the loop's thread
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_verify_handler_builds_sel_on_a_worker_thread(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict = {}
        fake_sel = MagicMock()
        fake_sel.verify_integrity.return_value = sel_mod.SelVerification(2, 2, True, "")
        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="test-disc")
        monkeypatch.setattr(core_mod, "_sel", self._recording_sel(fake_sel, seen))
        monkeypatch.setattr(core_mod, "discovery_executor", lambda: pool)
        try:
            resp = await core_mod.api_sel_verify(_request())
        finally:
            pool.shutdown(wait=True)

        assert seen["ident"] != threading.get_ident()
        assert resp.status == 200
