"""``GET /api/crew/mcp-servers`` — the Plane B managed MCP spec (N5-1).

The sidecar serves the ``mcpServers`` block a fresh agent-spec build would emit,
so KAS can register the servers for the live session without any shared-agent-
home rewrite. The handler membership must mirror emission: the two always-on
servers only, ``env.KIROCREW_HOME`` pinned when supervised, opt-ins and the
gated computer server absent.

Two test layers, matching the repo's split:

* the handler shape (mock request, call the handler directly, like
  ``test_sidecar_status_route.py``), and
* the admission matrix (the middleware, like
  ``test_supervised_phase4_admissions.py``).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from kiro_crew.config.paths import config_dir
from kiro_crew.dashboard.handlers_system import api_crew_mcp_servers
from kiro_crew.dashboard.server import (
    _MIXED_INTERNAL_API_PATHS,
    supervised_mixed_internal_paths,
)
from kiro_crew.dashboard.token_auth import token_auth_middleware
from test.test_token_auth import _make_request, _ok_handler

pytestmark = pytest.mark.asyncio

SECRET = "phase5-plane-b-secret"


def _request(
    *, supervised: bool, internal_auth: bool = True, session_key: str | None = None
) -> MagicMock:
    request = MagicMock()
    # MagicMock.get would auto-create a child mock, so back the two keys the
    # handler reads with a dict and a real .get.
    store = {"internal_auth": internal_auth}
    request.get.side_effect = store.get
    request.app = {"supervised": supervised}
    request.headers = {"X-Session-Key": session_key} if session_key is not None else {}
    return request


async def _body(request: MagicMock) -> dict:
    resp = await api_crew_mcp_servers(request)
    return json.loads(resp.body)


# -- handler shape -------------------------------------------------------------


async def test_supervised_returns_exactly_the_two_always_on_servers() -> None:
    data = await _body(_request(supervised=True))
    assert set(data) == {"mcpServers"}
    assert set(data["mcpServers"]) == {"kirocrew-core", "kirocrew-cron"}


async def test_each_entry_has_command_args_and_pinned_home() -> None:
    servers = (await _body(_request(supervised=True)))["mcpServers"]
    home = str(config_dir())
    for name, entry in servers.items():
        assert isinstance(entry["command"], str) and entry["command"], name
        assert isinstance(entry["args"], list) and entry["args"], name
        # The subcommand rides args regardless of the standalone-vs -m form.
        sub = name.replace("kirocrew-", "mcp-")
        assert sub in entry["args"], (name, entry["args"])
        assert entry["env"]["KIROCREW_HOME"] == home, name


async def test_supervised_kiro_cli_caller_gets_its_session_key_pinned() -> None:
    """P5-2: a KAS-spawned proxy has no gateway ancestor to inherit
    KIROCREW_SESSION_KEY from, so every session-keyed tool failed with
    'missing X-Session-Key'; the caller's own kiro-cli: key is pinned instead."""
    servers = (await _body(_request(supervised=True, session_key="kiro-cli:sess-42")))["mcpServers"]
    for name, entry in servers.items():
        assert entry["env"]["KIROCREW_SESSION_KEY"] == "kiro-cli:sess-42", name
        assert entry["env"]["KIROCREW_HOME"] == str(config_dir()), name


async def test_non_supervisor_caller_key_is_not_pinned() -> None:
    servers = (await _body(_request(supervised=True, session_key="dashboard:slot-1")))["mcpServers"]
    for entry in servers.values():
        assert "KIROCREW_SESSION_KEY" not in entry["env"]
    servers = (await _body(_request(supervised=False, session_key="kiro-cli:sess-42")))["mcpServers"]
    for entry in servers.values():
        assert "KIROCREW_SESSION_KEY" not in (entry.get("env") or {})


async def test_bundle_fallback_serves_interpreter_dash_m_kiro_crew(monkeypatch) -> None:
    """R1 (`python -m kiro_crew` parity): when no standalone ``kirocrew`` console
    script resolves — the packaged-bundle / systemd-user case where
    ``_resolve_kirocrew_bin`` returns the bare ``"kirocrew"`` sentinel —
    ``_kirocrew_mcp_invocation`` falls back to ``sys.executable -m kiro_crew``.
    The served spec must therefore carry the running interpreter as ``command``
    and ``["-m", "kiro_crew", <sub>]`` as ``args``, since ``python -m kiro_crew``
    dispatches the same CLI as the console script."""
    import sys

    from kiro_crew import agent

    # The resolver memoizes into ``_KIROCREW_BIN``; patch the function itself so
    # the cache is bypassed and the unresolved-sentinel branch is taken.
    monkeypatch.setattr(agent, "_resolve_kirocrew_bin", lambda: "kirocrew")

    servers = (await _body(_request(supervised=True)))["mcpServers"]
    assert set(servers) == {"kirocrew-core", "kirocrew-cron"}
    for name, entry in servers.items():
        sub = name.replace("kirocrew-", "mcp-")
        assert entry["command"] == sys.executable, name
        assert entry["args"][:2] == ["-m", "kiro_crew"], (name, entry["args"])
        assert entry["args"][2] == sub, (name, entry["args"])


async def test_supervised_pins_effective_profile(monkeypatch) -> None:
    """R3: a foreign-host proxy must compose the SAME edition the gateway did, so
    the served spec pins ``env.KIROCREW_PROFILE`` to ``current_context().profile``
    when supervised. Patched to a known value so the assert does not depend on the
    test host's own resolution."""
    import kiro_crew.dashboard.handlers_system as hs

    monkeypatch.setattr(
        hs, "current_context", lambda: SimpleNamespace(profile="enterprise")
    )
    servers = (await _body(_request(supervised=True)))["mcpServers"]
    assert servers  # non-empty
    for name, entry in servers.items():
        assert entry["env"]["KIROCREW_PROFILE"] == "enterprise", name


async def test_unsupervised_does_not_pin_profile() -> None:
    servers = (await _body(_request(supervised=False)))["mcpServers"]
    for entry in servers.values():
        assert "KIROCREW_PROFILE" not in (entry.get("env") or {})


async def test_unreadable_profile_omits_pin_without_failing(monkeypatch) -> None:
    """A context that cannot compose must not fail the whole spec — the pin is
    simply omitted (proxy falls back to its own resolution)."""
    import kiro_crew.dashboard.handlers_system as hs

    def _boom():
        raise RuntimeError("no context")

    monkeypatch.setattr(hs, "current_context", _boom)
    servers = (await _body(_request(supervised=True)))["mcpServers"]
    assert set(servers) == {"kirocrew-core", "kirocrew-cron"}
    for entry in servers.values():
        assert "KIROCREW_PROFILE" not in (entry.get("env") or {})
        # HOME pin still applied — the profile failure is isolated.
        assert entry["env"]["KIROCREW_HOME"] == str(config_dir())


async def test_opt_in_and_gated_servers_are_absent() -> None:
    servers = (await _body(_request(supervised=True)))["mcpServers"]
    # Opt-in sets are never auto-emitted; computer is gated off in a test process
    # (no enabled+supported driver), so its spec_gate is closed.
    for absent in ("kirocrew-dashboard", "kirocrew-work", "kirocrew-computer"):
        assert absent not in servers


async def test_unsupervised_membership_matches_supervised() -> None:
    # Membership is emission-driven, not supervision-driven: the same two always-
    # on servers appear in both modes. (The KIROCREW_HOME pin is what supervision
    # adds; on a default home an unsupervised reader would see no env, while under
    # an override home _managed_mcp_env already carries one — so membership, not
    # the env, is the mode-invariant this asserts.)
    servers = (await _body(_request(supervised=False)))["mcpServers"]
    assert set(servers) == {"kirocrew-core", "kirocrew-cron"}


async def test_missing_internal_auth_is_refused() -> None:
    resp = await api_crew_mcp_servers(_request(supervised=True, internal_auth=False))
    assert resp.status == 403
    assert json.loads(resp.body)["code"] == "internal_required"


# -- admission matrix ----------------------------------------------------------


def _mw(supervised: bool):
    return token_auth_middleware(
        mixed_internal_paths=supervised_mixed_internal_paths(supervised),
        internal_secret=SECRET,
    )


def test_route_is_in_the_supervised_set_only() -> None:
    added = supervised_mixed_internal_paths(True) - _MIXED_INTERNAL_API_PATHS
    assert "/api/crew/mcp-servers" in added
    assert "/api/crew/mcp-servers" not in _MIXED_INTERNAL_API_PATHS


async def test_supervised_secret_reaches_route() -> None:
    req = _make_request(path="/api/crew/mcp-servers", headers={"X-Internal-Secret": SECRET})
    resp = await _mw(True)(req, _ok_handler)
    assert resp.status == 200


async def test_unsupervised_secret_holder_denied() -> None:
    req = _make_request(path="/api/crew/mcp-servers", headers={"X-Internal-Secret": SECRET})
    resp = await _mw(False)(req, _ok_handler)
    assert resp.status == 403


async def test_wrong_secret_denied() -> None:
    req = _make_request(path="/api/crew/mcp-servers", headers={"X-Internal-Secret": "nope"})
    resp = await _mw(True)(req, _ok_handler)
    assert resp.status == 403
