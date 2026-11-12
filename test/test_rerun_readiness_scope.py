"""History reruns gate on kiro-cli only when their turn signs in through it.

``reject_if_kiro_unverified`` answers whether kiro-cli is signed in. Regenerate,
edit-resend and rewind ask it only when the slot's turn runs on a harness that
signs in from kiro-cli's own store; on any other harness the turn is its own
sign-in authority, as for an ordinary send. Edit-resend and rewind cut history
before their turn, so on such a harness a slot whose last turn ended signed out
is refused before the cut.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_regenerate_cov80_helpers import state_fixture  # noqa: F401
from chat_regenerate_cov80_helpers import _make_regen_app
from chat_test_helpers import pin_harness_choice

from kiro_crew.acp_backends import ACP_BACKEND_CLAUDE, ACP_BACKEND_KAS, ACP_BACKEND_KIRO
from kiro_crew.config import KiroCrewConfig
from kiro_crew.dashboard import kiro_readiness
from kiro_crew.dashboard.chat_rewind import api_chat_slot_rewind
from kiro_crew.kiro_prerequisite import KiroPrerequisiteService, PrerequisiteStatus

_REGENERATE = ("regenerate", {})
_EDIT_RESEND = ("edit-resend", {"index": 0, "content": "edited"})
_REWIND = ("rewind", {"at_message_index": 0, "content": "edited"})


class _SignedOutKiroCli(KiroPrerequisiteService):
    """A kiro-cli that a fresh probe finds signed out."""

    # Built without ``__init__``, so the refusal's latch read needs a status here.
    _status = PrerequisiteStatus(platform="Linux")

    async def verified_ready(self, *, max_age_secs: float) -> bool:
        del max_age_secs
        return False


def _pin_applied(state, **choices: str) -> None:
    """Pin the harness choice in ``config.json`` and apply it to the session manager."""
    pin_harness_choice(**choices)
    state.sessions._cfg = KiroCrewConfig.load()


def _answered_slot(state, key: str = "s1"):
    slot = state.get_or_create_slot(key)
    slot.append("user", "question")
    slot.append("assistant", "answer")
    slot.drain()
    return slot


async def _post(state, route: str, body: dict):
    app = _make_regen_app(state)
    app.router.add_post("/api/chat/slots/{slot}/rewind", api_chat_slot_rewind)
    with (
        patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=AsyncMock()),
        patch("kiro_crew.dashboard.chat_rewind._run_chat", new=AsyncMock()),
    ):
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(f"/api/chat/slots/s1/{route}", json=body)
            return resp.status, await resp.json()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("slot_key", "default", "member", "expected"),
    [
        ("s1", ACP_BACKEND_KIRO, ACP_BACKEND_CLAUDE, True),
        ("s1", ACP_BACKEND_KAS, ACP_BACKEND_CLAUDE, True),
        ("s1", ACP_BACKEND_CLAUDE, ACP_BACKEND_KIRO, False),
        # A member DM runs on agent.member_acp_backend, whatever the default.
        ("member-ana", ACP_BACKEND_KIRO, ACP_BACKEND_CLAUDE, False),
        ("member-ana", ACP_BACKEND_CLAUDE, ACP_BACKEND_KIRO, True),
    ],
)
async def test_the_gate_follows_the_backend_the_slot_turn_runs_on(
    state, slot_key, default, member, expected
) -> None:
    _pin_applied(state, acp_backend=default, member_acp_backend=member)

    mode = "member" if slot_key.startswith("member-") else ""
    slot = state.get_or_create_slot(slot_key, mode=mode)

    assert await kiro_readiness.rerun_signs_in_via_kiro_cli(state.sessions, slot) is expected


@pytest.mark.asyncio
async def test_no_slot_or_an_unreadable_selection_keeps_the_gate(state) -> None:
    _pin_applied(state, acp_backend=ACP_BACKEND_CLAUDE)
    slot = state.get_or_create_slot("s1")

    assert await kiro_readiness.rerun_signs_in_via_kiro_cli(state.sessions, None) is True
    assert await kiro_readiness.rerun_signs_in_via_kiro_cli(None, slot) is True
    with patch(
        "kiro_crew.dashboard.chat_handlers._configured_backend_for_slot",
        new=AsyncMock(side_effect=OSError("config unreadable")),
    ):
        assert await kiro_readiness.rerun_signs_in_via_kiro_cli(state.sessions, slot) is True


@pytest.mark.asyncio
@pytest.mark.parametrize(("route", "body"), [_REGENERATE, _EDIT_RESEND, _REWIND])
@pytest.mark.parametrize(
    ("backend", "status"), [(ACP_BACKEND_CLAUDE, 200), (ACP_BACKEND_KIRO, 503)]
)
async def test_a_signed_out_kiro_cli_refuses_only_a_rerun_that_runs_on_it(
    state, route, body, backend, status
) -> None:
    _pin_applied(state, acp_backend=backend)
    state.kiro_prerequisite_service = object.__new__(_SignedOutKiroCli)
    _answered_slot(state)

    got, payload = await _post(state, route, body)

    assert got == status, payload
    if status == 503:
        assert payload["code"] == "kiro_prerequisite_required"


@pytest.mark.asyncio
@pytest.mark.parametrize(("route", "body"), [_EDIT_RESEND, _REWIND])
@pytest.mark.parametrize(
    ("backend", "status"), [(ACP_BACKEND_CLAUDE, 503), (ACP_BACKEND_KIRO, 200)]
)
async def test_a_signed_out_last_turn_refuses_a_foreign_rerun_before_the_cut(
    state, route, body, backend, status
) -> None:
    # kiro-cli is verified signed in (the fixture's service), so on a kiro-cli
    # slot the gate is the authority and the stale flag refuses nothing.
    _pin_applied(state, acp_backend=backend)
    slot = _answered_slot(state)
    slot._last_turn_auth_required = True
    before = list(slot.messages)

    got, payload = await _post(state, route, body)

    assert got == status, payload
    if status == 503:
        assert payload["code"] == "harness_sign_in_required"
        assert slot.messages == before
        state.sessions.discard_conversation.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(("route", "body"), [_EDIT_RESEND, _REWIND])
async def test_a_choice_the_watcher_has_not_applied_keeps_the_gate_before_the_cut(
    state, route, body
) -> None:
    # The replacement turn starts from the applied Kiro CLI factory, whatever
    # config.json says until the watcher applies it.
    _pin_applied(state, acp_backend=ACP_BACKEND_KIRO)
    pin_harness_choice(acp_backend=ACP_BACKEND_CLAUDE)
    state.kiro_prerequisite_service = object.__new__(_SignedOutKiroCli)
    slot = _answered_slot(state)
    slot._last_turn_auth_required = False
    before = list(slot.messages)

    got, payload = await _post(state, route, body)

    assert got == 503, payload
    assert payload["code"] == "kiro_prerequisite_required"
    assert slot.messages == before
    state.sessions.discard_conversation.assert_not_awaited()
