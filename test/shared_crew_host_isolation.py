"""Keeps Shared Crew tests off this machine's own name and Tailscale daemon.

The owner's label is the host's computer name and the People list reads the
local Tailscale daemon's peers. A test that exercises either through a route
would otherwise see whoever runs the suite. ``pytestmark =
pytest.mark.usefixtures("host_isolation")`` in a test module pins both to fixed
answers; a test that needs a different one replaces it on the same objects.
"""

from __future__ import annotations

import pytest

from kiro_crew.dashboard import tailnet_localapi
from kiro_crew.shared_crew import owner, tailscale_sharees

MACHINE_NAME = "Test Machine"


@pytest.fixture
def host_isolation(monkeypatch):
    owner.reset_for_tests()
    monkeypatch.setattr(owner, "machine_name", lambda **_kw: MACHINE_NAME)
    tailscale_sharees.reset_for_tests(read=lambda: tailnet_localapi.Answer(None, "none"))
    yield
    owner.reset_for_tests()
    tailscale_sharees.reset_for_tests()
