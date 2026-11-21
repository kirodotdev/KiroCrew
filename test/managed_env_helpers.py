"""Shared fixtures for the managed-environment lane's tests.

Import the fixtures a module needs by name (``from managed_env_helpers import ...``); an
imported autouse fixture applies to that module only. Every value here is a placeholder:
no real endpoint, profile, Region or host.
"""

from __future__ import annotations

import pytest

from kiro_crew.managed_env import engine, install, settings

#: The real reader, kept before any test patches it (tests of the reader itself use this).
REAL_READ_SETTINGS = settings.read_settings

#: Every ``instances.managed_env_*`` value set, with placeholders only.
FULL_SETTINGS = settings.LaneSettings(
    endpoint="https://managed-env.example.test",
    start_url="https://example-org.example.test/start",
    profile_arn="arn:aws:codewhisperer:zz-example-1::profile/EXAMPLE",
    scopes=(),
    ssh_region="zz-example-1",
    kas_endpoint="https://runtime.example.test:443",
    kas_control_plane_endpoint="https://management.example.test",
    kas_region="zz-example-1",
    mcp_registry_mode=True,
    box_token_path="/run/example/access_token",
    box_token_env="EXAMPLE_ACCESS_TOKEN",
    box_env_drop=("EXAMPLE_CONFIG_ENDPOINT", "EXAMPLE_SESSIONS_ENDPOINT"),
    box_unset_env=("EXAMPLE_IMAGE_TOKEN",),
    box_openssl="/opt/example/bin/openssl",
)


@pytest.fixture(autouse=True)
def no_real_install_ssh(monkeypatch):
    """Keep every test's launch off this host's ``ssh``: tests inject a fake runner."""

    def _refuse(self, host, command, stdin=None):
        raise AssertionError(
            f"a test reached the real install ssh for {host!r}: inject install_runner"
        )

    monkeypatch.setattr(install.SshRunner, "run", _refuse)
    monkeypatch.setattr(engine.InstallSshRunner, "run", _refuse)


@pytest.fixture(autouse=True)
def lane_values(monkeypatch):
    """Every lane value set (placeholders), unless a test patches ``read_settings`` itself."""
    monkeypatch.setattr(settings, "read_settings", lambda cfg=None: FULL_SETTINGS)
    return FULL_SETTINGS


@pytest.fixture(autouse=True)
def fresh_shared_client():
    """No client (and no guard memory) carries over between tests."""
    engine.reset_shared_client()
    yield
    engine.reset_shared_client()
