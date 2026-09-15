"""The hire gallery's catalog (design step 6): ``GET /api/members/templates``.

Every template a member can be hired from, from all three sources -- an
installed app's job cards, the files this package ships, the user's own agent
files -- in one shape, with the exact ``source`` a hire sends, what the gallery
renders, and who was already hired from each.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import test_member_hire as _hire
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import agent_state, member_gallery
from kiro_crew.agent_files import CONDUCTOR_AGENT_FILENAME
from kiro_crew.apps.manager import APP_MANIFEST_FILENAME

APP = _hire.APP
TEMPLATE_AGENT = _hire.TEMPLATE_AGENT
SOURCE = _hire.SOURCE
_store_hire = _hire._store_hire
_hire_body = _hire._hire
_owner_caller = _hire._owner_caller
agents_dir = _hire.agents_dir
store_app = _hire.store_app


def _app() -> web.Application:
    from kiro_crew.dashboard.handlers import (
        api_kirocrew_agent_update,
        api_member_briefing_get,
        api_member_fire,
        api_member_hire,
        api_member_templates,
        api_members,
    )

    @web.middleware
    async def _auth(request: web.Request, handler):
        request.setdefault("app", "")
        request.setdefault("user", "local-app")
        return await handler(request)

    app = web.Application(middlewares=[_auth])
    app["state"] = MagicMock(sessions=None)
    app.router.add_get("/api/members/templates", api_member_templates)
    app.router.add_get("/api/members/{slug}/briefing", api_member_briefing_get)
    app.router.add_post("/api/members", api_member_hire)
    app.router.add_get("/api/members", api_members)
    app.router.add_post("/api/members/{member}/fire", api_member_fire)
    app.router.add_put("/api/agents/{name}", api_kirocrew_agent_update)
    return app


async def _cards(client) -> dict[str, dict]:
    resp = await client.get("/api/members/templates")
    assert resp.status == 200, await resp.text()
    return {c["id"]: c for c in (await resp.json())["templates"]}


def _card_full(store_app: Path) -> None:
    """Give the store app's card everything the gallery renders."""
    m = json.loads((store_app / APP_MANIFEST_FILENAME).read_text())
    m["crew"]["templates"][0].update(
        {
            "duty": "Triages every page, correlates it with deploys.",
            "description": "Owns a paging queue end to end. Never rolls back without an ack.",
            "category": "ops",
            "tags": ["Incident triage", "Deploy correlation", "Rollback plans"],
            "starter_prompts": [
                "What paged overnight?",
                {"text": "Draft a rollback plan.", "attachment": "incident.md"},
            ],
            "avatar": {"kind": "ghost", "traits": {"eyes": "visor", "tile": "#de2121"}},
        }
    )
    (store_app / APP_MANIFEST_FILENAME).write_text(json.dumps(m))


def _shipped_conductor(agents_dir: Path) -> None:
    (agents_dir / CONDUCTOR_AGENT_FILENAME).write_text(
        json.dumps(
            {
                "name": CONDUCTOR_AGENT_FILENAME[:-5],
                "description": "Runs one issue-to-PR pipeline as a supervised fleet.",
                "mcpServers": {"github": {"command": "gh-mcp"}},
                "resources": ["skill://~/.kiro/skills/prepare-pr/SKILL.md"],
            }
        )
    )


@pytest.fixture(autouse=True)
def _installed_apps_visible(store_app: Path, monkeypatch):
    """The discovery's app-name probe reads the apps root under the data home
    the fixtures already point at; nothing else to wire."""
    yield


class TestCatalog:
    @pytest.mark.asyncio
    async def test_gate_the_gallery_lists_cards_from_all_three_sources(
        self, agents_dir: Path, store_app: Path
    ):
        """One listing: the app's card (with everything the manifest gave it and
        the exact store source), the shipped conductor as a built-in, the
        user's ``reviewer`` file as local -- and NOT the assistant, NOT the
        app's materialized file on its own, NOT a member's private copy."""
        _card_full(store_app)
        _shipped_conductor(agents_dir)
        listed = [
            *[
                dict(name=n)
                for n in (SOURCE, "kirocrew", CONDUCTOR_AGENT_FILENAME[:-5], f"{APP}--triage")
            ],
        ]
        with patch(
            "kiro_crew.dashboard.handlers.agents.list_agents",
            lambda *a, **k: [MagicMock(name=x["name"]) for x in listed],
        ):
            async with TestClient(TestServer(_app())) as client:
                # A member hired from the local file: its private copy must not list.
                resp = await client.post("/api/members", json=_hire_body("Nia"))
                assert resp.status == 200, await resp.text()
                resp = await client.get("/api/members/templates")
                assert resp.status == 200, await resp.text()
                cards = {c["id"]: c for c in (await resp.json())["templates"]}
        assert set(cards) == {
            f"app:{APP}/{TEMPLATE_AGENT}",
            f"builtin:{CONDUCTOR_AGENT_FILENAME[:-5]}",
            f"local:{SOURCE}",
        }
        app_card = cards[f"app:{APP}/{TEMPLATE_AGENT}"]
        assert app_card["source"] == {"kind": "store", "app": APP, "agent": TEMPLATE_AGENT}
        assert app_card["role"] == "Oncall Triage Engineer"
        assert app_card["duty"] == "Triages every page, correlates it with deploys."
        assert app_card["category"] == "ops"
        assert app_card["tags"] == ["Incident triage", "Deploy correlation", "Rollback plans"]
        assert app_card["starter_prompts"] == [
            {"text": "What paged overnight?"},
            {"text": "Draft a rollback plan.", "attachment": "incident.md"},
        ]
        assert (
            app_card["avatar"]["kind"] == "ghost"
            and app_card["avatar"]["traits"]["eyes"] == "visor"
        )
        assert app_card["publisher"] == "Oncall pack" and app_card["version"] == "1.2.0"
        assert app_card["hireable"] is True and app_card["hired_as"] == []
        assert app_card["agent"] == "triage"
        builtin = cards[f"builtin:{CONDUCTOR_AGENT_FILENAME[:-5]}"]
        assert builtin["origin"] == "builtin" and builtin["publisher"] == "Kiro Crew"
        assert builtin["source"] == {"kind": "local", "agent": CONDUCTOR_AGENT_FILENAME[:-5]}
        assert builtin["role"] == member_gallery.humanize_agent_name(CONDUCTOR_AGENT_FILENAME[:-5])
        assert builtin["duty"] == "Runs one issue-to-PR pipeline as a supervised fleet."
        assert builtin["category"] == "other" and builtin["avatar"] is None
        assert {(c["kind"], c["name"]) for c in builtin["capabilities"]} == {
            ("mcp", "github"),
            ("skill", "prepare-pr"),
        }
        local = cards[f"local:{SOURCE}"]
        assert local["origin"] == "local" and local["publisher"] == ""
        assert local["role"] == "Reviewer" and local["duty"] == "Reviews pull requests."
        # The crewmate hired from it is attributed to it -- by the id that opens
        # its DM and the name the roster shows; its copy is not a card.
        assert local["hired_as"] == [{"id": "Nia", "display_name": "Nia"}]

    @pytest.mark.asyncio
    async def test_gate_hired_as_is_the_enrolled_active_roster_from_the_card(
        self, agents_dir: Path, store_app: Path
    ):
        """The 0 / 1 / 2+ the gallery's secondary action turns on. Two named
        hires from one card list both, in roster order, each with the id that
        opens its DM and its display name; a rename follows the name and keeps
        the id; a fire drops the crewmate; a card nobody hired from lists none.
        Membership is the enrollment record: a row a hand edit bound to the
        card's file, or a session that used the template, is not a hire."""
        from kiro_crew.config.loader import config_path

        card_id = f"app:{APP}/{TEMPLATE_AGENT}"
        async with TestClient(TestServer(_app())) as client:
            assert (await _cards(client))[card_id]["hired_as"] == []
            for name in ("Checkout triage", "Payments triage"):
                resp = await client.post("/api/members", json=_store_hire(name))
                assert resp.status == 200, await resp.text()
            cards = await _cards(client)
            assert cards[card_id]["hired_as"] == [
                {"id": "Checkout-triage", "display_name": "Checkout triage"},
                {"id": "Payments-triage", "display_name": "Payments triage"},
            ]
            # Nobody hired from the local file yet; the default member is the
            # assistant's, which is never a card.
            assert cards[f"local:{SOURCE}"]["hired_as"] == []
            assert not any(c["id"].endswith(":kirocrew") for c in cards.values())
            # Two crewmates may carry the same label: both stay listed, told
            # apart by id.
            resp = await client.put(
                "/api/agents/Payments-triage", json={"display_name": "Checkout triage"}
            )
            assert resp.status == 200, await resp.text()
            assert (await _cards(client))[card_id]["hired_as"] == [
                {"id": "Checkout-triage", "display_name": "Checkout triage"},
                {"id": "Payments-triage", "display_name": "Checkout triage"},
            ]
            # A row bound to the card's file by hand is a session agent, not a
            # hire: the count the gallery shows does not move.
            raw = json.loads(config_path().read_text())
            raw["agents"]["Hand-bound"] = {"kiro_agent": SOURCE, "memory_store": "default"}
            raw["agents"]["Hand-templated"] = {
                "kiro_agent": f"{APP}--triage",
                "memory_store": "default",
                "template": f"{APP}/{TEMPLATE_AGENT}",
            }
            config_path().write_text(json.dumps(raw))
            cards = await _cards(client)
            assert [m["id"] for m in cards[card_id]["hired_as"]] == [
                "Checkout-triage",
                "Payments-triage",
            ]
            assert cards[f"local:{SOURCE}"]["hired_as"] == []
            # Retired crewmates are excluded: the fire un-enrolls.
            resp = await client.post("/api/members/Payments-triage/fire", json={})
            assert resp.status == 200, await resp.text()
            assert (await _cards(client))[card_id]["hired_as"] == [
                {"id": "Checkout-triage", "display_name": "Checkout triage"},
            ]

    @pytest.mark.asyncio
    async def test_hired_as_reads_the_record_never_the_rows_lineage_alone(
        self, agents_dir: Path, store_app: Path
    ):
        """A record whose generation is not the row's store is a crewmate that
        was deleted and recreated under the same id: not this row, so not a
        hire from the card either -- the same predicate the roster applies."""
        card_id = f"app:{APP}/{TEMPLATE_AGENT}"
        async with TestClient(TestServer(_app())) as client:
            resp = await client.post("/api/members", json=_store_hire("Checkout triage"))
            assert resp.status == 200, await resp.text()
            assert len((await _cards(client))[card_id]["hired_as"]) == 1
            record = agent_state.get_crewmate_record("Checkout-triage", strict=True)
            agent_state.set_crewmate_record(
                "Checkout-triage",
                generation="member-someone-else",
                template=record["template"],
                hired_at=record["hired_at"],
            )
            assert (await _cards(client))[card_id]["hired_as"] == []

    @pytest.mark.asyncio
    async def test_an_unhireable_card_is_listed_with_the_hires_own_refusal(
        self, agents_dir: Path, store_app: Path
    ):
        """A card whose agent is not materialized (the app was never enabled
        cleanly) still lists -- the gallery says why Hire is off, in the code
        the hire itself would answer -- and still names the crewmates already
        hired from it (they are the owner's whatever the template's state, so
        "Chat with" stays), while a disabled app's cards do not list at all."""
        from kiro_crew.apps.manager import InstalledApp, _write_installed

        async with TestClient(TestServer(_app())) as client:
            resp = await client.post("/api/members", json=_store_hire("Checkout triage"))
            assert resp.status == 200, await resp.text()
            (agents_dir / f"{APP}--triage.json").unlink()
            resp = await client.get("/api/members/templates")
            cards = {c["id"]: c for c in (await resp.json())["templates"]}
            card = cards[f"app:{APP}/{TEMPLATE_AGENT}"]
            assert card["hireable"] is False
            assert card["unavailable_code"] == "template_not_materialized"
            assert card["role"] == "Oncall Triage Engineer"  # the card face still reads
            assert card["hired_as"] == [
                {"id": "Checkout-triage", "display_name": "Checkout triage"}
            ]
            _write_installed(APP, InstalledApp(name=APP, version="1.2.0", enabled=False))
            resp = await client.get("/api/members/templates")
            cards = {c["id"]: c for c in (await resp.json())["templates"]}
        assert not any(c["origin"] == "app" for c in cards.values())

    @pytest.mark.asyncio
    async def test_credential_shaped_card_text_is_redacted(self, agents_dir: Path, store_app: Path):
        """Every free-text field the gallery renders passes the redactor --
        the capabilities' names included (an MCP server or skill name is
        app- or user-authored config like the duty and the tags), and the app's
        version string (a semver build suffix is app-authored text too)."""
        from kiro_crew.apps.manager import InstalledApp, _write_installed

        m = json.loads((store_app / APP_MANIFEST_FILENAME).read_text())
        m["crew"]["templates"][0]["duty"] = "Rotate AKIAIOSFODNN7EXAMPLE daily"
        m["crew"]["templates"][0]["tags"] = ["AKIAIOSFODNN7EXAMPLE"]
        m["version"] = "1.2.0+AKIAIOSFODNN7EXAMPLE"
        (store_app / APP_MANIFEST_FILENAME).write_text(json.dumps(m))
        _write_installed(
            APP, InstalledApp(name=APP, version="1.2.0+AKIAIOSFODNN7EXAMPLE", enabled=True)
        )
        # A user agent file naming a credential-shaped MCP server and skill.
        (agents_dir / "leaky.json").write_text(
            json.dumps(
                {
                    "name": "leaky",
                    "description": "d",
                    "mcpServers": {"AKIAIOSFODNN7EXAMPLE": {"command": "x"}},
                    "skills": ["skills/AKIAIOSFODNN7EXAMPLE/SKILL.md"],
                }
            ),
            encoding="utf-8",
        )
        async with TestClient(TestServer(_app())) as client:
            resp = await client.get("/api/members/templates")
            body = await resp.text()
            cards = {c["id"]: c for c in json.loads(body)["templates"]}
        assert "AKIAIOSFODNN7EXAMPLE" not in body
        leaky = next(c for c in cards.values() if c["source"].get("agent") == "leaky")
        assert leaky["capabilities"], "the capabilities were dropped rather than redacted"
        assert all(c["kind"] in ("mcp", "skill") for c in leaky["capabilities"])

    @pytest.mark.asyncio
    async def test_a_card_whose_routing_identifier_reads_as_a_credential_is_omitted(
        self, agents_dir: Path, store_app: Path
    ):
        """``id``, ``agent`` and ``source`` are what the hire posts back, so
        they cannot be redacted and still work: a hand-authored agent file
        named like a credential is left out of the listing rather than shipped
        raw across the dashboard boundary. ``hired_as`` is display-only and
        passes the redactor like every other free-text field."""
        from kiro_crew.config.loader import config_path

        (agents_dir / "AKIAIOSFODNN7EXAMPLE.json").write_text(
            json.dumps({"name": "AKIAIOSFODNN7EXAMPLE", "description": "d"}), encoding="utf-8"
        )
        async with TestClient(TestServer(_app())) as client:
            # A hand-edited row named like a credential, bound to a listed file.
            raw = json.loads(config_path().read_text())
            raw["agents"]["AKIAIOSFODNN7EXAMPLE"] = {
                "kiro_agent": SOURCE,
                "memory_store": "default",
            }
            config_path().write_text(json.dumps(raw))
            resp = await client.get("/api/members/templates")
            assert resp.status == 200, await resp.text()
            body = await resp.text()
            cards = {c["id"]: c for c in json.loads(body)["templates"]}
        assert "AKIAIOSFODNN7EXAMPLE" not in body
        assert not any(c["source"].get("agent") == "AKIAIOSFODNN7EXAMPLE" for c in cards.values())
        # The other local cards are still listed; the hand-bound row is not a
        # crewmate, so it is not a hire from the card either.
        reviewer = next(c for c in cards.values() if c["source"].get("agent") == SOURCE)
        assert reviewer["hired_as"] == []

    @pytest.mark.asyncio
    async def test_a_credential_shaped_avatar_trait_ships_masked(
        self, agents_dir: Path, store_app: Path
    ):
        """The card's ghost is app-authored text like the manifest's other
        fields: its ``traits`` values pass the roster's avatar allowlist, so a
        credential planted in a trait axis never reaches the dashboard raw
        (the renderer resolves the masked trait to absent -- one axis
        degrades, the face stays)."""
        m = json.loads((store_app / APP_MANIFEST_FILENAME).read_text())
        m["crew"]["templates"][0]["avatar"] = {
            "kind": "ghost",
            "traits": {"eyes": "AKIAIOSFODNN7EXAMPLE", "tile": "#de2121"},
        }
        (store_app / APP_MANIFEST_FILENAME).write_text(json.dumps(m))
        async with TestClient(TestServer(_app())) as client:
            resp = await client.get("/api/members/templates")
            assert resp.status == 200, await resp.text()
            body = await resp.text()
        assert "AKIAIOSFODNN7EXAMPLE" not in body
        card = {c["id"]: c for c in json.loads(body)["templates"]}[f"app:{APP}/{TEMPLATE_AGENT}"]
        assert card["avatar"]["kind"] == "ghost"
        assert card["avatar"]["traits"]["tile"] == "#de2121"
        assert card["avatar"]["traits"]["eyes"] != "AKIAIOSFODNN7EXAMPLE"

    @pytest.mark.asyncio
    async def test_a_hired_crewmate_whose_id_reads_as_a_credential_is_omitted_from_hired_as(
        self, agents_dir: Path, store_app: Path
    ):
        """A ``hired_as`` id is a routing identifier (it opens the DM), so it
        cannot be redacted and still work. A row hand-keyed like a credential
        (the hire refuses such a name) is re-keyed by the member-id migration
        to an opaque id on load -- record included -- so what the card lists is
        that opaque id with the display name REDACTED; and were an id the
        redactor would alter ever to reach the listing, the entry is omitted
        rather than shipped raw. Either way the raw value never leaves.
        """
        from kiro_crew.config.loader import config_path

        async with TestClient(TestServer(_app())) as client:
            resp = await client.post("/api/members", json=_hire_body("Nia"))
            assert resp.status == 200, await resp.text()
            raw = json.loads(config_path().read_text())
            raw["agents"]["AKIAIOSFODNN7EXAMPLE"] = {
                "kiro_agent": SOURCE,
                "memory_store": "member-leaky",
                "display_name": "AKIAIOSFODNN7EXAMPLE",
            }
            config_path().write_text(json.dumps(raw))
            agent_state.set_crewmate_record(
                "AKIAIOSFODNN7EXAMPLE", generation="member-leaky", template=SOURCE, hired_at=""
            )
            resp = await client.get("/api/members/templates")
            assert resp.status == 200, await resp.text()
            body = await resp.text()
            cards = {c["id"]: c for c in json.loads(body)["templates"]}
        assert "AKIAIOSFODNN7EXAMPLE" not in body
        hired = cards[f"local:{SOURCE}"]["hired_as"]
        assert hired[0] == {"id": "Nia", "display_name": "Nia"}
        assert len(hired) == 2
        assert hired[1]["id"].startswith("member-") and "AKIA" not in hired[1]["id"]
        assert "AKIA" not in hired[1]["display_name"]
        # The omission rule itself, on the handler's own redactor.
        from kiro_crew.dashboard.handlers.members import _identity_text

        assert _identity_text("AKIAIOSFODNN7EXAMPLE") != "AKIAIOSFODNN7EXAMPLE"

    @pytest.mark.asyncio
    async def test_non_owner_is_refused(self, agents_dir: Path, store_app: Path, monkeypatch):
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
            lambda request: False,
        )
        async with TestClient(TestServer(_app())) as client:
            resp = await client.get("/api/members/templates")
            assert resp.status in (401, 403, 404)

    def test_humanize_agent_name(self):
        assert member_gallery.humanize_agent_name("pipeline-conductor") == "Pipeline Conductor"
        assert member_gallery.humanize_agent_name("code_reviewer.v2") == "Code Reviewer V2"
        assert member_gallery.humanize_agent_name("myAgent") == "myAgent"
        assert member_gallery._first_sentence("One. Two.") == "One."
        assert member_gallery._first_sentence("") == ""


class TestBriefingRead:
    @pytest.mark.asyncio
    async def test_the_drawer_reads_the_members_own_briefing_read_only(
        self, agents_dir: Path, store_app: Path
    ):
        """What the prompt builder would inject is what the drawer shows: the
        same pinned read, an absent file as empty text, a bad slug or a member
        that does not derive the slug refused."""
        from kiro_crew import members

        async with TestClient(TestServer(_app())) as client:
            resp = await client.post("/api/members", json=_store_hire("Pager triage"))
            assert resp.status == 200, await resp.text()
            slug = members.slug_for_name("Pager-triage")
            resp = await client.get(f"/api/members/{slug}/briefing?member=Pager-triage")
            assert resp.status == 200, await resp.text()
            body = await resp.json()
            if members.member_briefing_supported():
                # Seeded once from the card's initial_briefing at hire.
                assert body == {"text": members.read_member_briefing(slug), "supported": True}
                assert "Read the runbook." in body["text"]
            else:
                assert body == {"text": "", "supported": False}
            resp = await client.get(f"/api/members/{slug}/briefing?member=Nobody")
            assert resp.status == 400
            assert (await resp.json())["code"] == "member_slug_mismatch"
            resp = await client.get(f"/api/members/{slug}/briefing")
            assert resp.status == 400
            resp = await client.get("/api/members/..%2Fx/briefing?member=x")
            assert resp.status in (400, 404)
            # A member with no briefing yet reads as empty text, never 404.
            resp = await client.post("/api/members", json=_hire_body("Nia"))
            assert resp.status == 200, await resp.text()
            resp = await client.get(
                f"/api/members/{members.slug_for_name('Nia')}/briefing?member=Nia"
            )
            assert resp.status == 200
            assert (await resp.json())["text"] == ""

    @pytest.mark.asyncio
    async def test_the_briefing_is_owner_gated_and_redacted(
        self, agents_dir: Path, store_app: Path, monkeypatch
    ):
        """The briefing is prompt-adjacent text a member or a template wrote:
        read only by the owner, and passed through the same redactors as the
        identity fields, so a credential-shaped line never reaches the browser
        raw."""
        from kiro_crew import members

        async with TestClient(TestServer(_app())) as client:
            resp = await client.post("/api/members", json=_store_hire("Pager triage"))
            assert resp.status == 200, await resp.text()
            slug = members.slug_for_name("Pager-triage")
            if members.member_briefing_supported():
                members.member_briefing_path(slug).write_text(
                    "# Keys\n\nUse AKIAIOSFODNN7EXAMPLE for the bucket.\n", encoding="utf-8"
                )
                resp = await client.get(f"/api/members/{slug}/briefing?member=Pager-triage")
                assert resp.status == 200, await resp.text()
                text = (await resp.json())["text"]
                assert "AKIAIOSFODNN7EXAMPLE" not in text and "REDACTED" in text
            monkeypatch.setattr(
                "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
                lambda request: False,
            )
            resp = await client.get(f"/api/members/{slug}/briefing?member=Pager-triage")
            assert resp.status in (401, 403, 404)
