"""An MCP server Kiro Crew does not own can opt into a per-session identity LABEL.

The reported gap: a server an app installs into the agent spec is launched by the
host straight from that spec, so on the shared kiro-cli runtime it cannot tell which
session it was started for (the process environment names whichever session claimed
the process, and the signed pid mapping is written per turn, after the session's
servers have already started). The opt-in, ``"x-kirocrew-session-label": true`` on
the server's ``mcpServers`` entry, hands that server ``KIROCREW_SESSION_LABEL`` for
the session it serves, and nothing else: not the session token, not the port, not
``KIROCREW_SESSION_KEY``.

The marker and the variable are spelled as literals rather than imported, so the
runtime and KAS tests below fail on an assertion, not only on an import, against a
build without the feature.
"""

from __future__ import annotations

import pytest

from kiro_crew.mcp_gateway.claim import STUB_SESSION_TOKEN_ENV

MARKER = "x-kirocrew-session-label"
LABEL = "KIROCREW_SESSION_LABEL"
KEY = "dashboard:chat-label-1"

APP = {
    "command": "/opt/app/bin/memory-mcp",
    "args": ["serve", "--stdio"],
    "env": {"APP_MODE": "chat"},
    MARKER: True,
}


def _spec(entry=None, *, tools=("@kirocrew-core", "@memory:memory")):
    return {
        "tools": list(tools),
        "mcpServers": {
            "kirocrew-core": {"command": "test-crew", "args": ["mcp-core"]},
            "memory:memory": dict(APP if entry is None else entry),
        },
    }


def _env(element):
    return {pair["name"]: pair["value"] for pair in element.get("env", [])}


@pytest.fixture
def quiet_sources(monkeypatch):
    """No settings files, no registry mode, no builtin apps: the spec alone decides."""
    from kiro_crew.acp import session_mcp

    monkeypatch.setattr(session_mcp, "_global_settings", lambda **kwargs: {})
    monkeypatch.setattr(session_mcp, "_registry_mode", lambda: False)
    monkeypatch.setattr(session_mcp, "native_settings_sources", lambda work_dir: [])
    monkeypatch.setattr(session_mcp, "builtin_app_identity_launches", lambda: {})
    return session_mcp


def _label_elements(spec, tmp_path, session_key=KEY, existing=()):
    from kiro_crew.acp.session_label import kiro_session_label_elements

    return kiro_session_label_elements(
        "memory--agent",
        work_dir=tmp_path,
        session_key=session_key,
        existing_names=existing,
        spec_override=spec,
    )


class TestKiroLabelElements:
    def test_an_opted_in_server_is_relaunched_as_declared_plus_the_label(
        self, quiet_sources, tmp_path
    ):
        (element,) = _label_elements(_spec(), tmp_path)

        assert element["name"] == "memory:memory"
        assert (element["command"], element["args"]) == (APP["command"], APP["args"])
        assert element["type"] == "stdio"
        assert _env(element) == {"APP_MODE": "chat", LABEL: KEY}

    def test_the_label_carries_no_authority(self, quiet_sources, tmp_path):
        (element,) = _label_elements(_spec(), tmp_path)

        names = set(_env(element))
        assert STUB_SESSION_TOKEN_ENV not in names
        assert "KIROCREW_SESSION_KEY" not in names
        assert "KIROCREW_PORT" not in names
        assert MARKER not in element

    def test_a_declared_value_cannot_shadow_the_label(self, quiet_sources, tmp_path):
        entry = {**APP, "env": {LABEL: "forged", "APP_MODE": "chat"}}
        (element,) = _label_elements(_spec(entry), tmp_path)
        assert _env(element)[LABEL] == KEY

    @pytest.mark.parametrize("marker", [None, False, "true", 1])
    def test_only_a_boolean_true_opts_in(self, quiet_sources, tmp_path, marker):
        entry = {k: v for k, v in APP.items() if k != MARKER}
        if marker is not None:
            entry[MARKER] = marker
        assert _label_elements(_spec(entry), tmp_path) == []

    def test_no_session_key_means_no_label_not_a_stale_one(self, quiet_sources, tmp_path):
        """A warm-pool session is created before any session claims it."""
        assert _label_elements(_spec(), tmp_path, session_key="") == []

    def test_an_ungranted_server_is_not_mounted(self, quiet_sources, tmp_path):
        assert _label_elements(_spec(tools=("@kirocrew-core",)), tmp_path) == []

    def test_a_name_already_in_the_array_is_not_redeclared(self, quiet_sources, tmp_path):
        """A broker stub (shared across sessions) or Crew's own element keeps its slot."""
        assert _label_elements(_spec(), tmp_path, existing={"memory:memory"}) == []

    def test_crews_own_servers_never_take_the_label_path(self, quiet_sources, tmp_path):
        spec = _spec()
        spec["mcpServers"]["kirocrew-core"][MARKER] = True
        names = [e["name"] for e in _label_elements(spec, tmp_path)]
        assert names == ["memory:memory"]

    def test_a_builtin_app_server_keeps_its_token_element(
        self, quiet_sources, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(
            quiet_sources, "builtin_app_identity_launches", lambda: {"memory:memory": {}}
        )
        assert _label_elements(_spec(), tmp_path) == []

    def test_registry_mode_leaves_the_catalog_in_charge(self, quiet_sources, tmp_path, monkeypatch):
        monkeypatch.setattr(quiet_sources, "_registry_mode", lambda: True)
        assert _label_elements(_spec(), tmp_path) == []

    @pytest.mark.parametrize(
        "change",
        [
            {"disabledTools": ["forget"]},
            {"disabled": True},
            {"timeout": 120000},
            {"cwd": "/somewhere"},
            {"autoApprove": ["recall"]},
            {"env": {"TOKEN": "${MEMORY_TOKEN}"}},
            {"env": {"TOKEN": "secret://memory"}},
            {"url": "http://127.0.0.1:9100/mcp", "type": "http"},
        ],
        ids=[
            "disabledTools",
            "disabled",
            "timeout",
            "cwd",
            "autoApprove",
            "env-reference",
            "env-secret",
            "remote",
        ],
    )
    def test_a_declaration_the_element_cannot_reproduce_stays_native(
        self, quiet_sources, tmp_path, change
    ):
        """Relabelling must never change what the server is, or what it may do."""
        assert _label_elements(_spec({**APP, **change}), tmp_path) == []

    def test_unreadable_settings_withhold_the_label(self, quiet_sources, tmp_path, monkeypatch):
        def unreadable(_work_dir):
            raise quiet_sources.NativeSettingsUnreadable("the global MCP settings", tmp_path)

        monkeypatch.setattr(quiet_sources, "native_settings_sources", unreadable)
        assert _label_elements(_spec(), tmp_path) == []

    def test_an_internal_error_costs_the_label_not_the_session(
        self, quiet_sources, tmp_path, monkeypatch
    ):
        def boom(*_a, **_k):
            raise RuntimeError("boom")

        monkeypatch.setattr(quiet_sources, "acp_server_element", boom)
        assert _label_elements(_spec(), tmp_path) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("resume", [False, True])
async def test_the_shared_kiro_runtime_sends_the_label_on_the_sessions_own_element(
    quiet_sources, monkeypatch, resume
):
    """End to end on both session-start paths: the label rides the element, the token
    stays on Crew's own server only, and no resolver reads the label as identity."""
    from test_acp_runtime import _make_runtime

    from kiro_crew import mcp_caller
    from kiro_crew.acp import session_handle
    from kiro_crew.acp.types import METHOD_SESSION_LOAD, METHOD_SESSION_NEW

    spec = _spec()
    core = spec["mcpServers"]["kirocrew-core"]
    monkeypatch.setattr(quiet_sources, "_agent_spec_for", lambda *a, **k: spec)
    monkeypatch.setattr(quiet_sources, "managed_mcp_spec_entry", lambda name, **_kw: core)
    monkeypatch.setattr(session_handle, "_MCP_DRAIN_NO_REPORT_CEILING", 0)
    runtime, _, _ = _make_runtime()
    runtime._can_load_session = True
    sent = []

    async def send(method, params, timeout=None):
        if method in (METHOD_SESSION_NEW, METHOD_SESSION_LOAD):
            sent.append(params)
        return {"sessionId": "sid-label", "modes": {"currentModeId": "kirocrew"}}

    monkeypatch.setattr(runtime, "_send_and_await", send)
    if resume:
        await runtime.load_session("", "sid-label", session_key=KEY)
    else:
        await runtime.create_session(session_key=KEY)

    servers = {item["name"]: item for item in sent[0]["mcpServers"]}
    assert _env(servers["memory:memory"]) == {"APP_MODE": "chat", LABEL: KEY}
    assert STUB_SESSION_TOKEN_ENV in _env(servers["kirocrew-core"])
    assert LABEL not in _env(servers["kirocrew-core"])

    # The label is a name, not a credential: nothing that resolves identity reads it.
    for name in (STUB_SESSION_TOKEN_ENV, "KIROCREW_SESSION_KEY", "KIROCREW_HOST_PID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(LABEL, KEY)
    own = mcp_caller.resolve_own_identity(consult_protected_binding=False, skip_pid_mapping=True)
    assert own.session_key == ""
    assert own.source == mcp_caller.SOURCE_UNRESOLVED


@pytest.mark.asyncio
async def test_a_pool_session_with_no_owner_yet_starts_its_servers_unlabelled(
    quiet_sources, monkeypatch
):
    from test_acp_runtime import _make_runtime

    from kiro_crew.acp import session_handle
    from kiro_crew.acp.types import METHOD_SESSION_NEW

    spec = _spec()
    core = spec["mcpServers"]["kirocrew-core"]
    monkeypatch.setattr(quiet_sources, "_agent_spec_for", lambda *a, **k: spec)
    monkeypatch.setattr(quiet_sources, "managed_mcp_spec_entry", lambda name, **_kw: core)
    monkeypatch.setattr(session_handle, "_MCP_DRAIN_NO_REPORT_CEILING", 0)
    runtime, _, _ = _make_runtime()
    sent = []

    async def send(method, params, timeout=None):
        if method == METHOD_SESSION_NEW:
            sent.append(params)
        return {"sessionId": "sid-pool", "modes": {"currentModeId": "kirocrew"}}

    monkeypatch.setattr(runtime, "_send_and_await", send)
    await runtime.create_session(session_key="")
    names = {item["name"] for item in sent[0]["mcpServers"]}
    assert "memory:memory" not in names


class TestKasProjection:
    """KAS builds its injected agent per session, so the label rides the entry's env."""

    def _project(self, entry, session_key=KEY):
        from kas_agents_helpers import _spec as kas_spec

        from kiro_crew.acp.kas_agents import to_client_custom_agent

        spec = kas_spec(
            tools=["@kirocrew-core", "@memory:memory"],
            mcpServers={"kirocrew-core": {"command": "x"}, "memory:memory": entry},
        )
        return to_client_custom_agent("a", spec, "p", session_key=session_key)["mcpServers"]

    def test_an_opted_in_server_gains_the_label_and_nothing_else(self):
        out = self._project({**APP, "env": {"API_TOKEN": "s3cr3t"}})
        memory = out["memory:memory"]
        assert memory["env"] == {LABEL: KEY}
        assert MARKER not in memory
        assert "s3cr3t" not in str(out)

    def test_an_unmarked_server_is_unchanged(self):
        entry = {k: v for k, v in APP.items() if k != MARKER}
        assert "env" not in self._project(entry)["memory:memory"]

    def test_no_session_key_means_no_label(self):
        out = self._project(dict(APP), session_key="")
        assert LABEL not in out["memory:memory"].get("env", {})
        assert MARKER not in out["memory:memory"]

    def test_a_managed_server_keeps_its_own_identity_not_a_label(self):
        out = self._project(dict(APP))
        core = out["kirocrew-core"].get("env", {})
        assert core.get("KIROCREW_SESSION_KEY") == KEY
        assert LABEL not in core
