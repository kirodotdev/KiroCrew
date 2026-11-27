"""The owner-tier store-art prewarm spends its batch on cold rows before stale ones.

An explicit registry refresh expires every row's manifest cache before it prewarms
(``refresh_registries``), so every row reads as not warm afterwards, including rows
whose declared art is all in the blob cache already. Walked in plain index order,
each refresh would re-clone the same head of the index until the budget ran out, and
rows further down would never get their art. A row with art missing goes ahead of a
row that is only stale; stale rows still re-warm when the budget allows. A row the
batch budget cancelled goes after the unmarked rows of its own group, and is never
skipped.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import pytest
from registry_store_art_prewarm_helpers import (
    MANIFEST,
    _await_barrier,
    _entry,
    _fake_fetch,
    _pin_registry,
    _settled,
)

from kiro_crew.apps import registry as reg_mod
from kiro_crew.apps.registry import (
    _TRUST_OWNER,
    _expire_cache_file,
    _manifest_cache_path,
    _prewarm_owner_tier_store_assets,
    _read_manifest_cache,
    _store_art_cache_path,
)

ART_ONE = {"ui/icon.svg": b"<svg/>"}
MANIFEST_ONE = {**MANIFEST, "heroImage": "", "screenshots": []}


def _rows(n: int) -> list[dict[str, Any]]:
    """*n* index rows, each its own repository, so each has its own blob cache."""
    rows = []
    for i in range(n):
        url = f"https://forge.example.com/org/app-{i:02d}.git"
        rows.append(_entry(name=f"app-{i:02d}", gitUrl=url, repo=url))
    return rows


def _cloned(calls: list[dict[str, Any]]) -> list[str]:
    return [c["git_url"].rsplit("/", 1)[-1].removesuffix(".git") for c in calls]


def _has_art(row: dict[str, Any]) -> bool:
    path = _store_art_cache_path(row, "ui/icon.svg")
    return path is not None and path.is_file()


class TestColdRowsFirst:
    @pytest.mark.asyncio
    async def test_rows_with_missing_art_are_cloned_before_rows_that_are_only_stale(
        self, monkeypatch
    ):
        """Three rows warmed earlier and then expired, as a refresh leaves them, plus
        two rows never warmed further down the index: the two cold rows are cloned
        first, and the stale rows still re-warm after them in index order."""
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        monkeypatch.setattr(reg_mod, "_PREWARM_CONCURRENCY", 1)
        rows = _rows(5)
        calls = _fake_fetch(monkeypatch, ART_ONE, MANIFEST_ONE)
        assert await _prewarm_owner_tier_store_assets(reg, rows[:3]) == 3
        for row in rows[:3]:
            _expire_cache_file(_manifest_cache_path(row))
            assert _read_manifest_cache(row) is None
            assert _has_art(row)
        calls.clear()

        fetched = await _prewarm_owner_tier_store_assets(reg, rows)

        assert fetched == 5
        assert _cloned(calls) == ["app-03", "app-04", "app-00", "app-01", "app-02"]
        assert all(_has_art(row) for row in rows)

    @pytest.mark.asyncio
    async def test_a_stale_row_whose_art_is_gone_counts_as_cold(self, monkeypatch):
        """Only the art decides: a stale row whose blob-cache bytes were removed is as
        cold as a row never warmed, so it keeps its index place among the cold rows."""
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        monkeypatch.setattr(reg_mod, "_PREWARM_CONCURRENCY", 1)
        rows = _rows(3)
        calls = _fake_fetch(monkeypatch, ART_ONE, MANIFEST_ONE)
        assert await _prewarm_owner_tier_store_assets(reg, rows[:2]) == 2
        for row in rows[:2]:
            _expire_cache_file(_manifest_cache_path(row))
        _store_art_cache_path(rows[1], "ui/icon.svg").unlink()
        calls.clear()

        await _prewarm_owner_tier_store_assets(reg, rows)

        assert _cloned(calls) == ["app-01", "app-02", "app-00"]

    @pytest.mark.parametrize(
        "body",
        [
            pytest.param(b"[" * 100_000 + b"]" * 100_000, id="nested-past-the-decoder-stack"),
            pytest.param(b"{not json", id="malformed"),
            pytest.param(b"\xff\xfe", id="not-utf8"),
            pytest.param(b"[]", id="not-an-object"),
        ],
    )
    @pytest.mark.asyncio
    async def test_an_unreadable_stale_manifest_ranks_the_row_cold_and_the_batch_runs(
        self, monkeypatch, body
    ):
        """The ranking read opens an agent-writable file. A body it cannot use --
        including one under the size cap that nests deeply enough to exhaust the JSON
        decoder's stack -- ranks the row as cold; it never fails the batch."""
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        monkeypatch.setattr(reg_mod, "_PREWARM_CONCURRENCY", 1)
        rows = _rows(2)
        calls = _fake_fetch(monkeypatch, ART_ONE, MANIFEST_ONE)
        assert await _prewarm_owner_tier_store_assets(reg, rows) == 2
        for row in rows:
            _expire_cache_file(_manifest_cache_path(row))
        stat = _manifest_cache_path(rows[1]).stat()
        _manifest_cache_path(rows[1]).write_bytes(body)
        os.utime(_manifest_cache_path(rows[1]), (stat.st_atime, stat.st_mtime))
        calls.clear()

        assert await _prewarm_owner_tier_store_assets(reg, rows) == 2

        assert _cloned(calls) == ["app-01", "app-00"]

    @pytest.mark.asyncio
    async def test_an_unreadable_unobtainable_record_does_not_fail_the_ranking(self, monkeypatch):
        """The ranking also reads the ``.unobtainable.json`` record beside an expired
        manifest, another agent-writable file. One nested past the decoder's stack is
        malformed like any other: it reads as no record, and the batch runs."""
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        monkeypatch.setattr(reg_mod, "_PREWARM_CONCURRENCY", 1)
        rows = _rows(2)
        calls = _fake_fetch(monkeypatch, ART_ONE, MANIFEST_ONE)
        assert await _prewarm_owner_tier_store_assets(reg, rows) == 2
        for row in rows:
            _expire_cache_file(_manifest_cache_path(row))
        reg_mod._unobtainable_art_path(rows[1]).write_bytes(b"[" * 100_000 + b"]" * 100_000)
        calls.clear()

        assert await _prewarm_owner_tier_store_assets(reg, rows) == 2

        # Both rows still hold their art, so both rank as stale, in index order.
        assert _cloned(calls) == ["app-00", "app-01"]

    @pytest.mark.asyncio
    async def test_repeated_refreshes_reach_every_row_when_one_batch_cannot(self, monkeypatch):
        """The reported case, through ``refresh_registries``: six rows, and each batch
        lands only two clones. The rest of the batch fails, and the clone-failure backoff
        is held open so a failed row is retried on the next refresh in its cold place; a
        failure writes no budget mark, so only the cold-first order is under test. In
        plain index order every refresh restarts at the top, so the same two rows are
        cloned each time and four never get art. Cold-first, each refresh reaches the
        next cold rows, and three refreshes cover all six."""
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        monkeypatch.setattr(reg_mod, "_PREWARM_CONCURRENCY", 1)
        monkeypatch.setattr(reg_mod, "_read_clone_failure", lambda entry: None)
        rows = _rows(6)
        landed = _fake_fetch(monkeypatch, ART_ONE, MANIFEST_ONE)
        real_fetch = reg_mod._git_fetch_branch
        per_batch = {"n": 0}
        attempted: list[str] = []

        async def _two_then_fail(git_url, branch, dest, log_lines, **kw):
            attempted.append(git_url.rsplit("/", 1)[-1].removesuffix(".git"))
            per_batch["n"] += 1
            if per_batch["n"] > 2:
                return {"error": "batch over"}
            return await real_fetch(git_url, branch, dest, log_lines, **kw)

        async def _fetch_index(r):
            assert r == reg
            return rows

        async def _list():
            return rows

        monkeypatch.setattr(reg_mod, "_git_fetch_branch", _two_then_fail)
        monkeypatch.setattr(reg_mod, "_fetch_and_cache_external_registry", _fetch_index)
        monkeypatch.setattr(
            reg_mod, "_read_external_registry_cache", lambda name, *, ignore_ttl=False: None
        )
        monkeypatch.setattr(reg_mod, "list_registry", _list)

        batches: list[list[str]] = []
        for _ in range(3):
            per_batch["n"] = 0
            landed.clear()
            result = await reg_mod.refresh_registries()
            assert result["ok"] is True
            batches.append(_cloned(landed))

        assert batches == [["app-00", "app-01"], ["app-02", "app-03"], ["app-04", "app-05"]], (
            batches,
            attempted,
        )
        assert all(_has_art(row) for row in rows)


async def _batch(reg: Any, rows: list[dict[str, Any]], workers: set[Any], what: str) -> int:
    """Run one prewarm batch, then wait for every worker that fetched one of *rows*.

    The batch can return as soon as its first cancelled worker unwinds, while the
    others are still writing their marks, so each worker the fake ran on is awaited
    within the lost-run ceiling.
    """
    fetched = await _settled(_prewarm_owner_tier_store_assets(reg, rows), what)
    await _settled(asyncio.gather(*workers, return_exceptions=True), f"{what} workers")
    workers.clear()
    return fetched


class TestBudgetCancelledRowsLast:
    @pytest.mark.asyncio
    async def test_rows_the_budget_cancelled_wait_behind_rows_not_yet_tried(self, monkeypatch):
        """Four rows that outlast the budget at the head of the index: the next batch
        gives its slots to the four rows behind them, the slow rows are still tried,
        oldest mark first, when nothing else is left, and a row that lands clears its
        mark."""
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        monkeypatch.setattr(reg_mod, "_PREWARM_CONCURRENCY", 4)
        monkeypatch.setattr(reg_mod, "_PREWARM_BATCH_BUDGET", 0.2)
        rows = _rows(8)
        slow = {"app-00", "app-01", "app-02", "app-03"}
        started: list[str] = []
        workers: set[Any] = set()

        async def _fetch(entry, registry_name):
            started.append(entry["name"])
            workers.add(asyncio.current_task())
            if entry["name"] in slow:
                await asyncio.Event().wait()
            return True

        monkeypatch.setattr(reg_mod, "_fetch_owner_tier_store_assets", _fetch)

        assert await _batch(reg, rows, workers, "batch 1") == 0
        assert started == ["app-00", "app-01", "app-02", "app-03"]

        started.clear()
        assert await _batch(reg, rows, workers, "batch 2") == 4
        assert started[:4] == ["app-04", "app-05", "app-06", "app-07"]
        assert all(reg_mod._read_prewarm_budget_mark(row) is not None for row in rows[:4])

        # Age the marks in reverse index order: app-03's is the oldest.
        newest = reg_mod._prewarm_budget_mark_path(rows[1]).stat().st_mtime
        for step, row in enumerate(rows[1:4], start=1):
            at = newest - 10 * step
            os.utime(reg_mod._prewarm_budget_mark_path(row), (at, at))
        started.clear()
        assert await _batch(reg, rows[1:4], workers, "batch 3") == 0
        assert started == ["app-03", "app-02", "app-01"]

        # No row hangs now, so no budget fires and the batch ends on its own.
        monkeypatch.setattr(reg_mod, "_PREWARM_BATCH_BUDGET", 3600.0)
        slow.discard("app-00")
        assert await _batch(reg, [rows[0]], workers, "batch 4") == 1
        assert reg_mod._read_prewarm_budget_mark(rows[0]) is None

    @pytest.mark.asyncio
    async def test_a_mark_orders_a_row_only_inside_its_own_art_group(self, monkeypatch):
        """A marked row whose art is missing is still cloned before every stale row,
        as cold rows are on a batch with no marks, and a marked stale row goes after
        the unmarked one. The stale row holds the older mark, so mark age alone
        cannot produce this order."""
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        monkeypatch.setattr(reg_mod, "_PREWARM_CONCURRENCY", 1)
        rows = _rows(4)
        calls = _fake_fetch(monkeypatch, ART_ONE, MANIFEST_ONE)
        assert await _prewarm_owner_tier_store_assets(reg, rows[:2]) == 2
        for row in rows[:2]:
            _expire_cache_file(_manifest_cache_path(row))
            assert _has_art(row)
        # Mark stale app-01 twenty seconds ago and cold app-03 ten seconds ago.
        for row, age in ((rows[1], 20), (rows[3], 10)):
            reg_mod._write_prewarm_budget_mark(row)
            path = reg_mod._prewarm_budget_mark_path(row)
            at = path.stat().st_mtime - age
            os.utime(path, (at, at))
        calls.clear()

        assert await _prewarm_owner_tier_store_assets(reg, rows) == 4

        assert _cloned(calls) == ["app-02", "app-03", "app-00", "app-01"]

    @pytest.mark.asyncio
    async def test_an_outer_cancel_marks_no_row(self, monkeypatch):
        """Only the batch budget marks a row: a prewarm cancelled from outside, as a
        gateway shutdown mid cold load does, leaves its in-flight row unmarked."""
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        monkeypatch.setattr(reg_mod, "_PREWARM_BATCH_BUDGET", 3600.0)
        row = _rows(1)[0]
        started = asyncio.Event()

        async def _fetch(entry, registry_name):
            started.set()
            await asyncio.Event().wait()
            return True

        monkeypatch.setattr(reg_mod, "_fetch_owner_tier_store_assets", _fetch)
        task = asyncio.ensure_future(_prewarm_owner_tier_store_assets(reg, [row]))
        await _await_barrier(started, "the in-flight row")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await _settled(task, "the cancelled prewarm")

        assert reg_mod._read_prewarm_budget_mark(row) is None

    def test_a_mark_older_than_its_ttl_reads_as_absent(self):
        row = _rows(1)[0]
        reg_mod._write_prewarm_budget_mark(row)
        assert reg_mod._read_prewarm_budget_mark(row) is not None
        path = reg_mod._prewarm_budget_mark_path(row)
        old = path.stat().st_mtime - reg_mod._PREWARM_BUDGET_MARK_TTL - 60
        os.utime(path, (old, old))
        assert reg_mod._read_prewarm_budget_mark(row) is None
