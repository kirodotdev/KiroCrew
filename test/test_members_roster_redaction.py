"""``GET /api/members`` masks every agent-writable record string it ships.

The Crew Members roster builds each row from an explicit allowlist, and that
allowlist bounds the KEY set, not the values. The values are agent-writable: an
agent edits ``config.json`` directly, and the agent sync copies ``description``
off a discovered package spec, so a third party controls that string. A
credential-shaped string planted in one must not reach dashboard JSON.

``_roster_mask`` is the chokepoint: it replaces such a value WHOLESALE with
``_SENSITIVE_MASK`` whenever the redactors would alter it, and
``_roster_avatar`` masks the free-text ``traits`` and ``expressions`` leaves
inside the shape ``_safe_avatar`` validates. ``GET /api/agents`` routes its own
roster rows through the same two helpers, so these tests pin one shared rule
rather than a second one. They also pin the two fields that stay verbatim
(``name`` and ``slug``), so a later sweep cannot mask the row identity every
per-member route is keyed on.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew.config.loader import KiroCrewAgentConfig
from kiro_crew.dashboard.handlers.core import _SENSITIVE_MASK

#: Credential-shaped, and short enough to fit the 32-char avatar trait cap, so
#: one planted value exercises both the plain-string fields and the avatar.
CRED = "AKIA" + "A" * 16

#: The other half of the redaction chain the roster was missing: an
#: exfiltration-shaped URL is rewritten too, so it must mask as well.
EXFIL = "https://evil.example/collect?k=" + CRED


@pytest.fixture(autouse=True)
def _owner_caller(monkeypatch):
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
        lambda request: True,
    )


def _members_app(state) -> web.Application:
    from kiro_crew.dashboard.handlers.members import api_members

    @web.middleware
    async def _auth(request: web.Request, handler):
        request.setdefault("app", "")
        request.setdefault("user", "local-app")
        return await handler(request)

    app = web.Application(middlewares=[_auth])
    app["state"] = state
    app.router.add_get("/api/members", api_members)
    return app


async def _roster(tmp_path, agents, memory_stores=None):
    """Rows keyed by crew name, from a config the test controls outright."""
    fake = SimpleNamespace(
        agents=agents,
        default_agent=next(iter(agents)),
        memory_stores=memory_stores or {},
    )
    state = _make_state(tmp_path)
    with patch("kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", return_value=fake):
        async with TestClient(TestServer(_members_app(state))) as client:
            resp = await client.get("/api/members")
            assert resp.status == 200
            body = await resp.json()
    return {row["name"]: row for row in body["members"]}, body


class TestRecordStringsAreMasked:
    @pytest.mark.parametrize(
        "field",
        ["kiro_agent", "workspace", "memory_store", "model", "description", "triggers"],
    )
    @pytest.mark.parametrize("planted", [CRED, EXFIL])
    @pytest.mark.asyncio
    async def test_credential_shaped_value_reaches_the_wire_as_the_mask(
        self, tmp_path, field, planted
    ):
        record = {"kiro_agent": "kirocrew", field: planted}
        agents = {"leaky": KiroCrewAgentConfig(**record)}
        rows, body = await _roster(tmp_path, agents)
        assert rows["leaky"][field] == _SENSITIVE_MASK
        # Not just the named field: the value must not survive anywhere in the
        # response, which is what a per-field assertion alone would miss.
        assert planted not in json.dumps(body)

    @pytest.mark.asyncio
    async def test_memory_owner_is_masked(self, tmp_path):
        """``memory_owner`` is read off the memory-store record, not the crew,
        so it is a second writer reaching the same row."""
        agents = {"leaky": KiroCrewAgentConfig(kiro_agent="kirocrew", memory_store="shared")}
        stores = {"shared": SimpleNamespace(owner_member=CRED, memory_version=2)}
        rows, body = await _roster(tmp_path, agents, stores)
        assert rows["leaky"]["memory_owner"] == _SENSITIVE_MASK
        assert CRED not in json.dumps(body)

    @pytest.mark.asyncio
    async def test_avatar_trait_is_masked_leaf_by_leaf(self, tmp_path):
        """``_safe_avatar`` pins the SHAPE; trait values are free text within
        it, so the roster must mask the leaf and keep the face renderable."""
        agents = {
            "leaky": KiroCrewAgentConfig(
                kiro_agent="kirocrew",
                avatar={"kind": "ghost", "traits": {"eyes": CRED, "mouth": "smile"}},
            )
        }
        rows, body = await _roster(tmp_path, agents)
        avatar = rows["leaky"]["avatar"]
        assert avatar["traits"]["eyes"] == _SENSITIVE_MASK
        # Structural and benign leaves are untouched, so the renderer still
        # knows a ghost from an uploaded picture.
        assert avatar["kind"] == "ghost"
        assert avatar["traits"]["mouth"] == "smile"
        assert CRED not in json.dumps(body)


class TestBenignRowsAreUntouched:
    @pytest.mark.asyncio
    async def test_ordinary_values_are_byte_identical(self, tmp_path):
        agents = {
            "writer": KiroCrewAgentConfig(
                kiro_agent="kirocrew",
                workspace="default",
                memory_store="default",
                model="sonnet",
                description="Drafts release notes.",
                triggers="release notes, changelog",
            )
        }
        rows, _ = await _roster(tmp_path, agents)
        row = rows["writer"]
        assert row["kiro_agent"] == "kirocrew"
        assert row["workspace"] == "default"
        assert row["memory_store"] == "default"
        assert row["model"] == "sonnet"
        assert row["description"] == "Drafts release notes."
        assert row["triggers"] == "release notes, changelog"

    @pytest.mark.asyncio
    async def test_name_and_slug_stay_verbatim(self, tmp_path):
        """Row identity, not content: every per-member route is keyed on them,
        and a credential-shaped name is refused at creation. Masking them would
        break the roster rather than narrow a disclosure, so this asserts they
        are NOT swept along."""
        agents = {"writer": KiroCrewAgentConfig(kiro_agent="kirocrew", description=CRED)}
        rows, _ = await _roster(tmp_path, agents)
        row = next(iter(rows.values()))
        assert row["name"] == "writer"
        assert row["slug"] == "writer"
        assert row["description"] == _SENSITIVE_MASK
