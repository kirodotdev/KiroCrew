"""Direct-spawn entitlement revalidation on ``AcpClient``.

The shared-runtime path refuses an explicit pick only after a fresh backend
answer agrees (``AcpSessionProvider.set_model``) and revalidates the spawn-time
pin withhold. A direct ``AcpClient`` (one kiro-cli process per session) does the
same against its own ``session/new`` snapshot -- one unconfirmed answer a
startup race can leave at the free-tier default. These pin the direct-client
counterpart: refresh-before-refuse, refresh-before-withhold, the freshness rule
(a probe-confirmed snapshot inside the probe TTL is not re-probed, an older one
is), single-flight, and the probe's isolation on a dedicated transport of its own.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp.client import AcpClient, AcpModelUnavailable
from kiro_crew.acp.runtime import _ENTITLEMENT_PROBE_TTL_SECS
from kiro_crew.acp.types import JsonRpcMessage


def _rows(*ids: str) -> list[dict[str, str]]:
    return [{"modelId": m, "name": m, "description": ""} for m in ids]


def _kiro_client(advertised: list[str], model: str = "auto") -> AcpClient:
    client = AcpClient()
    client._session_id = "sess-own"
    client._model = model
    client._acp_backend = ""  # kiro-cli
    client._available_models = _rows(*advertised)
    # A session/new capture: unconfirmed, stamped now.
    client._available_models_captured_at = time.monotonic()
    client._available_models_probe_confirmed = False
    return client


def _record(sink: list) -> Any:
    async def _send_request(method: str, params: dict | None = None) -> int:
        sink.append((method, params or {}))
        return len(sink)

    return _send_request


# ── set_model: refresh-before-refuse ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_stale_narrow_snapshot_is_revalidated_before_refusing_a_pick():
    """An Auto-only startup-race snapshot must not refuse a model the account has."""
    client = _kiro_client(["auto"])
    client._probe_advertised_models = AsyncMock(  # type: ignore[method-assign]
        return_value=(_rows("auto", "claude-opus-5"), time.monotonic())
    )
    sent: list = []
    client._send_request = _record(sent)  # type: ignore[method-assign]

    await client.set_model("claude-opus-5")

    client._probe_advertised_models.assert_awaited_once()
    assert [m for m, _ in sent] == ["session/set_model"]
    assert sent[0][1]["modelId"] == "claude-opus-5"
    # The snapshot is healed in place and marked confirmed, so every later reader
    # (startup withhold, picker) sees the fresh answer.
    assert client._advertised_model_ids() == ["auto", "claude-opus-5"]
    assert client._available_models_probe_confirmed is True


@pytest.mark.asyncio
async def test_downgraded_fresh_answer_replaces_the_stale_broad_snapshot():
    """After a downgrade the refusal is judged by the FRESH answer, and the stale
    broader snapshot is not kept around to be trusted by a later read."""
    client = _kiro_client(["auto", "claude-sonnet-5"])
    client._probe_advertised_models = AsyncMock(  # type: ignore[method-assign]
        return_value=(_rows("auto"), time.monotonic())
    )
    sent: list = []
    client._send_request = _record(sent)  # type: ignore[method-assign]

    with pytest.raises(AcpModelUnavailable) as excinfo:
        await client.set_model("claude-opus-5")

    assert sent == []
    assert excinfo.value.advertised == ["auto"]
    assert client._advertised_model_ids() == ["auto"]
    # The next pick of the model the account lost is refused on the fresh list.
    assert client._model_is_unusable("claude-sonnet-5") is True


@pytest.mark.asyncio
async def test_fresh_probe_confirmed_snapshot_is_not_reprobed():
    """A snapshot a probe confirmed inside the probe TTL is fresh evidence."""
    client = _kiro_client(["auto", "claude-sonnet-5"])
    client._available_models_probe_confirmed = True
    client._available_models_captured_at = time.monotonic()
    client._probe_advertised_models = AsyncMock(  # type: ignore[method-assign]
        return_value=(_rows("auto", "claude-opus-5"), time.monotonic())
    )
    client._send_request = _record([])  # type: ignore[method-assign]

    with pytest.raises(AcpModelUnavailable):
        await client.set_model("claude-opus-5")

    client._probe_advertised_models.assert_not_awaited()


@pytest.mark.asyncio
async def test_confirmed_snapshot_older_than_the_ttl_is_reprobed():
    """The freshness floor is the snapshot's own capture time: past the TTL a
    confirmed snapshot is not evidence and the pick earns a fresh probe."""
    client = _kiro_client(["auto"])
    client._available_models_probe_confirmed = True
    client._available_models_captured_at = time.monotonic() - _ENTITLEMENT_PROBE_TTL_SECS - 1.0
    client._probe_advertised_models = AsyncMock(  # type: ignore[method-assign]
        return_value=(_rows("auto", "claude-opus-5"), time.monotonic())
    )
    client._send_request = _record([])  # type: ignore[method-assign]

    await client.set_model("claude-opus-5")

    client._probe_advertised_models.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_probe_keeps_the_snapshot_verdict():
    """No evidence never grants entitlement: a failed probe leaves the refusal."""
    client = _kiro_client(["auto"])
    client._probe_advertised_models = AsyncMock(return_value=([], 0.0))  # type: ignore[method-assign]
    sent: list = []
    client._send_request = _record(sent)  # type: ignore[method-assign]

    with pytest.raises(AcpModelUnavailable) as excinfo:
        await client.set_model("claude-opus-5")

    assert sent == []
    assert excinfo.value.advertised == ["auto"]
    assert client._available_models_probe_confirmed is False


# ── startup: refresh-before-withhold ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_startup_pin_is_revalidated_before_it_is_withheld():
    """The spawn-time withhold revalidates once, like the shared runtime's."""
    client = _kiro_client(["auto"], model="claude-opus-5")
    client._probe_advertised_models = AsyncMock(  # type: ignore[method-assign]
        return_value=(_rows("auto", "claude-opus-5"), time.monotonic())
    )
    sent: list = []
    client._send_request = _record(sent)  # type: ignore[method-assign]

    await client._apply_startup_model()

    client._probe_advertised_models.assert_awaited_once()
    assert client._model == "claude-opus-5"
    assert ("session/set_model", {"sessionId": "sess-own", "modelId": "claude-opus-5"}) in sent


# ── the probe itself ─────────────────────────────────────────────────────────


def _fake_probe_client(real: AcpClient, sent: list, *, answer: dict) -> AcpClient:
    """The REAL factory's probe client with only its process I/O stubbed.

    Its ``session/new`` wait fills the probe's OWN buffer with what a probe kiro-cli
    emits when the shared gateway is off or the agent is project-declared: ownerless
    MCP OAuth / init frames for the private servers it started, a substitution
    advisory, and a session update naming the probe session.
    """
    probe = real._entitlement_probe_client()
    probe._spawn = AsyncMock()  # type: ignore[method-assign]
    probe._session_work_dir = AsyncMock(return_value="/work")  # type: ignore[method-assign]
    probe._pooled_mcp_servers = lambda: []  # type: ignore[method-assign]
    probe.shutdown = AsyncMock()  # type: ignore[method-assign]
    probe._send_request = _record(sent)  # type: ignore[method-assign]

    async def _wait(req_id: int, timeout: float = 0.0, **_kw: Any) -> dict:
        if sent[req_id - 1][0] == "session/new":
            probe._mcp_notifications.extend(
                [
                    JsonRpcMessage(
                        method="_kiro.dev/mcp/oauth_request",
                        params={"serverName": "github", "oauthUrl": "https://example.invalid/a"},
                    ),
                    JsonRpcMessage(
                        method="_kiro.dev/mcp/server_initialized",
                        params={"serverName": "github"},
                    ),
                    JsonRpcMessage(method="session/update", params={"sessionId": "probe-1"}),
                ]
            )
            probe._last_substitution_model = "probe-advisory"
            return answer
        return {}

    probe._wait_for_response = _wait  # type: ignore[method-assign]
    return probe


@pytest.mark.asyncio
async def test_probe_runs_on_its_own_transport_and_nothing_it_emits_reaches_the_session():
    """The probe is a separate process: its ownerless MCP OAuth prompt (a private
    server the probe kiro-cli started) can never be published by, or poison the
    OAuth dedupe of, the real session -- whose own later sign-in prompt for the
    same server must still surface."""
    client = _kiro_client(["auto"])
    client._process = MagicMock()
    real_sent: list = []
    client._send_request = _record(real_sent)  # type: ignore[method-assign]
    # Stubbed so a probe that wrongly ran on THIS client fails fast on the
    # assertions below instead of driving a real process.
    client._spawn = AsyncMock()  # type: ignore[method-assign]
    client._wait_for_response = AsyncMock(return_value={})  # type: ignore[method-assign]
    client.shutdown = AsyncMock()  # type: ignore[method-assign]
    before = JsonRpcMessage(method="_kiro.dev/mcp/server_initialized", params={})
    client._mcp_notifications = [before]
    client._last_substitution_model = "before-probe"
    probe_sent: list = []
    probe = _fake_probe_client(
        client,
        probe_sent,
        answer={"sessionId": "probe-1", "models": {"availableModels": _rows("auto", "x")}},
    )
    client._entitlement_probe_client = lambda: probe  # type: ignore[method-assign]

    fresh, answered_at = await client._probe_advertised_models()

    assert [m["modelId"] for m in fresh] == ["auto", "x"]
    assert answered_at > 0.0
    # The whole handshake ran on the probe's own process, introduced exactly as
    # this session was, and was torn down.
    assert [m for m, _ in probe_sent] == [
        "initialize",
        "session/new",
        "_kiro.dev/session/terminate",
    ]
    assert probe_sent[0][1] == client._initialize_params()
    assert probe_sent[2][1] == {"sessionId": "probe-1"}
    probe.shutdown.assert_awaited_once()
    # Nothing touched this session's stream, buffer, advisory slot or dedupe.
    assert real_sent == []
    client._spawn.assert_not_awaited()
    client.shutdown.assert_not_awaited()
    assert client._mcp_notifications == [before]
    assert client._last_substitution_model == "before-probe"
    assert client._oauth_emitted_servers == set()
    assert client._pending_oauth_requests == []


def test_probe_client_is_launched_from_this_sessions_own_inputs():
    """No second spelling of the spawn: the probe is the ordinary constructor fed
    this client's launch inputs, with its own buffer and process slot."""
    client = AcpClient(
        work_dir="/work/proj",
        agent="my-agent",
        sandbox_mode="off",
        session_key="dashboard:chat-1",
        channel_id="chan-1",
        extra_env={"K": "V"},
        acp_backend="",
        mcp_gateway_overlay="/overlay",
        mcp_gateway_socket="/sock",
    )
    client._mcp_notifications.append(JsonRpcMessage(method="x", params={}))

    probe = client._entitlement_probe_client()

    assert probe is not client
    assert probe._work_dir == client._work_dir
    assert probe._agent == "my-agent"
    assert probe._sandbox_mode == "off"
    assert probe._session_key == "dashboard:chat-1"
    assert probe._channel_id == "chan-1"
    assert probe._extra_env == {"K": "V"} and probe._extra_env is not client._extra_env
    assert probe.backend == client.backend
    assert probe._mcp_gateway_overlay == "/overlay"
    assert probe._mcp_gateway_socket == "/sock"
    assert probe._process is None
    assert (
        probe._mcp_notifications == [] and probe._mcp_notifications is not client._mcp_notifications
    )
    assert probe._audit_source is None


@pytest.mark.asyncio
async def test_a_failed_probe_process_is_still_shut_down():
    client = _kiro_client(["auto"])
    probe = client._entitlement_probe_client()
    probe._spawn = AsyncMock(side_effect=OSError("no binary"))  # type: ignore[method-assign]
    probe.shutdown = AsyncMock()  # type: ignore[method-assign]
    client._entitlement_probe_client = lambda: probe  # type: ignore[method-assign]

    assert await client._probe_advertised_models() == ([], 0.0)
    probe.shutdown.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_spec_revoked_during_probe_init_aborts_before_session_new(monkeypatch):
    """The verify->create bracket every spawn path closes: a derived spec revoked
    between the probe's initialize and its session/new must abort, so no session
    (and none of that spec's MCP server commands) is ever created on an
    unverified spec. The stale-spec raise is absorbed as a failed probe."""
    from kiro_crew.agent_materialization.worker_agent import DerivedSpecStale

    client = _kiro_client(["auto"])
    sent: list = []
    probe = _fake_probe_client(client, sent, answer={"sessionId": "probe-1", "models": {}})
    client._entitlement_probe_client = lambda: probe  # type: ignore[method-assign]

    def _revoked(_snapshot: Any) -> None:
        raise DerivedSpecStale("worker spec revoked during probe init")

    monkeypatch.setattr("kiro_crew.acp.client.require_unchanged_derived_spec", _revoked)

    assert await client._probe_advertised_models() == ([], 0.0)
    # initialize happened; session/new never did.
    assert [m for m, _ in sent] == ["initialize"]
    probe.shutdown.assert_awaited_once()


@pytest.mark.asyncio
async def test_concurrent_refreshes_share_one_probe():
    """Single-flight is owned by the method: a picker poll and an explicit pick
    overlapping start ONE probe process, and both read its answer."""
    import asyncio

    client = _kiro_client(["auto"])
    gate = asyncio.Event()
    calls = 0

    async def _probe() -> tuple[list[dict[str, str]], float]:
        nonlocal calls
        calls += 1
        await gate.wait()
        return _rows("auto", "claude-opus-5"), time.monotonic()

    client._probe_advertised_models = _probe  # type: ignore[method-assign]

    first = asyncio.ensure_future(client.refresh_available_models())
    second = asyncio.ensure_future(client.refresh_available_models())
    await asyncio.sleep(0.01)
    gate.set()
    a, b = await asyncio.gather(first, second)

    assert calls == 1
    assert a == b == _rows("auto", "claude-opus-5")


@pytest.mark.asyncio
async def test_a_cancelled_caller_does_not_take_the_probe_from_the_others():
    import asyncio

    client = _kiro_client(["auto"])
    gate = asyncio.Event()
    calls = 0

    async def _probe() -> tuple[list[dict[str, str]], float]:
        nonlocal calls
        calls += 1
        await gate.wait()
        return _rows("auto", "claude-opus-5"), time.monotonic()

    client._probe_advertised_models = _probe  # type: ignore[method-assign]

    first = asyncio.ensure_future(client.refresh_available_models())
    second = asyncio.ensure_future(client.refresh_available_models())
    await asyncio.sleep(0.01)
    first.cancel()
    gate.set()
    assert await second == _rows("auto", "claude-opus-5")
    assert calls == 1
    assert client._available_models_probe_confirmed is True


@pytest.mark.asyncio
async def test_probe_without_a_live_session_is_no_evidence():
    client = _kiro_client(["auto"])
    client._session_id = None
    factory = MagicMock()
    client._entitlement_probe_client = factory  # type: ignore[method-assign]

    assert await client._probe_advertised_models() == ([], 0.0)
    factory.assert_not_called()


# ── the picker read path on the dedicated transport ──────────────────────────


def _dedicated_provider(client: AcpClient):
    """An ``AcpProvider`` whose inner client is still a plain kiro ``AcpClient``
    (the dedicated transport never swaps it for the shared-runtime handle)."""
    from kiro_crew.providers.acp import AcpProvider

    provider = AcpProvider(acp_backend="")
    provider._client = client
    return provider


@pytest.mark.asyncio
async def test_dedicated_transport_picker_read_is_healed_by_the_probe():
    """`/api/models` narrowing through a dedicated-spawn session's Auto-only
    startup-race snapshot is re-asked of the backend before it hides anything."""
    client = _kiro_client(["auto"])
    client._probe_advertised_models = AsyncMock(  # type: ignore[method-assign]
        return_value=(_rows("auto", "claude-opus-5"), time.monotonic())
    )
    provider = _dedicated_provider(client)

    rows = await provider.maybe_refresh_available_models(["auto", "claude-opus-5"])

    client._probe_advertised_models.assert_awaited_once()
    assert [m["modelId"] for m in rows] == ["auto", "claude-opus-5"]
    # Healed in place: the next read serves the confirmed list without a probe.
    assert provider.available_models() == _rows("auto", "claude-opus-5")
    assert client._available_models_probe_confirmed is True


@pytest.mark.asyncio
async def test_dedicated_transport_read_that_drops_nothing_costs_no_probe():
    """The picker's own per-row verdict decides whether a probe is worth it."""
    client = _kiro_client(["auto", "claude-opus-5"])
    client._probe_advertised_models = AsyncMock()  # type: ignore[method-assign]
    provider = _dedicated_provider(client)

    rows = await provider.maybe_refresh_available_models(["auto", "claude-opus-5"])

    client._probe_advertised_models.assert_not_awaited()
    assert rows == _rows("auto", "claude-opus-5")


@pytest.mark.asyncio
async def test_dedicated_transport_fresh_confirmed_list_is_not_reprobed():
    client = _kiro_client(["auto"])
    client._available_models_probe_confirmed = True
    client._available_models_captured_at = time.monotonic()
    client._probe_advertised_models = AsyncMock()  # type: ignore[method-assign]
    provider = _dedicated_provider(client)

    rows = await provider.maybe_refresh_available_models(["auto", "claude-opus-5"])

    client._probe_advertised_models.assert_not_awaited()
    assert rows == _rows("auto")


@pytest.mark.asyncio
async def test_dedicated_transport_failed_probe_keeps_the_snapshot():
    client = _kiro_client(["auto"])
    client._probe_advertised_models = AsyncMock(return_value=([], 0.0))  # type: ignore[method-assign]
    provider = _dedicated_provider(client)

    assert await provider.maybe_refresh_available_models(["auto", "x"]) == _rows("auto")


@pytest.mark.asyncio
async def test_dedicated_transport_read_during_a_turn_still_probes():
    """The probe has its own process, so a streaming turn on this session's
    stream is no reason to keep a narrowed picker: nothing is raced."""
    client = _kiro_client(["auto"])
    client._probe_advertised_models = AsyncMock(  # type: ignore[method-assign]
        return_value=(_rows("auto", "claude-opus-5"), time.monotonic())
    )
    client.has_active_turn = lambda: True  # type: ignore[method-assign]
    provider = _dedicated_provider(client)

    rows = await provider.maybe_refresh_available_models(["auto", "claude-opus-5"])

    client._probe_advertised_models.assert_awaited_once()
    assert rows == _rows("auto", "claude-opus-5")


@pytest.mark.asyncio
async def test_dedicated_transport_confirmed_narrow_list_honours_the_interval():
    """A confirmed narrow list past the probe TTL is not re-probed on every poll."""
    client = _kiro_client(["auto"])
    client._available_models_probe_confirmed = True
    client._available_models_captured_at = time.monotonic() - _ENTITLEMENT_PROBE_TTL_SECS - 1.0
    client._probe_advertised_models = AsyncMock(  # type: ignore[method-assign]
        return_value=(_rows("auto"), time.monotonic())
    )
    provider = _dedicated_provider(client)

    await provider.maybe_refresh_available_models(["auto", "claude-opus-5"])
    client._available_models_captured_at = time.monotonic() - _ENTITLEMENT_PROBE_TTL_SECS - 1.0
    await provider.maybe_refresh_available_models(["auto", "claude-opus-5"])

    client._probe_advertised_models.assert_awaited_once()


@pytest.mark.asyncio
async def test_dedicated_transport_interval_is_bypassed_while_a_probe_is_in_flight():
    """The interval short-circuit must not serve the stale snapshot as a live
    answer while a probe is already refreshing it: with the interval otherwise
    satisfied but a probe in flight, the read falls through and awaits the probe,
    so the corrected list it is fetching reaches the picker."""
    import asyncio

    client = _kiro_client(["auto"])
    client._available_models_probe_confirmed = True
    # A pending in-flight probe on the client.
    inflight: asyncio.Future = asyncio.get_event_loop().create_future()
    client._entitlement_probe_inflight = inflight  # type: ignore[attr-defined]
    client.refresh_available_models = AsyncMock(  # type: ignore[method-assign]
        return_value=_rows("auto", "claude-opus-5")
    )
    provider = _dedicated_provider(client)
    # Interval would otherwise short-circuit: a recent picker probe.
    provider._picker_probe_at = time.monotonic()

    rows = await provider.maybe_refresh_available_models(["auto", "claude-opus-5"])

    # Fell through to the refresh rather than returning the stale ["auto"] snapshot.
    client.refresh_available_models.assert_awaited_once()
    assert [m["modelId"] for m in rows] == ["auto", "claude-opus-5"]


@pytest.mark.asyncio
async def test_dedicated_transport_picker_read_deadline_raises_revalidating(monkeypatch):
    """A slow probe must not hold the picker (or a pin-save) for its full
    init+session timeout: past the shielded read deadline the read raises
    EntitlementRevalidating (the endpoint serves its degraded response and the
    frontend keeps its last-good list and polls again) while the probe keeps
    running."""
    import asyncio

    from kiro_crew.acp.session_handle import EntitlementRevalidating

    # Collapse the deadline so the test does not actually wait 3s.
    monkeypatch.setattr("kiro_crew.providers.acp._READ_PATH_PROBE_DEADLINE_SECS", 0.01)

    client = _kiro_client(["auto"])
    started = asyncio.Event()

    async def _hang() -> tuple[list[dict[str, str]], float]:
        started.set()
        await asyncio.sleep(3600)  # never lands inside the deadline
        return _rows("auto", "claude-opus-5"), time.monotonic()

    client.refresh_available_models = _hang  # type: ignore[method-assign]
    provider = _dedicated_provider(client)

    with pytest.raises(EntitlementRevalidating):
        await provider.maybe_refresh_available_models(["auto", "claude-opus-5"])
    assert started.is_set()


@pytest.mark.asyncio
async def test_dedicated_transport_unconfirmed_narrow_snapshot_is_rate_limited():
    """An UNCONFIRMED narrow snapshot (a failed probe never sets the confirmed
    flag) must still be rate-limited by `_picker_probe_at`, or every poll would
    cold-spawn another probe process forever. A recent probe short-circuits the
    next poll even though the snapshot is auto-only and unconfirmed."""
    client = _kiro_client(["auto"])
    client._available_models_probe_confirmed = False  # failed probe never confirmed
    probe = AsyncMock(return_value=([], 0.0))  # keeps failing (no evidence)
    client._probe_advertised_models = probe  # type: ignore[method-assign]
    provider = _dedicated_provider(client)

    # First poll probes (and fails); records _picker_probe_at.
    await provider.maybe_refresh_available_models(["auto", "claude-opus-5"])
    # Second poll within the interval must NOT probe again.
    await provider.maybe_refresh_available_models(["auto", "claude-opus-5"])

    probe.assert_awaited_once()


@pytest.mark.asyncio
async def test_shutdown_tears_down_an_in_flight_entitlement_probe():
    """The single-flight probe spawned its own kiro-cli; shutting the client down
    must cancel that in-flight probe so its process dies with the client rather
    than outliving it."""
    import asyncio

    client = _kiro_client(["auto"])
    client._kill_process = AsyncMock()  # type: ignore[method-assign]
    client._discard_bound_workspace = AsyncMock()  # type: ignore[method-assign]
    client._discard_claude_settings_seed = AsyncMock()  # type: ignore[method-assign]
    client._reset_state = MagicMock()  # type: ignore[method-assign]

    cancelled = asyncio.Event()

    async def _never_lands() -> tuple[list[dict[str, str]], float]:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return [], 0.0

    inflight = asyncio.ensure_future(_never_lands())
    client._entitlement_probe_inflight = inflight  # type: ignore[attr-defined]
    await asyncio.sleep(0.01)  # let it start

    await client.shutdown()

    assert inflight.cancelled() or cancelled.is_set()
