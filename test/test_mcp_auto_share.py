"""Every stdio MCP server is shared and its launch approved with no step for the person.

A fresh install's gateway start stubs a declared stdio server
(``mcp_gateway.auto_share``) and its rewrite pass approves the launch the first
time it sees it (``LaunchApprovals.auto``), so the server runs shared with no
card, dialog or click. Changed args are approved again on the next start, and
the launch they replaced is retired. Left per chat with no step: a server whose
args name the chat's folder, a disabled one, a never-pooled one, Kiro Crew's own and a
remote one.
A person's off is kept through every later start, and sharing turned off skips
the pass. A pass without ``auto`` (not the gateway start's) still refuses.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from mcp_launch_approval_helpers import (  # noqa: F401 -- the fixture is requested by name
    _APPROVED_CMD,
    _approval_module,
    _declare,
    _targets_running,
    toggle_home_fixture,
)

from kiro_crew.mcp_gateway import auto_share, folder_use, rewriter

#: Opts this module out of the conftest fixtures that approve every launch and
#: turn this pass off.
ENFORCE_LAUNCH_APPROVAL = True
AUTO_SHARE = True


#: The real rebuild hook, kept before the fixture below replaces it for every test.
_REAL_REFRESH = auto_share.refresh_agent_specs


@pytest.mark.parametrize(
    ("outcome", "expected"), [("wrote", True), ("skipped", False), ("raised", False)]
)
def test_the_rebuild_hook_reports_a_rebuild_that_wrote_nothing(monkeypatch, outcome, expected):
    """An unreadable spec or a refused shared home writes nothing, so the edit is not consumed."""
    from pathlib import Path as _Path

    from kiro_crew import agent

    def _reporting():
        if outcome == "raised":
            raise OSError("disk full")
        return _Path("kirocrew.json"), outcome == "wrote"

    monkeypatch.setattr(agent, "rebuild_agent_config_reporting", _reporting)
    monkeypatch.setattr(agent, "rebuild_agent_config", lambda *a, **k: _Path("kirocrew.json"))

    assert _REAL_REFRESH() is expected


@pytest.fixture(autouse=True)
def _fixed_mcp_sources(_floor_monkeypatch):
    """The MCP source files read as unchanged, so no watch here rebuilds a real spec."""
    _floor_monkeypatch.setattr(auto_share, "mcp_sources_fingerprint", lambda: ())
    _floor_monkeypatch.setattr(auto_share, "refresh_agent_specs", lambda: True)


def _boot(tmp_path: Path, agents: Path, stubs: frozenset[str], *, auto: bool = True):
    """One gateway start's rewrite pass and its save, as ``_init_mcp_gateway`` runs them."""
    mod = _approval_module()
    approvals = mod.load_approvals()
    approvals.auto = auto
    _results, target_env = rewriter.rewrite_agents(
        source_dir=agents,
        overlay_dir=tmp_path / "boot" / "mcp-gateway" / "agents",
        socket_path=tmp_path / "gw.sock",
        work_dir=tmp_path / "work",
        stub_servers=stubs,
        approvals=approvals,
    )
    mod.save_pass(approvals)
    return target_env, approvals


def _config(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "config.json").read_text())


def _agents(agents: Path, **specs: dict) -> None:
    """One agent spec per keyword, each declaring the given ``mcpServers``."""
    for path in agents.glob("*.json"):
        path.unlink()
    for agent, servers in specs.items():
        (agents / f"{agent}.json").write_text(
            json.dumps({"name": agent, "tools": ["*"], "mcpServers": servers})
        )


def test_a_fresh_install_shares_a_stdio_server_with_no_step(tmp_path, toggle_home):
    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])

    assert auto_share.share_declared_servers() == ["srv"]
    assert _config(tmp_path)["mcp_gateway"]["stub_overrides"] == {"srv": True}

    target_env, approvals = _boot(tmp_path, toggle_home, frozenset({"srv"}))
    assert _targets_running(target_env, _APPROVED_CMD)
    assert approvals.refused == {}
    assert set(_approval_module().load_approvals().approved) == {"SRV"}


def test_changed_args_are_approved_again_and_the_old_launch_retired(tmp_path, toggle_home):
    mod = _approval_module()
    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    _boot(tmp_path, toggle_home, frozenset({"srv"}))
    first = set(mod.load_approvals().approved["SRV"])

    _declare(toggle_home, "agent spec", _APPROVED_CMD, ["-c", "pass"])
    target_env, approvals = _boot(tmp_path, toggle_home, frozenset({"srv"}))

    assert _targets_running(target_env, _APPROVED_CMD)
    assert approvals.refused == {}
    now = mod.load_approvals().approved["SRV"]
    assert len(now) == 1 and now != first


def test_a_pass_without_auto_still_refuses_a_new_launch(tmp_path, toggle_home):
    """The dashboard's resolution and the app-refresh probe run without ``auto``."""
    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])

    target_env, approvals = _boot(tmp_path, toggle_home, frozenset({"srv"}), auto=False)

    assert _targets_running(target_env, _APPROVED_CMD) == []
    assert approvals.refused == {"SRV": _approval_module().REFUSED_UNAPPROVED}


def test_folder_users_disabled_never_pooled_and_remote_servers_stay_per_chat(toggle_home):
    weather = {"command": "npx", "args": ["-y", "@acme/weather-mcp"]}
    _agents(
        toggle_home,
        a={
            "shared": weather,
            # Folder-free here, but another agent runs it on the chat's folder.
            "fs": {"command": "npx", "args": ["-y", "@acme/fs-mcp", "/data"]},
            "placeholder": {"command": "git-mcp", "env": {"REPO": "${workspaceFolder}"}},
            "off": {"command": "npx", "disabled": True},
            "creds-agent": {"command": "creds-agent"},
            "kirocrew-secrets": {"command": "kirocrew", "args": ["mcp", "secrets"]},
            "remote": {"url": "https://mcp.example.com/sse"},
        },
        b={
            "fs": {"command": "npx", "args": ["-y", "@acme/fs-mcp", "."]},
            # Declared only by a second agent: still shared.
            "only-b": weather,
        },
    )

    assert auto_share.share_declared_servers() == ["only-b", "shared"]


def test_the_declaration_watch_does_not_follow_the_crewmate_folder(
    tmp_path, toggle_home, monkeypatch
):
    """A session can write the crewmate folder; an edit there restarts no broker and resets no chat."""
    import kiro_crew.config.paths as paths_mod

    mates = tmp_path / "kiro" / "mates" / "agents"
    mates.mkdir(parents=True)
    monkeypatch.setattr(paths_mod, "_mate_agents_dir_override", lambda: mates)
    _agents(toggle_home, a={"srv": {"command": _APPROVED_CMD}})
    stat, launches = auto_share.declaration_files_stat(), auto_share.declared_launches()

    (mates / "pixel.json").write_text(
        json.dumps({"name": "pixel", "mcpServers": {"added": {"command": "other", "args": ["x"]}}})
    )

    assert auto_share.declaration_files_stat() == stat
    assert auto_share.declared_launches() == launches


def test_a_persons_off_survives_every_later_start_under_either_spelling(tmp_path, toggle_home):
    _agents(
        toggle_home,
        a={
            "srv": {"command": _APPROVED_CMD},
            "playwright-mcp": {"command": "npx", "args": ["-y", "@playwright/mcp"]},
        },
    )
    off = {"srv": False, "npm:@playwright/mcp": False}
    (tmp_path / "config.json").write_text(json.dumps({"mcp_gateway": {"stub_overrides": off}}))

    assert auto_share.share_declared_servers() == []
    assert auto_share.share_declared_servers() == []
    assert _config(tmp_path)["mcp_gateway"]["stub_overrides"] == off


def test_sharing_turned_off_records_no_stub(tmp_path, toggle_home):
    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    (tmp_path / "config.json").write_text(json.dumps({"mcp_gateway": {"enabled": False}}))

    assert auto_share.share_declared_servers() == []
    assert "stub_overrides" not in _config(tmp_path)["mcp_gateway"]


def test_a_global_off_saved_before_the_lock_wins(tmp_path, toggle_home, monkeypatch):
    """The decision is taken again on the locked document, not the first read."""
    import kiro_crew.config.loader as loader

    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    locked = loader.update_config_locked

    def _off_lands_first(*args, **kwargs):
        (tmp_path / "config.json").write_text(json.dumps({"mcp_gateway": {"enabled": False}}))
        return locked(*args, **kwargs)

    monkeypatch.setattr(loader, "update_config_locked", _off_lands_first)

    assert auto_share.share_declared_servers() == []
    assert "stub_overrides" not in _config(tmp_path)["mcp_gateway"]


def test_a_launch_that_names_no_folder_needs_no_folder_answer(toggle_home):
    _declare(toggle_home, "agent spec", _APPROVED_CMD, ["-m", "srv"])
    facts = folder_use.read_declarations(toggle_home)

    assert facts.launch_free("srv")
    assert folder_use.folder_free_source("srv", frozenset(), None, launch_free=True) == (
        folder_use.SOURCE_LAUNCH
    )
    # A server that asked a chat for its roots, or was answered ``uses_cwd``, still asks.
    assert (
        folder_use.folder_free_source("srv", frozenset(), None, asked_roots=True, launch_free=True)
        is None
    )
    assert folder_use.folder_free_source("srv", frozenset(), "uses_cwd", launch_free=True) is None

    _declare(toggle_home, "agent spec", _APPROVED_CMD, ["./server.py"])
    assert not folder_use.read_declarations(toggle_home).launch_free("srv")


def test_a_shared_server_whose_launch_now_names_the_folder_stays_per_chat(tmp_path, toggle_home):
    """The folder rule holds on every start, not only when a server is first shared."""
    _declare(toggle_home, "agent spec", _APPROVED_CMD, ["/data"])
    assert auto_share.share_declared_servers() == ["srv"]
    target_env, _approvals = _boot(tmp_path, toggle_home, frozenset({"srv"}))
    assert _targets_running(target_env, _APPROVED_CMD)

    _declare(toggle_home, "agent spec", _APPROVED_CMD, ["."])
    target_env, approvals = _boot(tmp_path, toggle_home, frozenset({"srv"}))

    assert _targets_running(target_env, _APPROVED_CMD) == []
    assert approvals.refused == {}

    # An unchanged start is served from the rewrite cache and keeps it per chat.
    target_env, _approvals = _boot(tmp_path, toggle_home, frozenset({"srv"}))
    assert _targets_running(target_env, _APPROVED_CMD) == []


@pytest.mark.asyncio
async def test_a_not_used_answer_for_a_folder_named_launch_changes_routing(
    tmp_path, toggle_home, monkeypatch
):
    """The handler sees the start's folder rule, so the answer that lifts it changes routing.

    With no approved launch yet, a routing change takes the approval path
    instead of answering ``applied`` with nothing applied.
    """
    from mcp_launch_approval_helpers import _stub_request

    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    # Through the facade, which re-binds every member that shares the name.
    monkeypatch.setattr(mcp_mod, "kiro_agents_dir_path", lambda: toggle_home)
    _declare(toggle_home, "agent spec", _APPROVED_CMD, ["."])
    (tmp_path / "config.json").write_text(
        json.dumps({"mcp_gateway": {"stub_overrides": {"srv": True}}})
    )

    resp = await mcp_mod.api_mcp_gateway_set_stub(
        _stub_request({"name": "srv", "cwd_use": "not_used"})
    )

    assert json.loads(resp.body).get("code") == "expected_launch_required"


def test_the_rebuilds_secret_withhold_is_not_an_owner_off(toggle_home):
    from kiro_crew.mcp_provenance import WITHHELD_KEY

    entry = {"command": "npx", "env": {"TOKEN": "secret://vault/t"}, "disabled": True}
    _agents(toggle_home, a={"vaulted": {**entry, WITHHELD_KEY: True}, "off": entry})

    assert auto_share.share_declared_servers() == ["vaulted"]


def test_the_boot_pass_applies_new_stubs_live(tmp_path, toggle_home):
    """Run after the socket binds, it hands new stubs to the live apply path."""
    import asyncio
    from types import SimpleNamespace

    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.slack import gateway as gw

    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    applied: list[bool] = []

    async def _apply():
        applied.append(True)
        return {"applied": True}

    async def _no_watch(**_kw):
        return None

    orch = SimpleNamespace(
        _cfg=KiroCrewConfig.load(), _apply_mcp_stub=_apply, _watch_mcp_declarations=_no_watch
    )

    asyncio.run(gw.GatewayOrchestrator._auto_share_mcp_servers(orch))
    assert applied == [True]
    assert _config(tmp_path)["mcp_gateway"]["stub_overrides"] == {"srv": True}

    # A second start finds every server decided: nothing to apply.
    asyncio.run(gw.GatewayOrchestrator._auto_share_mcp_servers(orch))
    assert applied == [True]


def test_a_start_with_sharing_off_still_watches(tmp_path, toggle_home):
    """Sharing off at the start skips the pass but starts the watch that sees it come on."""
    import asyncio
    from types import SimpleNamespace

    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.slack import gateway as gw

    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    (tmp_path / "config.json").write_text(json.dumps({"mcp_gateway": {"enabled": False}}))
    calls: list[str] = []

    async def _apply():
        calls.append("apply")

    async def _watch(*, pending=False):
        calls.append(f"watch pending={pending}")

    orch = SimpleNamespace(
        _cfg=KiroCrewConfig.load(), _apply_mcp_stub=_apply, _watch_mcp_declarations=_watch
    )

    asyncio.run(gw.GatewayOrchestrator._auto_share_mcp_servers(orch))
    assert calls == ["watch pending=False"]
    assert "stub_overrides" not in _config(tmp_path)["mcp_gateway"]


def test_a_failed_boot_pass_is_handed_to_the_watch(tmp_path, toggle_home):
    """A broker that did not take the boot stubs makes the watch's first poll run the pass."""
    import asyncio
    from types import SimpleNamespace

    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.slack import gateway as gw

    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    calls: list[str] = []

    async def _apply():
        calls.append("apply")
        return {"applied": False}

    async def _watch(*, pending=False):
        calls.append(f"watch pending={pending}")

    orch = SimpleNamespace(
        _cfg=KiroCrewConfig.load(),
        _apply_mcp_stub=_apply,
        _watch_mcp_declarations=_watch,
        _mcp_gateway_pending=None,
    )

    asyncio.run(gw.GatewayOrchestrator._auto_share_mcp_servers(orch))
    assert calls == ["apply", "watch pending=True"]


def test_a_pending_pass_runs_on_the_first_poll(tmp_path, toggle_home):
    """Nothing moved on disk, yet the carried-over pass runs once and then settles."""
    import asyncio

    from kiro_crew.slack import gateway as gw

    _agents(toggle_home, a={"srv": {"command": _APPROVED_CMD, "args": []}})
    applied: list[bool] = []
    orch = _watch_orch(applied)
    polls = {"n": 0}

    async def _wait(_secs):
        polls["n"] += 1
        if polls["n"] == 3:
            raise _StopWatch

    import pytest

    with pytest.raises(_StopWatch):
        asyncio.run(
            gw.GatewayOrchestrator._watch_mcp_declarations(
                orch, poll_secs=0, wait=_wait, pending=True
            )
        )
    assert applied == [True]
    assert _config(tmp_path)["mcp_gateway"]["stub_overrides"] == {"srv": True}


def test_a_boot_apply_that_did_not_land_is_forced_again_until_it_does(tmp_path, toggle_home):
    """Config already holds the boot pass's shares, so only the carried retry forces the start."""
    _agents(toggle_home, a={"srv": {"command": _APPROVED_CMD, "args": []}})
    (tmp_path / "config.json").write_text(
        json.dumps({"mcp_gateway": {"stub_overrides": {"srv": True}}})
    )
    applied: list[bool] = []
    landed = iter([False, True])

    async def _apply(*, launch_approved: bool = False):
        applied.append(launch_approved)
        return {"applied": next(landed)}

    orch = _watch_orch([])
    orch._apply_mcp_stub = _apply
    polls = {"n": 0}

    async def _wait(_secs):
        polls["n"] += 1
        if polls["n"] == 4:
            raise _StopWatch

    import asyncio

    from kiro_crew.slack import gateway as gw

    with pytest.raises(_StopWatch):
        asyncio.run(
            gw.GatewayOrchestrator._watch_mcp_declarations(
                orch, poll_secs=0, wait=_wait, pending=True
            )
        )
    # Not landed, then forced again and landed; then nothing moved.
    assert applied == [True, True]


def test_a_failed_rebuild_is_retried_on_the_next_poll(tmp_path, toggle_home, monkeypatch):
    """A source edit whose rebuild failed is not taken as consumed."""
    _agents(toggle_home, a={"srv": {"command": _APPROVED_CMD, "args": []}})
    sources = {"now": (("mcp.json", b"before"),)}
    outcomes = iter([False, True])
    rebuilds: list[str] = []

    def _rebuild():
        rebuilds.append("rebuild")
        return next(outcomes)

    monkeypatch.setattr(auto_share, "mcp_sources_fingerprint", lambda: sources["now"])
    monkeypatch.setattr(auto_share, "refresh_agent_specs", _rebuild)
    orch = _watch_orch([])
    polls = {"n": 0}

    async def _wait(_secs):
        polls["n"] += 1
        if polls["n"] == 1:
            sources["now"] = (("mcp.json", b"after"),)
        elif polls["n"] == 4:
            raise _StopWatch

    _run_watch(orch, _wait)

    assert rebuilds == ["rebuild", "rebuild"]


def test_a_rebuild_that_never_writes_does_not_stop_the_watch(tmp_path, toggle_home, monkeypatch):
    """A refused shared home keeps the source edit pending, yet a spec edit is still shared."""
    _agents(toggle_home, a={"srv": {"command": _APPROVED_CMD, "args": []}})
    monkeypatch.setattr(auto_share, "mcp_sources_fingerprint", lambda: (("mcp.json", str(polls)),))
    monkeypatch.setattr(auto_share, "refresh_agent_specs", lambda: False)
    applied: list[bool] = []
    orch = _watch_orch(applied)
    polls = {"n": 0}

    async def _wait(_secs):
        polls["n"] += 1
        if polls["n"] == 2:
            _agents(
                toggle_home,
                a={
                    "srv": {"command": _APPROVED_CMD, "args": []},
                    "more": {"command": _APPROVED_CMD},
                },
            )
        elif polls["n"] == 4:
            raise _StopWatch

    _run_watch(orch, _wait)

    assert _config(tmp_path)["mcp_gateway"]["stub_overrides"].get("more") is True


def test_an_incomplete_census_still_keeps_a_known_folder_user_per_chat():
    """A source that could not be read never clears a folder signal another one carries."""
    facts = folder_use.FolderDeclarations(stdio={"fs"}, folder_named={"fs"}, complete=False)

    assert facts.folder_users(("fs",), frozenset(), {}, {}) == frozenset({"fs"})


def test_an_incomplete_first_read_is_reconciled_on_the_first_complete_one(
    tmp_path, toggle_home, monkeypatch
):
    """With no baseline, a shared server's edited launch is applied forced, not settled."""
    _agents(toggle_home, a={"srv": {"command": _APPROVED_CMD, "args": []}})
    (tmp_path / "config.json").write_text(
        json.dumps({"mcp_gateway": {"stub_overrides": {"srv": True}}})
    )
    real = auto_share.declared_launches
    reads = {"n": 0}

    def _launches():
        reads["n"] += 1
        return None if reads["n"] == 1 else real()

    monkeypatch.setattr(auto_share, "declared_launches", _launches)
    applied: list[bool] = []
    orch = _watch_orch(applied)
    polls = {"n": 0}

    async def _wait(_secs):
        polls["n"] += 1
        if polls["n"] == 3:
            raise _StopWatch

    _run_watch(orch, _wait)

    assert applied == [True]


def test_an_incomplete_census_is_kept_pending_until_a_complete_one(
    tmp_path, toggle_home, monkeypatch
):
    """A source that could not be read is no pass with nothing to do: it runs again."""
    _agents(toggle_home, a={"srv": {"command": _APPROVED_CMD, "args": []}})
    real = folder_use.read_declarations
    reads = {"n": 0}

    def _read(*a, **k):
        reads["n"] += 1
        facts = real(*a, **k)
        if reads["n"] == 1:
            facts.complete = False
        return facts

    monkeypatch.setattr(folder_use, "read_declarations", _read)
    with pytest.raises(auto_share.CensusIncomplete):
        auto_share.share_declared_servers()

    applied: list[bool] = []
    orch = _watch_orch(applied)
    polls = {"n": 0}

    async def _wait(_secs):
        polls["n"] += 1
        if polls["n"] == 3:
            raise _StopWatch

    import asyncio

    from kiro_crew.slack import gateway as gw

    with pytest.raises(_StopWatch):
        asyncio.run(
            gw.GatewayOrchestrator._watch_mcp_declarations(
                orch, poll_secs=0, wait=_wait, pending=True
            )
        )

    assert applied == [True]
    assert _config(tmp_path)["mcp_gateway"]["stub_overrides"] == {"srv": True}


def test_an_unreadable_source_still_lets_a_routed_launch_edit_through(
    tmp_path, toggle_home, monkeypatch
):
    """The census stays pending, yet an edited shared server is still reloaded."""
    shared = {"command": _APPROVED_CMD, "args": []}
    _agents(toggle_home, a={"srv": shared})
    (tmp_path / "config.json").write_text(
        json.dumps({"mcp_gateway": {"stub_overrides": {"srv": True}}})
    )
    calls: list[str] = []

    def _share():
        calls.append("share")
        raise auto_share.CensusIncomplete("crewmate record unreadable")

    monkeypatch.setattr(auto_share, "share_declared_servers", _share)
    applied: list[bool] = []
    orch = _watch_orch(applied)
    polls = {"n": 0}

    async def _wait(_secs):
        polls["n"] += 1
        if polls["n"] == 2:
            _agents(toggle_home, a={"srv": {**shared, "args": ["-c", "pass"]}})
        elif polls["n"] == 4:
            raise _StopWatch

    _run_watch(orch, _wait)

    # The edit's poll and the next both run the pending pass; the edit is applied.
    assert len(calls) == 2
    assert applied[:1] == [True]


def test_an_unchanged_start_with_a_saved_folder_answer_is_served_from_cache(tmp_path, toggle_home):
    """The folder inputs in the cache key survive the fingerprint's JSON round trip."""
    mod = _approval_module()
    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])

    def _pass():
        approvals = mod.load_approvals()
        approvals.auto = True
        rewriter.rewrite_agents(
            source_dir=toggle_home,
            overlay_dir=tmp_path / "boot" / "mcp-gateway" / "agents",
            socket_path=tmp_path / "gw.sock",
            work_dir=tmp_path / "work",
            stub_servers=frozenset({"srv"}),
            approvals=approvals,
            cwd_answers={"srv": "not_used"},
            cwd_bindings={"srv": "binding"},
        )
        mod.save_pass(approvals)
        return approvals

    assert _pass().full_pass is True
    # The first pass recorded the launch, which is in the key; from then on an
    # unchanged start is a cache hit.
    _pass()
    assert _pass().full_pass is False


def test_a_removed_server_leaves_the_share_list_and_an_off_stays(tmp_path, toggle_home):
    """A share entry for a server no declaration carries is dropped; a person's off is kept."""
    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    overrides = {"srv": True, "gone-mcp": True, "gone-off": False}
    (tmp_path / "config.json").write_text(
        json.dumps({"mcp_gateway": {"stub_overrides": overrides}})
    )

    assert auto_share.share_declared_servers() == ["gone-mcp"]
    assert _config(tmp_path)["mcp_gateway"]["stub_overrides"] == {"srv": True, "gone-off": False}


def test_a_share_the_pass_would_not_add_back_is_never_dropped(tmp_path, toggle_home):
    """A folder-answered, roster or kirocrew-* share stays; so does every share while a file will not parse."""
    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    overrides = {"srv": True, "answered": True, "kirocrew-computer": True, "gone": True}
    gateway = {"stub_overrides": overrides, "cwd_use": {"answered": "not_used"}}
    (tmp_path / "config.json").write_text(json.dumps({"mcp_gateway": gateway}))

    settings = toggle_home.parent / "settings" / "mcp.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text("{ not json")
    assert auto_share.share_declared_servers() == []

    settings.unlink()
    assert auto_share.share_declared_servers() == ["gone"]
    assert set(_config(tmp_path)["mcp_gateway"]["stub_overrides"]) == {
        "srv",
        "answered",
        "kirocrew-computer",
    }


def test_the_watch_shares_an_added_server_and_reloads_a_changed_launch(tmp_path, toggle_home):
    """An added or edited launch is applied live; an unchanged poll applies nothing."""
    from types import SimpleNamespace

    from kiro_crew.config.loader import KiroCrewConfig

    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    applied: list[bool] = []
    polls = {"n": 0}

    async def _apply(*, launch_approved: bool = False):
        applied.append(launch_approved)
        return {"applied": True}

    orch = SimpleNamespace(
        _cfg=KiroCrewConfig.load(), _apply_mcp_stub=_apply, _mcp_stub_servers_started=frozenset()
    )

    async def _wait(_secs):
        polls["n"] += 1
        if polls["n"] == 1:
            # The person edits the launch between two polls.
            _declare(toggle_home, "agent spec", _APPROVED_CMD, ["-c", "pass"])
            os.utime(toggle_home / "a.json", ns=(1, 1))
        elif polls["n"] == 3:
            raise _StopWatch

    _run_watch(orch, _wait)

    assert applied == [True]
    assert _config(tmp_path)["mcp_gateway"]["stub_overrides"] == {"srv": True}


class _StopWatch(Exception):
    """Raised from the injected wait to end a watch that runs for the gateway's lifetime."""


def _run_watch(orch, wait) -> None:
    import asyncio

    import pytest

    from kiro_crew.slack import gateway as gw

    with pytest.raises(_StopWatch):
        asyncio.run(gw.GatewayOrchestrator._watch_mcp_declarations(orch, poll_secs=0, wait=wait))


def _watch_orch(applied: list[bool]):
    from types import SimpleNamespace

    from kiro_crew.config.loader import KiroCrewConfig

    async def _apply(*, launch_approved: bool = False):
        applied.append(launch_approved)
        return {"applied": True}

    return SimpleNamespace(
        _cfg=KiroCrewConfig.load(),
        _apply_mcp_stub=_apply,
        _mcp_stub_servers_started=frozenset(),
        _mcp_gateway_pending=None,
    )


def test_the_watch_outlives_sharing_off_and_shares_what_was_added_meanwhile(tmp_path, toggle_home):
    """Off, then on again: the first poll after shares a server added while it was off."""
    (tmp_path / "config.json").write_text(json.dumps({"mcp_gateway": {"enabled": False}}))
    _agents(toggle_home, a={})
    applied: list[bool] = []
    orch = _watch_orch(applied)
    polls = {"n": 0}

    async def _wait(_secs):
        polls["n"] += 1
        if polls["n"] == 1:
            _agents(toggle_home, a={"srv": {"command": _APPROVED_CMD, "args": []}})
        elif polls["n"] == 3:
            assert applied == []
            (tmp_path / "config.json").write_text(json.dumps({"mcp_gateway": {"enabled": True}}))
        elif polls["n"] == 5:
            raise _StopWatch

    _run_watch(orch, _wait)

    assert applied == [True]
    assert _config(tmp_path)["mcp_gateway"]["stub_overrides"] == {"srv": True}


def test_the_watch_restarts_the_broker_only_when_what_it_serves_moved(tmp_path, toggle_home):
    """A per-chat server's edit, or sharing switched back on with nothing new, forces no restart."""
    shared = {"command": _APPROVED_CMD, "args": []}
    _agents(toggle_home, a={"srv": shared, "fs": {"command": _APPROVED_CMD, "args": ["."]}})
    (tmp_path / "config.json").write_text(
        json.dumps({"mcp_gateway": {"stub_overrides": {"srv": True}}})
    )
    applied: list[bool] = []
    orch = _watch_orch(applied)
    polls = {"n": 0}

    async def _wait(_secs):
        polls["n"] += 1
        if polls["n"] == 1:
            # The folder-named server stays per chat; its edit routes nothing new.
            _agents(
                toggle_home, a={"srv": shared, "fs": {"command": _APPROVED_CMD, "args": ["./x"]}}
            )
        elif polls["n"] == 2:
            assert applied == [False]
            # A shared server's launch moves: that one restarts the broker.
            _agents(
                toggle_home,
                a={
                    "srv": {**shared, "args": ["-c", "pass"]},
                    "fs": {"command": _APPROVED_CMD, "args": ["./x"]},
                },
            )
        elif polls["n"] == 3:
            raise _StopWatch

    _run_watch(orch, _wait)

    assert applied == [False, True]


def test_the_watch_applies_the_same_server_added_to_one_more_agent(tmp_path, toggle_home):
    """The declaring file is part of the launch: another agent's chats need the stub too."""
    server = {"srv": {"command": _APPROVED_CMD, "args": []}}
    _agents(toggle_home, a=server)
    applied: list[bool] = []
    orch = _watch_orch(applied)
    polls = {"n": 0}

    async def _wait(_secs):
        polls["n"] += 1
        if polls["n"] == 1:
            (toggle_home / "b.json").write_text(
                json.dumps({"name": "b", "tools": ["*"], "mcpServers": server})
            )
        elif polls["n"] == 3:
            raise _StopWatch

    _run_watch(orch, _wait)

    assert applied == [True]


def test_a_failed_watch_pass_is_retried_on_the_next_poll(tmp_path, toggle_home):
    """A pass that raises keeps the old state, so the next poll runs it again."""
    _agents(toggle_home, a={})
    applied: list[bool] = []
    orch = _watch_orch(applied)
    ok_apply = orch._apply_mcp_stub
    calls = {"n": 0}

    async def _flaky(*, launch_approved: bool = False):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("broker restart failed")
        return await ok_apply(launch_approved=launch_approved)

    orch._apply_mcp_stub = _flaky
    polls = {"n": 0}

    async def _wait(_secs):
        polls["n"] += 1
        if polls["n"] == 1:
            _agents(toggle_home, a={"srv": {"command": _APPROVED_CMD, "args": []}})
        elif polls["n"] == 4:
            raise _StopWatch

    _run_watch(orch, _wait)

    assert calls["n"] == 2
    assert applied == [True]


def test_a_failed_config_write_is_retried_on_the_next_poll(tmp_path, toggle_home, monkeypatch):
    """A pass whose write raises is not taken for a pass with nothing to do."""
    import kiro_crew.config.loader as loader

    _agents(toggle_home, a={})
    applied: list[bool] = []
    orch = _watch_orch(applied)
    locked = loader.update_config_locked
    writes = {"n": 0}

    def _full_once(*args, **kwargs):
        writes["n"] += 1
        if writes["n"] == 1:
            raise OSError(28, "No space left on device")
        return locked(*args, **kwargs)

    monkeypatch.setattr(loader, "update_config_locked", _full_once)
    polls = {"n": 0}

    async def _wait(_secs):
        polls["n"] += 1
        if polls["n"] == 1:
            _agents(toggle_home, a={"srv": {"command": _APPROVED_CMD, "args": []}})
        elif polls["n"] == 4:
            raise _StopWatch

    _run_watch(orch, _wait)

    assert writes["n"] == 2
    assert applied == [True]
    assert _config(tmp_path)["mcp_gateway"]["stub_overrides"] == {"srv": True}


def test_an_apply_the_broker_did_not_take_is_retried_on_the_next_poll(tmp_path, toggle_home):
    """``applied`` false from the live apply keeps the pass pending."""
    _agents(toggle_home, a={})
    results = [{"applied": False}, {"applied": True}]
    seen: list[bool] = []
    orch = _watch_orch([])

    async def _apply(*, launch_approved: bool = False):
        seen.append(launch_approved)
        return results.pop(0) if results else {"applied": True}

    orch._apply_mcp_stub = _apply
    polls = {"n": 0}

    async def _wait(_secs):
        polls["n"] += 1
        if polls["n"] == 1:
            _agents(toggle_home, a={"srv": {"command": _APPROVED_CMD, "args": []}})
        elif polls["n"] == 5:
            raise _StopWatch

    _run_watch(orch, _wait)

    assert seen == [True, True]


def test_a_broker_start_being_retried_is_not_run_again(tmp_path, toggle_home):
    """A failed start the broker retries by itself counts as landed: no restart per poll."""
    _agents(toggle_home, a={})
    seen: list[bool] = []
    orch = _watch_orch([])

    async def _apply(*, launch_approved: bool = False):
        seen.append(launch_approved)
        orch._mcp_gateway_pending = object()  # the start failed; its retry task runs
        return {"applied": False}

    orch._apply_mcp_stub = _apply
    polls = {"n": 0}

    async def _wait(_secs):
        polls["n"] += 1
        if polls["n"] == 1:
            _agents(toggle_home, a={"srv": {"command": _APPROVED_CMD, "args": []}})
        elif polls["n"] == 5:
            raise _StopWatch

    _run_watch(orch, _wait)

    assert seen == [True]


def test_on_windows_a_hand_edited_mcp_file_is_rebuilt_shared_and_reloaded(
    tmp_path, toggle_home, monkeypatch
):
    r"""B4 on Windows: an edit to ``%USERPROFILE%\.kiro\settings\mcp.json`` alone.

    Nothing else rebuilds the agent spec from that file there, so the watch does,
    then shares the added server and reloads the edited launch.
    """
    from kiro_crew import platform_compat

    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    uvx = r"C:\MCPW\UVENV\SCRIPTS\UVX.EXE"
    _agents(toggle_home, kirocrew={"time": {"command": uvx, "args": ["mcp-server-time"]}})
    sources = {"now": (("settings/mcp.json", b"before"),)}
    rebuilds: list[str] = []

    def _rebuild():
        # What ``rebuild_agent_config`` writes from the edited mcp.json.
        rebuilds.append("rebuild")
        _agents(
            toggle_home,
            kirocrew={
                "time": {"command": uvx, "args": ["mcp-server-time", "--local-timezone=X"]},
                "time-upper": {"command": uvx, "args": ["mcp-server-time"]},
            },
        )
        return True

    monkeypatch.setattr(auto_share, "mcp_sources_fingerprint", lambda: sources["now"])
    monkeypatch.setattr(auto_share, "refresh_agent_specs", _rebuild)
    applied: list[bool] = []
    orch = _watch_orch(applied)
    polls = {"n": 0}

    async def _wait(_secs):
        polls["n"] += 1
        if polls["n"] == 1:
            # The person edits only the kiro-cli settings file, by hand.
            sources["now"] = (("settings/mcp.json", b"after"),)
        elif polls["n"] == 3:
            raise _StopWatch

    _run_watch(orch, _wait)

    assert rebuilds == ["rebuild"]
    assert applied == [True]
    overrides = _config(tmp_path)["mcp_gateway"]["stub_overrides"]
    assert overrides.get("time-upper") is True


@pytest.mark.parametrize(
    "args",
    [
        ["mcp-server-time", "--local-timezone=America/Chicago"],
        ["mcp-server-time", "--local-timezone", "Europe/Paris"],
    ],
)
def test_a_time_zone_argument_is_not_a_chat_folder_and_the_server_is_shared(
    tmp_path, toggle_home, args
):
    """A Windows launch with an ``Area/City`` zone arg is shared, not kept per chat."""
    uvx = r"C:\MCPW\UVENV\SCRIPTS\UVX.EXE"
    _agents(toggle_home, kirocrew={"time-upper": {"command": uvx, "args": args}})

    assert not folder_use.read_declarations(toggle_home).folder_named
    assert auto_share.share_declared_servers() == ["time-upper"]
    assert _config(tmp_path)["mcp_gateway"]["stub_overrides"] == {"time-upper": True}


def test_a_relative_path_with_a_slash_still_names_the_chat_folder(tmp_path, toggle_home):
    """Only a known zone name is exempt: ``data/db.sqlite`` keeps the server per chat."""
    _agents(toggle_home, a={"db": {"command": "uvx", "args": ["mcp-db", "data/db.sqlite"]}})

    assert folder_use.read_declarations(toggle_home).folder_named == {"db"}
    assert auto_share.share_declared_servers() == []


def test_a_spec_windows_holds_for_a_replace_is_not_a_removed_server(
    tmp_path, toggle_home, monkeypatch
):
    """KAS on Windows: a read that hits a sharing violation must not drop a shared server."""
    import kiro_crew.agent_discovery as discovery

    _agents(toggle_home, kirocrew={"time": {"command": _APPROVED_CMD, "args": []}})
    assert auto_share.share_declared_servers() == ["time"]
    real = discovery.read_agent_spec_strict

    def _held(path, **kwargs):
        if path.name == "kirocrew.json":
            exc = PermissionError(13, "The process cannot access the file")
            exc.winerror = 32
            raise exc
        return real(path, **kwargs)

    monkeypatch.setattr(discovery, "read_agent_spec_strict", _held)

    assert folder_use.read_declarations(toggle_home).complete is False
    # Not a pass with nothing to do: the caller keeps it pending.
    with pytest.raises(auto_share.CensusIncomplete):
        auto_share.share_declared_servers()
    assert _config(tmp_path)["mcp_gateway"]["stub_overrides"] == {"time": True}


def test_the_global_mcp_file_follows_kiro_home(tmp_path):
    """The spec builder, the MCP page's reader and its writer read kiro-cli's file under KIRO_HOME."""
    import subprocess
    import sys

    kiro = tmp_path / "kiro"
    kiro.mkdir()
    code = (
        "from kiro_crew import agent, mcp_discovery_server_info as info;"
        "from kiro_crew.dashboard.handlers import mcp_config_files as files;"
        "print(agent._KIRO_MCP_JSON);"
        "print(info._mcp_sources()[1][0]);"
        "print(files._GLOBAL_MCP_JSON)"
    )
    env = {**os.environ, "KIRO_HOME": str(kiro), "KIROCREW_HOME": str(tmp_path / "crew")}
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=120,  # flake-ok: bounds a child that only imports, not a wait
        check=True,
    ).stdout.split()

    assert out == [str(kiro.resolve() / "settings" / "mcp.json")] * 3
