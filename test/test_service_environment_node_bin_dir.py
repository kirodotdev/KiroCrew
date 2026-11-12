"""``KIROCREW_NODE_BIN_DIR`` reaches the installed service.

launchd and systemd start the gateway with only what ``service_environment()``
bakes, so an override exported in the installing shell is otherwise lost and the
service's MCP search path falls back to nvm's newest Node, which on Amazon Linux
2 does not load. The value names ONE absolute directory; anything else would
smuggle extra entries into every PATH built from it, so it is dropped.
"""

from __future__ import annotations

import os

import pytest

from kiro_crew.service.common import service_environment

_KEY = "KIROCREW_NODE_BIN_DIR"

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="launchd and systemd services only; their PATH is colon-joined"
)


def test_an_absolute_dir_is_baked(monkeypatch):
    monkeypatch.setenv(_KEY, "/opt/node22/bin")
    assert service_environment("/home/tester")[_KEY] == "/opt/node22/bin"


def test_absent_when_the_installer_does_not_set_it(monkeypatch):
    monkeypatch.delenv(_KEY, raising=False)
    assert _KEY not in service_environment("/home/tester")


@pytest.mark.parametrize(
    "value",
    ["node22/bin", "/opt/a/bin:/opt/b/bin", "   "],
)
def test_a_value_that_is_not_one_absolute_dir_is_dropped(monkeypatch, value):
    monkeypatch.setenv(_KEY, value)
    assert _KEY not in service_environment("/home/tester")
