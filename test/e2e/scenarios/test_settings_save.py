"""Scenario: a setting saved through the API survives a gateway restart.

The flow a user performs: change something in Settings, the gateway restarts
(an update, a crash, a machine reboot), the setting is still there. Asserted
against a real service-managed pod, so the persistence path under test is the
one that ships -- config file, reload on boot and all.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.timeout(600)

# `agent.subagent_max_turns` is the target because PUT /api/config/kirocrew
# validates and persists it explicitly (an integer in 1..1000), so a write is
# provably accepted rather than silently dropped as an unrecognised key -- and
# GET returns it in the same masked config document, so the read-back needs no
# second route.
SETTING_SECTION = "agent"
SETTING_KEY = "subagent_max_turns"


def _current(pod) -> object:
    body = pod.api("GET", "config/kirocrew")
    assert isinstance(body, dict), f"GET config/kirocrew gave {type(body).__name__}"
    section = body.get(SETTING_SECTION)
    assert isinstance(section, dict), f"config has no {SETTING_SECTION!r} object: {body.keys()}"
    return section.get(SETTING_KEY)


def test_setting_survives_a_gateway_restart(pod, restart_pod_gateway) -> None:
    before = _current(pod)
    # A value that differs from whatever is there, so the PUT is a real change.
    # A no-op write would pass this scenario with persistence entirely broken.
    target = 37 if before != 37 else 41

    pod.api("PUT", "config/kirocrew", {SETTING_SECTION: {SETTING_KEY: target}})

    assert (
        _current(pod) == target
    ), f"the PUT did not take effect before any restart (got {_current(pod)!r}, want {target})"

    restart_pod_gateway()

    assert _current(pod) == target, (
        f"{SETTING_SECTION}.{SETTING_KEY} did not survive the gateway restart "
        f"(got {_current(pod)!r}, want {target}; was {before!r} before the write)"
    )
