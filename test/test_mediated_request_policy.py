"""Tests for the mediated-request owner-authorization policy."""

from __future__ import annotations

import json

import pytest

from kiro_crew.secrets_mediation.policy import (
    POLICY_FILENAME,
    CredentialPlacement,
    PolicyError,
    load_authorization,
    normalize_origin,
)


def _write_policy(config_dir, authorizations):
    from kiro_crew.secrets_mediation.provenance import SIGNATURE_FIELD, sign_authorizations

    sig = sign_authorizations(authorizations, config_dir)
    (config_dir / POLICY_FILENAME).write_text(
        json.dumps({"version": 1, "authorizations": authorizations, SIGNATURE_FIELD: sig}),
        encoding="utf-8",
    )


def test_normalize_origin_strips_path_and_default_port():
    assert normalize_origin("https://api.example.com/v1/x?y=1") == "https://api.example.com"
    assert normalize_origin("https://api.example.com:443/") == "https://api.example.com"
    assert normalize_origin("https://api.example.com:8443/") == "https://api.example.com:8443"


def test_normalize_origin_rejects_http():
    with pytest.raises(PolicyError):
        normalize_origin("http://api.example.com")


def test_missing_policy_file_fails_closed(tmp_path):
    with pytest.raises(PolicyError):
        load_authorization(tmp_path, "WEATHER_API_KEY")


def test_unauthorized_secret_fails_closed(tmp_path):
    _write_policy(
        tmp_path, {"OTHER": {"origin": "https://a.example", "placement": {"type": "bearer"}}}
    )
    with pytest.raises(PolicyError):
        load_authorization(tmp_path, "WEATHER_API_KEY")


def test_bearer_authorization(tmp_path):
    _write_policy(
        tmp_path,
        {
            "WEATHER_API_KEY": {
                "origin": "https://api.weather.example/",
                "placement": {"type": "bearer"},
            }
        },
    )
    auth = load_authorization(tmp_path, "WEATHER_API_KEY")
    assert auth.origin == "https://api.weather.example"
    assert auth.placement == CredentialPlacement(type="bearer")


def test_header_authorization(tmp_path):
    _write_policy(
        tmp_path,
        {
            "K": {
                "origin": "https://api.example",
                "placement": {"type": "header", "header": "X-Api-Key"},
            }
        },
    )
    auth = load_authorization(tmp_path, "K")
    assert auth.placement.type == "header"
    assert auth.placement.header == "X-Api-Key"


def test_header_placement_cannot_be_authorization(tmp_path):
    _write_policy(
        tmp_path,
        {
            "K": {
                "origin": "https://api.example",
                "placement": {"type": "header", "header": "Authorization"},
            }
        },
    )
    with pytest.raises(PolicyError):
        load_authorization(tmp_path, "K")


def test_header_placement_rejects_crlf_injection(tmp_path):
    _write_policy(
        tmp_path,
        {
            "K": {
                "origin": "https://api.example",
                "placement": {"type": "header", "header": "X\r\nEvil"},
            }
        },
    )
    with pytest.raises(PolicyError):
        load_authorization(tmp_path, "K")


def test_unknown_placement_type_fails_closed(tmp_path):
    _write_policy(
        tmp_path,
        {"K": {"origin": "https://api.example", "placement": {"type": "query"}}},
    )
    with pytest.raises(PolicyError):
        load_authorization(tmp_path, "K")


def test_malformed_json_fails_closed(tmp_path):
    (tmp_path / POLICY_FILENAME).write_text("{not json", encoding="utf-8")
    with pytest.raises(PolicyError):
        load_authorization(tmp_path, "K")


# --- Trust-root fence -------------------------------------------------------
# The authorization file names each stored secret's approved egress destination.
# If an auto-approved agent shell could write it, the agent could authorize any
# origin and have trusted code inject the live secret into a request to it — the
# exfiltration oracle the mediation exists to prevent. So the file MUST sit on
# the shared read+write keystone floor, exactly like ops_mission_control_policy.
# These tests fail if the leaf is dropped from ``security._CREW_SECRET_LEAVES``.


def test_policy_filename_is_registered_on_the_secret_floor():
    from kiro_crew import security

    assert POLICY_FILENAME in security._CREW_SECRET_LEAVES


def test_agent_file_tools_cannot_touch_the_policy():
    import os

    from kiro_crew import security

    for prefix in security._CREW_HOME_PREFIXES:
        path = os.path.expanduser(f"~/{prefix}/{POLICY_FILENAME}")
        assert security.is_sensitive_path(path), path


# --- Owner provenance ------------------------------------------------------
# The policy must carry an owner signature over the authorizations, keyed by the
# sandbox-masked member key. An agent-planted / pre-upgrade policy cannot forge
# it, so it must fail closed. These tests fail without the signature gate in
# ``load_authorization``.


def _auth_map():
    return {"K": {"origin": "https://api.example.com", "placement": {"type": "bearer"}}}


def test_unsigned_policy_fails_closed(tmp_path):
    # A plausible policy with NO signature — exactly what an agent could plant.
    (tmp_path / POLICY_FILENAME).write_text(
        json.dumps({"version": 1, "authorizations": _auth_map()}), encoding="utf-8"
    )
    with pytest.raises(PolicyError):
        load_authorization(tmp_path, "K")


def test_tampered_authorizations_fail_closed(tmp_path):
    # Sign one origin, then swap it for an attacker origin: the signature no
    # longer matches the authorizations, so it must be refused.
    from kiro_crew.secrets_mediation.provenance import SIGNATURE_FIELD, sign_authorizations

    good = _auth_map()
    sig = sign_authorizations(good, tmp_path)
    tampered = {"K": {"origin": "https://evil.example", "placement": {"type": "bearer"}}}
    (tmp_path / POLICY_FILENAME).write_text(
        json.dumps({"version": 1, "authorizations": tampered, SIGNATURE_FIELD: sig}),
        encoding="utf-8",
    )
    with pytest.raises(PolicyError):
        load_authorization(tmp_path, "K")


def test_correctly_signed_policy_loads(tmp_path):
    from kiro_crew.secrets_mediation.provenance import SIGNATURE_FIELD, sign_authorizations

    good = _auth_map()
    sig = sign_authorizations(good, tmp_path)
    (tmp_path / POLICY_FILENAME).write_text(
        json.dumps({"version": 1, "authorizations": good, SIGNATURE_FIELD: sig}),
        encoding="utf-8",
    )
    auth = load_authorization(tmp_path, "K")
    assert auth.origin == "https://api.example.com"
    assert auth.placement.type == "bearer"
