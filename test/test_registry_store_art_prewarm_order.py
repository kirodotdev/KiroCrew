"""The owner-tier store-art prewarm spends its batch on cold rows before stale ones.

An explicit registry refresh expires every row's manifest cache before it prewarms
(``refresh_registries``), so every row reads as not warm afterwards, including rows
whose declared art is all in the blob cache already. Walked in plain index order,
each refresh would re-clone the same head of the index until the budget ran out, and
rows further down would never get their art. A row with art missing goes ahead of a
row that is only stale; stale rows still re-warm when the budget allows.
"""

from __future__ import annotations

import os
from typing import Any

import pytest
from registry_store_art_prewarm_helpers import MANIFEST, _entry, _fake_fetch, _pin_registry

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
        lands only two clones. The rest of the batch fails, standing in for the budget
        cutting it off, and the clone-failure backoff is held open so a failed row is
        retried on the next refresh exactly as a budget-cancelled one is. In plain index
        order every refresh restarts at the top, so the same two rows are cloned each
        time and four never get art. Cold-first, each refresh reaches the next cold
        rows, and three refreshes cover all six."""
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
