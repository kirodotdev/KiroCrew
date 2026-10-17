"""Shared fixtures and fakes for the warm-mint suites.

``test_connections_warm.py`` and its ``test_connections_warm_<topic>.py`` siblings import these
by name: the two autouse resets of the warm engine's module singletons, the isolated agents
dir and data home, the neutralized activation, and the runtime, claim and provider fakes.
``test_connections_warm.py`` re-exports every one of them, because other suites import them
from there.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.connections import warm
from kiro_crew.connections.mint import _mints
from kiro_crew.connections.registry import Provider


@pytest.fixture(autouse=True)
def _clean_mint_table():
    _mints.clear()
    yield
    _mints.clear()


@pytest.fixture(autouse=True)
def _clean_warm_runtime():
    """Restore the module-level ``_warm_mint`` singleton around every test.

    The engine is a module singleton, so a test that drives ``_ensure_locked`` all the way to
    a successful spawn leaves a live runtime, a matching spec digest and a sleeping reaper on
    it -- and ``monkeypatch`` cannot undo state it never patched. A later test then reads its
    neighbour's process, which is one leak wearing three faces: the digest fast path returns
    before the test's own factory is ever constructed (no ``CancelledError`` raised, no kill
    recorded), a stand-down kills the neighbour's runtime first and inflates the kill count,
    and ``_park_or_kill_locked`` cancels a reaper task whose loop has since closed, which is
    a ``RuntimeError: Event loop is closed``. Which face appears depends only on which tests
    share a worker, so it moves whenever the file gains or loses a test.

    The containers are copied, not aliased: a test that appends to ``_retiring`` in place
    would otherwise leave the mutation behind in the very object being restored.

    ``_lock`` is reset to a FRESH lock rather than restored, because an ``asyncio.Lock``
    binds to the loop that first has to wait on it: the uncontended fast path never calls
    ``_get_loop``, so today's tests pass, but the first CONTENDED acquire in a later test's
    loop raises ``RuntimeError: ... is bound to a different event loop``. A new lock per test
    cannot carry a dead loop into the next one.
    """
    tracked = (
        "_runtime",
        "_plan",
        "_digest",
        "_generation",
        "_retiring",
        "_sessions",
        "_activation_seq",
        "_reaper",
        "_supervision_armed",
        "_rearms_remaining",
        "_rearm_task",
    )
    saved: dict[str, Any] = {name: getattr(warm._warm_mint, name) for name in tracked}
    saved["_retiring"] = list(saved["_retiring"])
    saved["_sessions"] = dict(saved["_sessions"])
    warm._warm_mint._lock = asyncio.Lock()
    yield
    for task_name in ("_reaper", "_rearm_task"):
        task = getattr(warm._warm_mint, task_name)
        if task is None or task is saved[task_name] or task.done():
            continue
        try:
            # Cancelled HERE, while the loop that owns it is still this test's. Cancelling it
            # from a later test's loop is the "Event loop is closed" face above, not the cure.
            task.cancel()
        except RuntimeError:
            # The loop is already gone; dropping the reference below is what matters.
            pass
    for name, value in saved.items():
        setattr(warm._warm_mint, name, value)
    # Fresh again on the way out: a lock left bound to THIS test's loop is exactly what the
    # next test must not inherit.
    warm._warm_mint._lock = asyncio.Lock()


class _Runtime:
    """A stand-in for one kiro-cli process, with the liveness answer we choose."""

    def __init__(self, alive: bool | BaseException) -> None:
        self._alive = alive

    def is_alive(self) -> bool:
        if isinstance(self._alive, BaseException):
            raise self._alive
        return self._alive


def _provider(slug: str, url: str = "") -> Provider:
    return {  # type: ignore[typeddict-item]
        "slug": slug,
        "mcp_url": url or f"https://{slug}.example/mcp",
        "l0_expectations": {"dcr": True},
    }


@pytest.fixture
def _agents_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """An isolated agents directory, through the module's own override hook."""
    agents = tmp_path / "agents"
    agents.mkdir()
    monkeypatch.setattr(warm._agent, "KIRO_AGENTS_DIR", agents)
    monkeypatch.setattr(warm, "_log_warm_event", lambda *a, **k: None)
    return agents


@pytest.fixture
def _private_warm_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Keep generation roots out of both the checkout and the operator's data home."""
    home = tmp_path / "crew-home"
    monkeypatch.setattr(warm, "data_home", lambda: home)
    return home


def _mimic_spec(agents: Path, stem: str) -> tuple[Path, str]:
    """A user's own spec at a warm path that COPIES every generic default we fix.

    Everything the user actually authored -- description, mcpServers, tools -- is theirs;
    only the four stock marks and the name coincide with ours.
    """
    path = agents / f"{stem}.json"
    body = json.dumps(
        {
            "name": stem,
            "description": "my own scratch agent that happens to sit here",
            "model": "auto",
            "includeMcpJson": False,
            "prompt": "",
            "mcpServers": {"private": {"command": "my-server"}},
            "tools": ["@private"],
            "allowedTools": [],
        },
        indent=2,
    )
    path.write_text(body, encoding="utf-8")
    return path, body


def _ours_shaped_spec_text(stem: str, *, pad: int = 0) -> str:
    """A body the ownership judge accepts as ours: right name, marks and sentinel.

    ``pad`` grows the file through a key the judge does NOT inspect. Padding one of the four
    marks (``prompt`` is one) would make the body read as foreign on its SHAPE, so the
    oversized test would pass without the cap ever being consulted.
    """
    body = warm._warm_spec_body(stem, {}, "planted")
    if pad:
        body["pad"] = "x" * pad
    return json.dumps(body)


@pytest.fixture
def _stub_activation(monkeypatch: pytest.MonkeyPatch):
    """Neutralize the process, the audit log and the grant watcher."""

    async def _no_watcher(slug: str, mcp_url: str, token: str) -> None:
        return None

    async def _no_settle(activation: int, in_use: set[int]) -> None:
        return None

    monkeypatch.setattr(warm, "_mint_watcher", _no_watcher)
    monkeypatch.setattr(warm, "_log_warm_event", lambda *a, **k: None)
    monkeypatch.setattr(warm._warm_mint, "settle_activation", _no_settle)


def _result(
    providers: list[Provider],
    requests: list[dict[str, str]],
    *,
    generation: int = 7,
    activation: int = 3,
) -> warm._WarmMintResult:
    return warm._WarmMintResult(
        generation=generation, activation=activation, providers=providers, requests=requests
    )


def _returns(result: warm._WarmMintResult | None):
    async def _activate(*, slug: str = ""):
        return result

    return _activate


async def _claim(*slugs: str) -> dict[str, str]:
    """Claim helper for the tests that do not exercise the displaced-row half."""
    claims, displaced = await warm._claim_shared_mints(list(slugs))
    assert not displaced, "these tests claim into empty slots"
    return claims


def _parked_session(generation: int) -> warm._WarmSession:
    """An unsettled session, so the sweep holds it exactly as a live activation's would be."""
    return warm._WarmSession(
        generation=generation, handle=object(), expires_at=time.monotonic() + 600
    )


@contextlib.asynccontextmanager
async def _rearm_registry():
    """The in-flight re-arm registry, cleared on entry and SETTLED on exit.

    An ``async with`` helper rather than an ``@pytest_asyncio.fixture``, by this repo's
    convention: the pinned pytest-asyncio does not collect async generator fixtures, and the
    teardown here has to await.

    Awaiting is the point. Cancelling each task and then clearing the registry synchronously
    drops the only reference to a task that has not yet observed its own cancellation, so the
    loop closes on it, its ``finally`` never runs, and the leak is the exact one
    :func:`~kiro_crew.connections.warm._settle_invalidation_rearms` exists to close -- a test
    harness must not reproduce the defect its subject fixes.

    Delegated to that production function rather than re-implementing cancel-then-gather here,
    so the idiom lives in exactly one place. It is not circular: the function's own behaviour
    is pinned independently by ``test_shutdown_settles_an_inflight_invalidation_rearm``, so the
    fixture is not the only thing claiming it works. ``finally``, so a failing test still
    settles.
    """
    warm._invalidation_rearms.clear()
    try:
        yield warm._invalidation_rearms
    finally:
        await warm._settle_invalidation_rearms()


class _SessionHandle:
    """A backend session. Records that it exists, and whether it was terminated."""

    def __init__(self, created: list[Any]) -> None:
        self.destroyed = False
        created.append(self)

    async def destroy(self) -> None:
        self.destroyed = True

    def pop_pending_oauth_requests(self) -> list[dict[str, str]]:
        return []

    async def drain_init(self, **kwargs: Any) -> None:
        return None
