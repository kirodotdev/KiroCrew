"""Poll-driven kiro-cli spawn sites must not run the CLI while signed out.

``kiro-cli`` auto-launches an interactive browser login whenever a subcommand
runs unauthenticated (``--no-interactive`` does not suppress it and there is no
opt-out env var). ``/api/models`` is polled every 8s while the model list is
degraded and ``/api/sessions/usage`` every 30s, so an unauthenticated gateway
used to spawn a browser-opening CLI on every cycle — dozens of browser windows
and an unusable dashboard.

These tests pin the fix: both handlers consult the prerequisite readiness latch
BEFORE resolving or spawning the binary, and return the shared 503 instead.

The signed-out cases drive the REAL ``reject_if_kiro_unverified`` (never a
stubbed guard) and pin binary resolution to a fixed path, so a deleted or
relocated gate must reach ``create_subprocess_exec`` — on a CI runner with no
kiro-cli installed as much as on a developer machine that has one.

Ordinary sends are UNGATED — the ACP attempt reports auth failures itself and
they mutate nothing up front. These two sites (and the destructive reruns, which
rewrite persisted history before their turn) keep failing closed because neither
can use the ACP attempt as its authority.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from chat_test_helpers import _make_ready_kiro_prerequisite

from kiro_crew.dashboard.handlers import agents, sessions
from kiro_crew.kiro_prerequisite import KiroPrerequisiteService

_RESOLVE_TARGET = "kiro_crew.acp.client._resolve_kiro_bin_for_spawn"
_FAKE_KIRO_BIN = "/usr/bin/kiro-cli"


class _SignedOutKiroPrerequisiteService(KiroPrerequisiteService):
    """Not-ready latch: the guard's ``isinstance`` check must still accept it."""

    async def session_ready(self) -> bool:
        return False

    # The gate authorizes on a fresh probe (`verified_ready`), never the bare
    # latch — a stale ready=True would otherwise green-light a signed-out spawn.
    async def verified_ready(self, *, max_age_secs: float) -> bool:
        del max_age_secs
        return False


def _make_signed_out_kiro_prerequisite() -> KiroPrerequisiteService:
    """Return a filesystem-free NOT-ready prerequisite service."""

    return object.__new__(_SignedOutKiroPrerequisiteService)


def _request(service: KiroPrerequisiteService) -> MagicMock:
    """A request whose app carries *service* as the prerequisite latch.

    ``reject_if_kiro_unverified`` reads ``app["kiro_prerequisite_service"]`` and
    falls back to ``app["state"].kiro_prerequisite_service``; both are wired so
    the real guard runs either way. ``state`` also carries the background-task
    set ``api_sessions_usage`` uses, so a removed gate reaches the scheduling
    line instead of dying on an unrelated AttributeError.
    """

    tasks: set[object] = set()
    app: dict[str, object] = {
        "kiro_prerequisite_service": service,
        "state": SimpleNamespace(
            kiro_prerequisite_service=service,
            _background_tasks=tasks,
        ),
    }
    request = MagicMock()
    request.app = app
    return request


@pytest.mark.asyncio
async def test_api_models_does_not_spawn_while_signed_out() -> None:
    request = _request(_make_signed_out_kiro_prerequisite())
    with patch(_RESOLVE_TARGET, AsyncMock(return_value=_FAKE_KIRO_BIN)) as resolve:
        with patch("asyncio.create_subprocess_exec", AsyncMock()) as spawn:
            resp = await agents.api_models(request)

    # The gate must run BEFORE resolution, not merely before the spawn.
    resolve.assert_not_called()
    # ``create_task``-style call sites make the coroutine without awaiting it,
    # so only ``assert_not_called`` proves the spawn was never reached.
    spawn.assert_not_called()
    assert resp.status == 503
    assert json.loads(resp.body)["code"] == "kiro_prerequisite_required"


@pytest.mark.asyncio
async def test_api_sessions_usage_does_not_schedule_fetch_while_signed_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Force the refresh branch live so a removed gate really schedules a fetch.
    monkeypatch.setattr(sessions, "_usage_cache_ts", 0.0)
    request = _request(_make_signed_out_kiro_prerequisite())
    with patch.object(sessions, "_fetch_usage_bg", AsyncMock()) as fetch:
        resp = await sessions.api_sessions_usage(request)

    # The handler schedules the fetch with ``asyncio.create_task``, so the
    # coroutine is CALLED but never AWAITED — ``assert_not_awaited`` would pass
    # even if the gate were moved below the scheduling line.
    fetch.assert_not_called()
    assert resp.status == 503
    assert json.loads(resp.body)["code"] == "kiro_prerequisite_required"


@pytest.mark.asyncio
async def test_api_models_still_reaches_spawn_path_when_ready() -> None:
    """The gate must be a pure add — a ready gateway keeps its existing behavior."""
    request = _request(_make_ready_kiro_prerequisite())
    with patch(_RESOLVE_TARGET, AsyncMock(return_value="")) as resolve:
        resp = await agents.api_models(request)

    # The handler got past the gate and into the pre-existing degraded branch:
    # binary unresolved, whose 503 body differs from the gate's.
    resolve.assert_awaited_once()
    assert resp.status == 503
    assert json.loads(resp.body) == {"error": "kiro binary not resolved"}
