"""The POSIX-only mark and the autouse ssh-probe pin the sandbox mask tests share.

``test_sandbox_governance_mask.py`` and ``test_sandbox_governance_mask_model_loaders.py``
both mark POSIX-only tests with ``_POSIX_ONLY`` and both need the host's ``ssh -V``
probe pinned. An autouse fixture applies only in a module that imports it, so each of
them imports ``_no_host_ssh_probe`` by name.
"""

from __future__ import annotations

import sys

import pytest

from kiro_crew import sandbox

_POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX launcher only")


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- no test in a module that imports this fixture calls monkeypatch.undo()
def _no_host_ssh_probe(monkeypatch):
    """A namespace plan asks the HOST's ``ssh -V`` for accept-new support.

    The mask lists read out of the plan do not depend on that answer, and a real ssh
    spawned from the test process is a host dependency this module is not about.
    Pinned so no binary runs.
    """
    monkeypatch.setattr(sandbox, "_ssh_supports_accept_new", lambda: True)
