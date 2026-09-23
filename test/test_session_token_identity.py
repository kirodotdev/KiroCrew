"""Switch-free session identity: the token on the element, and the resolver.

:mod:`kiro_crew.session_token_sig` proves the mapping is trustworthy; these tests
prove it is WIRED — that the token reaches a control-plane MCP element, that the
resolvers prefer it over an env key a warm-pool rekey has made stale, and that
nothing about the wiring depends on ``stub_servers``, on gatewayd or on a config
switch.

The precedence test is the load-bearing one. Both sources are present in the
failure this exists to fix (a recycled process whose children were spawned for the
previous session), and the ONLY thing that makes the resolution correct is reading
the file first — so a test that asserted the token works with no env key set would
pass on the broken ordering too.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from conftest import make_dir_link
from kiro_crew import mcp_core, members, session_pid, session_pid_sig, session_token_sig
from kiro_crew.mcp_gateway.claim import STUB_SESSION_TOKEN_ENV, mint_stub_session_token
from kiro_crew.providers.mirrors.identity import control_plane_identity_env

LIVE_KEY = "dashboard:chat-9-current"
STALE_KEY = "dashboard:chat-7-previous"
TOKEN = "c" * 64
OTHER_TOKEN = "d" * 64


def test_tool_policy_tracks_signed_session_rekeys_instead_of_stale_parent(cfg, monkeypatch):
    from kiro_crew import mcp_shared

    monkeypatch.setattr(mcp_shared, "_excluded_tools_by_session", {})
    monkeypatch.setattr(mcp_shared, "_last_failure_time", 0.0)
    monkeypatch.setattr(mcp_shared, "_last_startup_race_time", 0.0)
    monkeypatch.setattr(mcp_shared, "resolve_client_port_src", lambda port: (5476, "config"))
    monkeypatch.setattr(mcp_shared, "read_local_secret", lambda port: "synthetic-secret")
    monkeypatch.setattr(mcp_shared, "sel", Mock())
    requested = []

    def policy(request, **kwargs):
        key = request.get_header("X-session-key")
        requested.append(key)
        assert request.get_header("X-internal-secret") == "synthetic-secret"
        response = Mock()
        response.read.return_value = json.dumps({"exclude": [key]}).encode()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        return response

    monkeypatch.setattr(mcp_shared, "loopback_urlopen", policy)
    monkeypatch.setenv(STUB_SESSION_TOKEN_ENV, TOKEN)
    monkeypatch.setenv("KIROCREW_SESSION_KEY", STALE_KEY)
    session_token_sig.publish_session_token(TOKEN, LIVE_KEY)
    assert mcp_shared._resolve_excluded_tools() == {LIVE_KEY}
    session_token_sig.publish_session_token(TOKEN, "subagent:child")
    assert mcp_shared._resolve_excluded_tools() == {"subagent:child"}
    assert mcp_shared._resolve_excluded_tools(LIVE_KEY) == {LIVE_KEY}
    assert requested == [LIVE_KEY, "subagent:child"]


def _path_for(cfg, token: str):
    return cfg / f"session_token_{hashlib.sha256(token.encode()).hexdigest()}.sig"


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """Isolated mapping dir + trust root, with the resolver's OTHER sources off.

    ``current_caller`` is the source the resolvers consult ABOVE the token.
    It is pinned to "absent"
    so these tests measure the token-vs-env decision rather than whichever of
    them the host process happens to satisfy — a real gateway-injected caller
    context outranking the token is correct behaviour and is covered by the
    existing strict-resolver tests.
    """
    key_path = tmp_path / "sel_hmac.key"
    key_path.write_bytes(b"\x02" * 32)
    monkeypatch.delenv(STUB_SESSION_TOKEN_ENV, raising=False)
    monkeypatch.delenv("KIROCREW_SESSION_KEY", raising=False)
    monkeypatch.delenv("KIROCREW_HOST_PID", raising=False)
    with (
        patch.object(session_token_sig, "config_dir", return_value=tmp_path),
        patch.object(session_pid_sig, "sel_hmac_key_path", return_value=key_path),
        patch.object(session_token_sig, "sel_hmac_key_path", return_value=key_path),
        patch.object(session_pid_sig, "_sel_hmac_key_bytes", return_value=None),
        patch.object(mcp_core, "current_caller", return_value=None),
    ):
        session_pid_sig._reported.clear()
        yield tmp_path
        session_pid_sig._reported.clear()


class TestResolverPrecedence:
    @pytest.mark.parametrize(
        "resolve",
        [mcp_core._resolve_session_key, mcp_core._resolve_session_key_strict],
        ids=["lenient", "strict"],
    )
    def test_valid_token_beats_a_stale_env_key(self, cfg, monkeypatch, resolve):
        """THE warm-pool case: both sources present and disagreeing.

        The element was built for the session that held the process before the
        rekey, so its ``KIROCREW_SESSION_KEY`` names that session; the mapping was
        republished at claim time and names the current one. Answering with the env
        var here is the misattribution the whole change exists to remove.
        """
        session_token_sig.publish_session_token(TOKEN, LIVE_KEY)
        monkeypatch.setenv(STUB_SESSION_TOKEN_ENV, TOKEN)
        monkeypatch.setenv("KIROCREW_SESSION_KEY", STALE_KEY)
        assert resolve() == LIVE_KEY

    @pytest.mark.parametrize(
        "resolve",
        [mcp_core._resolve_session_key, mcp_core._resolve_session_key_strict],
        ids=["lenient", "strict"],
    )
    def test_token_with_no_mapping_falls_through_to_the_env_key(self, cfg, monkeypatch, resolve):
        """No trust root to sign with, or a publication that never landed.

        The env key is the fallback for exactly this, so an unresolvable token must
        cost nothing — a token that SHADOWED the env var would trade a
        stale-identity bug for a no-identity one.
        """
        monkeypatch.setenv(STUB_SESSION_TOKEN_ENV, TOKEN)
        monkeypatch.setenv("KIROCREW_SESSION_KEY", STALE_KEY)
        assert resolve() == STALE_KEY

    @pytest.mark.parametrize(
        "resolve",
        [mcp_core._resolve_session_key, mcp_core._resolve_session_key_strict],
        ids=["lenient", "strict"],
    )
    def test_forged_mapping_falls_through_rather_than_answering(self, cfg, monkeypatch, resolve):
        """An agent writes a mapping it cannot sign: refused, not trusted."""
        _path_for(cfg, TOKEN).write_text(f"{'0' * 64}\n{LIVE_KEY}", encoding="utf-8")
        monkeypatch.setenv(STUB_SESSION_TOKEN_ENV, TOKEN)
        monkeypatch.setenv("KIROCREW_SESSION_KEY", STALE_KEY)
        assert resolve() == STALE_KEY

    @pytest.mark.parametrize(
        "resolve",
        [mcp_core._resolve_session_key, mcp_core._resolve_session_key_strict],
        ids=["lenient", "strict"],
    )
    def test_no_token_leaves_behaviour_unchanged(self, cfg, monkeypatch, resolve):
        """Every install that has no token must resolve exactly as it did before."""
        monkeypatch.setenv("KIROCREW_SESSION_KEY", STALE_KEY)
        assert resolve() == STALE_KEY

    def test_no_token_and_nothing_else_still_refuses(self, cfg):
        assert mcp_core._resolve_session_key_strict() == ""

    def test_two_sessions_on_one_runtime_resolve_their_own_key(self, cfg, monkeypatch):
        """The ``spawn_run`` session-sharing topology, at the resolver.

        Parent and subagent share one kiro-cli process, so every pid-keyed source
        answers with the parent for both. Each session's own element carries its own
        token, and that is what separates them.
        """
        session_token_sig.publish_session_token(TOKEN, "dashboard:parent")
        session_token_sig.publish_session_token(OTHER_TOKEN, "dashboard:subagent")
        monkeypatch.setenv(STUB_SESSION_TOKEN_ENV, TOKEN)
        assert mcp_core._resolve_session_key_strict() == "dashboard:parent"
        monkeypatch.setenv(STUB_SESSION_TOKEN_ENV, OTHER_TOKEN)
        assert mcp_core._resolve_session_key_strict() == "dashboard:subagent"


class TestDiagnosis:
    def test_a_wired_token_that_will_not_verify_names_the_trust_root(self, cfg, monkeypatch):
        """The channel exists and the MAPPING failed — do not send the reader to
        configure routing they already have."""
        monkeypatch.setenv(STUB_SESSION_TOKEN_ENV, TOKEN)
        text = mcp_core.strict_identity_diagnosis("kirocrew-core")
        assert "session token" in text and "trust root" in text
        assert "stub_servers" not in text

    def test_resolvable_identity_produces_no_diagnosis(self, cfg, monkeypatch):
        session_token_sig.publish_session_token(TOKEN, LIVE_KEY)
        monkeypatch.setenv(STUB_SESSION_TOKEN_ENV, TOKEN)
        assert mcp_core.strict_identity_diagnosis("kirocrew-core") == ""


class TestElementCarriage:
    """The token has to REACH the element, on every carrier that carries the key."""

    def test_control_plane_identity_env_carries_the_token_beside_the_key(self):
        env = control_plane_identity_env(LIVE_KEY, "C123", label="codex", session_token=TOKEN)
        assert env[STUB_SESSION_TOKEN_ENV] == TOKEN
        # BESIDE, not instead of: the key remains the fallback for a host with no
        # SEL trust root to sign a mapping with.
        assert env["KIROCREW_SESSION_KEY"] == LIVE_KEY

    def test_no_token_leaves_the_env_byte_identical(self):
        with_token = control_plane_identity_env(LIVE_KEY, "C123", label="codex")
        assert STUB_SESSION_TOKEN_ENV not in with_token
        assert with_token["KIROCREW_SESSION_KEY"] == LIVE_KEY

    def test_codex_puts_the_token_on_the_control_plane_and_nowhere_else(self):
        """The identity line the codex mirror draws must hold for the token too.

        An element the SPEC describes is one whose command, args and env the spec
        chose, so handing it a bearer name for this session would let a
        hand-edited line drive the session it was mounted into.
        """
        from kiro_crew.providers.mirrors.codex import codex_elements

        out = codex_elements(
            [
                {"name": "kirocrew-core", "command": "kirocrew", "env": []},
                {"name": "third-party", "command": "x", "env": []},
            ],
            session_key=LIVE_KEY,
            channel_id="",
            session_token=TOKEN,
        )
        by_name = {e["name"]: e for e in out}
        core_env = {p["name"]: p["value"] for p in by_name["kirocrew-core"]["env"]}
        assert core_env[STUB_SESSION_TOKEN_ENV] == TOKEN
        assert by_name["third-party"]["env"] == []

    def test_opencode_puts_the_token_on_the_control_plane_and_nowhere_else(self):
        from kiro_crew.providers.mirrors.opencode import opencode_elements

        out = opencode_elements(
            [
                {"name": "kirocrew-core", "command": "kirocrew", "env": []},
                {"name": "third-party", "command": "x", "env": []},
            ],
            session_key=LIVE_KEY,
            channel_id="",
            session_token=TOKEN,
        )
        by_name = {e["name"]: e for e in out}
        core_env = {p["name"]: p["value"] for p in by_name["kirocrew-core"]["env"]}
        assert core_env[STUB_SESSION_TOKEN_ENV] == TOKEN
        assert by_name["third-party"]["env"] == []

    def test_member_dispatch_element_carries_the_token(self, monkeypatch):
        monkeypatch.setattr(
            members,
            "_kirocrew_mcp_invocation",
            lambda _v: ("kirocrew", ["mcp-dashboard"]),
            raising=False,
        )
        with (
            patch(
                "kiro_crew.agent._kirocrew_mcp_invocation",
                return_value=("kirocrew", ["mcp-dashboard"]),
            ),
            patch("kiro_crew.agent._managed_mcp_env", return_value={}),
            patch("kiro_crew.port_resolution.resolve_serving_port", return_value=4711),
        ):
            entry = members.member_dispatch_session_server("dashboard:member-x", TOKEN)
        assert entry is not None
        env = {p["name"]: p["value"] for p in entry["env"]}
        assert env[STUB_SESSION_TOKEN_ENV] == TOKEN
        assert env["KIROCREW_SESSION_KEY"] == "dashboard:member-x"

    def test_the_kiro_cli_child_env_carries_the_token_beside_the_key(self):
        """The one-session-per-process carrier: ``AcpClient``, which serves kiro.

        A shared runtime cannot use the process env (it names whichever session
        claimed the process), but one AcpClient drives one child serving one
        session, so here the process names exactly one session.
        """
        from kiro_crew.acp.client import AcpClient

        client = object.__new__(AcpClient)
        client._session_key = LIVE_KEY
        client._stub_session_token = TOKEN
        env: dict[str, str] = {}
        client._apply_session_identity_env(env)
        assert env[STUB_SESSION_TOKEN_ENV] == TOKEN
        assert env["KIROCREW_SESSION_KEY"] == LIVE_KEY

    def test_a_warm_pool_child_is_spawned_WITH_the_token_and_no_key(self):
        """THE pooled case, and the reason the two values are not symmetric.

        A warm-pool client is spawned with no session key — that is what makes it
        poolable — and a child's env is fixed at spawn. Withholding the token there
        would strip it from exactly the children whose identity has to survive a
        ``rekey()``, and nothing could add it back afterwards. The token is minted in
        ``__init__`` and its mapping is published once the key is known, so a child
        holding a token and no key still resolves.
        """
        from kiro_crew.acp.client import AcpClient

        client = object.__new__(AcpClient)
        client._session_key = ""
        client._stub_session_token = TOKEN
        env: dict[str, str] = {}
        client._apply_session_identity_env(env)
        assert env[STUB_SESSION_TOKEN_ENV] == TOKEN
        assert "KIROCREW_SESSION_KEY" not in env

    def test_an_inherited_key_is_cleared_and_an_inherited_token_is_replaced(self):
        """The stale-value hazard, resolved per value rather than in one sweep.

        An inherited KEY names a session this client is not serving and the resolver
        would read it as identity, so it goes. An inherited TOKEN is overwritten with
        this client's rather than dropped: the token is a pointer whose mapping is
        rewritten on every rekey, so the right answer is to make it name THIS client.
        """
        from kiro_crew.acp.client import AcpClient

        client = object.__new__(AcpClient)
        client._session_key = ""
        client._stub_session_token = TOKEN
        env = {"KIROCREW_SESSION_KEY": STALE_KEY, STUB_SESSION_TOKEN_ENV: OTHER_TOKEN}
        client._apply_session_identity_env(env)
        assert "KIROCREW_SESSION_KEY" not in env
        assert env[STUB_SESSION_TOKEN_ENV] == TOKEN

    def test_a_client_with_no_token_carries_no_empty_value(self):
        """An empty token would be tried FIRST and can never verify."""
        from kiro_crew.acp.client import AcpClient

        client = object.__new__(AcpClient)
        client._session_key = LIVE_KEY
        client._stub_session_token = ""
        env = {STUB_SESSION_TOKEN_ENV: OTHER_TOKEN}
        client._apply_session_identity_env(env)
        assert STUB_SESSION_TOKEN_ENV not in env
        assert env["KIROCREW_SESSION_KEY"] == LIVE_KEY

    def test_member_dispatch_element_without_a_token_is_unchanged(self):
        with (
            patch(
                "kiro_crew.agent._kirocrew_mcp_invocation",
                return_value=("kirocrew", ["mcp-dashboard"]),
            ),
            patch("kiro_crew.agent._managed_mcp_env", return_value={}),
            patch("kiro_crew.port_resolution.resolve_serving_port", return_value=4711),
        ):
            entry = members.member_dispatch_session_server("dashboard:member-x")
        assert entry is not None
        env = {p["name"]: p["value"] for p in entry["env"]}
        assert STUB_SESSION_TOKEN_ENV not in env


class TestMintIsSwitchFree:
    """The mint must not depend on a reachable gatewayd — that is the S2 change."""

    def test_own_stub_session_mints_and_publishes_with_no_gateway_socket(self, cfg):
        from kiro_crew.acp.runtime import AcpRuntime

        runtime = object.__new__(AcpRuntime)
        runtime._mcp_gateway_socket = None  # gateway off: the default install

        published: list[tuple[str, str]] = []
        with (
            patch.object(
                session_token_sig,
                "publish_session_token",
                side_effect=lambda t, k: published.append((t, k)),
            ),
            patch(
                "kiro_crew.acp.runtime.publish_session_token",
                side_effect=lambda t, k: published.append((t, k)),
            ),
        ):
            entries, token = asyncio.run(runtime._own_stub_session([], LIVE_KEY))

        assert entries == []
        assert token, "a session with no gatewayd still needs a name of its own"
        assert published == [(token, LIVE_KEY)]

    def test_a_session_with_no_key_yet_mints_but_publishes_nothing(self, cfg):
        """A warm-pool worker is claimed later; ``rekey()`` publishes for it."""
        from kiro_crew.acp.runtime import AcpRuntime

        runtime = object.__new__(AcpRuntime)
        runtime._mcp_gateway_socket = None
        with patch(
            "kiro_crew.acp.runtime.publish_session_token",
            side_effect=AssertionError("must not publish without a session key"),
        ):
            _entries, token = asyncio.run(runtime._own_stub_session([], ""))
        assert token

    def test_minted_tokens_are_unguessable_and_unique(self):
        tokens = {mint_stub_session_token() for _ in range(64)}
        assert len(tokens) == 64
        assert all(len(t) == 64 for t in tokens)


@pytest.mark.asyncio
@pytest.mark.parametrize("resume", [False, True])
async def test_kiro_unpooled_control_plane_receives_session_token(cfg, monkeypatch, resume):
    from test_acp_runtime import _make_runtime

    from kiro_crew.acp import session_handle, session_mcp
    from kiro_crew.acp.types import METHOD_SESSION_LOAD, METHOD_SESSION_NEW

    entry = {"command": "test-crew", "args": ["mcp"], "env": {}}
    spec = {"tools": ["@kirocrew-core"], "mcpServers": {"kirocrew-core": entry}}
    monkeypatch.setattr(session_mcp, "_agent_spec_for", lambda *a, **k: spec)
    monkeypatch.setattr(session_mcp, "_global_settings", lambda **kwargs: {})
    monkeypatch.setattr(session_mcp, "_registry_mode", lambda: False)
    monkeypatch.setattr(session_mcp, "managed_mcp_spec_entry", lambda name, **_kw: entry)
    monkeypatch.setattr(session_handle, "_MCP_DRAIN_NO_REPORT_CEILING", 0)
    runtime, _, _ = _make_runtime()
    runtime._can_load_session = True
    sent = []

    async def send(method, params, timeout=None):
        if method in (METHOD_SESSION_NEW, METHOD_SESSION_LOAD):
            sent.append(params)
        return {"sessionId": "sid-token", "modes": {"currentModeId": "kirocrew"}}

    monkeypatch.setattr(runtime, "_send_and_await", send)
    if resume:
        await runtime.load_session("", "sid-token", session_key=LIVE_KEY)
    else:
        await runtime.create_session(session_key=LIVE_KEY)
    servers = {item["name"]: item for item in sent[0]["mcpServers"]}
    assert "kirocrew-core" in servers
    env = {item["name"]: item["value"] for item in servers["kirocrew-core"]["env"]}
    monkeypatch.setenv(STUB_SESSION_TOKEN_ENV, env[STUB_SESSION_TOKEN_ENV])
    assert mcp_core._resolve_session_key_strict() == LIVE_KEY


@pytest.mark.asyncio
async def test_projected_skill_search_receives_the_shared_sessions_identity(cfg, monkeypatch):
    from test_acp_runtime import _make_runtime

    from kiro_crew.acp import session_mcp
    from kiro_crew.acp.skill_projection import NativeSkillProjection

    entry = {"command": "test-crew", "args": ["mcp"]}
    spec = {"tools": ["@kirocrew-core/skill_search"], "mcpServers": {"kirocrew-core": entry}}
    monkeypatch.setattr(session_mcp, "_agent_spec_for", lambda *a, **k: {"tools": []})
    monkeypatch.setattr(session_mcp, "_global_settings", lambda **kw: {})
    monkeypatch.setattr(session_mcp, "_registry_mode", lambda: False)
    monkeypatch.setattr(session_mcp, "managed_mcp_spec_entry", lambda name, **_kw: entry)
    runtime, _, _ = _make_runtime()
    runtime._native_skill_projection = NativeSkillProjection(
        {"custom": "alias"}, {"custom": spec}, search_agents={"custom"}
    )
    servers = await runtime._unpooled_control_planes([], "custom", runtime._work_dir)
    servers, token = await runtime._own_stub_session(servers, LIVE_KEY)
    env = {item["name"]: item["value"] for item in servers[0]["env"]}
    assert env[STUB_SESSION_TOKEN_ENV] == token
    monkeypatch.setenv(STUB_SESSION_TOKEN_ENV, token)
    assert mcp_core._resolve_session_key_strict() == LIVE_KEY


@pytest.mark.parametrize(
    "restriction",
    ["stub", "unreferenced", "disabled", "tool", "global", "project", "registry"],
)
def test_kiro_identity_projection_preserves_native_restrictions(tmp_path, monkeypatch, restriction):
    import json

    from kiro_crew.acp import session_mcp

    managed = {"command": "test-crew", "args": ["mcp"]}
    entry = dict(managed)
    spec = {"tools": ["@kirocrew-core"], "mcpServers": {"kirocrew-core": entry}}
    settings = {}
    if restriction == "unreferenced":
        spec["tools"] = []
    elif restriction == "disabled":
        entry["disabled"] = True
    elif restriction == "tool":
        entry["disabledTools"] = ["workflow_run"]
    elif restriction in {"global", "project"}:
        settings = {"mcpServers": {"kirocrew-core": {"disabledTools": ["workflow_run"]}}}
        if restriction == "project":
            path = tmp_path / ".kiro" / "settings" / "mcp.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(settings), encoding="utf-8")
            settings = {}
    monkeypatch.setattr(session_mcp, "_agent_spec_for", lambda *a, **k: spec)
    monkeypatch.setattr(session_mcp, "_global_settings", lambda **kwargs: settings)
    monkeypatch.setattr(session_mcp, "_registry_mode", lambda: restriction == "registry")
    monkeypatch.setattr(session_mcp, "managed_mcp_spec_entry", lambda name, **_kw: managed)
    assert (
        session_mcp.kiro_control_plane_servers(
            "kirocrew",
            work_dir=tmp_path,
            existing_names={"kirocrew-core"} if restriction == "stub" else (),
        )
        == []
    )


@pytest.mark.parametrize(
    "declared",
    [
        # The shape no spec author can avoid: the documented Toolbox launcher,
        # which resolves to the shared dispatcher, not the versioned binary.
        {"command": "kirocrew", "args": ["mcp-core"]},
        # A path pinned to a version since reaped by an upgrade.
        {"command": "/opt/toolbox/tools/kirocrew/0.6.0.9/bin/kirocrew", "args": ["mcp-core"]},
        # A third-party binary squatting the reserved name.
        {"command": "untrusted-custom-command", "args": ["mcp"]},
        # Right command, foreign args.
        {"command": "test-crew", "args": ["mcp", "--evil"]},
    ],
)
@pytest.mark.parametrize("where", ["spec", "global"])
def test_kiro_identity_projection_repairs_a_stale_reserved_name_command(
    tmp_path, monkeypatch, declared, where
):
    """Toolbox-shim report: a reserved name launches the MANAGED invocation, whatever the
    spec (or a settings override of the same name) spelled.

    Skipping the entry, as before, left a session that granted ``@kirocrew-core``
    with a server that mounted from the spec, carried no identity, and refused
    every call ``identity_unattested``. Repairing it is also the safe direction:
    the third-party command under a reserved name never runs -- ours does -- so a
    squatter gets no token for its own binary. Everything the spec restricts
    (mute, ``disabledTools``, the grant itself) is still honoured by the
    parametrized test above; only the invocation is re-derived."""
    from kiro_crew.acp import session_mcp

    managed = {"command": "test-crew", "args": ["mcp"]}
    if where == "spec":
        entry = dict(declared)
        settings = {}
    else:
        entry = dict(managed)
        settings = {"mcpServers": {"kirocrew-core": dict(declared)}}
    spec = {"tools": ["@kirocrew-core"], "mcpServers": {"kirocrew-core": entry}}
    monkeypatch.setattr(session_mcp, "_agent_spec_for", lambda *a, **k: spec)
    monkeypatch.setattr(session_mcp, "_global_settings", lambda **kwargs: settings)
    monkeypatch.setattr(session_mcp, "_registry_mode", lambda: False)
    monkeypatch.setattr(session_mcp, "managed_mcp_spec_entry", lambda name, **_kw: dict(managed))

    elements = session_mcp.kiro_control_plane_servers("kirocrew", work_dir=tmp_path)

    assert [e["name"] for e in elements] == ["kirocrew-core"]
    launched = (elements[0]["command"], elements[0]["args"])
    assert launched == ("test-crew", ["mcp"])
    # Negative control for the class: the declared launch never reaches the element.
    assert launched != (declared["command"], declared["args"])


@pytest.mark.parametrize("bad_args", [8080, "--flag", {"a": 1}, True])
@pytest.mark.parametrize("where", ["spec", "global"])
def test_kiro_identity_projection_survives_a_scalar_args_on_a_reserved_name(
    tmp_path, monkeypatch, bad_args, where
):
    """``"args": 8080`` is the easy hand-edit ``acp_server_element`` refuses to raise
    on; the repair's comparison must not raise on it either -- a TypeError here
    leaves ``create_session`` and aborts ``session/new``. It reads as "not the
    managed launch" and the managed one is mounted."""
    from kiro_crew.acp import session_mcp

    managed = {"command": "test-crew", "args": ["mcp"]}
    if where == "spec":
        entry = {"command": "test-crew", "args": bad_args}
        settings = {}
    else:
        entry = dict(managed)
        settings = {"mcpServers": {"kirocrew-core": {"args": bad_args}}}
    spec = {"tools": ["@kirocrew-core"], "mcpServers": {"kirocrew-core": entry}}
    monkeypatch.setattr(session_mcp, "_agent_spec_for", lambda *a, **k: spec)
    monkeypatch.setattr(session_mcp, "_global_settings", lambda **kwargs: settings)
    monkeypatch.setattr(session_mcp, "_registry_mode", lambda: False)
    monkeypatch.setattr(session_mcp, "managed_mcp_spec_entry", lambda name, **_kw: dict(managed))

    elements = session_mcp.kiro_control_plane_servers("kirocrew", work_dir=tmp_path)

    assert [(e["name"], e["command"], e["args"]) for e in elements] == [
        ("kirocrew-core", "test-crew", ["mcp"])
    ]


@pytest.mark.parametrize("server", ["kirocrew-dashboard", "kirocrew-work", "kirocrew-crew-log"])
def test_kiro_identity_projection_covers_a_granted_opt_in_server(tmp_path, monkeypatch, server):
    """The reported defect: a spec that grants ``@kirocrew-dashboard`` on the kiro
    backend mounted the server straight from the spec, with no session-valued
    environment, so its every ``tools/call`` refused as ``identity_unattested``.
    The identity projection must re-emit the granted opt-in element exactly as it
    does the control plane's, so the token attach that follows reaches it."""
    from kiro_crew.acp import session_mcp

    sub = {"kirocrew-dashboard": "mcp-dashboard", "kirocrew-work": "mcp-work"}.get(
        server, "mcp-crew-log"
    )
    managed = {"command": "test-crew", "args": [sub]}
    spec = {
        "tools": ["@kirocrew-core", f"@{server}"],
        "mcpServers": {
            "kirocrew-core": {"command": "test-crew", "args": ["mcp-core"]},
            server: dict(managed),
        },
    }
    monkeypatch.setattr(session_mcp, "_agent_spec_for", lambda *a, **k: spec)
    monkeypatch.setattr(session_mcp, "_global_settings", lambda **kwargs: {})
    monkeypatch.setattr(session_mcp, "_registry_mode", lambda: False)
    monkeypatch.setattr(
        session_mcp,
        "managed_mcp_spec_entry",
        lambda name, **_kw: dict(spec["mcpServers"][name]) if name in spec["mcpServers"] else None,
    )
    elements = session_mcp.kiro_control_plane_servers("kirocrew", work_dir=tmp_path)
    names = [e["name"] for e in elements]
    assert names == ["kirocrew-core", server], names
    granted = next(e for e in elements if e["name"] == server)
    assert granted["command"] == "test-crew" and granted["args"] == [sub]


def test_kiro_identity_projection_leaves_an_ungranted_opt_in_server_out(tmp_path, monkeypatch):
    """Widening the set must not widen the GRANT: a spec whose ``tools`` does not
    name the opt-in server gets no element for it, exactly as kiro-cli mounts
    nothing the allowlist does not reference."""
    from kiro_crew.acp import session_mcp

    spec = {
        "tools": ["@kirocrew-core"],
        "mcpServers": {
            "kirocrew-core": {"command": "test-crew", "args": ["mcp-core"]},
            "kirocrew-dashboard": {"command": "test-crew", "args": ["mcp-dashboard"]},
        },
    }
    monkeypatch.setattr(session_mcp, "_agent_spec_for", lambda *a, **k: spec)
    monkeypatch.setattr(session_mcp, "_global_settings", lambda **kwargs: {})
    monkeypatch.setattr(session_mcp, "_registry_mode", lambda: False)
    monkeypatch.setattr(
        session_mcp,
        "managed_mcp_spec_entry",
        lambda name, **_kw: dict(spec["mcpServers"][name]) if name in spec["mcpServers"] else None,
    )
    elements = session_mcp.kiro_control_plane_servers("kirocrew", work_dir=tmp_path)
    assert [e["name"] for e in elements] == ["kirocrew-core"]


def test_identity_bound_set_is_every_managed_server():
    """Derived, not enumerated: a managed server added later is identity-bound by
    construction, the two always-on control planes are a strict subset, and they
    LEAD in their own order -- a session granting only those two emits exactly the
    elements, in exactly the order, it emitted before the set widened."""
    from kiro_crew.acp.session_mcp import CONTROL_PLANE_SERVERS, IDENTITY_BOUND_SERVERS
    from kiro_crew.mcp_cleanup import KIROCREW_BIN_MCP_SERVERS

    assert set(IDENTITY_BOUND_SERVERS) == set(KIROCREW_BIN_MCP_SERVERS)
    assert len(IDENTITY_BOUND_SERVERS) == len(set(IDENTITY_BOUND_SERVERS))
    assert IDENTITY_BOUND_SERVERS[: len(CONTROL_PLANE_SERVERS)] == CONTROL_PLANE_SERVERS


class TestGrantedOptInResolution:
    """``managed_mcp_spec_entry(name, include_opt_in=True)`` is the form both
    identity paths read for an opt-in server the spec granted. Every opt-in managed
    server must resolve under it -- not only ``kirocrew-dashboard`` -- or the widened
    :data:`IDENTITY_BOUND_SERVERS` projection silently skips that server (the
    element loop drops a name whose invocation is ``None``) and its calls keep
    refusing as ``identity_unattested``."""

    def test_every_opt_in_server_resolves_only_under_the_flag(self):
        from kiro_crew.agent import managed_mcp_spec_entry
        from kiro_crew.mcp_cleanup import OPT_IN_BIN_MCP_SERVERS

        for name in OPT_IN_BIN_MCP_SERVERS:
            assert managed_mcp_spec_entry(name) is None, f"{name}: a writer never mints a grant"
            entry = managed_mcp_spec_entry(name, include_opt_in=True)
            assert entry is not None and entry["command"], name
            assert entry["args"] == [name.replace("kirocrew-", "mcp-")]

    def test_kiro_projection_asks_for_an_opt_in_invocation(self, tmp_path, monkeypatch):
        from kiro_crew.acp import session_mcp

        name = "kirocrew-dashboard"
        managed = {"command": "test-crew", "args": ["mcp-dashboard"]}
        spec = {"tools": [f"@{name}"], "mcpServers": {name: dict(managed)}}
        seen = []

        def _record(resolved_name, **kwargs):
            seen.append((resolved_name, kwargs))
            return dict(managed)

        monkeypatch.setattr(session_mcp, "_agent_spec_for", lambda *a, **k: spec)
        monkeypatch.setattr(session_mcp, "_global_settings", lambda **kwargs: {})
        monkeypatch.setattr(session_mcp, "_registry_mode", lambda: False)
        monkeypatch.setattr(session_mcp, "managed_mcp_spec_entry", _record)

        elements = session_mcp.kiro_control_plane_servers("kirocrew", work_dir=tmp_path)

        assert [element["name"] for element in elements] == [name]
        assert seen == [(name, {"include_opt_in": True})]

    def test_the_flag_leaves_the_control_plane_answer_unchanged(self):
        from kiro_crew.acp.session_mcp import CONTROL_PLANE_SERVERS
        from kiro_crew.agent import managed_mcp_spec_entry

        for name in CONTROL_PLANE_SERVERS:
            assert managed_mcp_spec_entry(name, include_opt_in=True) == managed_mcp_spec_entry(name)


@pytest.mark.parametrize("scope", ["global", "project"])
@pytest.mark.parametrize(
    "failure",
    [
        "refused",
        "invalid_json",
        "invalid_shape",
        "invalid_servers",
        "oversized",
        "sensitive_link",
        "deep_json",
    ],
)
def test_kiro_identity_projection_fails_closed_on_settings_errors(
    tmp_path, monkeypatch, scope, failure
):
    from kiro_crew import agent, hooks
    from kiro_crew.acp import session_mcp

    managed = {"command": "test-crew", "args": ["mcp"]}
    spec = {"tools": ["@kirocrew-core"], "mcpServers": {"kirocrew-core": managed}}
    global_path = tmp_path / "global" / "mcp.json"
    project_path = tmp_path / ".kiro" / "settings" / "mcp.json"
    path = global_path if scope == "global" else project_path
    path.parent.mkdir(parents=True)
    content = {
        "invalid_json": "{",
        "invalid_shape": "[]",
        "invalid_servers": '{"mcpServers": []}',
        "deep_json": "[" * 2000 + "0" + "]" * 2000,
    }.get(failure, "{}")
    path.write_text(content, encoding="utf-8")
    monkeypatch.setattr(agent, "_KIRO_MCP_JSON", global_path)
    monkeypatch.setattr(session_mcp, "_agent_spec_for", lambda *a, **k: spec)
    monkeypatch.setattr(session_mcp, "_registry_mode", lambda: False)
    monkeypatch.setattr(session_mcp, "managed_mcp_spec_entry", lambda name, **_kw: managed)
    if failure == "refused":
        monkeypatch.setattr(hooks, "validate_file_path", lambda raw: None)
    elif failure == "oversized":
        monkeypatch.setattr(hooks, "MAX_FILE_BYTES", 1)
    elif failure == "sensitive_link":
        target = tmp_path / "synthetic-credential"
        target.mkdir()
        target_file = target / "mcp.json"
        target_file.write_text("{}", encoding="utf-8")
        path.unlink()
        path.parent.rmdir()
        make_dir_link(path.parent, target)
        monkeypatch.setattr(hooks, "is_sensitive_path", lambda raw: Path(raw) == target_file)
        monkeypatch.setattr(
            hooks.platform_compat,
            "open_file_no_reparse",
            lambda *a, **k: pytest.fail("A refused credential target must never be opened"),
        )

    assert session_mcp.kiro_control_plane_servers("kirocrew", work_dir=tmp_path) == []


def test_the_control_plane_element_env_matches_the_spec_writing_consumer(tmp_path, monkeypatch):
    """The projected element's env is the one the disk path writes, and then the token.

    ``kiro_control_plane_servers`` re-declares Crew's OWN managed servers, and a
    session-injected element outranks the spec's same-named entry at launch -- so
    this ``env`` is the whole environment that shim receives, on the one element
    that also carries this session's identity token. It is held to
    ``agent._enforce_managed_mcp_ownership``'s result for the same input: EQUAL, not
    merely safe. Equality is what makes this refuse both directions -- a
    launcher-exec or home-deriving name reaching the element, and an ordinary user
    variable dropped here while the disk path keeps it.
    """
    from kiro_crew import agent as agent_mod
    from kiro_crew.acp import session_mcp
    from kiro_crew.mcp_gateway.session_servers import attach_stub_session_token

    withheld = agent_mod._HOME_DERIVING_ENV_KEYS | agent_mod._LAUNCHER_EXEC_ENV_KEYS
    # PRECONDITION -- the control being mirrored exists for this population, so what
    # follows measures parity with a live rule rather than stating a preference.
    assert {"BASH_ENV", "NODE_OPTIONS", "PATH"} <= withheld, "launcher-exec class absent"
    assert "HOME" in withheld, "home-deriving class absent"

    managed_home = str(tmp_path / "managed-home")
    monkeypatch.setattr(agent_mod, "_managed_mcp_env", lambda: {"KIROCREW_HOME": managed_home})
    declared = {
        "BASH_ENV": "/tmp/preload.sh",
        "NODE_OPTIONS": "--require /tmp/preload.js",
        "PATH": "/tmp/shadow-bin",
        "HOME": "/tmp/fake-home",
        "KIROCREW_SESSION_KEY": "forged:session",
        "KIROCREW_HOME": "/tmp/spec-home",
        "RUST_LOG": "debug",
    }
    managed = {"command": "test-crew", "args": ["mcp"]}
    entry = {**managed, "env": dict(declared)}
    spec = {"tools": ["@kirocrew-core"], "mcpServers": {"kirocrew-core": entry}}
    monkeypatch.setattr(session_mcp, "_agent_spec_for", lambda *a, **k: spec)
    monkeypatch.setattr(session_mcp, "_global_settings", lambda **kwargs: {})
    monkeypatch.setattr(session_mcp, "_registry_mode", lambda: False)
    monkeypatch.setattr(session_mcp, "managed_mcp_spec_entry", lambda name, **_kw: dict(managed))

    elements = session_mcp.kiro_control_plane_servers("an-agent", work_dir=None)

    # PRECONDITION -- the entry really is projected, so every env assertion below is
    # about a value that reaches a session, not about an element that was dropped.
    assert [e.get("name") for e in elements] == ["kirocrew-core"], "the entry was not projected"
    env = {p["name"]: p["value"] for p in elements[0]["env"]}

    # The user's own ordinary variable survives, which is the half a stricter rule
    # would silently cost them.
    assert env["RUST_LOG"] == "debug"
    leaked = sorted(name for name in env if name.upper() in withheld)
    assert leaked == [], f"a spec chose what Crew's own shim executes or reads: {leaked}"
    # Crew's managed value is pinned LAST, so the spec's spelling of the same name loses.
    assert env["KIROCREW_HOME"] == managed_home
    assert "KIROCREW_SESSION_KEY" not in env

    disk_entry = {**managed, "env": dict(declared)}
    agent_mod._enforce_managed_mcp_ownership(disk_entry, {}, False, auto_approve="own")
    assert env == disk_entry["env"], "the two consumers of one managed population disagree"

    # The identity token still lands on that same element, after everything above.
    stamped = attach_stub_session_token(elements, TOKEN)
    assert stamped[0]["env"][-1] == {"name": STUB_SESSION_TOKEN_ENV, "value": TOKEN}
    assert {p["name"]: p["value"] for p in stamped[0]["env"][:-1]} == env


class TestSweep:
    def test_aged_mappings_are_pruned_and_fresh_ones_kept(self, tmp_path):
        fresh = tmp_path / "session_token_aaa.sig"
        stale = tmp_path / "session_token_bbb.sig"
        fresh.write_text("x", encoding="utf-8")
        stale.write_text("x", encoding="utf-8")
        old = time.time() - 30 * 24 * 60 * 60
        import os

        os.utime(stale, (old, old))
        with patch.object(session_pid, "config_dir", return_value=tmp_path):
            removed = session_pid._prune_stale_session_token_files()
        assert removed == 1
        assert fresh.exists() and not stale.exists()

    def test_sweep_ignores_unrelated_files(self, tmp_path):
        other = tmp_path / "session_pid_4242.txt"
        other.write_text("x", encoding="utf-8")
        old = time.time() - 30 * 24 * 60 * 60
        import os

        os.utime(other, (old, old))
        with patch.object(session_pid, "config_dir", return_value=tmp_path):
            assert session_pid._prune_stale_session_token_files() == 0
        assert other.exists()
