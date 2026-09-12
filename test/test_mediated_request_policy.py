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
    (config_dir / POLICY_FILENAME).write_text(
        json.dumps({"version": 1, "authorizations": authorizations}), encoding="utf-8"
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
