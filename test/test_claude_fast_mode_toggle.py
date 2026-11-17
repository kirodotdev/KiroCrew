"""The claude harness's ``fast`` switch (Fast mode), read and written per session.

claude-agent-acp advertises ``fast`` in ``configOptions`` only while the model supports
Fast mode, as an ``on``/``off`` select to a client that declares no boolean config
options. Crew surfaces it only for the claude harness, through the path the
``ultracode`` switch takes: the session reports it through ``selection-capabilities``
while the option is advertised, and ``POST /fast`` writes it with
``session/set_config_option`` and answers the value the adapter's reply carries.

``fast-mode.jsonl`` is written from the adapter's source (its header cites the lines): a
``session/new`` result advertising the switch off, the reply to a write turning it on,
then a turn whose result the SDK reported Fast mode off with a reason, which the adapter
sends as a ``Fast mode turned off`` notice and then a ``config_option_update`` whose
option folds the reason into its description. That description is the only sign that the
switch cannot be turned on, so ``selection-capabilities`` carries the reason as
``fast_unavailable`` and the dashboard draws the switch disabled with it.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from acp_frame_replay_harness import CORPUS, read_fixture
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.acp.client import AcpClient, AcpError
from kiro_crew.acp.client_native_children import client_capabilities_for
from kiro_crew.acp.harness_notices import NOTICE_KIND_ADAPTER, SEVERITY_WARNING
from kiro_crew.acp.types import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    EVENT_COMPLETE,
    STOP_REASON_END_TURN,
    AcpEvent,
    JsonRpcMessage,
)
from kiro_crew.agent_sdk.backends import ACP_BACKENDS_FAST_MODE_CONFIG_OPTION
from kiro_crew.dashboard.chat import (
    api_chat_slot_fast_mode,
    api_chat_slot_selection_capabilities,
    api_chat_slot_ultracode,
)
from kiro_crew.dashboard.state import DashboardState, _ChatSlot
from kiro_crew.providers.acp import AcpProvider
from kiro_crew.providers.acp_model_effort import (
    FAST_MODE_AVAILABLE_DESCRIPTION,
    FAST_MODE_UNAVAILABLE_MAX,
    fast_mode_unavailable_reason,
)
from kiro_crew.testing.synthetic_credentials import github_classic
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

FIXTURE = CORPUS / "claude" / "fast-mode.jsonl"
SESSION = "claude-session-fast"
#: The description of a switch the free plan blocks (dist/session-fast-mode.js:25-34,
#: 69-76): the available description, " — ", the reason.
FREE_PLAN_DESCRIPTION = "Faster responses on supported models — not available on the free plan"


def _fast_option(value: str, description: str = "Faster responses on supported models") -> dict:
    # The select-fallback shape claude-agent-acp builds for a client that did not opt
    # into boolean config options (dist/session-fast-mode.js:69-89).
    return {
        "id": "fast",
        "name": "Fast mode",
        "description": description,
        "category": "model_config",
        "type": "select",
        "currentValue": value,
        "options": [{"value": "on", "name": "On"}, {"value": "off", "name": "Off"}],
    }


def _ultracode_option(value: str) -> dict:
    return {**_fast_option(value), "id": "ultracode", "name": "Ultracode"}


def _provider(backend: str, options: list[dict]) -> AcpProvider:
    with patch("kiro_crew.providers.acp.AcpClient"):
        provider = AcpProvider(acp_backend=backend)
    client = MagicMock()
    client.backend = backend
    client.acp_config_options = options
    client.has_active_turn = MagicMock(return_value=False)

    async def _write(config_id: str, value: str) -> None:
        # The adapter answers with the session's full configOptions, which the
        # client stores in place of the old ones.
        client.acp_config_options = [
            {**opt, "currentValue": value} if opt.get("id") == config_id else opt for opt in options
        ]

    client.set_config_option = AsyncMock(side_effect=_write)
    provider._client = client
    return provider


# ── 1. The provider ─────────────────────────────────────────────────────────────


def test_the_fast_mode_set_names_claude_alone() -> None:
    assert ACP_BACKENDS_FAST_MODE_CONFIG_OPTION == frozenset({ACP_BACKEND_CLAUDE})


def test_claude_declares_no_boolean_config_options() -> None:
    """The reader reads an ``on``/``off`` select because Crew never asks for the
    boolean form (``clientSupportsBooleanConfigOptions``)."""
    assert "configOptions" not in (client_capabilities_for(ACP_BACKEND_CLAUDE).get("session") or {})


@pytest.mark.asyncio
@pytest.mark.parametrize(("start", "enabled", "sent"), [("off", True, "on"), ("on", False, "off")])
async def test_a_claude_session_writes_its_switch_and_reads_the_reply_back(start, enabled, sent):
    provider = _provider(ACP_BACKEND_CLAUDE, [_fast_option(start)])
    assert provider.fast_mode_enabled() is not enabled

    assert await provider.set_fast_mode(enabled) is enabled

    provider._client.set_config_option.assert_awaited_once_with("fast", sent)
    assert provider.fast_mode_enabled() is enabled


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("backend", "options"),
    [
        # Another harness advertising an option of the same id is not offered it.
        (ACP_BACKEND_KIRO, [_fast_option("off")]),
        (ACP_BACKEND_KAS, [_fast_option("off")]),
        (ACP_BACKEND_CODEX, [_fast_option("off")]),
        # A claude model without Fast mode support advertises no option, even
        # where it advertises ultracode.
        (ACP_BACKEND_CLAUDE, [{"id": "effort", "type": "select", "currentValue": "high"}]),
        (ACP_BACKEND_CLAUDE, [_ultracode_option("on")]),
    ],
)
async def test_no_switch_is_offered_or_written_without_the_claude_option(backend, options):
    provider = _provider(backend, options)

    assert provider.fast_mode_enabled() is None
    assert await provider.set_fast_mode(True) is None
    provider._client.set_config_option.assert_not_awaited()


def test_the_two_switches_read_their_own_options():
    provider = _provider(ACP_BACKEND_CLAUDE, [_fast_option("on"), _ultracode_option("off")])
    assert (provider.fast_mode_enabled(), provider.ultracode_enabled()) == (True, False)


# ── 2. Why the switch cannot be turned on ──────────────────────────────────────


@pytest.mark.parametrize(
    ("description", "reason"),
    [
        # Each explanation the adapter folds in (dist/session-fast-mode.js:25-34, 49).
        (FREE_PLAN_DESCRIPTION, "not available on the free plan"),
        (
            f"{FAST_MODE_AVAILABLE_DESCRIPTION} — not available on this API provider",
            "not available on this API provider",
        ),
        (
            f"{FAST_MODE_AVAILABLE_DESCRIPTION} — disabled by environment configuration",
            "disabled by environment configuration",
        ),
        (
            f"{FAST_MODE_AVAILABLE_DESCRIPTION} — requires extra usage to be enabled for this "
            "account",
            "requires extra usage to be enabled for this account",
        ),
        # A code a newer SDK adds gets the adapter's generic text.
        (
            f"{FAST_MODE_AVAILABLE_DESCRIPTION} — not available (new_code)",
            "not available (new_code)",
        ),
        # Any other description is a reason too, read whole, without its closing period.
        ("Fast mode is off for this workspace.", "Fast mode is off for this workspace"),
        (f"{FAST_MODE_AVAILABLE_DESCRIPTION} —", ""),
        # The text is the adapter's: redacted, then bounded; too long to redact, no words.
        (
            f"{FAST_MODE_AVAILABLE_DESCRIPTION} — key " + github_classic(seed=351),
            "key [REDACTED: credential]",
        ),
        (f"{FAST_MODE_AVAILABLE_DESCRIPTION} — " + "x" * 400, "x" * FAST_MODE_UNAVAILABLE_MAX),
        ("y" * 5000, ""),
        # Nothing blocks it: the available description, or no description at all.
        (FAST_MODE_AVAILABLE_DESCRIPTION, None),
        (f"{FAST_MODE_AVAILABLE_DESCRIPTION}.", None),
        ("", None),
        (None, None),
    ],
)
def test_the_reason_is_read_out_of_the_description(description, reason):
    assert fast_mode_unavailable_reason(description) == reason


@pytest.mark.parametrize(
    ("backend", "options", "reason"),
    [
        (
            ACP_BACKEND_CLAUDE,
            [_fast_option("off", FREE_PLAN_DESCRIPTION)],
            "not available on the free plan",
        ),
        (ACP_BACKEND_CLAUDE, [_fast_option("off")], None),
        # A switch that reads on is not blocked, whatever its description says.
        (ACP_BACKEND_CLAUDE, [_fast_option("on", FREE_PLAN_DESCRIPTION)], None),
        (ACP_BACKEND_CLAUDE, [_ultracode_option("off")], None),
        (ACP_BACKEND_KIRO, [_fast_option("off", FREE_PLAN_DESCRIPTION)], None),
    ],
)
def test_only_a_claude_switch_that_reads_off_carries_a_reason(backend, options, reason):
    assert _provider(backend, options).fast_mode_unavailable() == reason


# ── 3. The recorded frames, through the real client ────────────────────────────


def _fixture() -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    """The fixture's ``session/new`` result, the write's reply and the turn's updates."""
    meta, frames = read_fixture(FIXTURE)
    assert meta["backend"] == ACP_BACKEND_CLAUDE
    session_new, write_reply, *updates, _prompt_result = frames
    return session_new["result"], write_reply["result"], updates


def _real_provider() -> AcpProvider:
    with patch("kiro_crew.providers.acp.AcpClient"):
        provider = AcpProvider(acp_backend=ACP_BACKEND_CLAUDE)
    client = AcpClient(acp_backend=ACP_BACKEND_CLAUDE)
    client._session_id = SESSION
    client._store_session_config(_fixture()[0])
    provider._client = client
    return provider


async def _turn(client: AcpClient, updates: list[dict[str, Any]]) -> list[AcpEvent]:
    """Run one ``_dispatch_events`` turn over *updates*, then the prompt's answer."""
    frames: list[tuple[str, JsonRpcMessage]] = [
        ("update", JsonRpcMessage.from_dict(update)) for update in updates
    ]
    frames.append(("complete", JsonRpcMessage(id=1, result={"stopReason": STOP_REASON_END_TURN})))

    async def _fake_loop(req_id, timeout):
        for frame in frames:
            yield frame

    client._prompt_loop = _fake_loop  # type: ignore[assignment]
    return [
        event async for event in client._dispatch_events(req_id=1, timeout=FALLBACK_TIMEOUT_SECS)
    ]


@pytest.mark.asyncio
async def test_the_recorded_write_is_sent_as_the_select_value_and_read_back():
    provider = _real_provider()
    client = provider._client
    assert provider.fast_mode_enabled() is False
    session_new, write_reply, _updates = _fixture()
    # The fresh session's switch is not blocked: it carries the available description.
    (fast,) = [opt for opt in session_new["configOptions"] if opt["id"] == "fast"]
    assert fast["description"] == FAST_MODE_AVAILABLE_DESCRIPTION
    assert provider.fast_mode_unavailable() is None
    client._send_request = AsyncMock(return_value=3)  # type: ignore[method-assign]
    client._wait_for_response = AsyncMock(return_value=write_reply)  # type: ignore[method-assign]

    assert await provider.set_fast_mode(True) is True

    client._send_request.assert_awaited_once_with(
        "session/set_config_option", {"sessionId": SESSION, "configId": "fast", "value": "on"}
    )


@pytest.mark.asyncio
async def test_the_sdk_turning_it_off_is_a_notice_row_and_the_switch_reads_off():
    """The adapter says once why the switch snapped back, then sends the option update
    the next ``selection-capabilities`` poll reads: the switch off, with the reason."""
    provider = _real_provider()
    _session_new, write_reply, updates = _fixture()
    provider._client._acp_config_options = write_reply["configOptions"]
    assert provider.fast_mode_enabled() is True
    assert [u["params"]["update"]["sessionUpdate"] for u in updates] == [
        "agent_message_chunk",
        "notice",
        "config_option_update",
    ]

    events = await _turn(provider._client, updates)

    notices = [e.harness_notice for e in events if e.harness_notice is not None]
    assert [(n.kind, n.severity, n.title, n.description) for n in notices] == [
        (
            NOTICE_KIND_ADAPTER,
            SEVERITY_WARNING,
            "Fast mode turned off",
            "Not available on the free plan.",
        )
    ]
    assert events[-1].kind == EVENT_COMPLETE
    assert provider.fast_mode_enabled() is False
    assert provider.fast_mode_unavailable() == "not available on the free plan"


# ── 4. The dashboard routes ────────────────────────────────────────────────────


def _app(provider: AcpProvider) -> tuple[web.Application, _ChatSlot]:
    slot = _ChatSlot("test")
    state = MagicMock(spec=DashboardState)
    state._slots = {slot.key: slot}
    state.sessions = MagicMock()
    state.sessions.get_provider = MagicMock(return_value=provider)
    state.sessions.try_acquire = AsyncMock(return_value=True)
    state.sessions.release = MagicMock()
    state.sessions._fold_key = MagicMock(side_effect=lambda key: key)
    app = web.Application()
    app["state"] = state
    app.router.add_get(
        "/api/chat/slots/{slot}/selection-capabilities", api_chat_slot_selection_capabilities
    )
    app.router.add_post("/api/chat/slots/{slot}/fast", api_chat_slot_fast_mode)
    app.router.add_post("/api/chat/slots/{slot}/ultracode", api_chat_slot_ultracode)
    return app, slot


async def _caps(client: TestClient) -> dict:
    with (
        patch.object(AcpProvider, "supports_effort", return_value=False),
        patch("kiro_crew.dashboard.chat_handlers._own_models_field", AsyncMock(return_value={})),
    ):
        return await (await client.get("/api/chat/slots/test/selection-capabilities")).json()


@pytest.mark.asyncio
async def test_the_dashboard_reports_the_switch_and_writes_it_live():
    provider = _provider(ACP_BACKEND_CLAUDE, [_fast_option("off")])
    app, _slot = _app(provider)
    sessions = app["state"].sessions
    async with TestClient(TestServer(app)) as client:
        caps = await _caps(client)
        assert caps["fast"] is False
        assert "ultracode" not in caps

        resp = await client.post("/api/chat/slots/test/fast", json={"enabled": True})
        assert resp.status == 200
        assert await resp.json() == {"ok": True, "fast": True}

        assert (await _caps(client))["fast"] is True
    provider._client.set_config_option.assert_awaited_once_with("fast", "on")
    sessions.try_acquire.assert_awaited_once_with("dashboard:test")
    sessions.release.assert_called_once_with("dashboard:test", wake_queue=True)


@pytest.mark.asyncio
async def test_the_dashboard_writes_the_switch_off_and_reads_it_back():
    provider = _provider(ACP_BACKEND_CLAUDE, [_fast_option("on")])
    app, _slot = _app(provider)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/chat/slots/test/fast", json={"enabled": False})
        assert resp.status == 200
        assert await resp.json() == {"ok": True, "fast": False}

        assert (await _caps(client))["fast"] is False
    provider._client.set_config_option.assert_awaited_once_with("fast", "off")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("option", "caps"),
    [
        (
            _fast_option("off", FREE_PLAN_DESCRIPTION),
            {"fast": False, "fast_unavailable": "not available on the free plan"},
        ),
        (_fast_option("off"), {"fast": False}),
        (_fast_option("on"), {"fast": True}),
    ],
)
async def test_the_dashboard_reports_why_the_switch_cannot_be_turned_on(option, caps):
    provider = _provider(ACP_BACKEND_CLAUDE, [option])
    app, _slot = _app(provider)
    async with TestClient(TestServer(app)) as client:
        answer = await _caps(client)
    assert {key: answer[key] for key in ("fast", "fast_unavailable") if key in answer} == caps


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "options",
    [
        # The model does not support Fast mode: no option, no switch.
        [{"id": "effort", "type": "select", "currentValue": "high"}],
        # Ultracode alone shows ultracode alone.
        [_ultracode_option("off")],
    ],
)
async def test_a_session_that_does_not_advertise_it_shows_no_switch_and_refuses_a_write(options):
    provider = _provider(ACP_BACKEND_CLAUDE, options)
    app, _slot = _app(provider)
    async with TestClient(TestServer(app)) as client:
        assert "fast" not in await _caps(client)

        resp = await client.post("/api/chat/slots/test/fast", json={"enabled": True})
        assert resp.status == 409
        assert await resp.json() == {
            "error": "this session offers no Fast mode switch",
            "code": "fast_not_offered",
        }
    provider._client.set_config_option.assert_not_awaited()
    app["state"].sessions.try_acquire.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("backend", "busy", "code"),
    [
        (ACP_BACKEND_KIRO, False, "fast_not_offered"),
        # A write mid-turn would read the harness's output beside the streaming turn.
        (ACP_BACKEND_CLAUDE, True, "turn_in_flight"),
    ],
)
async def test_the_dashboard_refuses_a_write_it_cannot_make_safely(backend, busy, code):
    provider = _provider(backend, [_fast_option("off")])
    provider._client.has_active_turn = MagicMock(return_value=busy)
    app, _slot = _app(provider)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/chat/slots/test/fast", json={"enabled": True})
        assert resp.status == 409
        assert (await resp.json())["code"] == code
    provider._client.set_config_option.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_dashboard_refuses_a_write_while_a_channel_turn_holds_the_session():
    # A channel turn takes the lease before its prompt is in flight, so the provider
    # reports no active turn yet; its prompt would read the stdout the write waits on.
    provider = _provider(ACP_BACKEND_CLAUDE, [_fast_option("off")])
    app, _slot = _app(provider)
    app["state"].sessions.try_acquire = AsyncMock(return_value=False)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/chat/slots/test/fast", json={"enabled": True})
        assert resp.status == 409
        assert (await resp.json())["code"] == "turn_in_flight"
    provider._client.set_config_option.assert_not_awaited()
    app["state"].sessions.release.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("route", "option", "code"),
    [
        ("fast", _fast_option("off"), "fast_refused"),
        ("ultracode", _ultracode_option("off"), "ultracode_refused"),
    ],
)
async def test_a_write_the_adapter_rejects_answers_502_and_frees_the_lease(route, option, code):
    provider = _provider(ACP_BACKEND_CLAUDE, [option])
    provider._client.set_config_option = AsyncMock(side_effect=AcpError("Invalid params"))
    app, _slot = _app(provider)
    sessions = app["state"].sessions
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(f"/api/chat/slots/test/{route}", json={"enabled": True})
        assert resp.status == 502
        assert await resp.json() == {"error": "Invalid params", "code": code}
    sessions.release.assert_called_once_with("dashboard:test", wake_queue=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{}, {"enabled": "on"}, {"enabled": 1}])
async def test_a_value_that_is_not_a_boolean_is_refused(body):
    provider = _provider(ACP_BACKEND_CLAUDE, [_fast_option("off")])
    app, _slot = _app(provider)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/chat/slots/test/fast", json=body)
        assert resp.status == 400
        assert (await resp.json())["code"] == "invalid_value"
    provider._client.set_config_option.assert_not_awaited()
