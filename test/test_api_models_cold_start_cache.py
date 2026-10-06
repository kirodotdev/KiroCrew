"""The model catalog fetch outlives the request that starts it.

On a host where a cold ``kiro-cli --list-models`` spawn always takes longer than
one request's bound, binding the spawn's lifetime to the request meant no call
ever finished: every call killed its spawn at the bound and cached nothing, so
the 8s self-heal poll started another doomed cold start and the picker stayed on
``auto`` forever (the reporter counted 153/153 timeouts, zero successes).

These tests pin the decoupling: the first poll still answers 503 at the request
bound, but the shared fetch keeps running under its own longer ceiling and warms
an in-memory cache, so the NEXT poll is served from it. A single-flight guard
means concurrent polls share one spawn, and a cached catalog is served straight
away. They fail on a head that kills the spawn at the request bound.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew.dashboard.handlers import agents
from kiro_crew.kiro_prerequisite import KiroPrerequisiteService


def _drain_catalog_task():
    # A degraded fetch that timed out at the request bound leaves its background
    # task running; cancel and await it before clearing the slot so it cannot
    # resume after this test's patches exit and run real sandbox / kiro-cli
    # resolution against a later test (no-test-side-effects).
    task = agents._catalog_cache.task
    if task is None or task.done():
        return
    task.cancel()
    try:
        asyncio.get_event_loop().run_until_complete(asyncio.gather(task, return_exceptions=True))
    except RuntimeError:
        # No usable loop (closed/none): the task is detached from any live loop,
        # so clearing the slot is enough — it has no loop to resume on.
        pass


@pytest.fixture(autouse=True)
def _reset_catalog_cache():
    _drain_catalog_task()
    agents._catalog_cache.models = None
    agents._catalog_cache.fetched_at = 0.0
    agents._catalog_cache.task = None
    yield
    _drain_catalog_task()
    agents._catalog_cache.models = None
    agents._catalog_cache.fetched_at = 0.0
    agents._catalog_cache.task = None


async def _no_audit(**kwargs: Any) -> None:
    del kwargs


def _stub_wrap_argv(argv: list[str], **kwargs: Any) -> tuple[list[str], None]:
    del kwargs
    return argv, None


def _kiro_request(tmp_path: Path):
    service = KiroPrerequisiteService(
        platform_name="linux",
        environ={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
        home=tmp_path,
        audit_writer=_no_audit,
        assume_ready=True,
    )
    request = MagicMock()
    # No live providers in app["state"]: _entitled_kiro_models fails open and
    # returns the catalog unchanged, so these tests pin the fetch/cache path only.
    request.app = {"kiro_prerequisite_service": service}
    return request


def _kiro_cfg() -> SimpleNamespace:
    return SimpleNamespace(agent=SimpleNamespace(provider="kiro"))


def _body(resp) -> object:
    return json.loads(resp.body)


#: Lost-run ceiling for the released fetch, not a race to tune. Once the gate is
#: set the shared fetch lands in milliseconds, and in about a second with every
#: executor job started late. 30 s is far past that and a quarter of the suite's
#: 120 s ``--timeout``, so a fetch that never lands fails here by name instead of
#: killing the Windows xdist worker.
_LOST_RUN_CEILING_SECS = 30.0


async def _await_landed(task: "asyncio.Task[list[dict]]") -> list[dict]:
    """Await the released shared fetch under the lost-run ceiling, by name."""
    loop = asyncio.get_running_loop()
    started = loop.time()
    try:
        return await asyncio.wait_for(task, _LOST_RUN_CEILING_SECS)
    except asyncio.TimeoutError:
        raise AssertionError(
            "the shared catalog fetch never landed after the gate opened "
            f"(waited {loop.time() - started:.1f}s)"
        ) from None


def _spawning(proc: "_GatedProc", spawned: list[int] | None = None):
    """A stand-in for the handler's own ``spawn_supervised_oneshot`` binding.

    The seam's contract: a coroutine function that returns the process. Patched on
    ``agents``, never on stdlib ``asyncio``, so no platform's supervisor, spawn-shim
    or no-shim branch runs and no other code in the process sees the stand-in.
    """

    async def _spawn(*a: Any, **k: Any) -> "_GatedProc":
        if spawned is not None:
            spawned.append(1)
        return proc

    return _spawn


class _GatedProc:
    """A spawn whose ``communicate()`` blocks until released.

    Models the cold start: the first request's bounded wait gives up before this
    resolves, but the shared fetch keeps awaiting it, so once released the result
    lands in the cache for the next poll.
    """

    def __init__(self, payload: bytes, gate: asyncio.Event):
        self._payload = payload
        self._gate = gate
        self.returncode = 0
        self.pid = 99_999_999_999

    def kill(self):  # noqa: D401
        pass

    async def communicate(self):
        await self._gate.wait()
        return self._payload, b""


def _base_patches():
    return [
        patch.object(agents.KiroCrewConfig, "load", return_value=_kiro_cfg()),
        patch("kiro_crew.acp.client._resolve_kiro_bin_for_spawn", return_value="/usr/bin/kiro-cli"),
        patch("kiro_crew.acp.client._resolve_ssh_auth_sock", lambda env: None),
        patch("kiro_crew.env.augmented_path", lambda p: p),
        patch("kiro_crew.dashboard.handlers.agents.wrap_argv", _stub_wrap_argv),
        patch("kiro_crew.dashboard.handlers.agents.cgroup_scope_argv", lambda argv: argv),
        patch("kiro_crew.sandbox.resource_limit_preexec", lambda: None),
    ]


def test_slow_cold_start_503s_once_then_serves_the_cache(tmp_path):
    """First poll 503s at the request bound; the fetch is NOT killed, so the
    second poll is served from the catalog it warmed."""
    payload = json.dumps({"models": [{"model_name": "claude-opus-4.8"}]}).encode()

    async def _drive():
        gate = asyncio.Event()
        proc = _GatedProc(payload, gate)
        ctxs = _base_patches()
        ctxs.append(patch.object(agents, "spawn_supervised_oneshot", _spawning(proc)))
        for c in ctxs:
            c.start()
        try:
            # The cold start has not finished: a very short request bound gives up.
            with patch.object(agents, "_LIST_MODELS_SUBPROCESS_TIMEOUT_SECS", 0.05):
                first = await agents.api_models(_kiro_request(tmp_path))
            assert first.status == 503
            assert _body(first) == {"error": "model list timed out"}

            # The shared fetch kept running. Release the spawn and let it land.
            task = agents._catalog_cache.task
            assert task is not None, "the fetch was killed with the request"
            gate.set()
            await _await_landed(task)

            # The next poll is served from the warmed cache — no new spawn needed.
            second = await agents.api_models(_kiro_request(tmp_path))
            assert second.status == 200
            assert [m["model_name"] for m in _body(second)] == ["claude-opus-4.8"]
        finally:
            for c in ctxs:
                c.stop()

    asyncio.run(_drive())


def test_concurrent_polls_share_one_spawn(tmp_path):
    """The self-heal loop fires every 8s while degraded; those polls must share
    one cold start, not each launch their own."""
    payload = json.dumps({"models": [{"model_name": "auto"}]}).encode()
    spawns: list[int] = []

    async def _drive():
        gate = asyncio.Event()
        proc = _GatedProc(payload, gate)
        ctxs = _base_patches()
        ctxs.append(patch.object(agents, "spawn_supervised_oneshot", _spawning(proc, spawns)))
        for c in ctxs:
            c.start()
        try:
            with patch.object(agents, "_LIST_MODELS_SUBPROCESS_TIMEOUT_SECS", 0.05):
                a, b, c2 = await asyncio.gather(
                    agents.api_models(_kiro_request(tmp_path)),
                    agents.api_models(_kiro_request(tmp_path)),
                    agents.api_models(_kiro_request(tmp_path)),
                )
            # The spawn is gated, so all three are the request-bound 503, not a
            # fetch that already failed, and the shared fetch is still in flight.
            assert [(r.status, _body(r)) for r in (a, b, c2)] == [
                (503, {"error": "model list timed out"})
            ] * 3
            task = agents._catalog_cache.task
            assert task is not None, "the shared fetch ended with the requests"
            gate.set()
            assert await _await_landed(task) == [{"model_name": "auto"}]
            # Three concurrent degraded polls, one spawn.
            assert len(spawns) == 1, spawns
        finally:
            for c in ctxs:
                c.stop()

    asyncio.run(_drive())


def test_fresh_cache_is_served_without_a_spawn(tmp_path):
    """A cached catalog younger than the TTL is served straight away."""
    agents._catalog_cache.models = [{"model_name": "claude-opus-4.8"}]
    agents._catalog_cache.fetched_at = agents.time.monotonic()

    def _boom(*a, **k):
        raise AssertionError("a catalog fetch was started despite a fresh cache")

    async def _drive():
        with (
            patch.object(agents.KiroCrewConfig, "load", return_value=_kiro_cfg()),
            patch(
                "kiro_crew.acp.client._resolve_kiro_bin_for_spawn", return_value="/usr/bin/kiro-cli"
            ),
            # The handler's own single-flight entry, which it calls synchronously:
            # any fetch started on a fresh cache, in the request or behind it,
            # raises here and turns the reply into a 503.
            patch.object(agents, "_shared_catalog_fetch", _boom),
        ):
            return await agents.api_models(_kiro_request(tmp_path))

    resp = asyncio.run(_drive())
    assert resp.status == 200
    assert [m["model_name"] for m in _body(resp)] == ["claude-opus-4.8"]


def test_bounded_catalog_caps_rows_fields_and_scalar_size():
    """The cache's retention bound: row count, fields per row, and scalar size.

    The retained list is the one place the first-party ``--list-models`` output is
    held past the request, so an oversized or malformed catalog must not grow the
    gateway's resident memory without limit (the ``_catalog_cache.models`` write).
    The bound reuses the repo's one shared admission (``model_registry``).
    """
    from kiro_crew import model_registry

    max_rows = model_registry.ADVERTISED_MODELS_MAX_IDS
    max_chars = model_registry.ADVERTISED_MODEL_ID_MAX_CHARS
    big = [
        {
            "model_name": "m%d" % i,
            "context_window_tokens": 1000,
            "huge": "x" * (max_chars + 500),
            "nested": {"drop": "me"},
            "arr": [1, 2, 3],
        }
        for i in range(max_rows + 50)
    ]
    bounded = agents._bounded_catalog(big)

    # Row count capped at the shared admission bound.
    assert len(bounded) == max_rows
    # Order preserved.
    assert bounded[0]["model_name"] == "m0"
    # Scalar fields kept and clamped; non-scalar fields dropped.
    row = bounded[0]
    assert row["context_window_tokens"] == 1000
    assert len(row["huge"]) == max_chars
    assert "nested" not in row and "arr" not in row


def test_bounded_catalog_refuses_over_long_id_rows():
    """An over-long identifying field is REFUSED (whole row skipped), not sliced.

    Slicing would serve an id no window / advertised store retained, so
    ``model_scope`` would read a native pin as foreign — the same ``REFUSED, not
    truncated`` invariant ``model_registry.admit_catalog_rows`` enforces.
    """
    from kiro_crew import model_registry

    max_chars = model_registry.ADVERTISED_MODEL_ID_MAX_CHARS
    rows = [
        {"model_name": "ok"},
        {"model_name": "x" * (max_chars + 1)},
        {"model_id": "y" * (max_chars + 1)},
    ]
    bounded = agents._bounded_catalog(rows)
    assert bounded == [{"model_name": "ok"}]


def test_bounded_catalog_skips_over_long_field_keys():
    """An over-long field KEY is a retained string too — skipped, not cached.

    Pins the ``a-bound-bounds-every-field-it-retains`` property for keys: a
    pathological JSON key must not enter the cache even when its value is small.
    """
    from kiro_crew import model_registry

    max_chars = model_registry.ADVERTISED_MODEL_ID_MAX_CHARS
    row = {"model_name": "ok", "x" * (max_chars + 1): "small"}
    bounded = agents._bounded_catalog([row])
    assert bounded == [{"model_name": "ok"}]


def test_bounded_catalog_caps_fields_per_row():
    """A row with pathologically many fields keeps at most the per-row cap."""
    row = {"f%d" % i: i for i in range(agents._CATALOG_CACHE_MAX_FIELDS_PER_ROW + 20)}
    bounded = agents._bounded_catalog([row])
    assert len(bounded[0]) == agents._CATALOG_CACHE_MAX_FIELDS_PER_ROW


def test_bounded_catalog_skips_non_dict_rows():
    """A malformed (non-dict) row is skipped, never fatal."""
    bounded = agents._bounded_catalog([{"model_name": "ok"}, "not-a-dict", 42, None])
    assert bounded == [{"model_name": "ok"}]
