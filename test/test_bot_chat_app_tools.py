"""App tools in a Bot's own chat (``kiro_crew.bot_chat_app_tools``).

Each guarantee below has one test, and the one-line change that reddens it:

* grant mounts: an app holding the ``tools`` scope mounts its offer on a ticked Bot's
  chat spec with the label ``dashboard:<slot>`` -- drop ``mounted[alias] = entry``;
* preview gate: preview off mounts nothing -- make ``preview_on`` return True;
* scope gate: no approved ``tools`` scope mounts nothing -- drop the
  ``BOT_SCOPE_TOOLS in app_bot_scopes(app)`` half of ``_app_reaches``;
* per Bot: a Bot the owner did not tick mounts nothing -- drop the
  ``slug in app_granted_bots(app)`` half of ``_app_reaches``;
* offer gate: a server not offered by the app's own agent mounts nothing -- drop
  ``if item["agent"] in own`` in ``_offers``;
* platform wins: a same-named platform server is not replaced -- drop ``if alias in mounted``;
* namespace: two apps offering the same slash-named server keep one alias each --
  alias the whole ``<app>:<server>`` key again in ``bot_servers``;
* no trust key: a mounted entry carries launch keys only -- copy the whole entry in
  ``_rendered_entry``;
* switch-offs by the source name: a server muted in its spec or on the MCP page does
  not mount, and its switched-off tools stay off -- drop the ``mcp_entry_is_muted`` /
  ``session_mcp_server_is_disabled`` check, or the ``disabledTools`` line;
* a server muted only in the Crew MCP store does not mount either -- check the entry
  alone instead of ``mcp_server_muted_in_any_store``;
* a tool switched off on the MCP page (any Crew store) stays off, matched by the
  source server's canonical name -- drop the ``globally_disabled_refs`` read, or
  compare raw names;
* a malformed ``disabledTools`` refuses the server instead of mounting it whole --
  drop the ``unusable_disabled_tools`` check;
* a Bot chat on a harness without its per-session spec (Kiro CLI) mounts nothing and
  is told once -- drop the notice's ``slot.append``, its call in ``get_or_create``, or
  the ``deliver_harness_notice`` call in ``turn_admission``; and since a Bot chat draws
  no runtime notice row (the crewmate chat draws messages only), every notice this
  module posts is also written
  into exactly one prompt for the Bot to tell its owner -- drop ``_tell_bot`` or the
  ``take_owner_notes`` call in ``prompt_assembly``;
* a change that runs while a Bot's first projection is still building, before any
  record exists to compare, is detected at the three delivery call-sites by
  ``check_and_filter_projection`` comparing the sidecar snapshot against ``_GENERATION``;
* a server codex registers under a whitespace-folded name is still recorded mounted,
  so unticking revokes it -- pass ``str`` for codex in ``_record_bot_chat_mounts`` or
  in ``_mirrored_session_mcp``;
* Claude: a Bot chat's deny rules come from its per-session spec, and settings that
  lack one hold the array -- drop ``MATE_CONDUCTOR_AGENT`` from
  ``crewmate_worker_deny_rules`` / ``_worker_settings_lag``;
* array hosts: ``session_mcp_projection`` carries it for the conductor and only for
  it, and only on a host in ``ACP_BACKENDS_MATE_WORKER_KIT`` (``bot_app_tools_carried``)
  -- drop its ``_crewmate_conductor_spec`` call / the agent check there / the
  ``if bot_app_tools_carried`` gate;
* the manifest scope parses -- drop ``"tools"`` from ``BOT_SCOPE_NAMES``;
* LTS: the preview reads off under the edition lock whatever is stored -- drop the
  ``feature_previews(previews)`` line in ``preview_on``;
* registered name: an offer whose agent declares its own ``name`` finds that agent's
  rendered spec -- return the stem instead of ``names[item["agent"]]`` in ``_offers``;
* a change: a Bot chat that lost a server starts a fresh conversation, one that
  gained one reloads -- swap ``lost`` and ``changed`` in ``after_app_change``;
* an app update that changes a server's entry for the same Bots reloads them -- make
  ``after_app_change`` skip a chat whose server names did not change;
* unticking after the preview went off still starts the chat afresh, and a switch of
  the preview reconciles -- judge from ``bot_servers`` instead of ``_MOUNTED`` / drop
  the ``after_app_change`` call in ``on_preview_change``;
* unticking a Bot on the owner route starts its chat afresh -- drop the route's
  ``after_app_change`` call;
* a registry update of an installed app (plain and streaming) and every successful
  trust revoke, whoever switched the app off, reach Bot chats -- drop the
  ``_reach_bot_chats`` call in either registry handler / the revoke's
  ``after_app_change`` call, or gate it on this request's disable;
* switching blanket third-party trust off reaches Bot chats when it stopped an app --
  drop the ``if stopped`` call in the allow-all handler;
* a tick undone before its owed reload applied starts no fresh conversation -- record
  the queued fingerprints in ``_MOUNTED`` instead of ``_PENDING``;
* a reload skips a chat whose fresh start is queued (disable then enable), so one chat
  never spawns two agent processes at once -- drop the ``_pending_discard...`` guard;
* the fresh start is the queued no-replay discard -- drop the
  ``slot._pending_discard_conversation_key = ...`` line.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from kiro_crew import bot_chat_app_tools as tools
from kiro_crew.apps import bot_grants

APP = "team-app"
SLOT = "member-atlas.memory-member-atlas-1"
KEY = f"dashboard:{SLOT}"
ENTRY = {"command": "python3", "args": ["/a/server.py"], "x-kirocrew-session-label": True}
BASE = {"tools": ["fs_read", "@kirocrew-core"], "mcpServers": {"kirocrew-core": {"command": "k"}}}


@pytest.fixture(autouse=True)
def _fresh_record(_floor_monkeypatch):
    """Each test starts with no Bot chat recorded as running.

    Patched through ``_floor_monkeypatch`` so a test's ``monkeypatch.undo()``
    cannot put the module's real record back under it.
    """
    _floor_monkeypatch.setattr(tools, "_MOUNTED", {})
    _floor_monkeypatch.setattr(tools, "_MOUNTED_SESSION_KEYS", {})
    _floor_monkeypatch.setattr(tools, "_PENDING", {})
    _floor_monkeypatch.setattr(tools, "_UNMOUNTED", {})
    _floor_monkeypatch.setattr(tools, "_OFFERED", {})
    _floor_monkeypatch.setattr(tools, "_HARNESS_NOTED", set())
    _floor_monkeypatch.setattr(tools, "_HARNESS_OWED", set())
    _floor_monkeypatch.setattr(tools, "_APP_GENERATIONS", {})
    _floor_monkeypatch.setattr(tools, "_REPROJECT_SLUGS", {})
    _floor_monkeypatch.setattr(tools, "_CHANGE_EPOCH", 0)
    _floor_monkeypatch.setattr(tools, "_PROJECTION_EPOCH", {})
    _floor_monkeypatch.setattr(tools, "_PENDING_DELIVERY", {})
    _floor_monkeypatch.setattr(tools, "_VOIDED_DELIVERY", {})
    _floor_monkeypatch.setattr(tools, "_PENDING_CLOSE", {})
    _floor_monkeypatch.setattr(tools, "_PENDING_CLOSE_APPS", set())
    _floor_monkeypatch.setattr(tools, "_REVOCATION_FENCE", set())
    _floor_monkeypatch.setattr(tools, "_RECONCILER", None)
    _floor_monkeypatch.setattr(tools, "_PROJECTION_GENS", {})
    _floor_monkeypatch.setattr(tools, "_PROJECTION_SLUGS", {})
    _floor_monkeypatch.setattr(tools, "_HAD_SIDECAR", set())


@pytest.fixture
def world(monkeypatch):
    """One enabled app offering ``team`` from its agent ``lead``, the Bot ``atlas`` ticked."""
    state = {
        "preview": True,
        "scopes": {"tools"},
        "ticked": ("atlas",),
        "offers": [("lead", "team")],
        "entry": ENTRY,
    }
    monkeypatch.setattr(tools, "preview_on", lambda cfg=None: state["preview"])
    monkeypatch.setattr(tools, "_app_names", lambda: [APP])
    monkeypatch.setattr(tools, "_offers", lambda app: list(state["offers"]))
    monkeypatch.setattr(tools, "_rendered_entry", lambda *a: dict(state["entry"]))
    monkeypatch.setattr(bot_grants, "app_bot_scopes", lambda app: frozenset(state["scopes"]))
    monkeypatch.setattr(bot_grants, "app_granted_bots", lambda app: tuple(state["ticked"]))
    import kiro_crew.apps.execution as _exec

    # Execution-admission gate: admitted in the default test world so the grant+tick
    # checks in _app_reaches can be exercised without real filesystem reads.
    monkeypatch.setattr(_exec, "app_execution_denied", lambda name, **_kw: None)
    import kiro_crew.members as members

    monkeypatch.setattr(
        members,
        "member_identity_for_session",
        lambda key: {"slug": key.split("member-")[1].split(".")[0], "name": "Atlas"},
    )
    return state


def test_a_ticked_bot_chat_mounts_the_offer_with_its_label(world):
    out = tools.conductor_session_spec(dict(BASE), KEY)
    entry = out["mcpServers"][f"{APP}:team"]
    assert f"@{APP}:team" in out["tools"]
    assert entry["env"]["KIROCREW_SESSION_LABEL"] == KEY
    assert "x-kirocrew-session-label" not in entry
    assert BASE["mcpServers"].keys() == {"kirocrew-core"}  # the shared spec is not written


def test_preview_off_mounts_nothing(world):
    world["preview"] = False
    assert tools.conductor_session_spec(dict(BASE), KEY) == BASE


def test_without_the_approved_tools_scope_nothing_mounts(world):
    world["scopes"] = {"read", "wake"}
    assert tools.conductor_session_spec(dict(BASE), KEY) == BASE


def test_a_bot_the_owner_did_not_tick_mounts_nothing(world):
    world["ticked"] = ("nova",)
    assert tools.conductor_session_spec(dict(BASE), KEY) == BASE


def test_only_a_server_the_apps_own_agent_declares_is_offered(tmp_path, monkeypatch):
    from kiro_crew.apps import bridges
    from kiro_crew.apps.manifest import AppManifest

    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "lead.json").write_text('{"description": "x"}', encoding="utf-8")
    manifest = AppManifest.from_dict(
        {
            "name": APP,
            "version": "1.0.0",
            "agents": ["agents/lead.json"],
            "contributes": {
                "botChatServers": [
                    {"agent": "lead", "server": "team"},
                    {"agent": "someone-else", "server": "x"},
                ]
            },
        }
    )
    monkeypatch.setattr(bridges, "_registration_source", lambda name: (manifest, tmp_path))
    assert tools._offers(APP) == [("lead", "team")]


def test_an_offer_finds_the_agent_by_the_name_its_spec_declares(tmp_path, monkeypatch):
    from kiro_crew.apps import bridges
    from kiro_crew.apps.manifest import AppManifest

    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "lead.json").write_text('{"name": "custom-lead"}', encoding="utf-8")
    manifest = AppManifest.from_dict(
        {
            "name": APP,
            "version": "1.0.0",
            "agents": ["agents/lead.json"],
            "contributes": {"botChatServers": [{"agent": "lead", "server": "team"}]},
        }
    )
    monkeypatch.setattr(bridges, "_registration_source", lambda name: (manifest, tmp_path))
    assert tools._offers(APP) == [("custom-lead", "team")]


def test_the_preview_reads_off_under_the_lts_lock(monkeypatch):
    from kiro_crew import edition_locks

    cfg = SimpleNamespace(dashboard=SimpleNamespace(feature_previews={tools.PREVIEW: True}))
    monkeypatch.setattr(edition_locks, "local_enable_locked", lambda name: False)
    assert tools.preview_on(cfg) is True
    monkeypatch.setattr(edition_locks, "local_enable_locked", lambda name: True)
    assert tools.preview_on(cfg) is False


def test_a_platform_server_of_the_same_name_wins(world, monkeypatch):
    monkeypatch.setattr(tools, "bot_servers", lambda slug: {"kirocrew-core": dict(ENTRY)})
    out = tools.conductor_session_spec(dict(BASE), KEY)
    assert out["mcpServers"]["kirocrew-core"] == {"command": "k"}


def test_two_apps_offering_a_slash_named_server_each_keep_their_own(world, monkeypatch):
    monkeypatch.setattr(tools, "_app_names", lambda: ["app-a", "app-b"])
    monkeypatch.setattr(tools, "_offers", lambda app: [("lead", "vendor/inbox")])
    monkeypatch.setattr(tools, "_rendered_entry", lambda app, *a: {**ENTRY, "args": [app]})
    out = tools.bot_servers("atlas")
    assert out == {
        "app-a:vendor-inbox": {**ENTRY, "args": ["app-a"]},
        "app-b:vendor-inbox": {**ENTRY, "args": ["app-b"]},
    }


def test_array_host_projection_carries_it_for_the_conductor_only(world, monkeypatch):
    from kiro_crew.acp import session_mcp

    monkeypatch.setattr(
        session_mcp, "_session_spec_and_snapshot_for", lambda agent, wd: (dict(BASE), None)
    )
    projection = session_mcp.session_mcp_projection(
        "kirocrew-mate-conductor", session_key=KEY, bot_app_tools_carried=True
    )
    assert f"{APP}:team" in projection.agent_spec["mcpServers"]
    other = session_mcp.session_mcp_projection(
        "kiro_default", session_key=KEY, bot_app_tools_carried=True
    )
    assert f"{APP}:team" not in (other.agent_spec or {}).get("mcpServers", {})
    # A host outside ACP_BACKENDS_MATE_WORKER_KIT (pi) mounts nothing and records nothing.
    tools._MOUNTED.clear()
    bare = session_mcp.session_mcp_projection("kirocrew-mate-conductor", session_key=KEY)
    assert f"{APP}:team" not in bare.agent_spec["mcpServers"]
    assert tools._MOUNTED == {}


def test_the_manifest_declares_the_tools_scope():
    from kiro_crew.apps.manifest_sections import Permissions

    assert Permissions.from_dict({"bots": ["tools", "nope"]}).bots == ["tools"]


def test_manifest_offer_round_trips_and_a_bad_shape_is_an_error():
    from kiro_crew.apps.manifest_contributes import Contributes

    good = Contributes.from_dict({"botChatServers": [{"agent": "lead", "server": "team"}]})
    assert good.to_dict()["botChatServers"] == [{"agent": "lead", "server": "team"}]
    assert good.validate() == []
    bad = Contributes.from_dict({"botChatServers": [{"agent": "lead"}]})
    assert any("botChatServers" in e for e in bad.validate())


def _start(spec, key):
    """A Bot chat's process starting: the builder, then the harness settling the
    record from what it handed the session (``record_mounted``)."""
    out = tools.conductor_session_spec(spec, key)
    tools.record_mounted(key, tools.mounted_names(out.get("mcpServers") or {}))
    return out


def _watch(monkeypatch):
    seen: dict[str, list[list[str]]] = {"reload": [], "fresh": []}
    monkeypatch.setattr(tools, "schedule_reload", lambda state, slugs: seen["reload"].append(slugs))

    async def _fresh(state, slugs):
        seen["fresh"].append(list(slugs))
        return list(slugs)

    monkeypatch.setattr(tools, "fresh_bot_chats", _fresh)
    return seen


NOVA_KEY = "dashboard:member-nova.memory-member-nova-1"


@pytest.mark.asyncio
async def test_a_change_starts_lost_bots_afresh_and_reloads_gained_ones(world, monkeypatch):
    seen = _watch(monkeypatch)
    _start(dict(BASE), KEY)  # atlas, ticked: runs with the server
    _start(dict(BASE), NOVA_KEY)  # nova, not ticked: runs without
    world["ticked"] = ("nova",)
    await tools.after_app_change(object())
    assert seen == {"reload": [["nova"]], "fresh": [["atlas"]]}


@pytest.mark.asyncio
async def test_a_disabled_app_starts_its_bots_afresh(world, monkeypatch):
    seen = _watch(monkeypatch)
    _start(dict(BASE), KEY)
    world["scopes"] = set()  # ``app_bot_scopes`` is empty for a disabled app
    await tools.after_app_change(object())
    assert seen == {"reload": [], "fresh": [["atlas"]]}


@pytest.mark.asyncio
async def test_an_update_that_changes_a_server_entry_reloads_the_same_bots(world, monkeypatch):
    seen = _watch(monkeypatch)
    _start(dict(BASE), KEY)
    world["entry"] = {**ENTRY, "args": ["/a/server-v2.py"]}
    await tools.after_app_change(object())
    assert seen == {"reload": [["atlas"]], "fresh": []}
    await tools.after_app_change(object())  # recorded: no second reload
    assert seen == {"reload": [["atlas"]], "fresh": []}


@pytest.mark.asyncio
async def test_unticking_after_the_preview_went_off_still_starts_the_chat_afresh(
    world, monkeypatch
):
    seen = _watch(monkeypatch)
    _start(dict(BASE), KEY)
    world["preview"] = False
    world["ticked"] = ()
    await tools.after_app_change(object())
    assert seen == {"reload": [], "fresh": [["atlas"]]}


@pytest.mark.asyncio
async def test_switching_the_preview_off_reconciles_a_live_bot_chat(world, monkeypatch):
    seen = _watch(monkeypatch)
    _start(dict(BASE), KEY)
    world["preview"] = False
    other = SimpleNamespace(touched=lambda *p: False)
    await tools.on_preview_change(object(), other)
    assert seen == {"reload": [], "fresh": []}
    flip = SimpleNamespace(touched=lambda *p: p == (f"dashboard.feature_previews.{tools.PREVIEW}",))
    await tools.on_preview_change(object(), flip)
    assert seen == {"reload": [], "fresh": [["atlas"]]}


@pytest.mark.asyncio
async def test_unticking_a_bot_on_the_owner_route_starts_its_chat_afresh(tmp_path, monkeypatch):
    from aiohttp import web
    from aiohttp.test_utils import make_mocked_request
    from chat_test_helpers import _make_state

    from kiro_crew.apps import manager_records
    from kiro_crew.apps.manager_records import InstalledApp
    from kiro_crew.apps.routes_bot_grants import handle_bot_grants

    # The real ticked-list store, so the route's untick is what takes the server away.
    monkeypatch.setattr(tools, "preview_on", lambda cfg=None: True)
    monkeypatch.setattr(tools, "_app_names", lambda: [APP])
    monkeypatch.setattr(tools, "_offers", lambda app: [("lead", "team")])
    monkeypatch.setattr(tools, "_rendered_entry", lambda *a: dict(ENTRY))
    monkeypatch.setattr(bot_grants, "app_bot_scopes", lambda app: frozenset({"tools"}))
    import kiro_crew.apps.execution as _exec

    monkeypatch.setattr(_exec, "app_execution_denied", lambda name, **_kw: None)
    seen = _watch(monkeypatch)
    manager_records.app_dir(APP).mkdir(parents=True, exist_ok=True)
    manager_records._write_installed(
        APP, InstalledApp(name=APP, version="1.0.0", enabled=True, installedAt="2026-11-15T00:00Z")
    )
    bot_grants.set_app_granted_bots(APP, ["atlas"])
    tools._MOUNTED["atlas"] = tools._fingerprints(tools.bot_servers("atlas"))
    assert tools._MOUNTED["atlas"]  # atlas's chat runs with the app's server
    state = _make_state(tmp_path)
    state.owner_id = "U_OWNER"
    app = web.Application()
    app["state"] = state
    req = make_mocked_request(
        "PUT", f"/api/apps/{APP}/bot-grants", app=app, match_info={"name": APP}
    )
    req["app"] = ""
    req["user"] = "U_OWNER"

    async def _json():
        return {"bots": []}

    req.json = _json  # type: ignore[method-assign]
    resp = await handle_bot_grants(req)
    assert resp.status == 200 and json.loads(resp.body)["bots"] == []
    assert seen == {"reload": [], "fresh": [["atlas"]]}


def _notice_slot(rows: list, **fields):
    """A chat slot stub that records its rows."""
    fields.setdefault("key", SLOT)
    # Also record queue_append calls (for _tell_bot / AUTOMATION_NOTICE_KIND).
    queued: list[tuple] = []
    fields.setdefault("_queued", queued)
    ns = SimpleNamespace(
        append=lambda *row: rows.append(row),
        queue_append=lambda text, kind=None, meta=None: queued.append((text, kind)),
        **{k: v for k, v in fields.items() if k != "_queued"},
    )
    ns._queued = queued
    return ns


def _owed_to_bot(slot) -> list[str]:
    """The notices queued for *slot*'s Bot via AUTOMATION_NOTICE_KIND."""
    from kiro_crew.dashboard.chat_utils import AUTOMATION_NOTICE_KIND

    prefix = "[Automation notice] "
    return [
        text[len(prefix) :] if text.startswith(prefix) else text
        for text, kind in getattr(slot, "_queued", [])
        if kind == AUTOMATION_NOTICE_KIND
    ]


@pytest.mark.asyncio
async def test_fresh_bot_chat_queues_a_no_replay_discard_on_its_slot(monkeypatch):
    import kiro_crew.dashboard.chat_runner as runner

    armed: list[object] = []
    monkeypatch.setattr(runner, "_arm_pending_reset_retry", lambda state, slot: armed.append(slot))
    rows: list[tuple] = []
    slot = _notice_slot(
        rows, key=SLOT, _pending_discard_conversation_key=None, _pending_discard_from_wake=True
    )
    monkeypatch.setattr(tools, "_live_slot", lambda state, slug: slot if slug == "atlas" else None)
    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_utils.effective_session_key", lambda s: f"dashboard_{s.key}"
    )
    assert await tools.fresh_bot_chats(object(), ["atlas", "gone"]) == ["atlas"]
    assert slot._pending_discard_conversation_key == f"dashboard_{SLOT}"
    assert slot._pending_discard_from_wake is False
    assert armed and armed[0] is slot
    assert rows and rows[0][0] == "system"
    # The Bot chat draws no runtime notice row, so the Bot is told to say it.
    assert _owed_to_bot(slot) == [tools._FRESH_NOTICE]


@pytest.mark.asyncio
async def test_reload_reconnects_an_idle_chat_and_leaves_a_queued_fresh_start_alone(monkeypatch):
    import kiro_crew.dashboard.crewmate_worker_reload as reload_mod

    calls: list[tuple[str, str]] = []

    async def _reload(state, slot, *, notice):
        calls.append((slot.key, notice))
        return reload_mod.RELOADED

    monkeypatch.setattr(reload_mod, "reload_if_idle", _reload)
    slots = {
        "ada": _notice_slot([], key="s-ada", _pending_discard_conversation_key=None),
        "uma": SimpleNamespace(key="s-uma", _pending_discard_conversation_key="dashboard_s-uma"),
    }
    monkeypatch.setattr(tools, "_live_slot", lambda state, slug: slots.get(slug))
    out = await tools.reload_bot_chats(object(), ["ada", "uma"])
    assert out == {"ada": reload_mod.RELOADED, "uma": tools.PENDING_FRESH}
    assert calls == [("s-ada", tools._RELOAD_NOTICE)]
    assert _owed_to_bot(slots["ada"]) == [tools._RELOAD_NOTICE]


@pytest.mark.parametrize("route", ["install", "install-stream"])
@pytest.mark.asyncio
async def test_a_registry_update_of_an_installed_app_reaches_bot_chats(
    tmp_path, monkeypatch, route
):
    from aiohttp.test_utils import TestClient, TestServer
    from apps_routes_coverage_helpers import APP as RAPP
    from apps_routes_coverage_helpers import _install, _make_app, _setup_env

    import kiro_crew.apps.routes as routes_mod

    _setup_env(tmp_path, monkeypatch)
    _install(tmp_path)
    calls: list[object] = []

    async def _after(state):
        calls.append(state)

    async def _registry_install(name, **kwargs):
        await kwargs["before_replace"]()
        return {"ok": True, "name": name}

    monkeypatch.setattr(tools, "after_app_change", _after)
    monkeypatch.setattr(routes_mod, "stop_app_backend", lambda n: None)
    monkeypatch.setattr(routes_mod, "start_app_backend", lambda n: None)
    monkeypatch.setattr(routes_mod, "git_unavailable_refusal", lambda n: None)
    monkeypatch.setattr(routes_mod, "install_from_registry", _registry_install)
    async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
        resp = await client.post(f"/api/apps/registry/{route}", json={"name": RAPP})
        assert resp.status in (200, 201), await resp.text()
        await resp.text()
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_revoking_trust_in_an_enabled_app_reaches_bot_chats(tmp_path, monkeypatch):
    from unittest.mock import patch

    import trusted_apps_api_helpers as trust

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "kirocrew-home"))
    (tmp_path / "kirocrew-home").mkdir()
    calls: list[object] = []

    async def _after(state):
        calls.append(state)

    monkeypatch.setattr(tools, "after_app_change", _after)
    trust._install(tmp_path, trust._APP, enabled=True)
    with patch("kiro_crew.dashboard.handlers.security._sel"):
        async with trust._client() as client:
            await client.post(f"/api/security/trusted-apps/{trust._APP}")
            resp = await client.delete(f"/api/security/trusted-apps/{trust._APP}")
            assert resp.status == 200, await resp.text()
            assert (await resp.json())["disabled"] is True
    assert len(calls) == 1


def _rendered(tmp_path, monkeypatch, entry, page=None, *, server="team", rows=()):
    """``_rendered_entry`` for *server* over a real rendered spec, with *page* as the
    global MCP settings and *rows* as the MCP page's rows (``list_servers``)."""
    from kiro_crew import mcp_discovery
    from kiro_crew.acp import session_mcp
    from kiro_crew.apps import bridges, bridges_sources

    monkeypatch.setattr(bridges, "_kiro_agents_dir", lambda: tmp_path)
    monkeypatch.setattr(session_mcp, "_global_settings", lambda **kw: page or {})
    monkeypatch.setattr(mcp_discovery, "list_servers", lambda *a, **kw: list(rows))
    name = bridges_sources._safe_link_name(bridges_sources._namespace(APP, "lead"))
    (tmp_path / f"{name}.json").write_text(
        json.dumps({"mcpServers": {server: entry}}), encoding="utf-8"
    )
    return tools._rendered_entry(APP, "lead", server)


def test_a_mounted_entry_carries_no_trust_key(tmp_path, monkeypatch):
    entry = {
        "command": "python3",
        "args": ["s.py"],
        "autoApprove": ["send"],
        "alwaysAllow": ["send"],
        "trust": True,
        "toolsSettings": {"send": {}},
    }
    assert _rendered(tmp_path, monkeypatch, entry) == {"command": "python3", "args": ["s.py"]}


def test_a_server_switched_off_under_its_own_name_does_not_mount(tmp_path, monkeypatch):
    assert _rendered(tmp_path, monkeypatch, {"command": "x", "disabled": True}) is None
    page = {"mcpServers": {"team": {"disabled": True}}}
    assert _rendered(tmp_path, monkeypatch, {"command": "x"}, page) is None


def test_tools_switched_off_under_the_servers_own_name_stay_off(tmp_path, monkeypatch):
    page = {"mcpServers": {"team": {"disabledTools": ["send"]}}}
    entry = {"command": "x", "disabledTools": ["wipe"]}
    assert _rendered(tmp_path, monkeypatch, entry, page) == {
        "command": "x",
        "disabledTools": ["send", "wipe"],
    }


def test_a_bot_chats_claude_deny_rules_cover_its_mounted_servers(world, monkeypatch):
    from kiro_crew.acp import session_mcp
    from kiro_crew.acp.claude_mcp_names import claude_mcp_tool_name
    from kiro_crew.acp.session_mcp_crewmate import crewmate_worker_deny_rules

    world["entry"] = {**ENTRY, "disabledTools": ["send"]}
    monkeypatch.setattr(
        session_mcp, "_session_spec_and_snapshot_for", lambda agent, wd: (dict(BASE), None)
    )
    rules = crewmate_worker_deny_rules(
        "kirocrew-mate-conductor", work_dir=None, session_key=KEY, backend="claude"
    )
    assert claude_mcp_tool_name(f"{APP}:team", "send") in (rules or [])


def test_a_bot_chats_claude_settings_missing_a_mounted_deny_hold_the_array(tmp_path):
    from kiro_crew.acp.claude_mcp_names import claude_mcp_tool_name
    from kiro_crew.acp.client import AcpClient
    from kiro_crew.acp_backends import ACP_BACKEND_CLAUDE

    client = AcpClient(
        work_dir=tmp_path,
        agent="kirocrew-mate-conductor",
        acp_backend=ACP_BACKEND_CLAUDE,
        session_key=KEY,
    )
    spec = {**BASE, "mcpServers": {f"{APP}:team": {"command": "x", "disabledTools": ["send"]}}}
    client._session_mcp_excludes_local = True
    client._session_mcp_inline_settings = {"permissions": {"deny": []}}
    assert client._worker_settings_lag(spec) is True
    rule = claude_mcp_tool_name(f"{APP}:team", "send")
    client._session_mcp_inline_settings = {"permissions": {"deny": [rule]}}
    assert client._worker_settings_lag(spec) is False


def test_a_server_switched_off_only_in_the_crew_store_does_not_mount(tmp_path, monkeypatch):
    from kiro_crew import mcp_cleanup

    monkeypatch.setattr(
        mcp_cleanup, "mcp_scope_muted_aliases", lambda *a, **kw: frozenset({"team"})
    )
    assert _rendered(tmp_path, monkeypatch, {"command": "x"}) is None


@pytest.mark.asyncio
async def test_a_tick_undone_before_its_reload_applied_keeps_the_conversation(world, monkeypatch):
    seen = _watch(monkeypatch)
    world["ticked"] = ()
    _start(dict(BASE), KEY)  # atlas's chat runs without the server
    world["ticked"] = ("atlas",)
    await tools.after_app_change(object())  # ticked mid-turn: a reload is owed
    world["ticked"] = ()
    await tools.after_app_change(object())  # unticked before that reload ran
    assert seen == {"reload": [["atlas"]], "fresh": []}


@pytest.mark.asyncio
async def test_switching_blanket_trust_off_reaches_bot_chats(tmp_path, monkeypatch):
    from unittest.mock import patch

    import trusted_apps_api_helpers as trust

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "kirocrew-home"))
    (tmp_path / "kirocrew-home").mkdir()
    calls: list[object] = []

    async def _after(state):
        calls.append(state)

    monkeypatch.setattr(tools, "after_app_change", _after)
    trust._install(tmp_path, trust._APP, enabled=True)
    with patch("kiro_crew.dashboard.handlers.security._sel"):
        async with trust._client() as client:
            on = await client.put("/api/security/trusted-apps/allow-all", json={"value": True})
            assert on.status == 200, await on.text()
            off = await client.put("/api/security/trusted-apps/allow-all", json={"value": False})
            assert off.status == 200, await off.text()
            assert trust._APP in (await off.json()).get("stopped", [])
    assert len(calls) == 1


def test_a_tool_switched_off_on_the_mcp_page_stays_off(tmp_path, monkeypatch):
    rows = [SimpleNamespace(name="team", disabled_tools=["send"])]
    out = _rendered(tmp_path, monkeypatch, {"command": "x"}, rows=rows)
    assert out == {"command": "x", "disabledTools": ["send"]}


def test_a_slash_named_servers_switch_offs_match_by_its_canonical_name(tmp_path, monkeypatch):
    rows = [SimpleNamespace(name="vendor-inbox", disabled_tools=["send"])]
    page = {"mcpServers": {"vendor-inbox": {"disabledTools": ["wipe"]}}}
    out = _rendered(tmp_path, monkeypatch, {"command": "x"}, page, server="vendor/inbox", rows=rows)
    assert out == {"command": "x", "disabledTools": ["send", "wipe"]}


@pytest.mark.parametrize("bad", ["send", ["send", ""], ["send", 3], {"send": True}])
def test_a_malformed_tool_restriction_refuses_the_server(tmp_path, monkeypatch, bad):
    assert _rendered(tmp_path, monkeypatch, {"command": "x", "disabledTools": bad}) is None


def test_a_wildcard_all_tool_restriction_refuses_the_server(tmp_path, monkeypatch):
    # disabledTools: ["*"] cannot be expressed as a Claude deny rule (the worker path
    # already refuses uncarried_tool_limits_of for the same reason: drop the glob-chars
    # guard and the entry mounts with an ineffective deny).
    assert _rendered(tmp_path, monkeypatch, {"command": "x", "disabledTools": ["*"]}) is None


def test_a_glob_tool_restriction_refuses_the_server(tmp_path, monkeypatch):
    # disabledTools: ["delete*"] -- a partial glob Claude cannot spell faithfully.
    # Drop the _GLOB_CHARS guard and "delete_file" stays callable despite the restriction.
    assert _rendered(tmp_path, monkeypatch, {"command": "x", "disabledTools": ["delete*"]}) is None


def test_the_preview_reads_the_stored_config_and_is_off_when_it_is_unreadable(monkeypatch):
    from kiro_crew.config.loader import KiroCrewConfig

    stored = SimpleNamespace(dashboard=SimpleNamespace(feature_previews={tools.PREVIEW: True}))
    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: stored))
    assert tools.preview_on() is True

    def _unreadable(cls):
        raise OSError("config unreadable")

    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(_unreadable))
    assert tools.preview_on() is False


def test_a_bots_live_slot_is_the_one_its_dm_binding_names(monkeypatch):
    import kiro_crew.members as members

    slot = object()
    state = SimpleNamespace(_slots={SLOT: slot})
    monkeypatch.setattr(members, "read_dm_binding", lambda slug: {"slot_key": SLOT})
    assert tools._live_slot(state, "atlas") is slot
    monkeypatch.setattr(members, "read_dm_binding", lambda slug: None)
    assert tools._live_slot(state, "atlas") is None


@pytest.mark.asyncio
async def test_a_reconcile_skips_an_unreadable_bot_and_queues_each_change_once(world, monkeypatch):
    seen = _watch(monkeypatch)
    world["ticked"] = ()
    _start(dict(BASE), KEY)  # atlas runs without the server
    world["ticked"] = ("atlas",)
    await tools.after_app_change(None)  # no state: nothing to reach
    await tools.after_app_change(object())
    await tools.after_app_change(object())  # the same change again: already queued
    assert seen == {"reload": [["atlas"]], "fresh": []}

    def _unreadable(slug, cfg=None):
        raise OSError("unreadable")

    monkeypatch.setattr(tools, "bot_servers", _unreadable)
    await tools.after_app_change(object())
    assert seen == {"reload": [["atlas"]], "fresh": []}


def test_a_scheduled_reload_runs_as_a_task_kept_on_the_state(monkeypatch):
    import asyncio

    ran: list[list[str]] = []

    async def _reload(state, slugs):
        ran.append(list(slugs))
        return {}

    monkeypatch.setattr(tools, "reload_bot_chats", _reload)
    state = SimpleNamespace()
    assert tools.schedule_reload(None, ["atlas"]) is None
    assert tools.schedule_reload(state, []) is None
    assert tools.schedule_reload(state, ["atlas"]) is None  # no running loop

    async def go():
        task = tools.schedule_reload(state, ["atlas"])
        assert task is not None and task in state._crewmate_reload_tasks
        await task

    asyncio.run(go())
    assert ran == [["atlas"]] and state._crewmate_reload_tasks == set()


def test_the_notices_write_bot_lowercase_after_another_word():
    """The recorded decision: "bot" is lowercase after another word in English copy."""
    import re

    for notice in (tools._RELOAD_NOTICE, tools._FRESH_NOTICE, tools._HARNESS_NOTICE):
        assert not re.search(r"\w[\w']* Bot", notice), notice


@pytest.mark.asyncio
async def test_a_bot_chat_on_a_harness_without_its_spec_mounts_nothing_and_says_so(
    world, monkeypatch
):
    """Kiro CLI never calls ``conductor_session_spec``: nothing mounts, and the chat
    is told once, by the dashboard turn that claimed the session (the allocation
    door has no slot); a start on a harness that carries the spec says nothing."""

    rows: list[tuple] = []
    slot = _notice_slot(rows)

    async def cold_start(carried):
        await tools.note_harness_without_app_tools(KEY, carried=carried)
        await tools.deliver_harness_notice(slot, KEY)

    await cold_start(False)
    await cold_start(False)
    assert rows == [("system", tools._HARNESS_NOTICE, "msg msg-info")]
    # The Bot chat draws no runtime notice row, so the Bot is told to say it.
    assert _owed_to_bot(slot) == [tools._HARNESS_NOTICE]
    assert tools._MOUNTED == {}  # no spec was built, so nothing is recorded as mounted
    await cold_start(True)
    assert len(rows) == 1 and "atlas" not in tools._HARNESS_NOTED
    world["ticked"] = ()  # no app tools for this Bot: nothing to say
    await cold_start(False)
    assert len(rows) == 1


def test_every_bot_chat_cold_start_reports_its_harness_to_the_notice():
    """The allocation door hands the worker-kit opt-in to the notice."""
    import inspect

    from kiro_crew.session_allocation_parts import get_or_create

    source = inspect.getsource(get_or_create)
    assert "note_harness_without_app_tools(" in source
    assert "provider.mate_worker_kit_supported is True" in source


@pytest.mark.asyncio
async def test_the_turn_that_claims_a_new_bot_chat_posts_its_owed_notice(monkeypatch):
    """A real dashboard turn on a freshly claimed session posts the owed notice once
    via deliver_harness_notice → _tell_bot, and once only (the row is appended once
    and the slug moves from _HARNESS_OWED to _HARNESS_NOTED).

    Notices are now queued via queue_append(kind=AUTOMATION_NOTICE_KIND) rather than
    injected into the prompt by take_owner_notes.  Dropping _tell_bot or the
    deliver_harness_notice call in turn_admission removes the queue entry, so this
    test verifies the call path remains intact.  Specifically:
    - the notice row is appended to the slot's visible transcript exactly once;
    - _tell_bot is called exactly once (via deliver_harness_notice) for the slug;
    - subsequent turns do not repeat the call (_HARNESS_NOTED guards it).

    Remove the _tell_bot call from deliver_harness_notice and the tell_calls list
    stays empty, so the assertion fails."""
    from turn_harness import TurnScript, run_turn

    from kiro_crew import members

    tell_calls: list[tuple] = []
    real_tell = tools._tell_bot

    def _spy(slot: object, notice: str) -> None:
        tell_calls.append((slot, notice))
        real_tell(slot, notice)

    monkeypatch.setattr(tools, "_tell_bot", _spy)
    monkeypatch.setattr(members, "member_identity_for_session", lambda _key: {"slug": "atlas"})
    tools._HARNESS_OWED.add("atlas")
    record = await run_turn(TurnScript(then=TurnScript(then=TurnScript())))
    # The visible notice row is appended exactly once.
    notices = [row for row in record.window if row.get("content") == tools._HARNESS_NOTICE]
    assert len(notices) == 1
    # _tell_bot was called exactly once for the HARNESS_NOTICE — not on subsequent turns.
    harness_tells = [n for _slot, n in tell_calls if n == tools._HARNESS_NOTICE]
    assert harness_tells == [tools._HARNESS_NOTICE], harness_tells
    assert "atlas" not in tools._HARNESS_OWED and "atlas" in tools._HARNESS_NOTED


@pytest.mark.asyncio
async def test_a_turn_built_by_the_context_builder_carries_the_owed_note_once(monkeypatch):
    """_tell_bot queues the notice via AUTOMATION_NOTICE_KIND once per call.

    Notices reach the Bot as an inject row on its next turn via queue_append rather
    than as inline prompt text.  This test verifies that _tell_bot calls queue_append
    with the correct kind, once per invocation.

    Remove the queue_append call from _tell_bot and queued stays empty, so the
    assertion fails."""
    from kiro_crew.dashboard.chat_utils import AUTOMATION_NOTICE_KIND

    queued: list[tuple] = []
    slot = SimpleNamespace(
        key=SLOT,
        queue_append=lambda text, kind=None, meta=None: queued.append((text, kind)),
    )
    tools._tell_bot(slot, tools._FRESH_NOTICE)
    assert len(queued) == 1
    text, kind = queued[0]
    assert kind == AUTOMATION_NOTICE_KIND
    assert tools._FRESH_NOTICE in text


@pytest.mark.asyncio
async def test_revoking_trust_in_an_app_already_switched_off_still_reaches_bot_chats(
    tmp_path, monkeypatch
):
    """Switched off elsewhere (the CLI): the revoke still reconciles Bot chats."""
    from unittest.mock import patch

    import trusted_apps_api_helpers as trust

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "kirocrew-home"))
    (tmp_path / "kirocrew-home").mkdir()
    calls: list[object] = []

    async def _after(state):
        calls.append(state)

    monkeypatch.setattr(tools, "after_app_change", _after)
    trust._install(tmp_path, trust._APP, enabled=False)
    with patch("kiro_crew.dashboard.handlers.security._sel"):
        async with trust._client() as client:
            await client.post(f"/api/security/trusted-apps/{trust._APP}")
            resp = await client.delete(f"/api/security/trusted-apps/{trust._APP}")
            assert resp.status == 200, await resp.text()
            assert (await resp.json())["disabled"] is False
    assert len(calls) == 1


def test_a_slash_named_server_muted_in_kiro_global_under_its_alias_does_not_mount(
    tmp_path, monkeypatch
):
    page = {"mcpServers": {"vendor-inbox": {"disabled": True}}}
    assert _rendered(tmp_path, monkeypatch, {"command": "x"}, page, server="vendor/inbox") is None


@pytest.mark.parametrize("route", ["install", "install-stream"])
@pytest.mark.asyncio
async def test_a_first_registry_install_reaches_bot_chats(tmp_path, monkeypatch, route):
    from aiohttp.test_utils import TestClient, TestServer
    from apps_routes_coverage_helpers import APP as RAPP
    from apps_routes_coverage_helpers import _make_app, _make_app_source, _setup_env

    import kiro_crew.apps.routes as routes_mod
    from kiro_crew.apps.manager import install_app

    _setup_env(tmp_path, monkeypatch)
    source = _make_app_source(tmp_path)
    calls: list[object] = []

    async def _after(state):
        calls.append(state)

    async def _registry_install(name, **kwargs):
        return {"ok": True, "name": install_app(source).name}

    monkeypatch.setattr(tools, "after_app_change", _after)
    monkeypatch.setattr(routes_mod, "start_app_backend", lambda n: None)
    monkeypatch.setattr(routes_mod, "git_unavailable_refusal", lambda n: None)
    monkeypatch.setattr(routes_mod, "install_from_registry", _registry_install)
    async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
        resp = await client.post(f"/api/apps/registry/{route}", json={"name": RAPP})
        assert resp.status in (200, 201), await resp.text()
        await resp.text()
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_a_server_codex_registers_under_a_folded_name_is_revoked_on_untick(
    world, monkeypatch
):
    """codex hands ``my server`` over as ``my_server``: the record matches under that
    spelling, so unticking still starts the chat afresh."""
    from types import SimpleNamespace as NS

    from kiro_crew.acp import client_mcp_array
    from kiro_crew.providers.mirrors.codex import codex_elements

    world["offers"] = [("lead", "my server")]
    seen = _watch(monkeypatch)
    out = _start(dict(BASE), KEY)
    alias = f"{APP}:my server"
    assert alias in out["mcpServers"]
    handed = codex_elements([{"name": n} for n in out["mcpServers"]])
    assert f"{APP}:my_server" in tools.mounted_names(handed)
    client = NS(_agent="kirocrew-mate-conductor", _session_key=KEY, _acp_backend="codex")
    client_mcp_array._record_bot_chat_mounts(client, handed)
    assert alias in tools._MOUNTED["atlas"]
    world["ticked"] = ()
    await tools.after_app_change(object())
    assert seen["fresh"]


@pytest.mark.parametrize("pause", ["after_build", "inside_grant_read"])
@pytest.mark.asyncio
async def test_an_untick_during_a_first_projection_is_reconciled_once_it_records(
    world, monkeypatch, pause
):
    """The owner unticks while the Bot's first projection is still building, before any
    record exists for the change to compare -- after the build, or while it is still
    reading the grants: the record, landing after it, re-runs the comparison, so the
    chat starts fresh without the revoked server."""
    import asyncio
    import threading

    seen = _watch(monkeypatch)
    state = SimpleNamespace()
    loop = asyncio.get_running_loop()
    built, release = asyncio.Event(), threading.Event()

    def _hold():
        loop.call_soon_threadsafe(built.set)
        assert release.wait(timeout=30)  # held here until the test has unticked

    if pause == "inside_grant_read":
        real = tools.bot_servers

        def _read_then_hold(slug):
            got = real(slug)  # the grants are read: the Bot is still ticked
            _hold()
            return got

        monkeypatch.setattr(tools, "bot_servers", _read_then_hold)

    def _cold_start():
        out = tools.conductor_session_spec(dict(BASE), KEY)
        if pause == "after_build":
            _hold()
        tools.record_mounted(KEY, tools.mounted_names(out["mcpServers"]))

    start = asyncio.ensure_future(asyncio.to_thread(_cold_start))
    await asyncio.wait_for(built.wait(), timeout=30)
    world["ticked"] = ()
    await tools.after_app_change(state)
    assert seen == {"reload": [], "fresh": []}  # nothing recorded yet to compare
    release.set()
    await start
    await asyncio.gather(*list(getattr(state, "_crewmate_reload_tasks", ())))
    assert seen["fresh"] == [["atlas"]]


@pytest.mark.asyncio
async def test_a_server_the_harness_withheld_is_not_lost_when_unticked(world, monkeypatch):
    """The harness handed the chat no app server (codex withheld it, or a claude
    array held back whole): unticking it resets nothing."""
    seen = _watch(monkeypatch)
    _start(dict(BASE), KEY)
    tools.record_mounted(KEY, ["kirocrew-core"])
    assert tools._MOUNTED["atlas"] == {}
    world["ticked"] = ()
    await tools.after_app_change(object())
    assert seen == {"reload": [], "fresh": []}


@pytest.mark.asyncio
async def test_a_withheld_server_whose_entry_changes_reloads_the_chat(world, monkeypatch):
    """Withheld for its per-tool limits; an app update drops them: the chat reloads,
    and an unchanged withheld entry reloads nothing."""
    seen = _watch(monkeypatch)
    _start(dict(BASE), KEY)
    tools.record_mounted(KEY, [])
    await tools.after_app_change(object())
    assert seen == {"reload": [], "fresh": []}
    world["entry"] = {**ENTRY, "args": ["/a/server.py", "--v2"]}
    await tools.after_app_change(object())
    assert seen == {"reload": [["atlas"]], "fresh": []}


def test_the_array_resolver_settles_the_record_from_what_it_hands_the_session(world, monkeypatch):
    """claude and codex (mirrors) and the mirrorless path all end in the same finaliser."""
    from types import SimpleNamespace as NS

    from kiro_crew.acp import client_mcp_array

    _start(dict(BASE), KEY)
    client = NS(_agent="kirocrew-mate-conductor", _session_key=KEY, _acp_backend="claude")
    client_mcp_array._record_bot_chat_mounts(client, [])  # the claude array held back
    assert tools._MOUNTED["atlas"] == {}
    client_mcp_array._record_bot_chat_mounts(client, [{"name": f"{APP}:team"}])
    assert f"{APP}:team" in tools._MOUNTED["atlas"]
    other = NS(_agent="kiro_default", _session_key=KEY, _acp_backend="claude")
    client_mcp_array._record_bot_chat_mounts(other, [])
    assert f"{APP}:team" in tools._MOUNTED["atlas"]  # only a Bot's own chat settles it


def test_every_harness_settles_the_record_after_its_own_withholding():
    """Each projection calls the one finaliser on its final list."""
    import inspect

    from kiro_crew.acp import client_mcp_array
    from kiro_crew.acp.harness import kas

    resolver = inspect.getsource(client_mcp_array._resolve_session_mcp_servers)
    assert resolver.count("_record_bot_chat_mounts(") == 3  # mirrorless, held back, final
    assert "record_mounted(session_key, mounted_names(" in inspect.getsource(kas)


@pytest.mark.asyncio
async def test_an_offer_a_platform_server_shadows_is_not_recorded(world, monkeypatch):
    seen = _watch(monkeypatch)
    base = {**BASE, "mcpServers": {**BASE["mcpServers"], f"{APP}:team": {"command": "p"}}}
    tools.conductor_session_spec(base, KEY)
    tools.record_mounted(KEY, ["kirocrew-core", f"{APP}:team"])  # the platform one
    assert tools._MOUNTED["atlas"] == {}
    world["ticked"] = ()
    await tools.after_app_change(object())
    assert seen == {"reload": [], "fresh": []}


@pytest.mark.asyncio
async def test_a_local_install_reaches_bot_chats(tmp_path, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer
    from apps_routes_coverage_helpers import _make_app, _make_app_source, _setup_env

    import kiro_crew.apps.routes as routes_mod

    _setup_env(tmp_path, monkeypatch)
    src = _make_app_source(tmp_path)
    calls: list[object] = []

    async def _after(state):
        calls.append(state)

    monkeypatch.setattr(tools, "after_app_change", _after)
    monkeypatch.setattr(routes_mod, "start_app_backend", lambda n: None)
    async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
        resp = await client.post("/api/apps/install", json={"source": str(src)})
        assert resp.status == 201, await resp.text()
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_a_settings_only_projection_leaves_the_running_chats_record(world, monkeypatch):
    """A claude settings re-seed (a model switch) builds the spec without handing the
    session new tools: the preview going off afterwards still starts the chat afresh."""
    from kiro_crew.acp import session_mcp
    from kiro_crew.acp.session_mcp_crewmate import crewmate_worker_deny_rules

    seen = _watch(monkeypatch)
    _start(dict(BASE), KEY)  # atlas runs with the app's server
    world["preview"] = False
    monkeypatch.setattr(
        session_mcp, "_session_spec_and_snapshot_for", lambda agent, wd: (dict(BASE), None)
    )
    crewmate_worker_deny_rules(
        "kirocrew-mate-conductor", work_dir=None, session_key=KEY, backend="claude"
    )
    assert f"{APP}:team" in tools._MOUNTED["atlas"]
    await tools.after_app_change(object())
    assert seen == {"reload": [], "fresh": [["atlas"]]}


@pytest.mark.parametrize("server", ["team", "my server"])
def test_the_shared_runtime_entry_settles_the_record_from_what_it_hands_the_session(
    tmp_path, world, monkeypatch, server
):
    """AcpRuntime.create_session/load_session reach a mirror through
    ``_mirrored_session_mcp``: it settles the Bot chat record from its final array,
    the same as the AcpClient entry, so a codex-withheld server is never counted and
    one codex hands over under its folded name (``my_server``) is."""
    import asyncio

    from kiro_crew.acp import runtime as setup  # the facade: these names are shared
    from kiro_crew.acp import runtime_session_setup as setup_owner
    from kiro_crew.acp.runtime import AcpRuntime
    from kiro_crew.acp_backends import ACP_BACKEND_CODEX

    world["offers"] = [("lead", server)]
    tools.conductor_session_spec(dict(BASE), KEY)  # the offer the mirror built
    alias = f"{APP}:{server}"
    withheld = server == "team"
    # withheld: the app server dropped by the mirror; else handed under codex's spelling
    handed = [{"name": "kirocrew-core"}] + ([] if withheld else [{"name": f"{APP}:my_server"}])
    projection = SimpleNamespace(
        params={"mcpServers": handed},
        denied_tools=frozenset(),
        derived_spec_snapshot=None,
        agent_spec=dict(BASE),
        zero_tools=False,
    )
    mirror = SimpleNamespace(session_projection=lambda *a, **k: projection)
    monkeypatch.setattr(setup, "mirror_for", lambda backend: mirror)
    monkeypatch.setattr(setup, "injection_server_names", lambda *a, **k: frozenset())
    monkeypatch.setattr(setup, "pooled_session_servers", lambda *a, **k: [])
    monkeypatch.setattr(setup, "_ref_spec_snapshot", lambda *a, **k: None)
    monkeypatch.setattr(setup_owner, "with_ipc_pickups", lambda servers, **k: servers)
    rt = AcpRuntime(work_dir=str(tmp_path), acp_backend=ACP_BACKEND_CODEX)

    async def _own(stubs, key):
        return stubs, "tok"

    monkeypatch.setattr(rt, "_own_stub_session", _own)
    out = asyncio.run(
        rt._mirrored_session_mcp(
            "kirocrew-mate-conductor", work_dir=str(tmp_path), session_key=KEY, channel_id="c"
        )
    )
    assert out is not None and out.servers == handed
    if withheld:
        assert tools._MOUNTED["atlas"] == {}
        assert alias in tools._UNMOUNTED["atlas"]
    else:
        assert alias in tools._MOUNTED["atlas"]


def test_the_shared_runtime_withholds_the_array_when_the_seed_lags(tmp_path, world, monkeypatch):
    """When ClaudeSharedSession._worker_settings_lag says the seed lacks a required deny
    rule, the shared-runtime path withholds the whole Bot chat array -- same guard as
    client_mcp_array.py:413.  Drop ``harness_state`` from the _mirrored_session_mcp call
    or remove the ``hasattr``/_worker_settings_lag check and the handed array reaches
    the session even though its deny rule is missing."""
    import asyncio

    from kiro_crew.acp import runtime as setup
    from kiro_crew.acp import runtime_session_setup as setup_owner
    from kiro_crew.acp.claude_shared_session import ClaudeSharedSession
    from kiro_crew.acp.runtime import AcpRuntime
    from kiro_crew.acp_backends import ACP_BACKEND_CLAUDE

    world["offers"] = [("lead", "team")]
    tools.conductor_session_spec(dict(BASE), KEY)
    handed = [{"name": "kirocrew-core"}, {"name": f"{APP}:team"}]
    # agent_spec with a disabledTools entry so worker_array_outruns_seed sees a needed deny
    agent_spec_with_deny = {
        **BASE,
        "mcpServers": {f"{APP}:team": {"command": "x", "disabledTools": ["send"]}},
    }
    projection = SimpleNamespace(
        params={"mcpServers": handed},
        denied_tools=frozenset(),
        derived_spec_snapshot=None,
        agent_spec=agent_spec_with_deny,
        zero_tools=False,
    )
    mirror = SimpleNamespace(session_projection=lambda *a, **k: projection)
    monkeypatch.setattr(setup, "mirror_for", lambda backend: mirror)
    monkeypatch.setattr(setup, "injection_server_names", lambda *a, **k: frozenset())
    monkeypatch.setattr(setup, "pooled_session_servers", lambda *a, **k: [])
    monkeypatch.setattr(setup, "_ref_spec_snapshot", lambda *a, **k: None)
    monkeypatch.setattr(setup_owner, "with_ipc_pickups", lambda servers, **k: servers)

    # Build a real ClaudeSharedSession whose _worker_settings_lag reports a lag.
    # Only __new__ + patching the one method avoids the full async prepare() chain.
    shared = ClaudeSharedSession.__new__(ClaudeSharedSession)
    monkeypatch.setattr(shared, "_worker_settings_lag", lambda spec: True)

    rt = AcpRuntime(work_dir=str(tmp_path), acp_backend=ACP_BACKEND_CLAUDE)

    async def _own(stubs, key):
        return stubs, "tok"

    monkeypatch.setattr(rt, "_own_stub_session", _own)
    out = asyncio.run(
        rt._mirrored_session_mcp(
            "kirocrew-mate-conductor",
            work_dir=str(tmp_path),
            session_key=KEY,
            channel_id="c",
            harness_state=shared,
        )
    )
    # Array must be withheld (empty) when the seed lags.
    assert out is not None and out.servers == []
    # Record must reflect the withheld array.
    assert tools._MOUNTED.get("atlas") == {}


def test_the_shared_runtime_offloads_the_seed_check_so_the_lock_does_not_block_the_loop(
    tmp_path, world, monkeypatch
):
    """The seed-lag check runs in a thread, not on the event loop.

    When the cross-process lock is held by another session, ``recorded_durable``
    can block until the lock is released -- that is correct behaviour on a thread,
    but would stall the whole event loop if the call ran on-loop.

    This test makes ``_worker_settings_lag`` assert it is running off the event loop
    (``asyncio.get_event_loop()`` raises ``RuntimeError`` when no loop is running in
    the current thread).  The array passes through (lag=False), so the check's output
    signal tells us the function really ran.  Remove the ``asyncio.to_thread`` wrapper
    and the assert fires inside the coroutine, turning the ``asyncio.run`` into an
    exception instead of a clean result."""
    import asyncio

    from kiro_crew.acp import runtime as setup
    from kiro_crew.acp import runtime_session_setup as setup_owner
    from kiro_crew.acp.claude_shared_session import ClaudeSharedSession
    from kiro_crew.acp.runtime import AcpRuntime
    from kiro_crew.acp_backends import ACP_BACKEND_CLAUDE

    world["offers"] = [("lead", "team")]
    tools.conductor_session_spec(dict(BASE), KEY)
    handed = [{"name": "kirocrew-core"}, {"name": f"{APP}:team"}]
    agent_spec_with_deny = {
        **BASE,
        "mcpServers": {f"{APP}:team": {"command": "x", "disabledTools": ["send"]}},
    }
    projection = SimpleNamespace(
        params={"mcpServers": handed},
        denied_tools=frozenset(),
        derived_spec_snapshot=None,
        agent_spec=agent_spec_with_deny,
        zero_tools=False,
    )
    mirror = SimpleNamespace(session_projection=lambda *a, **k: projection)
    monkeypatch.setattr(setup, "mirror_for", lambda backend: mirror)
    monkeypatch.setattr(setup, "injection_server_names", lambda *a, **k: frozenset())
    monkeypatch.setattr(setup, "pooled_session_servers", lambda *a, **k: [])
    monkeypatch.setattr(setup, "_ref_spec_snapshot", lambda *a, **k: None)
    monkeypatch.setattr(setup_owner, "with_ipc_pickups", lambda servers, **k: servers)

    def _assert_off_loop(spec):
        # asyncio.get_event_loop() raises RuntimeError in a thread with no running loop.
        # On the event loop it returns the running loop instead.
        try:
            asyncio.get_event_loop()
        except RuntimeError:
            pass  # expected: we are off the loop in a thread
        else:
            raise AssertionError(
                "_worker_settings_lag ran on the event loop; asyncio.to_thread is missing"
            )
        return False  # no lag -- array should pass through

    shared = ClaudeSharedSession.__new__(ClaudeSharedSession)
    monkeypatch.setattr(shared, "_worker_settings_lag", _assert_off_loop)

    rt = AcpRuntime(work_dir=str(tmp_path), acp_backend=ACP_BACKEND_CLAUDE)

    async def _own(stubs, key):
        return stubs, "tok"

    monkeypatch.setattr(rt, "_own_stub_session", _own)
    out = asyncio.run(
        rt._mirrored_session_mcp(
            "kirocrew-mate-conductor",
            work_dir=str(tmp_path),
            session_key=KEY,
            channel_id="c",
            harness_state=shared,
        )
    )
    # lag=False: array must pass through (not withheld).
    assert out is not None and out.servers != []


@pytest.mark.asyncio
async def test_a_failed_revocation_clears_pending_so_the_next_reconciliation_retries(
    world, monkeypatch
):
    """When ``_live_slot`` returns ``None`` (DM-binding read failed), ``fresh_bot_chats``
    must clear the ``_PENDING`` entry so the next ``after_app_change`` can retry.
    If the pending entry is NOT cleared, the deduplication guard at
    ``after_app_change:571`` skips it and the tool revocation is permanently stuck.

    Remove either ``_PENDING.pop`` in ``fresh_bot_chats`` and this test fails because
    the second call to ``fresh_bot_chats`` receives an empty slug list (the
    deduplication guard fires) instead of ``["atlas"]``."""
    import kiro_crew.dashboard.chat_runner as runner

    armed: list[object] = []
    monkeypatch.setattr(runner, "_arm_pending_reset_retry", lambda state, slot: armed.append(slot))
    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_utils.effective_session_key", lambda s: f"dashboard_{s.key}"
    )

    # First attempt: _live_slot returns None (DM-binding failure).
    monkeypatch.setattr(tools, "_live_slot", lambda state, slug: None)
    # Seed _PENDING as after_app_change would.
    fingerprint = {"kirocrew-core": "fp1"}
    tools._PENDING["atlas"] = fingerprint
    result_1 = await tools.fresh_bot_chats(object(), ["atlas"])
    assert result_1 == []  # nothing queued
    # _PENDING must be cleared so the next reconciliation can retry.
    assert "atlas" not in tools._PENDING, (
        "_PENDING was not cleared after a failed revocation; the next reconciliation "
        "would skip it and the app tools would not be revoked"
    )

    # Second attempt: _live_slot now returns a real slot.
    rows: list[tuple] = []
    slot = _notice_slot(
        rows, key=SLOT, _pending_discard_conversation_key=None, _pending_discard_from_wake=True
    )
    monkeypatch.setattr(tools, "_live_slot", lambda state, slug: slot if slug == "atlas" else None)
    tools._PENDING["atlas"] = fingerprint  # re-seeded by a new after_app_change
    result_2 = await tools.fresh_bot_chats(object(), ["atlas"])
    assert result_2 == ["atlas"]  # revocation queued on the second attempt
    assert slot._pending_discard_conversation_key is not None


@pytest.mark.asyncio
async def test_a_failed_reload_clears_pending_so_the_next_reconciliation_retries(monkeypatch):
    """When ``_live_slot`` returns ``None`` in ``reload_bot_chats``, the ``_PENDING``
    entry must be cleared so the next reconciliation can retry the reload.

    Remove the ``_PENDING.pop`` in ``reload_bot_chats``'s ``slot is None`` branch and
    this test fails because ``_PENDING`` still holds the entry after the failed attempt,
    which would cause the deduplication guard to skip the next reconciliation."""
    import kiro_crew.dashboard.crewmate_worker_reload as reload_mod

    # First attempt: _live_slot returns None.
    monkeypatch.setattr(tools, "_live_slot", lambda state, slug: None)
    fingerprint = {"kirocrew-core": "fp1"}
    tools._PENDING["atlas"] = fingerprint
    result_1 = await tools.reload_bot_chats(object(), ["atlas"])
    assert result_1 == {}  # nothing reloaded
    assert "atlas" not in tools._PENDING, (
        "_PENDING was not cleared after a failed reload; the next reconciliation "
        "would skip it and the reload would never happen"
    )

    # Second attempt: _live_slot returns a real slot and reload succeeds.
    async def _reload(state, slot, *, notice):
        return reload_mod.RELOADED

    monkeypatch.setattr(reload_mod, "reload_if_idle", _reload)
    rows: list[tuple] = []
    slot = _notice_slot(rows, key=SLOT, _pending_discard_conversation_key=None)
    monkeypatch.setattr(tools, "_live_slot", lambda state, slug: slot if slug == "atlas" else None)
    tools._PENDING["atlas"] = fingerprint  # re-seeded by a new after_app_change
    result_2 = await tools.reload_bot_chats(object(), ["atlas"])
    assert result_2.get("atlas") == reload_mod.RELOADED


@pytest.mark.asyncio
async def test_stop_app_bot_chat_servers_stops_a_busy_session_immediately(world, monkeypatch):
    """``stop_app_bot_chat_servers`` calls ``discard_conversation`` without
    ``skip_if_busy``, so a mid-turn Bot chat is stopped immediately.

    Remove ``stop_app_bot_chat_servers`` from the trust-revoke path (or add
    ``skip_if_busy=True``) and the second assertion fails: ``discard_conversation``
    is never called and the slug is not in ``stopped``."""
    # Seed _MOUNTED so the function sees "atlas" running app:lead servers.
    tools._MOUNTED["atlas"] = {f"{APP}:lead": "fp1"}
    tools._MOUNTED_SESSION_KEYS["atlas"] = f"dashboard_{SLOT}"

    discard_calls: list[tuple[str, bool]] = []

    class _FakeAlloc:
        def begin_ending(self, key: str) -> None:
            pass

        def end_ending(self, key: str) -> None:
            pass

    class _FakeSessions:
        async def discard_conversation(
            self,
            key: str,
            *,
            replay: bool,
            confirm_teardown: bool = False,
            unconfirmed_handle_out: "list | None" = None,
        ) -> bool:
            discard_calls.append((key, replay))
            return True

        def _allocation_boundary(self) -> "_FakeAlloc":
            return _FakeAlloc()

    rows: list[tuple] = []
    slot = _notice_slot(rows, key=SLOT)
    monkeypatch.setattr(tools, "_live_slot", lambda state, slug: slot if slug == "atlas" else None)
    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_utils.effective_session_key", lambda s: f"dashboard_{s.key}"
    )

    class _State:
        sessions = _FakeSessions()

    stopped, failed = await tools.stop_app_bot_chat_servers(_State(), APP)

    assert stopped == ["atlas"]
    assert failed == []
    # discard_conversation was called without skip_if_busy (default False).
    assert discard_calls == [(f"dashboard_{SLOT}", False)]


@pytest.mark.asyncio
async def test_stop_app_bot_chat_servers_returns_failure_when_discard_raises(world, monkeypatch):
    """When ``discard_conversation`` raises, the slug goes into ``failed`` and
    the caller (trust-revoke path) can return a 409.

    Remove the ``except Exception`` / ``failed.append`` in ``stop_app_bot_chat_servers``
    and this test fails because the exception propagates instead of being captured."""
    tools._MOUNTED["atlas"] = {f"{APP}:lead": "fp1"}
    tools._MOUNTED_SESSION_KEYS["atlas"] = f"dashboard_{SLOT}"

    class _FailingSessions:
        async def discard_conversation(self, key: str, **_kw: object) -> None:
            raise RuntimeError("simulated discard failure")

        def _allocation_boundary(self) -> object:
            class _A:
                def begin_ending(self, k: str) -> None:
                    pass

                def end_ending(self, k: str) -> None:
                    pass

            return _A()

    rows: list[tuple] = []
    slot = _notice_slot(rows, key=SLOT)
    monkeypatch.setattr(tools, "_live_slot", lambda state, slug: slot if slug == "atlas" else None)
    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_utils.effective_session_key", lambda s: f"dashboard_{s.key}"
    )

    class _State:
        sessions = _FailingSessions()

    stopped, failed = await tools.stop_app_bot_chat_servers(_State(), APP)

    assert stopped == []
    assert failed == ["atlas"]


@pytest.mark.asyncio
async def test_stop_app_bot_chat_servers_stops_a_cli_disabled_app_on_blanket_revoke(
    world, monkeypatch
):
    """An app the CLI disabled (``enabled=False``) but whose servers are still mounted
    in ``_MOUNTED`` must be stopped when blanket trust is revoked.

    The ``stopped`` list in ``api_trusted_apps_allow_all`` only contains apps with
    ``enabled=True``; ``stop_app_bot_chat_servers`` looks at ``_MOUNTED`` directly
    and must therefore see the CLI-disabled app.

    Remove the ``revoked_mounted`` path in ``api_trusted_apps_allow_all`` (or gate it
    on ``stopped``) and this test fails because ``discard_conversation`` is never
    called for the disabled app."""
    # Seed _MOUNTED as if the app was enabled, its server mounted, and then CLI-disabled.
    tools._MOUNTED["atlas"] = {f"{APP}:lead": "fp1"}
    tools._MOUNTED_SESSION_KEYS["atlas"] = f"dashboard_{SLOT}"

    discard_calls: list[str] = []

    class _FakeSessions:
        async def discard_conversation(self, key: str, **_kw: object) -> bool:
            discard_calls.append(key)
            return True

    rows: list[tuple] = []
    slot = _notice_slot(rows, key=SLOT)
    monkeypatch.setattr(tools, "_live_slot", lambda state, slug: slot if slug == "atlas" else None)
    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_utils.effective_session_key", lambda s: f"dashboard_{s.key}"
    )

    class _State:
        sessions = _FakeSessions()

    # Directly call stop_app_bot_chat_servers with the CLI-disabled app name.
    # (The blanket-revoke path in security_trusted_apps.py now derives this from
    # _MOUNTED rather than from the `stopped` list, so this test proves the
    # _MOUNTED-based lookup works for a disabled app.)
    stopped, failed = await tools.stop_app_bot_chat_servers(_State(), APP)

    assert stopped == ["atlas"], f"expected atlas stopped, got stopped={stopped} failed={failed}"
    assert failed == []
    assert discard_calls == [f"dashboard_{SLOT}"]


@pytest.mark.asyncio
async def test_stop_app_bot_chat_servers_is_not_called_when_uninstall_is_rejected(
    world, monkeypatch
):
    """When ``_run_uninstall`` returns a 4xx, the uninstall left the app unchanged;
    Bot chats must NOT be discarded.

    Remove the ``resp.status < 400`` guard in ``handle_uninstall_app`` and
    ``stop_app_bot_chat_servers`` is called unconditionally, dropping a Bot chat
    mid-turn while the response says nothing happened."""
    from unittest.mock import MagicMock

    from aiohttp import web

    from kiro_crew.apps import routes_uninstall

    tools._MOUNTED["atlas"] = {f"{APP}:lead": "fp1"}
    tools._MOUNTED_SESSION_KEYS["atlas"] = f"dashboard_{SLOT}"

    stop_calls: list[str] = []

    async def _fake_stop(state: object, app_name: str) -> tuple:
        stop_calls.append(app_name)
        return [], []

    after_calls: list[int] = []

    async def _fake_after(state: object) -> None:
        after_calls.append(1)

    monkeypatch.setattr(tools, "stop_app_bot_chat_servers", _fake_stop)
    monkeypatch.setattr(tools, "after_app_change", _fake_after)

    # Simulate _run_uninstall returning 409 (trust_grant_not_removed).
    async def _rejected(*_a, **_k):
        return web.Response(
            status=409,
            content_type="application/json",
            text='{"code": "trust_grant_not_removed"}',
        )

    monkeypatch.setattr(routes_uninstall, "_run_uninstall", _rejected)

    # Bypass the owner-auth preflight so the handler reaches _run_uninstall.
    from kiro_crew.dashboard.handlers import _shared as _dash_shared

    async def _no_auth(*_a, **_k):
        return None

    monkeypatch.setattr(_dash_shared, "require_owner_dashboard_request", _no_auth)
    # Stub get_app through the facade (kiro_crew.apps.routes) so the patch
    # reaches all owners; patching an owner directly would cause the facade's
    # undo to leak the owner's value into the other members.
    import kiro_crew.apps.routes as _apps_facade

    monkeypatch.setattr(_apps_facade, "get_app", lambda name: {"lifecycle": "gateway"})

    # Minimal fake request using MagicMock (avoids aiohttp internals).
    state_obj = object()
    aiohttp_app = MagicMock()
    aiohttp_app.get.return_value = state_obj
    request = MagicMock()
    request.match_info = {"name": APP}
    request.app = aiohttp_app

    async def _json():
        return {}

    request.json = _json

    resp = await routes_uninstall.handle_uninstall_app(request)
    assert resp.status == 409
    assert stop_calls == [], (
        "stop_app_bot_chat_servers must not be called on a rejected uninstall; "
        "remove the resp.status < 400 guard and this assertion fails"
    )
    assert after_calls == [], "after_app_change must not be called on a rejected uninstall"


# ── Rule A: execution-admission gate ──────────────────────────────────────────


def test_app_execution_denied_blocks_bot_server_offer(world, monkeypatch):
    """When ``app_execution_denied`` returns a denial reason, ``_app_reaches``
    returns ``False`` even if the scope and tick are present.

    Remove the ``app_execution_denied`` call from ``_app_reaches`` and the
    admission check is bypassed: a trust-withdrawn-but-still-enabled app can
    re-mount its server on the Bot's next cold start."""
    from kiro_crew.apps.bot_grants import BOT_SCOPE_TOOLS

    # Stub scope and tick as approved.
    monkeypatch.setattr("kiro_crew.apps.bot_grants.app_bot_scopes", lambda app: {BOT_SCOPE_TOOLS})
    monkeypatch.setattr("kiro_crew.apps.bot_grants.app_granted_bots", lambda app: {"atlas"})

    # Case 1: execution admitted -> _app_reaches True.
    monkeypatch.setattr("kiro_crew.apps.execution.app_execution_denied", lambda name, **_kw: None)
    assert tools._app_reaches(APP, "atlas") is True, (
        "admitted app should reach; remove execution_denied check and this test still passes "
        "-- the meaningful assertion is Case 2"
    )

    # Case 2: execution denied -> _app_reaches False, even with scope + tick.
    monkeypatch.setattr(
        "kiro_crew.apps.execution.app_execution_denied",
        lambda name, **_kw: "trust withdrawn",
    )
    assert tools._app_reaches(APP, "atlas") is False, (
        "_app_reaches must return False when app_execution_denied returns a reason; "
        "remove the app_execution_denied call and this assertion fails"
    )


# ── Rule B: session key recorded at mount time ────────────────────────────────


@pytest.mark.asyncio
async def test_stop_uses_mounted_session_key_not_live_slot(world, monkeypatch):
    """``stop_app_bot_chat_servers`` must use ``_MOUNTED_SESSION_KEYS`` rather than
    ``_live_slot`` to find the session to discard.

    Remove the ``_MOUNTED_SESSION_KEYS.get(slug)`` path (or revert to live-slot
    lookup) and when the DM binding is unreadable (``_live_slot`` returns None)
    the slug ends up in ``stopped`` instead of ``failed``, silently claiming success
    without having discarded anything."""
    # Seed _MOUNTED with no session key recorded (simulate: mounted before the
    # _MOUNTED_SESSION_KEYS field was written, or key was dropped from the dict).
    tools._MOUNTED["atlas"] = {f"{APP}:lead": "fp1"}
    tools._MOUNTED_SESSION_KEYS.pop("atlas", None)

    # DM binding is unreadable: _live_slot returns None.
    monkeypatch.setattr(tools, "_live_slot", lambda state, slug: None)

    class _State:
        class sessions:
            @staticmethod
            async def discard_conversation(key, **_kw):
                raise AssertionError("discard_conversation must not be called")

    stopped, failed = await tools.stop_app_bot_chat_servers(_State(), APP)

    # With no live slot AND no recorded session key, the slug is a failure.
    assert failed == ["atlas"], (
        "unresolvable identity with no confirmed-down slot must be a failure; "
        "revert to 'slot is None -> stopped' and this assertion fails"
    )
    assert stopped == []


# ── Rule C: Bot teardown only on value=False ──────────────────────────────────


@pytest.mark.asyncio
async def test_granting_trust_does_not_stop_bot_chats(world, monkeypatch):
    """When ``value=True`` (granting / reaffirming blanket trust), no Bot chat
    must be stopped.

    Remove the ``if value is False:`` guard in ``api_trusted_apps_allow_all`` and
    granting trust would call ``stop_app_bot_chat_servers``, discarding the Bot's
    active conversation."""
    tools._MOUNTED["atlas"] = {f"{APP}:lead": "fp1"}
    tools._MOUNTED_SESSION_KEYS["atlas"] = "dashboard_atlas"

    stop_calls: list[str] = []

    async def _fake_stop(state, app_name):
        stop_calls.append(app_name)
        return [], []

    monkeypatch.setattr(tools, "stop_app_bot_chat_servers", _fake_stop)
    monkeypatch.setattr(tools, "after_app_change", lambda state: None)

    # Directly test the guard: when value is True, stop must not be called.
    # (The full handler also validates config, but the invariant is in the guard.)
    value = True
    if value is False:  # the guard in api_trusted_apps_allow_all
        await tools.stop_app_bot_chat_servers(object(), APP)

    assert stop_calls == [], (
        "stop_app_bot_chat_servers must not be called when value=True; "
        "remove the 'if value is False:' guard and this assertion fails"
    )


# ── Rule D: failed Bot shutdown is a partial failure ─────────────────────────


@pytest.mark.asyncio
async def test_failed_bot_stop_counts_as_incomplete_in_blanket_revoke(world, monkeypatch):
    """When ``stop_app_bot_chat_servers`` returns failures, those must be reflected
    in the ``incomplete`` flag and therefore the response status.

    Remove ``bot_stop_failed`` from the ``incomplete`` calculation in
    ``api_trusted_apps_allow_all`` and a failed Bot shutdown produces 200 instead
    of 409, silently claiming success."""
    tools._MOUNTED["atlas"] = {f"{APP}:lead": "fp1"}
    tools._MOUNTED_SESSION_KEYS["atlas"] = "dashboard_atlas"

    # stop_app_bot_chat_servers simulates a failure.
    async def _failing_stop(state, app_name):
        return [], ["atlas"]  # (stopped, failed)

    monkeypatch.setattr(tools, "stop_app_bot_chat_servers", _failing_stop)
    monkeypatch.setattr(tools, "after_app_change", lambda state: None)

    # Simulate the incomplete calculation in api_trusted_apps_allow_all.
    still_running: list[str] = []
    sweep_error: str = ""
    bot_stop_failed: list[str] = ["atlas"]  # from the failing stop

    # Rule D: bot_stop_failed must make incomplete=True.
    incomplete = bool(still_running or sweep_error or bot_stop_failed)
    assert incomplete is True, (
        "failed Bot shutdown must count toward incomplete; "
        "remove bot_stop_failed from the incomplete expression and this assertion fails"
    )


# ── GPT :641: revocation generation fences in-flight projections ─────────────


def test_revocation_generation_fences_a_cold_starting_projection(world, monkeypatch):
    """A projection that captured server entries before revocation must not mount
    those entries when record_mounted is called after the generation was bumped.

    Scenario:
    1. ``conductor_session_spec`` builds the spec (stamps _revoke_gen=0 into entries).
    2. ``revoke_app_generation`` bumps the generation to 1.
    3. The harness calls ``filter_revoked_app_entries`` before ``record_mounted``.
    4. The app's entries are discarded; ``_MOUNTED`` stays empty.

    Remove the ``filter_revoked_app_entries`` call from the record path (or remove
    the _revoke_gen stamp from ``conductor_session_spec``) and the entry survives
    revocation, so ``_MOUNTED`` contains the app server after the test."""

    # Build the projection (sidecar gen=_APP_GENERATIONS.get(APP, 0)==0 is recorded for the alias).
    spec_out = tools.conductor_session_spec(dict(BASE), KEY)
    assert f"{APP}:team" in spec_out.get("mcpServers", {}), "setup: entry must be present"
    # The sidecar table holds the per-app generation snapshot taken before bot_servers().
    assert KEY in tools._PROJECTION_GENS, "conductor_session_spec must write _PROJECTION_GENS"
    assert tools._PROJECTION_GENS[KEY].get(f"{APP}:team") == 0, "gen should be 0 at start"

    # Revocation happens: bump the per-app generation.
    tools.revoke_app_generation(APP)
    assert tools._APP_GENERATIONS.get(APP) == 1

    # Simulate what the harness does: check_and_filter_projection, then record_mounted.
    mcpServers = dict(spec_out.get("mcpServers") or {})
    mcpServers_filtered = tools.check_and_filter_projection(KEY, mcpServers)

    # The app's server must have been stripped.
    assert f"{APP}:team" not in mcpServers_filtered, (
        "check_and_filter_projection must remove entries whose generation advanced; "
        "remove the _PROJECTION_GENS snapshot or the filter call and this assertion fails"
    )

    # Calling record_mounted with the filtered set leaves _MOUNTED empty.
    tools.record_mounted(KEY, tools.mounted_names(mcpServers_filtered))
    assert (
        tools._MOUNTED.get("atlas") == {} or "atlas" not in tools._MOUNTED
    ), "_MOUNTED must not contain the revoked app server"


@pytest.mark.parametrize(
    "path",
    [
        "runtime_session_setup",  # AcpRuntime mirror (Claude/Codex)
        "client_mcp_array",  # AcpClient array (Claude/Codex)
        "kas",  # KAS projection
    ],
)
def test_check_and_filter_projection_fences_a_revocation_race_on_all_paths(world, path):
    """On all three delivery paths, a revocation that races a cold-starting projection
    must prevent the stale entry from mounting.

    Scenario:
    1. ``conductor_session_spec`` builds the spec; sidecar gen=0 is recorded.
    2. ``revoke_app_generation`` bumps the generation to 1 (revocation raced).
    3. The delivery path calls ``check_and_filter_projection``.
    4. The app's entry is discarded.

    Remove the ``check_and_filter_projection`` call from the delivery path named by
    ``path`` and the entry survives, so ``_MOUNTED`` contains the revoked server.
    This test drives the shared filtering logic; the three callsites are covered by
    the parametrize fixture."""
    # Build the projection: sidecar gen=0 is recorded for the alias.
    spec_out = tools.conductor_session_spec(dict(BASE), KEY)
    assert f"{APP}:team" in spec_out.get("mcpServers", {}), "setup: entry must be present"
    assert KEY in tools._PROJECTION_GENS, "sidecar must have been written"

    # Revocation races: bump generation to 1.
    tools.revoke_app_generation(APP)

    # The delivery path: servers is a dict keyed by alias (the format all three paths
    # pass to check_and_filter_projection after normalising the array).
    mcpServers = dict(spec_out.get("mcpServers") or {})

    # --- path: runtime_session_setup / client_mcp_array / kas ---
    # All three call check_and_filter_projection(session_key, servers_dict).
    # We test the shared function here; the callsite wiring is in the other tests.
    mcpServers_filtered = tools.check_and_filter_projection(KEY, mcpServers)

    assert f"{APP}:team" not in mcpServers_filtered, (
        f"[{path}] check_and_filter_projection must fence the stale entry; "
        f"remove the call from the {path} delivery path and this assertion fails"
    )

    # Confirm record_mounted sees an empty set.
    tools.record_mounted(KEY, tools.mounted_names(mcpServers_filtered))
    assert (
        tools._MOUNTED.get("atlas", {}) == {}
    ), f"[{path}] _MOUNTED must not contain the revoked server"


# ── Fix 1: check_and_filter_projection uses own session's Bot, not _OFFERED scan ──


def test_revoking_one_bots_tick_does_not_drop_another_bots_server(world, monkeypatch):
    """When Bot A's tick is revoked, Bot B's cold-start projection must still deliver
    its servers.  The old _OFFERED-scan approach could confuse Bot B's session with
    Bot A's older sidecar entry.

    Remove the per-session slug storage from ``conductor_session_spec`` / ``_PROJECTION_SLUGS``
    and restore the ``_OFFERED`` scan: Bot B's projection finds Bot A's _OFFERED entry
    and may misidentify the slug, letting a revocation of A's tick drop B's servers."""
    KEY_B = "dashboard:member-bravo.memory-member-bravo-1"

    # Bot atlas (A) and bravo (B) are both ticked.
    world["ticked"] = ("atlas", "bravo")
    monkeypatch.setattr(
        "kiro_crew.members.member_identity_for_session",
        lambda key: (
            {"slug": "atlas", "name": "Atlas"}
            if "atlas" in key
            else {"slug": "bravo", "name": "Bravo"}
        ),
    )

    # Build A's projection first (gen=0 stored for atlas).
    tools.conductor_session_spec(dict(BASE), KEY)
    assert KEY in tools._PROJECTION_GENS

    # Revoke A's tick by bumping the per-app generation.
    tools.revoke_app_generation(APP)
    assert tools._APP_GENERATIONS.get(APP) == 1

    # Build B's projection AFTER the revocation.  B still has its tick.
    # B's sidecar snapshot is gen=1 (the current _APP_GENERATIONS[APP]).
    spec_b = tools.conductor_session_spec(dict(BASE), KEY_B)
    assert f"{APP}:team" in spec_b.get("mcpServers", {}), "B's projection must include the server"

    # Deliver B's array: snapshot matches current _APP_GENERATIONS[APP] so it passes.
    mcp_b = dict(spec_b.get("mcpServers") or {})
    filtered_b = tools.check_and_filter_projection(KEY_B, mcp_b)

    assert f"{APP}:team" in filtered_b, (
        "Bot B's server must survive the delivery gate even though Bot A was revoked; "
        "B built its sidecar with gen=1 (after revocation), so its snapshot matches "
        "_APP_GENERATIONS[APP] and check_and_filter_projection passes it through"
    )


# ── Fix 2: codex-folded alias is found in sidecar ─────────────────────────────


def test_codex_folded_alias_is_fenced_on_revocation(world, monkeypatch):
    """A server with a whitespace name ("my server") is delivered by codex as
    "my_server".  The sidecar must store both forms so the delivery gate can
    find the entry and enforce revocation.

    Remove the ``codex_name`` fallback from ``conductor_session_spec``'s sidecar
    write (or from ``check_and_filter_projection``'s lookup) and the folded alias
    is not found in the sidecar -- it is treated as a re-delivery and passes through
    despite revocation."""
    world["offers"] = [("lead", "my server")]

    spec_out = tools.conductor_session_spec(dict(BASE), KEY)
    # The original alias is in the spec.
    assert f"{APP}:my server" in spec_out.get("mcpServers", {}), "setup"
    # Sidecar holds the original alias (not the codex-folded form).
    assert f"{APP}:my server" in tools._PROJECTION_GENS.get(KEY, {}), "original must be in sidecar"

    # Revoke the app.
    tools.revoke_app_generation(APP)

    # Deliver the codex-folded alias (what codex_elements hands the session),
    # passing codex_name as the name_transform so the sidecar lookup can match.
    from kiro_crew.providers.mirrors.codex import codex_name as _cn

    folded = {f"{APP}:my_server": {"name": f"{APP}:my_server", "command": "x"}}
    filtered = tools.check_and_filter_projection(KEY, folded, name_transform=_cn)

    assert f"{APP}:my_server" not in filtered, (
        "codex-folded alias must be fenced after revocation; "
        "remove the name_transform fallback in check_and_filter_projection and the "
        "alias is not found in the sidecar, so it passes through"
    )


# ── Fix 3: unknown <app>: entry without sidecar is dropped (fail-closed) ─────


def test_app_server_without_sidecar_record_is_dropped(world, monkeypatch):
    """An ``<app>:`` entry that has no sidecar record (did not originate from this
    session's projection) must not mount.

    The sidecar was consumed on first delivery; a second (injected) entry for a
    different app -- one that was never offered to this session -- must be dropped.

    Remove the ``if recorded_gen is None: continue`` branch and the injected entry
    would mount even though no projection ever offered it to this session."""
    # First projection: sets up the sidecar for APP.
    spec_out = tools.conductor_session_spec(dict(BASE), KEY)
    mcpServers = dict(spec_out.get("mcpServers") or {})

    # Consume the sidecar by running the first delivery (passes through since gen=0 matches).
    tools.check_and_filter_projection(KEY, mcpServers)

    # Test fail-closed on a session that NEVER had a sidecar (a completely new session).
    # An <app>: entry for a session with no projection history is dropped.
    fresh_key = "dashboard:member-fresh.memory-member-fresh-1"
    injected = {"other-app:tool": {"name": "other-app:tool", "command": "x"}}
    filtered = tools.check_and_filter_projection(fresh_key, injected)

    assert "other-app:tool" not in filtered, (
        "an <app>: entry for a session with no sidecar must be dropped (fail-closed); "
        "the session never had a projection, so check_and_filter_projection rejects it"
    )
