"""Managed MCP children of a centrally governed gateway run in cache-only mode.

KAS starts its managed MCP children with a minimal environment, so the gateway
derives the cache-only markers itself (``_managed_mcp_env``) instead of
forwarding the policy URL or its request headers. Without the marker, a governed
child either tries a fetch it has no credential for or drops to a local ceiling.
"""

from __future__ import annotations

import pytest

from kiro_crew import agent as agent_mod
from kiro_crew.agent_materialization.managed_mcp import _managed_mcp_env
from kiro_crew.platform import policy_distribution as pd
from kiro_crew.platform.enterprise_policy_fetch import PROFILE_PIN_ENV


@pytest.fixture(autouse=True)
def no_inherited_profile_pin(_floor_monkeypatch):
    """The gateway's own pin is process env; a test run must not inherit one."""
    _floor_monkeypatch.delenv(PROFILE_PIN_ENV, raising=False)


@pytest.fixture
def no_override_home(monkeypatch):
    monkeypatch.setattr(agent_mod, "_valid_override_home", lambda: None)


def test_a_central_ceiling_puts_managed_children_in_cache_only_mode(no_override_home, monkeypatch):
    monkeypatch.setattr(pd, "central_ceiling_installed", lambda: True)
    monkeypatch.setattr(pd, "effective_max_cache_age", lambda: 86400)

    env = _managed_mcp_env()

    assert env == {pd.POLICY_CACHE_ONLY_ENV: "1", pd.POLICY_MAX_AGE_ENV: "86400"}


def test_no_staleness_bound_sends_no_max_age(no_override_home, monkeypatch):
    monkeypatch.setattr(pd, "central_ceiling_installed", lambda: True)
    monkeypatch.setattr(pd, "effective_max_cache_age", lambda: 0)

    assert _managed_mcp_env() == {pd.POLICY_CACHE_ONLY_ENV: "1"}


def test_the_policy_source_never_reaches_a_managed_child(no_override_home, monkeypatch):
    monkeypatch.setenv(pd.POLICY_URL_ENV, "https://policy.example/p?X-Amz-Signature=abc")
    monkeypatch.setattr(pd, "central_ceiling_installed", lambda: True)
    monkeypatch.setattr(pd, "effective_max_cache_age", lambda: 0)

    env = _managed_mcp_env()

    assert pd.POLICY_URL_ENV not in env
    assert all("policy.example" not in value for value in env.values())


def test_a_default_install_with_no_central_ceiling_emits_no_env(no_override_home, monkeypatch):
    monkeypatch.setattr(pd, "central_ceiling_installed", lambda: False)

    assert _managed_mcp_env() == {}


def test_the_home_pin_and_the_markers_travel_together(monkeypatch, tmp_path):
    monkeypatch.setattr(agent_mod, "_valid_override_home", lambda: tmp_path)
    monkeypatch.setattr(pd, "central_ceiling_installed", lambda: True)
    monkeypatch.setattr(pd, "effective_max_cache_age", lambda: 0)

    assert _managed_mcp_env() == {"KIROCREW_HOME": str(tmp_path), pd.POLICY_CACHE_ONLY_ENV: "1"}


def test_the_marker_key_set_names_the_real_env_vars():
    from kiro_crew.agent_materialization.managed_mcp import POLICY_MARKER_KEYS

    assert POLICY_MARKER_KEYS == {pd.POLICY_CACHE_ONLY_ENV, pd.POLICY_MAX_AGE_ENV, PROFILE_PIN_ENV}


def test_the_gateway_profile_pin_reaches_a_cache_only_child(no_override_home, monkeypatch):
    """A cache-only child reads its profile only from the pin variable."""
    monkeypatch.setenv(PROFILE_PIN_ENV, "arn:aws:codewhisperer:us-east-1:111:profile/P")
    monkeypatch.setattr(pd, "central_ceiling_installed", lambda: True)
    monkeypatch.setattr(pd, "effective_max_cache_age", lambda: 0)

    assert _managed_mcp_env() == {
        pd.POLICY_CACHE_ONLY_ENV: "1",
        PROFILE_PIN_ENV: "arn:aws:codewhisperer:us-east-1:111:profile/P",
    }


def test_no_profile_pin_sends_no_pin(no_override_home, monkeypatch):
    monkeypatch.setattr(pd, "central_ceiling_installed", lambda: True)
    monkeypatch.setattr(pd, "effective_max_cache_age", lambda: 0)

    assert PROFILE_PIN_ENV not in _managed_mcp_env()


def test_a_profile_pin_without_a_central_ceiling_is_not_forwarded(no_override_home, monkeypatch):
    monkeypatch.setenv(PROFILE_PIN_ENV, "arn:aws:codewhisperer:us-east-1:111:profile/P")
    monkeypatch.setattr(pd, "central_ceiling_installed", lambda: False)

    assert _managed_mcp_env() == {}


def test_a_spec_written_before_the_central_policy_still_probes_as_first_party(
    no_override_home, monkeypatch
):
    """The markers in a spec never run; a stale copy must not cost first-party status."""
    from kiro_crew import mcp_discovery_managed as mdm

    monkeypatch.setattr(mdm, "_cached_managed_invocation", lambda name: ("/bin/kc", ["mcp"]))
    monkeypatch.setattr(pd, "central_ceiling_installed", lambda: True)
    monkeypatch.setattr(pd, "effective_max_cache_age", lambda: 600)
    stale = {pd.POLICY_CACHE_ONLY_ENV: "1", pd.POLICY_MAX_AGE_ENV: "86400"}
    name = next(iter(mdm._MANAGED_SERVER_SUBCOMMANDS))

    assert mdm._is_first_party_managed_argv(name, "/bin/kc", ["mcp"], stale)
    assert not mdm._is_first_party_managed_argv(
        name, "/bin/kc", ["mcp"], {**stale, "LD_PRELOAD": "/x.so"}
    )
