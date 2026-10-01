"""Role-pin entitlement evidence is revalidated and read from the newest session.

``_validate_role_model`` (every ``agent.role_models.*`` / fallback /
decision-route pin, and a crew's model pin through ``_model_pin_rejected``)
judges a pin against a live session's ``session/new`` snapshot. These pin both
halves of trusting that snapshot: the newest session speaks for the account (a
stale broad pre-downgrade list is not trusted), and the awaited pre-step hands
that session's snapshot to the same read-path revalidation ``/api/models`` uses,
so a startup-race snapshot is healed before the synchronous validator reads it
-- while a probe-confirmed, recently-probed snapshot is not re-probed.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from kiro_crew.acp.runtime import AcpRuntime
from kiro_crew.acp.session_handle import AcpSessionHandle
from kiro_crew.agent_sdk.drivers.acp import EntitlementRevalidating
from kiro_crew.dashboard.handlers import core


def _rows(*ids: str) -> list[dict[str, str]]:
    return [{"modelId": m, "name": m, "description": ""} for m in ids]


class _HandleProvider:
    """The two methods the pin path reads, delegated exactly as
    ``AcpSessionProvider`` delegates them to its handle."""

    def __init__(self, handle: AcpSessionHandle) -> None:
        self._handle = handle

    def available_models(self) -> list[dict[str, str]]:
        return self._handle.available_models

    async def maybe_refresh_available_models(self, catalog_ids: list[str]) -> list[dict[str, str]]:
        return await self._handle.maybe_refresh_available_models(catalog_ids)


def _handle(advertised: list[str]) -> tuple[AcpRuntime, AcpSessionHandle]:
    rt = AcpRuntime(work_dir="/tmp")
    rt._spawn_monotonic = time.monotonic() - 3600.0  # long past the spawn race band
    queue: asyncio.Queue = asyncio.Queue()
    rt._session_queues["sR"] = queue
    handle = AcpSessionHandle("sR", queue, rt)
    handle._available_models = _rows(*advertised)
    handle._mark_available_models_captured()  # a session/new capture: unconfirmed
    return rt, handle


def _request(*providers: Any) -> Any:
    state = SimpleNamespace(sessions=SimpleNamespace(active_providers=lambda: list(providers)))
    return SimpleNamespace(app={"state": state})


@pytest.fixture(autouse=True)
def _no_display_only_rejection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_handlers._model_rejected_reason",
        lambda _v, provider=None: None,
    )


def test_newest_session_judges_the_pin_not_a_stale_broad_one() -> None:
    """A session started before a downgrade still advertises the lost model; the
    newest session's narrower list is the account's current answer."""
    older = SimpleNamespace(available_models=lambda: _rows("auto", "claude-opus-5"))
    newer = SimpleNamespace(available_models=lambda: _rows("auto", "claude-sonnet-5"))

    reason = core._validate_role_model("claude-opus-5", _request(older, newer))

    assert reason is not None and "not available" in reason
    assert core._active_advertised_ids(_request(older, newer)) == ["auto", "claude-sonnet-5"]


def test_newest_first_stays_scoped_to_the_pins_own_harness() -> None:
    """A member DM on another harness, created AFTER the default-harness session,
    is the newest live session but cannot judge a default-harness pin: its
    catalog would deterministically reject every kiro id."""
    kiro = SimpleNamespace(
        client=SimpleNamespace(backend=""),
        available_models=lambda: _rows("auto", "claude-opus-5"),
    )
    claude_member = SimpleNamespace(
        client=SimpleNamespace(backend="claude"),
        available_models=lambda: _rows("claude-opus-4-1", "claude-sonnet-4-5"),
    )
    request = _request(kiro, claude_member)

    # Scoped to the default harness (kiro), the kiro session is the evidence.
    assert core._validate_role_model("claude-opus-5", request, backend="") is None
    assert core._active_advertised_ids(request, backend="") == ["auto", "claude-opus-5"]
    # Unscoped, newest-first would read the member's catalog -- which is exactly
    # why the PATCH path resolves the default harness and passes it down.
    assert core._active_advertised_ids(request) == ["claude-opus-4-1", "claude-sonnet-4-5"]


def test_patch_scope_is_the_default_harness(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = SimpleNamespace(agent=SimpleNamespace(provider="kiro", acp_backend="kas"))
    monkeypatch.setattr("kiro_crew.config.loader.KiroCrewConfig.load", staticmethod(lambda: cfg))

    assert core._active_provider_and_pin_backend() == ("kiro", "kas")


@pytest.mark.asyncio
async def test_startup_race_snapshot_is_healed_before_the_pin_is_judged() -> None:
    rt, handle = _handle(["auto"])
    rt.probe_advertised_models = AsyncMock(  # type: ignore[method-assign]
        return_value=_rows("auto", "claude-sonnet-5", "claude-opus-5")
    )
    request = _request(_HandleProvider(handle))
    assert core._validate_role_model("claude-opus-5", request) is not None

    assert await core._revalidate_role_pin_evidence("claude-opus-5", request) is None

    rt.probe_advertised_models.assert_awaited_once()
    # The probe's floor is the snapshot's own capture time.
    assert rt.probe_advertised_models.await_args.kwargs["not_before"] > 0.0
    assert core._validate_role_model("claude-opus-5", request) is None


@pytest.mark.asyncio
async def test_a_narrow_non_auto_snapshot_is_still_revalidated() -> None:
    """The pin is judged beside the rows the snapshot serves, so a lone pin is not
    mistaken for the picker's namespace-mismatch fail-open and left unprobed."""
    rt, handle = _handle(["claude-sonnet-5"])
    rt.probe_advertised_models = AsyncMock(  # type: ignore[method-assign]
        return_value=_rows("claude-sonnet-5", "claude-opus-5")
    )
    request = _request(_HandleProvider(handle))

    assert await core._revalidate_role_pin_evidence("claude-opus-5", request) is None

    rt.probe_advertised_models.assert_awaited_once()
    assert core._validate_role_model("claude-opus-5", request) is None


@pytest.mark.asyncio
async def test_a_fresh_confirmed_snapshot_is_not_reprobed() -> None:
    rt, handle = _handle(["auto", "claude-sonnet-5"])
    handle._available_models_probe_confirmed = True
    handle._available_models_read_probe_at = time.monotonic()  # just probed
    rt.probe_advertised_models = AsyncMock(return_value=_rows("auto", "claude-opus-5"))  # type: ignore[method-assign]
    request = _request(_HandleProvider(handle))

    assert await core._revalidate_role_pin_evidence("claude-opus-5", request) is None

    rt.probe_advertised_models.assert_not_awaited()
    assert core._validate_role_model("claude-opus-5", request) is not None


@pytest.mark.asyncio
async def test_a_pin_the_snapshot_already_serves_costs_no_probe() -> None:
    rt, handle = _handle(["auto", "claude-opus-5"])
    rt.probe_advertised_models = AsyncMock(return_value=[])  # type: ignore[method-assign]

    assert (
        await core._revalidate_role_pin_evidence("claude-opus-5", _request(_HandleProvider(handle)))
        is None
    )
    rt.probe_advertised_models.assert_not_awaited()


@pytest.mark.asyncio
async def test_in_flight_revalidation_is_a_retryable_denial() -> None:
    """Past the read deadline the snapshot in hand would refuse and the fresh
    answer has not landed: deny with a retry, never accept on no evidence."""

    class _Pending:
        def available_models(self) -> list[dict[str, str]]:
            return _rows("auto")

        async def maybe_refresh_available_models(self, _ids: list[str]) -> list[dict[str, str]]:
            raise EntitlementRevalidating

    reason = await core._revalidate_role_pin_evidence("claude-opus-5", _request(_Pending()))

    assert reason == core._ROLE_PIN_REVALIDATING


@pytest.mark.asyncio
async def test_a_failed_revalidation_proceeds_on_the_snapshot() -> None:
    class _Broken:
        def available_models(self) -> list[dict[str, str]]:
            return _rows("auto")

        async def maybe_refresh_available_models(self, _ids: list[str]) -> list[dict[str, str]]:
            raise RuntimeError("boom")

    assert await core._revalidate_role_pin_evidence("claude-opus-5", _request(_Broken())) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["", "auto"])
async def test_inherit_values_never_revalidate(value: str) -> None:
    rt, handle = _handle(["auto"])
    rt.probe_advertised_models = AsyncMock(return_value=[])  # type: ignore[method-assign]

    assert (
        await core._revalidate_role_pin_evidence(value, _request(_HandleProvider(handle))) is None
    )
    rt.probe_advertised_models.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_newer_session_registered_during_the_await_is_reselected_and_refreshed() -> None:
    """A newer startup-race session that registers DURING the first refresh await
    becomes the newest evidence the synchronous validator will read, so it must be
    refreshed too -- the pin path reselects the newest provider after each await
    and refreshes until the newest is unchanged."""
    providers: list[Any] = []

    class _Prov:
        def __init__(self, name: str, *, spawns: Any = None) -> None:
            self.name = name
            self.refreshed = 0
            self._spawns = spawns  # a provider to append to the active list mid-await

        def available_models(self) -> list[dict[str, str]]:
            return _rows("auto")  # narrow -> triggers a probe

        async def maybe_refresh_available_models(self, _ids: list[str]) -> list[dict[str, str]]:
            self.refreshed += 1
            if self._spawns is not None:
                providers.append(self._spawns)  # a newer session appears during the await
                self._spawns = None
            return _rows("auto")

    newer = _Prov("newer")
    older = _Prov("older", spawns=newer)  # older is newest at first; registers `newer` mid-await
    providers.append(older)

    reason = await core._revalidate_role_pin_evidence(
        "claude-opus-5",
        SimpleNamespace(
            app={
                "state": SimpleNamespace(
                    sessions=SimpleNamespace(active_providers=lambda: list(providers))
                )
            }
        ),
    )

    assert reason is None
    # Both were refreshed: the one selected first, then the newer one that
    # displaced it during the await.
    assert older.refreshed == 1
    assert newer.refreshed == 1
