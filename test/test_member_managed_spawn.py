"""Finite public-core delegation for KAS member-managed workers."""

from __future__ import annotations

import copy
import json
from unittest.mock import AsyncMock

import pytest
from acp_runtime_helpers import _fast_no_report_ceiling  # noqa: F401
from acp_runtime_helpers import _pinned_kiro_cli_version  # noqa: F401
from acp_runtime_helpers import _stub_sel_for_permission_tests  # noqa: F401
from acp_runtime_helpers import _make_runtime

from kiro_crew import mcp_core, members
from kiro_crew.acp.harness.base import SessionExtras
from kiro_crew.acp.types import ACP_BACKEND_KAS, METHOD_SESSION_LOAD, METHOD_SESSION_NEW
from kiro_crew.mcp_gateway.claim import STUB_SESSION_TOKEN_ENV
from kiro_crew.mcp_gateway.hashing import hash_effective_env

MEMBER_KEY = "dashboard_member-builder"
TOKEN = "a" * 64
TOOLS = members.MEMBER_MANAGED_SPAWN_TOOLS
SCOPE_ENV = members.MEMBER_MANAGED_SPAWN_ENV


def _element(server: str, invocation: str, key: str, token: str) -> dict:
    return {
        "name": server,
        "command": "/opt/kirocrew",
        "args": [invocation],
        "env": [
            {"name": STUB_SESSION_TOKEN_ENV, "value": token},
            {"name": "KIROCREW_SESSION_KEY", "value": key},
        ],
        "type": "stdio",
    }


@pytest.fixture
def mount_floor(monkeypatch):
    from kiro_crew.acp import session_mcp
    from kiro_crew.dashboard import session_control

    monkeypatch.setattr(members, "_member_session_element", _element)
    monkeypatch.setattr(session_control, "member_dispatch_enabled", lambda: True)
    monkeypatch.setattr(session_mcp, "native_settings_sources", lambda _work: [])
    monkeypatch.setattr(session_mcp, "native_mount_withholding", lambda *_args: None)


def _mount(
    *,
    source: dict | None = None,
    descriptor: dict | None = None,
    servers: list[dict] | None = None,
    key: str = MEMBER_KEY,
    token: str = TOKEN,
):
    agents = [descriptor or {"id": "builder", "prompt": "p", "tools": ["fs_read"]}]
    server_list = servers or [{"name": "other", "command": "/other"}]
    source_spec = source or {"name": "builder", "prompt": "p", "tools": ["fs_read"]}
    inputs = (agents, server_list, source_spec)
    before = copy.deepcopy(inputs)
    result = members.member_managed_spawn_mount(
        agents,
        "builder",
        key,
        token,
        server_list,
        source_spec,
        work_dir="/work",
    )
    return inputs, before, result


class TestMemberManagedSpawnMount:
    def test_signed_member_gets_only_the_finite_refs_and_scope(self, mount_floor):
        inputs, before, (agents, servers) = _mount()

        assert agents[0]["tools"] == [
            "fs_read",
            *(f"@kirocrew-core/{name}" for name in TOOLS),
        ]
        core = next(server for server in servers if server["name"] == "kirocrew-core")
        env = {pair["name"]: pair["value"] for pair in core["env"]}
        assert env[STUB_SESSION_TOKEN_ENV] == TOKEN
        assert env["KIROCREW_SESSION_KEY"] == MEMBER_KEY
        assert json.loads(env[SCOPE_ENV]) == list(TOOLS)
        assert inputs == before, "the captured projection, source spec, and array are immutable"

    @pytest.mark.parametrize(
        ("key", "token"),
        [("dashboard_chat-1", TOKEN), (MEMBER_KEY, "")],
        ids=["ordinary", "unsigned"],
    )
    def test_ineligible_session_is_unchanged(self, mount_floor, key, token):
        inputs, _before, result = _mount(key=key, token=token)
        assert result[0] is inputs[0]
        assert result[1] is inputs[1]

    @pytest.mark.parametrize("failure", [False, True], ids=["switched-off", "read-failed"])
    def test_member_dispatch_opt_out_withholds_the_mount(self, mount_floor, monkeypatch, failure):
        from kiro_crew.dashboard import session_control

        if failure:
            monkeypatch.setattr(
                session_control,
                "member_dispatch_enabled",
                lambda: (_ for _ in ()).throw(ValueError("unreadable")),
            )
        else:
            monkeypatch.setattr(session_control, "member_dispatch_enabled", lambda: False)
        inputs, _before, result = _mount()
        assert result[0] is inputs[0]
        assert result[1] is inputs[1]

    @pytest.mark.parametrize(
        "source",
        [
            {"name": "builder", "prompt": "p", "tools": []},
            {
                "name": "builder",
                "prompt": "p",
                "tools": ["@kirocrew-core/skill_search"],
            },
            {
                "name": "builder",
                "prompt": "p",
                "tools": ["fs_read"],
                "mcpServers": {"kirocrew-core": {"command": "/custom"}},
            },
        ],
        ids=["empty-tools", "explicit-core-ref", "explicit-core-server"],
    )
    def test_authored_empty_or_explicit_core_selection_is_preserved(self, mount_floor, source):
        inputs, _before, result = _mount(source=source)
        assert result[0] is inputs[0]
        assert result[1] is inputs[1]

    @pytest.mark.parametrize(
        "source",
        [
            {
                "name": "builder",
                "prompt": "p",
                "tools": ["fs_read"],
                "managedToolPolicy": [],
            },
            {
                "name": "builder",
                "prompt": "p",
                "tools": ["fs_read"],
                "managedToolPolicy": {"exclude": "spawn_status"},
            },
            {
                "name": "builder",
                "prompt": "p",
                "tools": ["fs_read"],
                "excludedTools": [7],
            },
        ],
        ids=["policy-shape", "exclude-shape", "excluded-tool-shape"],
    )
    def test_malformed_policy_fails_closed(self, mount_floor, source):
        inputs, _before, result = _mount(source=source)
        assert result[0] is inputs[0]
        assert result[1] is inputs[1]

    def test_excluded_tools_narrow_the_finite_scope(self, mount_floor):
        source = {
            "name": "builder",
            "prompt": "p",
            "tools": ["fs_read"],
            "excludedTools": ["@kirocrew-core/spawn_status"],
        }
        _inputs, _before, (agents, servers) = _mount(source=source)
        assert "@kirocrew-core/spawn_status" not in agents[0]["tools"]
        core = next(server for server in servers if server["name"] == "kirocrew-core")
        env = {pair["name"]: pair["value"] for pair in core["env"]}
        assert json.loads(env[SCOPE_ENV]) == ["spawn_run", "spawn_list", "spawn_steer"]

    def test_excluding_spawn_run_withholds_the_mount(self, mount_floor):
        source = {
            "name": "builder",
            "prompt": "p",
            "tools": ["fs_read"],
            "managedToolPolicy": {"exclude": ["spawn_*"]},
        }
        inputs, _before, result = _mount(source=source)
        assert result[0] is inputs[0]
        assert result[1] is inputs[1]

    def test_native_disabled_or_unreadable_settings_withhold(self, mount_floor, monkeypatch):
        from kiro_crew.acp import session_mcp

        monkeypatch.setattr(session_mcp, "native_mount_withholding", lambda *_args: object())
        inputs, _before, result = _mount()
        assert result[0] is inputs[0]
        assert result[1] is inputs[1]

        monkeypatch.setattr(
            session_mcp,
            "native_settings_sources",
            lambda _work: (_ for _ in ()).throw(ValueError("unreadable")),
        )
        inputs, _before, result = _mount()
        assert result[0] is inputs[0]
        assert result[1] is inputs[1]

    def test_existing_projected_skill_search_is_retained_once(self, mount_floor):
        descriptor = {
            "id": "builder",
            "prompt": "p",
            "tools": ["fs_read", "@kirocrew-core/skill_search"],
            "permissions": {"rules": [{"capability": "mcp", "effect": "allow"}]},
        }
        servers = [_element("kirocrew-core", "mcp-core", MEMBER_KEY, TOKEN)]
        _inputs, _before, (agents, mounted) = _mount(descriptor=descriptor, servers=servers)
        assert agents[0]["tools"].count("@kirocrew-core/skill_search") == 1
        assert agents[0]["permissions"] is descriptor["permissions"]
        core = next(server for server in mounted if server["name"] == "kirocrew-core")
        scope = json.loads(next(p["value"] for p in core["env"] if p["name"] == SCOPE_ENV))
        assert scope == [*TOOLS, "skill_search"]

    def test_scope_env_partitions_the_existing_pool_key(self):
        ordinary = {"KIROCREW_HOME": "/home/test"}
        finite = {**ordinary, SCOPE_ENV: json.dumps(list(TOOLS))}
        assert hash_effective_env(ordinary) != hash_effective_env(finite)


class TestFiniteCoreProcess:
    def test_unscoped_core_is_unchanged(self, monkeypatch):
        monkeypatch.delenv(SCOPE_ENV, raising=False)
        names = mcp_core._list_tool_names()
        assert "learn_add" in names
        assert set(TOOLS) <= set(names)

    def test_discovery_and_dispatch_share_one_scope(self, monkeypatch):
        monkeypatch.setenv(SCOPE_ENV, json.dumps(list(TOOLS)))
        monkeypatch.setattr(mcp_core, "build_tool_names", lambda: [*TOOLS, "learn_add"])
        monkeypatch.setattr(
            mcp_core,
            "build_tool_list",
            lambda: [{"name": name, "description": name} for name in [*TOOLS, "learn_add"]],
        )
        called = []
        monkeypatch.setattr(mcp_core, "dispatch", lambda name, args: called.append(name) or "ok")

        assert mcp_core._list_tool_names() == list(TOOLS)
        assert [tool["name"] for tool in mcp_core._list_tools()] == list(TOOLS)
        assert mcp_core._call_tool_inner("learn_add", {}).startswith("Error:")
        assert mcp_core._call_tool_inner("spawn_run", {}) == "ok"
        assert called == ["spawn_run"]

    @pytest.mark.parametrize("raw", ["not-json", json.dumps(["learn_add"]), json.dumps("x")])
    def test_malformed_or_widened_scope_is_empty(self, monkeypatch, raw):
        monkeypatch.setenv(SCOPE_ENV, raw)
        assert mcp_core._list_tool_names() == []
        assert mcp_core._call_tool_inner("spawn_run", {}).startswith("Error:")


class TestRuntimeRequestWiring:
    @staticmethod
    def _prepare(monkeypatch):
        from kiro_crew.acp import runtime as runtime_mod
        from kiro_crew.dashboard import session_control

        rt, _, _ = _make_runtime()
        rt._acp_backend = ACP_BACKEND_KAS
        rt._can_load_session = True
        monkeypatch.setattr(members, "_member_session_element", _element)
        monkeypatch.setattr(members, "crew_panel_enabled", lambda: False)
        monkeypatch.setattr(session_control, "member_dispatch_enabled", lambda: True)
        monkeypatch.setattr(runtime_mod, "session_mcp_server_is_disabled", lambda *_a, **_k: False)

        from kiro_crew.acp import session_mcp

        monkeypatch.setattr(session_mcp, "native_settings_sources", lambda _work: [])
        monkeypatch.setattr(session_mcp, "native_mount_withholding", lambda *_args: None)

        source = {"name": "builder", "prompt": "p", "tools": ["fs_read"]}

        async def _agents(_agent, **_kwargs):
            return SessionExtras(
                custom_agents=[{"id": "builder", "prompt": "p", "tools": ["fs_read"]}],
                source_spec=source,
            )

        monkeypatch.setattr(rt, "_kas_custom_agents", _agents)
        monkeypatch.setattr(rt, "_wait_managed_mcp", AsyncMock(return_value=None))
        sent = []

        async def _send(method, params, timeout=None):
            sent.append((method, params))
            if method == METHOD_SESSION_NEW:
                return {"sessionId": "sid-new", "modes": {"currentModeId": "builder"}}
            if method == METHOD_SESSION_LOAD:
                return {"modes": {"currentModeId": "builder"}, "models": []}
            return {}

        monkeypatch.setattr(rt, "_send_and_await", _send)
        return rt, sent

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "method", [METHOD_SESSION_NEW, METHOD_SESSION_LOAD], ids=["new", "load"]
    )
    async def test_both_session_paths_send_the_finite_tokened_mount(self, monkeypatch, method):
        rt, sent = self._prepare(monkeypatch)
        if method == METHOD_SESSION_NEW:
            await rt.create_session(
                cwd="/work",
                agent="builder",
                member_session_key=MEMBER_KEY,
                session_key=MEMBER_KEY,
            )
        else:
            await rt.load_session(
                "",
                "sid-load",
                cwd="/work",
                agent="builder",
                member_session_key=MEMBER_KEY,
                session_key=MEMBER_KEY,
            )

        params = next(params for sent_method, params in sent if sent_method == method)
        [agent] = params["_meta"]["kiro"]["customAgents"]
        assert agent["tools"] == [
            "fs_read",
            *(f"@kirocrew-core/{name}" for name in TOOLS),
        ]
        core = next(server for server in params["mcpServers"] if server["name"] == "kirocrew-core")
        env = {pair["name"]: pair["value"] for pair in core["env"]}
        assert env[STUB_SESSION_TOKEN_ENV]
        assert env["KIROCREW_SESSION_KEY"] == MEMBER_KEY
        assert json.loads(env[SCOPE_ENV]) == list(TOOLS)

    @pytest.mark.asyncio
    async def test_ordinary_kas_session_keeps_the_normal_core(self, monkeypatch):
        rt, sent = self._prepare(monkeypatch)
        await rt.create_session(cwd="/work", agent="builder", session_key="dashboard_chat-1")
        params = next(params for method, params in sent if method == METHOD_SESSION_NEW)
        assert all(
            pair.get("name") != SCOPE_ENV
            for server in params["mcpServers"]
            for pair in server.get("env", [])
        )
        assert params["_meta"]["kiro"]["customAgents"][0]["tools"] == ["fs_read"]
