"""Every kiro-cli child Crew spawns names Crew as its client application.

kiro-cli stamps ``KIRO_CLI_CLIENT_APPLICATION`` onto the user-agent of every
request it sends to its model backend (``clientApp/<value>``). ``clientInfo.name``
does not reach there, so without this variable a backend-side record cannot tell a
Crew-driven request from any other kiro-cli request.
"""

from __future__ import annotations

import pytest

from kiro_crew.acp.harness._common import apply_client_application_env
from kiro_crew.acp.harness.kiro import KiroHarness
from kiro_crew.acp.types import KIRO_CLI_CLIENT_APPLICATION, KIRO_CLI_CLIENT_APPLICATION_ENV


@pytest.fixture
def quiet_credentials(monkeypatch):
    """Silence the credential half of both harness hooks, which reads the data home."""
    monkeypatch.setattr(
        "kiro_crew.config.loader.inject_kiro_cli_api_key", lambda _env: None, raising=True
    )
    monkeypatch.setattr(
        "kiro_crew.config.loader.strip_kiro_cli_api_key", lambda _env: None, raising=True
    )


def test_the_name_is_the_one_kiro_cli_reads() -> None:
    """Pinned: kiro-cli reads this exact spelling, so a rename silently drops the tag."""
    assert KIRO_CLI_CLIENT_APPLICATION_ENV == "KIRO_CLI_CLIENT_APPLICATION"
    assert KIRO_CLI_CLIENT_APPLICATION == "kirocrew"


def test_the_tag_and_the_handshake_name_are_one_constant() -> None:
    """One filter string finds Crew in both channels only while these agree."""
    from kiro_crew.acp.client import CLIENT_NAME

    assert CLIENT_NAME is KIRO_CLI_CLIENT_APPLICATION


def test_an_inherited_value_is_overwritten() -> None:
    env = {KIRO_CLI_CLIENT_APPLICATION_ENV: "SomeOuterHost"}
    apply_client_application_env(env)
    assert env[KIRO_CLI_CLIENT_APPLICATION_ENV] == KIRO_CLI_CLIENT_APPLICATION


def test_the_kiro_harness_applies_it(quiet_credentials) -> None:
    env: dict[str, str] = {}
    KiroHarness().apply_spawn_env(env)
    assert env[KIRO_CLI_CLIENT_APPLICATION_ENV] == KIRO_CLI_CLIENT_APPLICATION
