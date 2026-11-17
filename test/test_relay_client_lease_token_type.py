"""The lease call's TokenType when ``tunnel.relay_lease_token_type`` is empty or NONE.

A credential stored as a secret or a file, with the type empty, is a team Crew's crew
API key. ``NONE`` sends no header. Both empty is still a personal Crew's sign-in, and
a reference that is neither a secret nor a file is never taken for a crew API key.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import pytest

from kiro_crew.relay_client import provider as provider_module
from kiro_crew.relay_client.lease import LeaseHostTokenSource
from kiro_crew.relay_client.provider import (
    RelaySettings,
    RelaySettingsError,
    lease_token_source,
)

LEASE_URL = "https://lease.example.com"


def _settings(credential: str, token_type: str = "") -> RelaySettings:
    return RelaySettings(
        enabled=True,
        host_url="",
        crew_domain="",
        lease_url=LEASE_URL,
        lease_credential=credential,
        lease_token_type=token_type,
    )


def _sent_type(settings: RelaySettings) -> Optional[str]:
    source = lease_token_source(settings)
    assert isinstance(source, LeaseHostTokenSource)
    return source._token_type


@pytest.mark.parametrize("stored", ["secret://relay-crew-key", "file"])
def test_a_stored_credential_with_the_type_empty_sends_api_key(tmp_path: Path, stored: str) -> None:
    credential = str(tmp_path / "crew-key") if stored == "file" else stored
    settings = _settings(credential)
    settings.validate()
    assert not settings.uses_sign_in
    assert settings.lease_header_token_type == "API_KEY"
    assert settings.is_team_crew
    assert _sent_type(settings) == "API_KEY"


def test_none_sends_no_token_type_header() -> None:
    settings = _settings("secret://idc-bearer", "NONE")
    settings.validate()
    assert settings.lease_header_token_type is None
    assert not settings.is_team_crew
    assert _sent_type(settings) is None


def test_an_explicit_api_key_is_unchanged() -> None:
    settings = _settings("secret://relay-crew-key", "API_KEY")
    settings.validate()
    assert settings.is_team_crew
    assert _sent_type(settings) == "API_KEY"


def test_both_empty_is_still_the_personal_sign_in() -> None:
    settings = _settings("")
    assert settings.uses_sign_in
    assert not settings.is_team_crew
    source = lease_token_source(settings)
    assert isinstance(source, LeaseHostTokenSource)
    assert source._sign_in is not None and source._token_type is None


@pytest.mark.parametrize("admitted", [False, True])
def test_a_workload_identity_reference_is_not_taken_for_a_crew_api_key(
    monkeypatch: pytest.MonkeyPatch, admitted: bool
) -> None:
    if admitted:
        # As when the setting accepts a workload identity reference as a credential.
        monkeypatch.setattr(provider_module, "credential_reference_problem", lambda ref: None)
    settings = _settings("workload-identity:jwt-svid")
    assert not settings.uses_sign_in
    assert settings.lease_header_token_type is None
    assert not settings.is_team_crew
    if admitted:
        settings.validate()
        assert _sent_type(settings) is None


def test_none_beside_the_sign_in_is_refused() -> None:
    with pytest.raises(RelaySettingsError, match="must be empty"):
        _settings("kiro-sign-in", "NONE").validate()
