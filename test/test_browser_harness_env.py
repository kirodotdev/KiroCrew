"""The daemon environment fence (``browser_harness/env.py``).

Pins the RFC's table: the three telemetry switches, the dead PostHog host, the
relay URL, and the removed cloud keys -- which must stay removed even when the
gateway's own environment carries them.
"""

from __future__ import annotations

import pytest

from kiro_crew.browser_harness import env

_POISONED_BASE = {
    "PATH": "/usr/bin",
    "HOME": "/home/someone",
    "BROWSER_USE_API_KEY": "bu_live_secret",
    "BU_BROWSER_ID": "hosted-browser-1",
    "BU_CDP_WS": "ws://example.invalid/devtools",
    "BU_AUTOSPAWN": "1",
    "BH_TELEMETRY": "true",
    "BROWSER_HARNESS_TELEMETRY": "1",
    "ANONYMIZED_TELEMETRY": "yes",
    "BH_POSTHOG_HOST": "https://eu.i.posthog.com",
    "BH_TAB_MARKER": "1",
    "BH_HOME": "/somewhere/visible",
    "BH_AGENT_WORKSPACE": "/somewhere/with/a/dotenv",
}


def test_every_removed_key_stays_removed_even_when_the_base_carries_it() -> None:
    out = env.daemon_env(_POISONED_BASE, "http://127.0.0.1:4242")
    for key in env.REMOVED_ENV:
        assert key not in out, key


def test_every_forced_value_overrides_the_base() -> None:
    out = env.daemon_env(_POISONED_BASE, "http://127.0.0.1:4242")
    for switch in env.TELEMETRY_SWITCHES:
        assert out[switch] == "false"
    assert out["BH_POSTHOG_HOST"] == env.DEAD_POSTHOG_HOST
    assert out["BH_TAB_MARKER"] == "0"
    assert out["BH_UPDATE_CHECK"] == "0"


def test_the_relay_url_is_the_only_browser_the_daemon_can_reach() -> None:
    out = env.daemon_env(_POISONED_BASE, "http://127.0.0.1:4242")
    assert out[env.CDP_URL_ENV] == "http://127.0.0.1:4242"
    assert "BU_CDP_WS" not in out
    assert "BU_BROWSER_ID" not in out


def test_the_gateway_environment_is_otherwise_inherited() -> None:
    out = env.daemon_env(_POISONED_BASE, "http://127.0.0.1:1")
    assert out["PATH"] == "/usr/bin"
    assert out["HOME"] == "/home/someone"


def test_the_dead_host_is_loopback() -> None:
    assert env.DEAD_POSTHOG_HOST.startswith("http://127.0.0.1:")


@pytest.mark.parametrize(
    "key", ["BROWSER_USE_API_KEY", "BU_BROWSER_ID", "BU_CDP_WS", "BU_AUTOSPAWN"]
)
def test_the_rfc_table_names_each_cloud_key(key: str) -> None:
    assert key in env.REMOVED_ENV


def test_the_base_mapping_is_not_mutated() -> None:
    base = dict(_POISONED_BASE)
    env.daemon_env(base, "http://127.0.0.1:1")
    assert base == _POISONED_BASE


def test_the_loopback_relay_hop_bypasses_any_proxy_the_shell_set() -> None:
    base = {**_POISONED_BASE, "HTTPS_PROXY": "http://proxy.corp:3128", "HTTP_PROXY": "http://p:1"}
    out = env.daemon_env(base, "http://127.0.0.1:4242/secret")
    for key in env.NO_PROXY_KEYS:
        entries = out[key].split(",")
        assert "127.0.0.1" in entries and "localhost" in entries, key
    # The proxy itself is inherited: only the loopback hop is exempted from it.
    assert out["HTTPS_PROXY"] == "http://proxy.corp:3128"


def test_an_existing_no_proxy_list_is_extended_not_replaced() -> None:
    base = {**_POISONED_BASE, "no_proxy": ".corp.example, 10.0.0.0/8", "NO_PROXY": "localhost"}
    out = env.daemon_env(base, "http://127.0.0.1:1")
    entries = out["NO_PROXY"].split(",")
    assert ".corp.example" in entries and "10.0.0.0/8" in entries
    assert entries.count("localhost") == 1 and "127.0.0.1" in entries
    assert out["no_proxy"] == out["NO_PROXY"]
