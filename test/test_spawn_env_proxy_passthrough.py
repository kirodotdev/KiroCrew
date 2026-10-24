"""Proxy variables survive the agent spawn scrub.

The desktop shell discovers the system proxy for GUI launches that never
inherit a shell profile's ``HTTP_PROXY`` / ``HTTPS_PROXY``
(``website/electron/system-proxy-env.js``): on a proxy-only network a
bundled ``kiro-cli-chat`` without those variables reaches for the model
backend directly and every turn reports a "transient 5xx". The merge happens
in the gateway's own environment, so the Python spawn chain's part of the
contract is the agent scrub: a credential denylist that never names a proxy
variable, so an inherited proxy setting flows to the kiro-cli child
untouched.

Both halves are pinned here. If the scrub ever grows a proxy prefix, or
``agent_env_scrub_prefixes()`` (the refusal oracle reused by env-injecting
validators) reports one as stripped, proxy-only networks regress to failing
every turn and the shell-side discovery stops mattering.
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
