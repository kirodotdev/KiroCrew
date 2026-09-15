"""Regression coverage for conservative Linux gateway heap reclamation."""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest

from kiro_crew import platform_compat


def test_large_linux_heap_is_trimmed(monkeypatch) -> None:
    monkeypatch.setattr(platform_compat, "IS_LINUX", True)
    readings = iter([2 * 1024**3, 1200 * 1024**2])
    trims: list[bool] = []

    released = platform_compat.trim_heap_if_needed(
        rss_reader=lambda: next(readings),
        trimmer=lambda: trims.append(True) or True,
    )

    assert released == 848 * 1024**2
    assert trims == [True]


def test_small_heap_is_not_trimmed(monkeypatch) -> None:
    monkeypatch.setattr(platform_compat, "IS_LINUX", True)

    def forbidden() -> bool:
        raise AssertionError("trim must not run")

    assert (
        platform_compat.trim_heap_if_needed(
            rss_reader=lambda: 512 * 1024**2,
            trimmer=forbidden,
        )
        == 0
    )


def test_non_linux_heap_is_not_trimmed(monkeypatch) -> None:
    monkeypatch.setattr(platform_compat, "IS_LINUX", False)

    def forbidden() -> bool:
        raise AssertionError("trim must not run")

    assert (
        platform_compat.trim_heap_if_needed(
            rss_reader=lambda: 2 * 1024**3,
            trimmer=forbidden,
        )
        == 0
    )


def test_trim_failure_never_escapes(monkeypatch) -> None:
    monkeypatch.setattr(platform_compat, "IS_LINUX", True)
    assert (
        platform_compat.trim_heap_if_needed(
            rss_reader=lambda: 2 * 1024**3,
            trimmer=lambda: (_ for _ in ()).throw(OSError("unsupported")),
        )
        == 0
    )


def test_missing_current_rss_never_falls_back_to_peak(monkeypatch) -> None:
    monkeypatch.setattr(platform_compat, "IS_LINUX", True)
    monkeypatch.setattr(platform_compat, "_linux_current_rss_bytes", lambda: None)
    monkeypatch.setattr(
        platform_compat,
        "proc_rss_bytes",
        lambda: 4 * 1024**3,
    )
    trims: list[bool] = []

    released = platform_compat.trim_heap_if_needed(
        trimmer=lambda: trims.append(True) or True,
    )

    assert released == 0
    assert trims == []


def test_missing_post_trim_rss_never_inflates_release(monkeypatch) -> None:
    monkeypatch.setattr(platform_compat, "IS_LINUX", True)
    readings = iter([2 * 1024**3, None])

    assert (
        platform_compat.trim_heap_if_needed(
            rss_reader=lambda: next(readings),
            trimmer=lambda: True,
        )
        == 0
    )


@pytest.mark.asyncio
async def test_maintainer_owns_its_cadence(monkeypatch) -> None:
    monkeypatch.setattr(platform_compat, "IS_LINUX", True)
    now = [0.0]
    trims: list[bool] = []

    async def inline_to_thread(fn):
        return fn()

    monkeypatch.setattr(platform_compat.asyncio, "to_thread", inline_to_thread)
    maintainer = platform_compat.HeapTrimMaintainer(
        clock=lambda: now[0],
        trim=lambda: trims.append(True) or 123,
    )

    now[0] = platform_compat.HEAP_TRIM_INTERVAL_SECONDS - 1
    assert await maintainer.maybe_trim() == 0
    now[0] = platform_compat.HEAP_TRIM_INTERVAL_SECONDS
    assert await maintainer.maybe_trim() == 123
    assert await maintainer.maybe_trim() == 0
    now[0] = 2 * platform_compat.HEAP_TRIM_INTERVAL_SECONDS
    assert await maintainer.maybe_trim() == 123
    assert trims == [True, True]


@pytest.mark.asyncio
async def test_non_linux_maintainer_never_submits_worker(monkeypatch) -> None:
    monkeypatch.setattr(platform_compat, "IS_LINUX", False)

    async def forbidden_to_thread(_fn):
        raise AssertionError("non-Linux maintenance must not submit a worker")

    monkeypatch.setattr(platform_compat.asyncio, "to_thread", forbidden_to_thread)
    maintainer = platform_compat.HeapTrimMaintainer(clock=lambda: 999999.0)

    assert await maintainer.maybe_trim() == 0


@pytest.mark.asyncio
async def test_maintainer_bounds_executor_wait_and_does_not_resubmit(monkeypatch) -> None:
    monkeypatch.setattr(platform_compat, "IS_LINUX", True)
    now = [0.0]
    submissions: list[bool] = []

    async def stalled_to_thread(_fn):
        submissions.append(True)
        await asyncio.Event().wait()

    monkeypatch.setattr(platform_compat.asyncio, "to_thread", stalled_to_thread)
    monkeypatch.setattr(platform_compat, "HEAP_TRIM_TIMEOUT_SECONDS", 0.001)
    maintainer = platform_compat.HeapTrimMaintainer(clock=lambda: now[0])

    now[0] = platform_compat.HEAP_TRIM_INTERVAL_SECONDS
    assert await maintainer.maybe_trim() == 0
    now[0] = 2 * platform_compat.HEAP_TRIM_INTERVAL_SECONDS
    assert await maintainer.maybe_trim() == 0
    assert submissions == [True]


def test_gateway_trim_maintainer_is_created_after_socket_bind() -> None:
    source = Path(__file__).parents[1] / "src" / "kiro_crew" / "dashboard" / "server.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    start_site_line = next(
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_start_site"
    )
    maintainer_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "platform_compat"
        and node.func.attr == "HeapTrimMaintainer"
    ]
    assert len(maintainer_calls) == 1
    assert maintainer_calls[0].lineno > start_site_line
