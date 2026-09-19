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
import time
from unittest.mock import patch

import pytest

from kiro_crew import mcp_core, members, session_pid, session_pid_sig, session_token_sig
from kiro_crew.mcp_gateway.claim import STUB_SESSION_TOKEN_ENV, mint_stub_session_token
from kiro_crew.providers.mirrors.identity import control_plane_identity_env

LIVE_KEY = "dashboard:chat-9-current"
STALE_KEY = "dashboard:chat-7-previous"
TOKEN = "c" * 64
OTHER_TOKEN = "d" * 64


def _path_for(cfg, token: str):
    return cfg / f"session_token_{hashlib.sha256(token.encode()).hexdigest()}.sig"


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """Isolated mapping dir + trust root, with the resolver's OTHER sources off.

    ``current_caller`` and ``protected_member_session_for_pid`` are the two
    sources the resolvers consult ABOVE the token. They are pinned to "absent"
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
        patch("kiro_crew.member_memory_auth.protected_member_session_for_pid", return_value=None),
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
