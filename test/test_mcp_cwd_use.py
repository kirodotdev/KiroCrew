"""A server goes on the one shared backend only when a fact marks it folder-free.

A shared backend runs in one fixed folder outside every sandbox. So the Share
press refuses (``per_project``) a server that gets one backend per project
instead: every server not marked folder-free by Kiro Crew, the edition, or a
saved ``not_used`` fact in ``mcp_gateway.cwd_use``. The config resolver never
routes one marked ``uses_cwd``. The fact is recorded per server, so a renamed
server has none and its old approval does not carry over, and a ``not_used``
fact is bound to the launch it was given for. A per-project row's Share switch
keeps a server one per chat when off and drops that choice when on again.
"""

from __future__ import annotations

import json

import pytest
from body_stream_helpers import attach_body
from mcp_launch_approval_helpers import (  # noqa: F401 -- the fixture is requested by name
    _APPROVED_CMD,
    _approval_module,
    _declare,
    _targets_running,
    toggle_home_fixture,
)

from kiro_crew.config.integration_sections import _resolve_stub_servers
from kiro_crew.mcp_gateway import rewriter

#: Opts this module out of the conftest fixture that approves every gatewayd launch.
ENFORCE_LAUNCH_APPROVAL = True


def _raw_request(body: dict):
    """A stub request sent exactly as given: no answer is filled in for it."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from aiohttp import web
    from dashboard_owner_helpers import owner_claims

    request = MagicMock(spec=web.Request)
    request.app = {"state": SimpleNamespace(_mcp_gateway_apply_stub=None)}
    attach_body(request, body)
    return owner_claims(request)


async def _post(body: dict) -> tuple[int, dict]:
    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    response = await mcp_mod.api_mcp_gateway_set_stub(_raw_request(body))
    return response.status, json.loads(response.body)


async def _expected_launch(name: str = "srv") -> str:
    import asyncio

    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    launches = await asyncio.to_thread(
        mcp_mod.launch_resolve.resolve_launches, [name], refresh_specs=False
    )
    return mcp_mod.display_launches(name, launches[name])["expected_launch"]


def _config(tmp_path) -> dict:
    return json.loads((tmp_path / "config.json").read_text())


def _boot_rewrite(tmp_path, agents):
    """The next gateway start: the routed set comes from config, as boot reads it."""
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.mcp_gateway.launch_resolve import rewrite_kwargs

    cfg = KiroCrewConfig.load()
    stubs = frozenset(cfg.mcp_gateway.stub_servers)
    inputs = rewrite_kwargs(cfg, stubs)
    inputs.update(
        source_dir=agents,
        overlay_dir=tmp_path / "boot" / "mcp-gateway" / "agents",
        socket_path=tmp_path / "gw.sock",
        work_dir=tmp_path / "work",
    )
    _results, target_env = rewriter.rewrite_agents(
        **inputs, approvals=_approval_module().load_approvals()
    )
    return stubs, target_env


@pytest.mark.parametrize(
    ("section", "expected"),
    [
        ({"stub_servers": ["a", "b"]}, ["a", "b"]),
        ({"stub_servers": ["a", "b"], "cwd_use": {"a": "uses_cwd"}}, ["b"]),
        ({"stub_overrides": {"a": True}, "cwd_use": {"a": "uses_cwd"}}, []),
        ({"stub_servers": ["a"], "cwd_use": {"a": "not_used"}}, ["a"]),
        # An unknown value reads as unanswered: it neither routes nor unroutes.
        ({"stub_servers": ["a"], "cwd_use": {"a": "yes", "b": 1}}, ["a"]),
        ({"stub_servers": ["a"], "cwd_use": "uses_cwd"}, ["a"]),
        ({"stub_servers": ["a"], "cwd_use": {"a": ["uses_cwd"], "b": {}}}, ["a"]),
    ],
)
def test_a_server_that_uses_the_folder_is_never_in_the_routed_set(section, expected):
    assert _resolve_stub_servers(section) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("where", ["agent spec", "settings mcp.json"])
async def test_routing_an_unanswered_server_is_refused_and_writes_nothing(
    tmp_path, toggle_home, where
):
    _declare(toggle_home, where, _APPROVED_CMD, [])
    expected = await _expected_launch()

    status, body = await _post({"name": "srv", "stub": True, "expected_launch": expected})

    assert (status, body["code"]) == (409, "per_project")
    assert "mcp_gateway" not in _config(tmp_path)
    assert _approval_module().load_approvals().approved == {}
    stubs, target_env = _boot_rewrite(tmp_path, toggle_home)
    assert "srv" not in stubs and _targets_running(target_env, _APPROVED_CMD) == []


@pytest.mark.asyncio
async def test_a_vault_server_with_no_folder_fact_stays_per_chat(
    tmp_path, toggle_home, monkeypatch
):
    """A server that cannot go per project (its env names a vault secret) and has
    no folder-free fact never reaches the shared backend outside every sandbox."""
    from kiro_crew import agent

    # The folder-fact scan reads the agents dir the gateway reads.
    monkeypatch.setattr(agent, "KIRO_AGENTS_DIR", toggle_home)
    (toggle_home / "a.json").write_text(
        json.dumps(
            {
                "name": "a",
                "tools": ["*"],
                "mcpServers": {
                    "srv": {
                        "command": _APPROVED_CMD,
                        "args": [],
                        "env": {"TOKEN": "secret://srv-token"},
                    }
                },
            }
        )
    )
    expected = await _expected_launch()

    status, body = await _post({"name": "srv", "stub": True, "expected_launch": expected})

    assert (status, body["code"]) == (409, "per_chat")
    assert _approval_module().load_approvals().approved == {}


@pytest.mark.asyncio
async def test_a_vault_server_row_says_it_runs_one_per_chat(tmp_path, rows_home, monkeypatch):
    """The row the Share press would be refused on says so, and why, so the
    page offers no press that can never succeed."""
    from kiro_crew import agent

    monkeypatch.setattr(agent, "KIRO_AGENTS_DIR", rows_home)
    (rows_home / "a.json").write_text(
        json.dumps(
            {
                "name": "a",
                "tools": ["*"],
                "mcpServers": {
                    "srv": {
                        "command": _APPROVED_CMD,
                        "args": [],
                        "env": {"TOKEN": "secret://srv-token"},
                    },
                    "plain": {"command": _APPROVED_CMD, "args": ["--other"]},
                },
            }
        )
    )

    row = await _listed_row("srv")
    assert (row["per_project"], row["per_chat"], row["per_chat_reason"]) == (False, True, "vault")
    other = await _listed_row("plain")
    assert (other["per_project"], other["per_chat"], other["per_chat_reason"]) == (True, False, "")


@pytest.mark.asyncio
async def test_a_server_answered_uses_cwd_cannot_be_routed(tmp_path, toggle_home):
    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    expected = await _expected_launch()

    status, body = await _post(
        {"name": "srv", "stub": True, "expected_launch": expected, "cwd_use": "uses_cwd"}
    )

    assert (status, body["code"]) == (409, "per_project")
    assert _approval_module().load_approvals().approved == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("where", ["agent spec", "settings mcp.json"])
async def test_answering_not_used_routes_the_server_at_the_next_start(tmp_path, toggle_home, where):
    _declare(toggle_home, where, _APPROVED_CMD, [])
    expected = await _expected_launch()

    status, _body = await _post(
        {"name": "srv", "stub": True, "expected_launch": expected, "cwd_use": "not_used"}
    )

    assert status == 200
    assert _config(tmp_path)["mcp_gateway"]["cwd_use"] == {"srv": "not_used"}
    stubs, target_env = _boot_rewrite(tmp_path, toggle_home)
    assert "srv" in stubs and _targets_running(target_env, _APPROVED_CMD)


@pytest.mark.asyncio
async def test_answering_uses_cwd_later_unroutes_a_routed_server(tmp_path, toggle_home):
    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    expected = await _expected_launch()
    await _post({"name": "srv", "stub": True, "expected_launch": expected, "cwd_use": "not_used"})

    status, body = await _post({"name": "srv", "stub": False, "cwd_use": "uses_cwd"})

    assert status == 200 and body["stub"] is False
    assert _config(tmp_path)["mcp_gateway"]["cwd_use"] == {"srv": "uses_cwd"}
    stubs, target_env = _boot_rewrite(tmp_path, toggle_home)
    assert "srv" not in stubs and _targets_running(target_env, _APPROVED_CMD) == []


@pytest.mark.asyncio
async def test_a_routed_server_with_no_answer_keeps_running_but_reapproval_asks(
    tmp_path, toggle_home
):
    """Routed before the question existed: it keeps today's behaviour until answered."""
    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    expected = await _expected_launch()
    await _post({"name": "srv", "stub": True, "expected_launch": expected, "cwd_use": "not_used"})
    data = _config(tmp_path)
    del data["mcp_gateway"]["cwd_use"]
    (tmp_path / "config.json").write_text(json.dumps(data))

    stubs, target_env = _boot_rewrite(tmp_path, toggle_home)
    assert "srv" in stubs and _targets_running(target_env, _APPROVED_CMD)

    status, body = await _post({"name": "srv", "stub": True, "expected_launch": expected})
    assert (status, body["code"]) == (409, "per_project")


@pytest.mark.asyncio
async def test_a_hand_written_routing_entry_runs_nothing_routed(tmp_path, toggle_home):
    """A name put in ``stub_servers`` by hand has no approval, so it stays in the chat."""
    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    (tmp_path / "config.json").write_text(json.dumps({"mcp_gateway": {"stub_servers": ["srv"]}}))

    stubs, target_env = _boot_rewrite(tmp_path, toggle_home)

    assert "srv" in stubs and _targets_running(target_env, _APPROVED_CMD) == []


@pytest.mark.asyncio
async def test_a_renamed_server_is_unanswered_and_unapproved(tmp_path, toggle_home):
    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    expected = await _expected_launch()
    await _post({"name": "srv", "stub": True, "expected_launch": expected, "cwd_use": "not_used"})
    spec = json.loads((toggle_home / "a.json").read_text())
    spec["mcpServers"] = {"srv2": spec["mcpServers"]["srv"]}
    (toggle_home / "a.json").write_text(json.dumps(spec))
    data = _config(tmp_path)
    data["mcp_gateway"]["stub_overrides"]["srv2"] = True
    (tmp_path / "config.json").write_text(json.dumps(data))

    _stubs, target_env = _boot_rewrite(tmp_path, toggle_home)
    assert _targets_running(target_env, _APPROVED_CMD) == []

    status, body = await _post(
        {"name": "srv2", "stub": True, "expected_launch": await _expected_launch("srv2")}
    )
    assert (status, body["code"]) == (409, "per_project")


@pytest.mark.asyncio
async def test_changed_args_after_the_answer_stop_routing_until_approved(tmp_path, toggle_home):
    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    expected = await _expected_launch()
    await _post({"name": "srv", "stub": True, "expected_launch": expected, "cwd_use": "not_used"})

    _declare(toggle_home, "agent spec", _APPROVED_CMD, ["-m", "srv_in_this_folder"])

    _stubs, target_env = _boot_rewrite(tmp_path, toggle_home)
    assert _targets_running(target_env, _APPROVED_CMD) == []


@pytest.mark.asyncio
async def test_the_batch_share_skips_an_unanswered_server(tmp_path, toggle_home, monkeypatch):
    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    expected = await _expected_launch()
    monkeypatch.setattr(mcp_mod, "_stub_eligibility", lambda names, **_kw: (list(names), []))

    status, body = await _post(
        {"name": "srv", "stub": True, "expected_launch": expected, "resolve_eligibility": True}
    )

    assert status == 200
    assert body["stubbed"] == []
    assert body["skipped"] == [{"name": "srv", "reason": "per_project"}]
    assert _approval_module().load_approvals().approved == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"name": "srv", "cwd_use": "maybe"}, "cwd_use_invalid"),
        ({"names": ["srv"], "stub": False, "cwd_use": "uses_cwd"}, "cwd_use_invalid"),
        ({"name": "kirocrew-core", "cwd_use": "uses_cwd"}, "cwd_use_not_asked"),
        ({"name": "srv", "cwd_use": []}, "cwd_use_invalid"),
    ],
)
async def test_a_bad_answer_is_refused(tmp_path, toggle_home, body, code):
    status, answer = await _post(body)

    assert (status, answer.get("code")) == (400, code)
    assert _config(tmp_path) == {}


@pytest.mark.asyncio
async def test_answering_uses_cwd_forgets_the_approval(tmp_path, toggle_home):
    """Losing the answer later by hand cannot bring the server back routed."""
    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    expected = await _expected_launch()
    await _post({"name": "srv", "stub": True, "expected_launch": expected, "cwd_use": "not_used"})

    await _post({"name": "srv", "stub": False, "cwd_use": "uses_cwd"})
    data = _config(tmp_path)
    del data["mcp_gateway"]["cwd_use"]
    data["mcp_gateway"]["stub_overrides"] = {"srv": True}
    (tmp_path / "config.json").write_text(json.dumps(data))

    stubs, target_env = _boot_rewrite(tmp_path, toggle_home)
    assert "srv" in stubs and _targets_running(target_env, _APPROVED_CMD) == []


@pytest.mark.asyncio
async def test_an_overlay_answer_for_another_server_keeps_the_base_answer(
    tmp_path, toggle_home, monkeypatch
):
    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    expected = await _expected_launch()
    (tmp_path / "config.json").write_text(
        json.dumps({"mcp_gateway": {"cwd_use": {"srv": "not_used"}}})
    )
    monkeypatch.setattr(
        mcp_mod, "_local_overlay_section", lambda: {"cwd_use": {"other": "uses_cwd"}}
    )

    status, _body = await _post({"name": "srv", "stub": True, "expected_launch": expected})

    assert status == 200


@pytest.mark.asyncio
async def test_a_bad_overlay_answer_hides_the_base_answer(tmp_path, toggle_home, monkeypatch):
    """The overlay's value wins whole, as the loader merges it, even when unreadable."""
    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    expected = await _expected_launch()
    (tmp_path / "config.json").write_text(
        json.dumps({"mcp_gateway": {"cwd_use": {"srv": "not_used"}}})
    )
    monkeypatch.setattr(
        mcp_mod, "_local_overlay_section", lambda: {"cwd_use": {"srv": "uses_cwds"}}
    )

    status, body = await _post({"name": "srv", "stub": True, "expected_launch": expected})

    assert (status, body["code"]) == (409, "per_project")


@pytest.fixture
def rows_home(toggle_home, monkeypatch):
    """The server list's own scan reads the same agents dir the rewrite does."""
    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    monkeypatch.setattr(mcp_mod, "kiro_agents_dir_path", lambda: toggle_home)
    return toggle_home


async def _listed_row(name: str = "srv") -> dict:
    from handlers_mcp_coverage_helpers import _payload, _request

    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    response = await mcp_mod.api_mcp_gateway_servers(_request(method="GET"))
    rows = {r["name"]: r for r in _payload(response)["servers"]}
    return rows[name]


@pytest.mark.parametrize(
    ("answer", "bound", "current", "expected"),
    [
        ("not_used", "h1", "h1", "not_used"),
        ("not_used", "h1", "h2", None),
        # Recorded before bindings existed: kept, so nobody is asked again.
        ("not_used", None, "h2", "not_used"),
        # The launch cannot be read now: today's result.
        ("not_used", "h1", None, "not_used"),
        # Only a not_used answer is bound; uses_cwd only ever blocks routing.
        ("uses_cwd", "h1", "h2", "uses_cwd"),
        (None, "h1", "h2", None),
    ],
)
def test_a_not_used_answer_holds_only_for_its_launch(answer, bound, current, expected):
    from kiro_crew.config.integration_sections import effective_cwd_answer

    assert effective_cwd_answer(answer, bound, current) == expected


def test_the_launch_binding_is_a_modelled_config_field(tmp_path, toggle_home):
    from kiro_crew.config.loader import KiroCrewConfig

    (tmp_path / "config.json").write_text(
        json.dumps({"mcp_gateway": {"cwd_use_launch": {"srv": "h1", "bad": 3}}})
    )
    cfg = KiroCrewConfig.load()
    assert cfg.mcp_gateway.cwd_use_launch == {"srv": "h1"}

    cfg.save()
    assert _config(tmp_path)["mcp_gateway"]["cwd_use_launch"] == {"srv": "h1"}


@pytest.mark.asyncio
async def test_the_answer_is_bound_and_a_changed_launch_goes_per_project(tmp_path, rows_home):
    toggle_home = rows_home
    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    expected = await _expected_launch()
    await _post({"name": "srv", "stub": True, "expected_launch": expected, "cwd_use": "not_used"})
    assert set(_config(tmp_path)["mcp_gateway"]["cwd_use_launch"]) == {"srv"}
    await _post({"name": "srv", "stub": False})

    assert (await _listed_row())["per_project"] is False

    _declare(toggle_home, "agent spec", _APPROVED_CMD, ["-m", "srv_in_this_folder"])
    # The fact holds for another launch, so this one shares per project.
    assert (await _listed_row())["per_project"] is True
    changed = await _expected_launch()
    status, body = await _post({"name": "srv", "stub": True, "expected_launch": changed})
    assert (status, body["code"]) == (409, "per_project")

    status, _body = await _post(
        {"name": "srv", "stub": True, "expected_launch": changed, "cwd_use": "not_used"}
    )
    assert status == 200


@pytest.mark.asyncio
async def test_an_answer_from_before_bindings_routes_and_is_bound(tmp_path, rows_home):
    toggle_home = rows_home
    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    expected = await _expected_launch()
    (tmp_path / "config.json").write_text(
        json.dumps({"mcp_gateway": {"cwd_use": {"srv": "not_used"}}})
    )

    status, _body = await _post({"name": "srv", "stub": True, "expected_launch": expected})

    assert status == 200
    assert set(_config(tmp_path)["mcp_gateway"]["cwd_use_launch"]) == {"srv"}


@pytest.mark.asyncio
async def test_a_failed_scan_keeps_an_unbound_answer_working(tmp_path, rows_home, monkeypatch):
    toggle_home = rows_home
    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    expected = await _expected_launch()

    from kiro_crew.mcp_gateway.folder_use import FolderDeclarations

    monkeypatch.setattr(mcp_mod, "_folder_declarations", lambda: FolderDeclarations(complete=False))
    status, _body = await _post(
        {"name": "srv", "stub": True, "expected_launch": expected, "cwd_use": "not_used"}
    )

    assert status == 200
    assert "cwd_use_launch" not in _config(tmp_path)["mcp_gateway"]


@pytest.mark.asyncio
async def test_an_overlay_answer_is_bound_when_routed(tmp_path, rows_home, monkeypatch):
    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    _declare(rows_home, "agent spec", _APPROVED_CMD, [])
    expected = await _expected_launch()
    monkeypatch.setattr(mcp_mod, "_local_overlay_section", lambda: {"cwd_use": {"srv": "not_used"}})

    status, _body = await _post({"name": "srv", "stub": True, "expected_launch": expected})

    assert status == 200
    assert set(_config(tmp_path)["mcp_gateway"]["cwd_use_launch"]) == {"srv"}


@pytest.mark.asyncio
async def test_an_unanswered_server_is_listed_per_project_with_nothing_to_answer(
    tmp_path, rows_home
):
    _declare(rows_home, "agent spec", _APPROVED_CMD, [])

    row = await _listed_row()

    assert row["per_project"] is True and row["declined"] is False
    assert not {"cwd_use", "cwd_proven", "cwd_roots"} & set(row)


@pytest.mark.asyncio
async def test_dont_share_keeps_one_per_chat_and_turns_off_again(tmp_path, rows_home):
    _declare(rows_home, "agent spec", _APPROVED_CMD, [])

    status, body = await _post({"name": "srv", "dont_share": True})
    assert status == 200 and body["stub"] is False
    assert _config(tmp_path)["mcp_gateway"]["stub_overrides"] == {"srv": False}
    assert (await _listed_row())["declined"] is True

    status, body = await _post({"name": "srv", "dont_share": False})
    assert status == 200 and body["dont_share"] is False
    assert "stub_overrides" not in _config(tmp_path)["mcp_gateway"]
    assert (await _listed_row())["declined"] is False
    assert _approval_module().load_approvals().approved == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"name": "srv", "dont_share": "yes"},
        {"names": ["srv"], "dont_share": True},
        {"name": "srv", "dont_share": True, "stub": False},
        {"name": "srv", "dont_share": False, "cwd_use": "uses_cwd"},
    ],
)
async def test_a_bad_dont_share_is_refused(tmp_path, toggle_home, body):
    status, answer = await _post(body)

    assert (status, answer.get("code")) == (400, "dont_share_invalid")
    assert _config(tmp_path) == {}
