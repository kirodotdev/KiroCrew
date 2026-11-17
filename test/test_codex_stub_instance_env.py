"""A codex broker stub for a Kiro Crew server carries the env that names its gateway.

codex starts every stdio MCP server from an allowlist plus the element's own
``env``, so a pooled broker stub for a Crew server reaches its process with only
its session token. When the broker is unreachable, the stub's fallback execs the
real server with that environment. Without ``KIROCREW_BOUND_PORT`` and the home
override, the server finds its gateway by fallback and can authenticate against
another instance on the same host.

This module pins the codex projection's handling of those stubs:

* a stub for a Crew server gets the instance env
  (:func:`~kiro_crew.providers.mirrors.identity.control_plane_instance_env`): the
  bound port of the gateway that composed the session, beside the token it had;
* the instance env adds no session key, because the stub names its session by
  token;
* a third-party stub is mounted with the env it was given;
* the caller's stub list is not mutated.

The session-bound Crew stubs that ``is_session_bound_crew_stub`` admits get the
same instance env after that shape check, and keep their token as the only
credential; ``test_codex_crew_stub.py`` pins that side.
"""

from __future__ import annotations

OWNER_PORT = 47211  # the gateway that composed the session


class TestCodexStubInstanceEnv:
    def test_a_crew_server_stub_carries_the_bound_port_and_a_third_party_stub_does_not(
        self, tmp_path, monkeypatch
    ):
        import codex_session_mcp_helpers as helpers

        from kiro_crew import agent as agent_mod
        from kiro_crew.acp import session_mcp
        from kiro_crew.providers.mirrors.codex import codex_projection

        agents = tmp_path / "agents"
        agents.mkdir()
        monkeypatch.setattr(agent_mod, "KIRO_AGENTS_DIR", agents)
        monkeypatch.setattr(agent_mod, "_KIRO_MCP_JSON", tmp_path / "settings-mcp.json")
        monkeypatch.setattr(session_mcp, "ensure_agent_materialized", lambda _a: True)
        monkeypatch.setattr(
            session_mcp,
            "managed_mcp_spec_entry",
            lambda name, **_k: dict(helpers._CORE) if name == "kirocrew-core" else None,
        )
        monkeypatch.setattr(session_mcp, "_mcp_registry_mode", lambda: False)
        monkeypatch.setenv("KIROCREW_BOUND_PORT", str(OWNER_PORT))
        monkeypatch.delenv("KIROCREW_PORT", raising=False)
        helpers._write_spec(
            agents,
            servers={"kirocrew-core": dict(helpers._CORE), "echo-mcp": {"command": "/bin/e"}},
            tools=["*"],
        )
        stubs = [
            {
                "name": "kirocrew-core",
                "command": "/stub",
                "args": [],
                "type": "stdio",
                "env": [{"name": "KIROCREW_STUB_SESSION_TOKEN", "value": "tok"}],
            },
            {"name": "echo-mcp", "command": "/stub", "args": [], "env": [], "type": "stdio"},
        ]

        out = codex_projection(
            "kirocrew",
            stub_server_names=("kirocrew-core", "echo-mcp"),
            stub_elements=stubs,
            session_key="dashboard:chat-1-1",
        ).params["mcpServers"]

        by_name = {e["name"]: e for e in out}
        core = {p["name"]: p["value"] for p in by_name["kirocrew-core"]["env"]}
        echo = {p["name"]: p["value"] for p in by_name["echo-mcp"]["env"]}
        assert core.get("KIROCREW_BOUND_PORT") == str(OWNER_PORT)
        assert core.get("KIROCREW_STUB_SESSION_TOKEN") == "tok"
        # Instance only: the stub names its session by token, never by key.
        assert "KIROCREW_SESSION_KEY" not in core
        assert echo == {}
        # The caller's stub list is not mutated.
        assert stubs[0]["env"] == [{"name": "KIROCREW_STUB_SESSION_TOKEN", "value": "tok"}]

    def test_the_home_override_rides_the_stub_and_replaces_a_stale_pair(
        self, tmp_path, monkeypatch
    ):
        """Under a data-home override the stub also names that home, and a pair the
        stub already carried under the same name is replaced by the current value."""
        from kiro_crew.agent_materialization import managed_mcp
        from kiro_crew.providers.mirrors import identity

        home = str(tmp_path / "override-home")
        monkeypatch.setattr(managed_mcp, "_managed_mcp_env", lambda: {"KIROCREW_HOME": home})
        monkeypatch.setenv("KIROCREW_BOUND_PORT", str(OWNER_PORT))
        monkeypatch.delenv("KIROCREW_PORT", raising=False)

        env = identity.control_plane_instance_env(label="codex")
        assert env == {"KIROCREW_HOME": home, "KIROCREW_BOUND_PORT": str(OWNER_PORT)}

        stale = {
            "name": "kirocrew-core",
            "env": [
                {"name": "KIROCREW_STUB_SESSION_TOKEN", "value": "tok"},
                {"name": "KIROCREW_BOUND_PORT", "value": "1"},
            ],
        }
        merged = {p["name"]: p["value"] for p in identity.with_env(stale, env)["env"]}
        assert merged == {
            "KIROCREW_STUB_SESSION_TOKEN": "tok",
            "KIROCREW_HOME": home,
            "KIROCREW_BOUND_PORT": str(OWNER_PORT),
        }
