"""The client factory, notification builder and kiro-cli pin the ACP client tests share.

``test_acp_client_more_coverage.py`` and its ``_session_frames``, ``_observers`` and
``_encode_refusal`` siblings import these by name: ``_client``, an ``AcpClient`` rooted at
the test's ``tmp_path``; ``_notify``, the ``JsonRpcMessage`` notification the client
reads; and ``_pinned_kiro_cli_version``, an autouse fixture every module imports so it
applies to all their tests, which pins the release the spec ``permissions`` gate reads so
no client start spawns the host's ``kiro-cli --version``.
"""

from pathlib import Path

import pytest

from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.types import JsonRpcMessage
from kiro_crew.kiro_cli import SPEC_PERMISSIONS_MIN_VERSION


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- no test in the test_acp_client_more_coverage*.py modules calls monkeypatch.undo()
def _pinned_kiro_cli_version(monkeypatch):
    """Pin the kiro-cli release the spec ``permissions`` gate believes is installed.

    A client start here materialises the agent spec (``ensure_agent_materialized``
    -> ``rebuild_agent_config`` -> ``_write_derived_permissions``), which reads
    ``installed_kiro_cli_version`` function-locally from ``kiro_crew.kiro_cli``:
    one real ``kiro-cli --version`` spawn per binary identity, process-cached, so
    whichever test in the worker starts a client first pays it against the HOST's
    install with the checkout as the child's cwd. Pinned to the floor release, as
    ``test_agent.py`` and the generated-writer suites pin it.
    """
    monkeypatch.setattr(
        "kiro_crew.kiro_cli.installed_kiro_cli_version",
        lambda: SPEC_PERMISSIONS_MIN_VERSION,
    )


def _client(tmp_path: Path, **kwargs) -> AcpClient:
    """An AcpClient pinned to *tmp_path* so nothing is written outside it."""
    return AcpClient(work_dir=tmp_path, **kwargs)


def _notify(method, params):
    return JsonRpcMessage(method=method, params=params)
