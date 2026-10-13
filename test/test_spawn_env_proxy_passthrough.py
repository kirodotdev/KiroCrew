"""Proxy variables survive the agent spawn scrub.

The desktop shell discovers the system proxy for GUI launches that never
inherit a shell profile's ``HTTP_PROXY`` / ``HTTPS_PROXY``
(``website/electron/system-proxy-env.js``, issue #19821: on a proxy-only
network every bundled ``kiro-cli-chat`` turn failed as a "transient 5xx"
because the child reached for the model backend directly). The merge happens
in the gateway's own environment, so the only thing the Python spawn chain has
to guarantee is the other half of the fix: the agent scrub is a credential
denylist that never names a proxy variable, so an inherited proxy setting
flows to the kiro-cli child untouched.

Pin both halves of that guarantee here. If the scrub ever grows a proxy
prefix, or ``agent_env_scrub_prefixes()`` (the refusal oracle reused by
env-injecting validators) starts reporting one as stripped, proxy-only
networks regress to failing every turn — and the shell-side discovery would
silently stop mattering.
"""

from __future__ import annotations

import pytest

from kiro_crew.sandbox import agent_env_scrub_prefixes, scrub_agent_subprocess_env

PROXY_VARS = [
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
]


@pytest.mark.parametrize("name", PROXY_VARS)
def test_a_proxy_var_in_the_gateway_env_reaches_the_agent_child(name: str) -> None:
    env = {"PATH": "/usr/bin", name: "http://proxy.corp.example:3128"}
    scrubbed = scrub_agent_subprocess_env(env)
    assert scrubbed.get(name) == "http://proxy.corp.example:3128"


@pytest.mark.parametrize("name", PROXY_VARS)
def test_no_scrub_prefix_names_a_proxy_var(name: str) -> None:
    prefixes = agent_env_scrub_prefixes()
    assert not any(name.startswith(prefix) for prefix in prefixes)
