"""Shared Crew repair: A user row written by a request handler names its server-derived author; channel rows the bounded principal."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from kiro_crew.shared_crew import admission_gate as ag
from kiro_crew.shared_crew import attribution, wiring
from kiro_crew.shared_crew.contracts import (
    ActorContext,
    DisplaySnapshot,
    Principal,
    PrincipalKind,
    Provenance,
    Transport,
    principal_id_for,
)


def _person(login: str) -> Principal:
    return Principal(
        id=principal_id_for("tailscale", "tailnet", login),
        kind=PrincipalKind.HUMAN,
        provider="tailscale",
        issuer="tailnet",
        subject=login,
        provenance=Provenance.TAILSCALE_SERVE,
        display=DisplaySnapshot(name=login.split("@")[0], login=login),
    )


ALICE = _person("alice@example.test")
BOB = _person("bob@example.test")


def _actor(principal: Principal) -> ActorContext:
    return ActorContext(principal=principal, request_id="r", transport=Transport.HTTP)


@pytest.fixture
def dash_state(tmp_path, monkeypatch, _floor_monkeypatch):
    from chat_test_helpers import _make_state

    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    st = _make_state(tmp_path)
    st.broadcast_ws = MagicMock()
    st.broadcast_ws_owners = MagicMock()
    st.push_slots_update = MagicMock()
    st.owner_id = ""
    return st


# -- 6. a request handler's user rows name their author ------------------------------------


@pytest.mark.asyncio
async def test_request_author_is_stamped_only_in_the_request_task(dash_state):
    slot = dash_state.get_or_create_slot("tab-auth")

    async def handler():
        attribution.bind_request_author(_actor(BOB))
        row = slot.append("user", "from the handler", "msg msg-u")
        claimed = slot.append(
            "user", "queued entry", "msg msg-u", meta={"author": ALICE.to_public_dict()}
        )

        async def later_turn():
            return slot.append("user", "drained later", "msg msg-u")

        spawned = await asyncio.create_task(later_turn())
        return row, claimed, spawned

    row, claimed, spawned = await asyncio.create_task(handler())
    assert row["meta"]["author"]["id"] == BOB.id
    assert claimed["meta"]["author"]["id"] == ALICE.id
    assert "author" not in (spawned.get("meta") or {})
    outside = slot.append("user", "no request", "msg msg-u")
    assert "author" not in (outside.get("meta") or {})


@pytest.mark.asyncio
async def test_published_actor_binds_the_request_author():
    from aiohttp.test_utils import make_mocked_request

    from kiro_crew.shared_crew import identity

    async def go():
        request = make_mocked_request("POST", "/api/chat")
        await identity.publish_actor(request, trust=None, serve_peer=None, internal=True)
        return attribution.with_request_author(None)

    meta = await asyncio.create_task(go())
    assert meta is not None and meta["author"]["kind"] == "system"


def test_channel_rows_name_the_bounded_principal():
    meta = attribution.stamp_principal_author({}, Principal.unknown())
    assert attribution.message_author(meta).is_unknown
    auto = attribution.stamp_principal_author({}, ag.AUTOMATION_PRINCIPAL)
    assert attribution.message_author(auto).id == ag.AUTOMATION_PRINCIPAL.id


def test_attribution_capability_is_available():
    assert wiring.identity_source().states["attribution"].value == "available"
