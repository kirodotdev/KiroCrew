"""``GET/PUT /api/config/default-template``: the template a session runs when nothing names one.

The route writes ``agent.default_agent`` and nothing else. The rules under test
are the ones that keep it from silently pointing every future plain session at
something that is not there: only an installed GLOBAL template qualifies; a
crewmate's private copy, a background-only runtime spec and an app's
materialized agent are refused with their own codes; an unknown name is 404; the
write is owner-only and goes through the locked config read-modify-write so no
sibling setting is reverted.
"""

from __future__ import annotations

import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import agent_state
from kiro_crew.agent_files import LITE_AGENT_FILENAME
from kiro_crew.config.loader import KiroCrewConfig, config_local_path, config_path
from kiro_crew.dashboard.handlers import agent_templates
from kiro_crew.dashboard.routes import agents as agents_routes

pytestmark = pytest.mark.asyncio

#: AWS's documented EXAMPLE key id, the probe every roster-mask test uses; it
#: trips the mask and is recognised by the content scan as the sample it is.
PROBE = "AKIAIOSFODNN7EXAMPLE"


class _FakeState:
    """No owner configured: only the signed local bootstrap subjects pass."""

    owner_id = ""

    async def read_folders(self, read):
        # The roster snapshots the chat-folder store for ``used_by``; none here.
        return read([])


def _build_app() -> web.Application:
    @web.middleware
    async def _identity(request, handler):
        request["user"] = request.headers.get("X-Test-User", "local-app")
        request["app"] = request.headers.get("X-Test-App", "")
        return await handler(request)

    app = web.Application(middlewares=[_identity])
    app["state"] = _FakeState()
    agents_routes.register(app)
    return app


@pytest.fixture(autouse=True)
def _owner_caller(_floor_monkeypatch):
    """Past the owner boundary by default; the 403 case flips it off."""
    _floor_monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
        lambda request: True,
    )


@pytest.fixture
def agents_dir(tmp_path, monkeypatch):
    d = tmp_path / "agents"
    d.mkdir()
    monkeypatch.setattr("kiro_crew.agent.KIRO_AGENTS_DIR", d)
    return d


def _write(agents_dir, filename: str, **spec) -> None:
    data = {"name": filename.rsplit(".", 1)[0], "tools": ["fs_read"], **spec}
    (agents_dir / filename).write_text(json.dumps(data), encoding="utf-8")


def _seed_config(default_template: str = "", **extra) -> None:
    cfg = KiroCrewConfig()
    cfg.agent.default_agent = default_template
    cfg.save()
    if extra:
        doc = json.loads(config_path().read_text(encoding="utf-8"))
        doc.update(extra)
        config_path().write_text(json.dumps(doc), encoding="utf-8")


def _stored() -> dict:
    return json.loads(config_path().read_text(encoding="utf-8"))


async def _put(client: TestClient, template, **headers):
    return await client.put(
        "/api/config/default-template", json={"template": template}, headers=headers
    )


async def test_get_reports_stored_and_effective(agents_dir):
    """Unset stores "" but STARTS the runtime template; the picker needs both."""
    _seed_config()
    async with TestClient(TestServer(_build_app())) as client:
        resp = await client.get("/api/config/default-template")
        assert resp.status == 200
        assert await resp.json() == {
            "default_template": "",
            "effective": "kirocrew",
            "overridden": False,
        }

        _seed_config("reviewer")
        resp = await client.get("/api/config/default-template")
        assert await resp.json() == {
            "default_template": "reviewer",
            "effective": "reviewer",
            "overridden": False,
        }


async def test_put_writes_agent_default_agent_and_nothing_else(agents_dir):
    _write(agents_dir, "reviewer.json")
    # A crewmate default that is NOT the template being picked, so the two
    # settings can be told apart in the written document.
    _seed_config(
        sidecar_setting={"kept": True},
        agents={"pr-bot": {"kiro_agent": "reviewer"}},
        default_agent="pr-bot",
    )
    async with TestClient(TestServer(_build_app())) as client:
        resp = await _put(client, "reviewer")
        assert resp.status == 200
        assert await resp.json() == {
            "ok": True,
            "default_template": "reviewer",
            "effective": "reviewer",
        }
    doc = _stored()
    assert doc["agent"]["default_agent"] == "reviewer"
    # A locked read-modify-write, not a whole-document publish: the unrelated
    # key written beside it survives.
    assert doc["sidecar_setting"] == {"kept": True}
    # The CREWMATE default is a different setting and is untouched.
    assert doc["default_agent"] == "pr-bot"


async def test_put_accepts_a_package_template(agents_dir):
    """A package template is read-only to EDIT, but every session start can reach it."""
    _write(agents_dir, "SomePkg-atlas.json", name="atlas")
    _seed_config()
    async with TestClient(TestServer(_build_app())) as client:
        resp = await _put(client, "atlas")
        assert resp.status == 200
    assert _stored()["agent"]["default_agent"] == "atlas"


async def test_put_empty_clears_back_to_the_runtime_default(agents_dir):
    _seed_config("reviewer")
    async with TestClient(TestServer(_build_app())) as client:
        resp = await _put(client, "")
        assert resp.status == 200
        assert await resp.json() == {"ok": True, "default_template": "", "effective": "kirocrew"}
    assert _stored()["agent"]["default_agent"] == ""


async def test_put_unknown_template_is_404_and_writes_nothing(agents_dir):
    _write(agents_dir, "reviewer.json")
    _seed_config("reviewer")
    async with TestClient(TestServer(_build_app())) as client:
        resp = await _put(client, "ghost")
        assert resp.status == 404
        assert (await resp.json())["code"] == "template_not_found"
    assert _stored()["agent"]["default_agent"] == "reviewer"


async def test_put_rechecks_eligibility_under_the_spec_lock(agents_dir, monkeypatch):
    """The ONE eligibility scan runs inside the locked write, under the spec lock.

    A spec deleted between the request arriving and the lock being taken must
    never become a dangling default: the scan sees the directory as it is at
    commit time, and nothing is written.
    """
    _write(agents_dir, "reviewer.json")
    _seed_config("reviewer", sidecar_setting={"kept": True})
    real_list_agents = agent_templates.list_agents
    seen: list[bool] = []

    def _scan_after_delete(*args, **kwargs):
        # Stands in for a DELETE that won the spec lock just before this scan.
        (agents_dir / "reviewer.json").unlink(missing_ok=True)
        seen.append(agent_templates.agents_spec_lock is not None)
        return real_list_agents(*args, **kwargs)

    monkeypatch.setattr(agent_templates, "list_agents", _scan_after_delete)
    async with TestClient(TestServer(_build_app())) as client:
        resp = await _put(client, "reviewer")
        assert resp.status == 404
        assert (await resp.json())["code"] == "template_not_found"
    # One scan, not a pre-lock pass plus a locked re-check.
    assert len(seen) == 1
    assert _stored()["agent"]["default_agent"] == "reviewer"
    assert _stored()["sidecar_setting"] == {"kept": True}


async def test_put_refuses_a_default_pinned_by_the_local_overlay(agents_dir):
    _write(agents_dir, "reviewer.json")
    _write(agents_dir, "writer.json")
    _seed_config("reviewer", sidecar_setting={"kept": True})
    config_local_path().write_text(
        json.dumps({"agent": {"default_agent": "local-reviewer"}}), encoding="utf-8"
    )

    async with TestClient(TestServer(_build_app())) as client:
        resp = await _put(client, "writer")
        assert resp.status == 409
        body = await resp.json()
        assert body["code"] == "default_template_overridden_by_local"
        assert body["override_path"] == str(config_local_path())
        # The overlay value is operator-authored text that never went through
        # the roster mask, so the error names the FILE, not the value.
        assert "config.local.json" in body["error"]
        assert "local-reviewer" not in body["error"]

    assert _stored()["agent"]["default_agent"] == "reviewer"
    assert _stored()["sidecar_setting"] == {"kept": True}


@pytest.mark.parametrize("local_default", ["local-reviewer", ""])
async def test_get_reports_a_string_local_override_to_the_owner(agents_dir, local_default):
    """Every string pin is reported before a pick, including the empty string."""
    _seed_config("reviewer")
    config_local_path().write_text(
        json.dumps({"agent": {"default_agent": local_default}}), encoding="utf-8"
    )
    async with TestClient(TestServer(_build_app())) as client:
        resp = await client.get("/api/config/default-template")
        assert resp.status == 200
        body = await resp.json()
    assert body == {
        "default_template": local_default,
        "effective": local_default or "kirocrew",
        "overridden": True,
        "override_path": str(config_local_path()),
    }


async def test_get_hides_the_local_override_path_from_a_non_owner(agents_dir, monkeypatch):
    _seed_config("reviewer")
    config_local_path().write_text(
        json.dumps({"agent": {"default_agent": "local-reviewer"}}), encoding="utf-8"
    )
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
        lambda request: False,
    )
    async with TestClient(TestServer(_build_app())) as client:
        resp = await client.get("/api/config/default-template")
        assert resp.status == 200
        body = await resp.json()
    assert body == {
        "default_template": "local-reviewer",
        "effective": "local-reviewer",
        "overridden": True,
    }


@pytest.mark.parametrize(
    "overlay_text",
    [json.dumps({"agent": {"default_agent": 7}}), "{"],
)
async def test_get_treats_non_string_or_malformed_local_values_as_unpinned(
    agents_dir, overlay_text
):
    _seed_config("reviewer")
    config_local_path().write_text(overlay_text, encoding="utf-8")
    async with TestClient(TestServer(_build_app())) as client:
        resp = await client.get("/api/config/default-template")
        assert resp.status == 200
        body = await resp.json()
    assert body["overridden"] is False
    assert "override_path" not in body


async def test_put_refuses_a_private_copy(agents_dir):
    """A crewmate's copy is deleted by that crewmate's publish/reset cleanup."""
    _write(agents_dir, "pr-bot-copy.json")
    agent_state.set_fork_info("pr-bot-copy", forked_from="reviewer", private_to="pr-bot")
    _seed_config()
    async with TestClient(TestServer(_build_app())) as client:
        resp = await _put(client, "pr-bot-copy")
        assert resp.status == 409
        body = await resp.json()
        assert body["code"] == "template_private_copy"
        assert "pr-bot" in body["error"]
    assert _stored()["agent"]["default_agent"] == ""


async def test_put_refuses_a_background_only_runtime_spec(agents_dir):
    _write(agents_dir, LITE_AGENT_FILENAME)
    _seed_config()
    async with TestClient(TestServer(_build_app())) as client:
        resp = await _put(client, LITE_AGENT_FILENAME.rsplit(".", 1)[0])
        assert resp.status == 409
        assert (await resp.json())["code"] == "template_background_only"


async def test_put_refuses_an_app_registered_agent(agents_dir):
    """``<app>--<agent>.json`` is unlinked when the app is disabled."""
    _write(agents_dir, "notes--scribe.json", name="scribe")
    _seed_config()
    async with TestClient(TestServer(_build_app())) as client:
        resp = await _put(client, "scribe")
        assert resp.status == 409
        assert (await resp.json())["code"] == "app_registered_template"


@pytest.mark.parametrize("bad", [["reviewer"], 7, None])
async def test_put_non_string_is_400(agents_dir, bad):
    _seed_config()
    async with TestClient(TestServer(_build_app())) as client:
        resp = await _put(client, bad)
        assert resp.status == 400
        assert (await resp.json())["code"] == "invalid_template_type"


@pytest.mark.parametrize("bad", ["../escape", "has space", "a" * 64])
async def test_put_malformed_name_is_400_before_any_scan(agents_dir, bad):
    _seed_config()
    async with TestClient(TestServer(_build_app())) as client:
        resp = await _put(client, bad)
        assert resp.status == 400
        assert (await resp.json())["code"] == "invalid_template_name"


async def test_put_is_owner_only(agents_dir, monkeypatch):
    """Refused before the body is read: a 403, not a 400 for the missing body."""
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
        lambda request: False,
    )
    _write(agents_dir, "reviewer.json")
    _seed_config()
    async with TestClient(TestServer(_build_app())) as client:
        resp = await client.put("/api/config/default-template")
        assert resp.status == 403
        assert (await resp.json())["code"] == "owner_only"
        # GET stays open: the picker's readout is not a mutation.
        resp = await client.get("/api/config/default-template")
        assert resp.status == 200
    assert _stored()["agent"]["default_agent"] == ""


async def test_roster_flags_which_rows_the_picker_may_offer(agents_dir):
    """The PUT's refusals, as one display flag per row, so the picker lists only what would land."""
    _write(agents_dir, "reviewer.json")
    _write(agents_dir, "pr-bot-copy.json")
    agent_state.set_fork_info("pr-bot-copy", forked_from="reviewer", private_to="pr-bot")
    _write(agents_dir, LITE_AGENT_FILENAME)
    _write(agents_dir, "notes--scribe.json", name="scribe")
    # A declared name the PUT's grammar refuses: listed, never storable.
    _write(agents_dir, "spaced.json", name="My Agent")
    _seed_config()
    async with TestClient(TestServer(_build_app())) as client:
        resp = await client.get("/api/agents/templates")
        assert resp.status == 200
        rows = {r["name"]: r["default_eligible"] for r in (await resp.json())["templates"]}
    assert rows["reviewer"] is True
    assert rows["pr-bot-copy"] is False
    assert rows[LITE_AGENT_FILENAME.rsplit(".", 1)[0]] is False
    assert rows["scribe"] is False
    assert rows["My Agent"] is False


async def test_get_masks_a_credential_shaped_stored_name(agents_dir):
    """A name the Slack ``!agent`` path persisted unvalidated never reaches the browser verbatim."""
    _seed_config(PROBE)
    async with TestClient(TestServer(_build_app())) as client:
        resp = await client.get("/api/config/default-template")
        assert resp.status == 200
        body = await resp.json()
    assert PROBE not in json.dumps(body)
    assert body["default_template"] == body["effective"]


async def test_put_refuses_an_empty_string_local_override_too(agents_dir):
    """An empty overlay value still wins the deep merge (it pins the runtime default)."""
    _write(agents_dir, "reviewer.json")
    _seed_config("")
    config_local_path().write_text(json.dumps({"agent": {"default_agent": ""}}), encoding="utf-8")
    async with TestClient(TestServer(_build_app())) as client:
        resp = await _put(client, "reviewer")
        assert resp.status == 409
        assert (await resp.json())["code"] == "default_template_overridden_by_local"
    assert _stored()["agent"]["default_agent"] == ""


async def test_private_copy_refusal_masks_the_owning_crew_name(agents_dir):
    """A crew name the fork path stored verbatim never reaches the browser verbatim."""
    _write(agents_dir, "pr-bot-copy.json")
    agent_state.set_fork_info("pr-bot-copy", forked_from="reviewer", private_to=PROBE)
    _seed_config()
    async with TestClient(TestServer(_build_app())) as client:
        resp = await _put(client, "pr-bot-copy")
        assert resp.status == 409
        body = await resp.json()
    assert body["code"] == "template_private_copy"
    assert PROBE not in json.dumps(body)
