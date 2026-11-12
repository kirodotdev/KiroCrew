"""The relay viewer's settings: off and empty by default, and refused when unsafe.

``tunnel.relay_viewer_cps_url`` carries the person's sign-in, so it follows the
lease URL's rule. ``tunnel.relay_viewer_credential`` names where a credential is
and never holds one, so a value that is not a reference is dropped without being
logged.
"""

from __future__ import annotations

import json
import logging

import pytest

from kiro_crew.config.loader import KiroCrewConfig

CREDENTIAL = "not-a-reference-pasted-credential-0123456789"


def _load(tmp_path, monkeypatch, tunnel: dict) -> KiroCrewConfig:
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"tunnel": tunnel}), encoding="utf-8")
    monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: config_file)
    return KiroCrewConfig.load()


def test_the_viewer_is_off_and_empty_by_default() -> None:
    tunnel = KiroCrewConfig().tunnel
    assert tunnel.relay_viewer_enabled is False
    assert tunnel.relay_viewer_cps_url == ""
    assert tunnel.relay_viewer_credential == ""
    assert tunnel.relay_viewer_relay_domain == ""


def test_the_viewer_settings_round_trip(tmp_path, monkeypatch) -> None:
    stored = {
        "relay_viewer_enabled": True,
        "relay_viewer_cps_url": "https://control.example.com/",
        "relay_viewer_credential": "secret://viewer-test",
        "relay_viewer_relay_domain": "us-east-1.relay.example.invalid",
    }
    cfg = _load(tmp_path, monkeypatch, stored)
    tunnel = cfg.to_dict()["tunnel"]
    assert {k: tunnel[k] for k in stored} == stored
    again = _load(tmp_path, monkeypatch, tunnel)
    assert again.tunnel == cfg.tunnel


def test_a_string_true_does_not_turn_the_viewer_on(tmp_path, monkeypatch) -> None:
    assert (
        _load(tmp_path, monkeypatch, {"relay_viewer_enabled": "yes"}).tunnel.relay_viewer_enabled
        is False
    )


@pytest.mark.parametrize(
    "url",
    ["https://control.example.com", "http://127.0.0.1:8790", "http://localhost:8790/"],
)
def test_an_https_or_loopback_url_is_kept(tmp_path, monkeypatch, url) -> None:
    assert (
        _load(tmp_path, monkeypatch, {"relay_viewer_cps_url": url}).tunnel.relay_viewer_cps_url
        == url
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://control.example.com",
        "https://user:pw@control.example.com",
        "https://control.example.com/?x=1",
        "https://control.example.com/#f",
        "ftp://127.0.0.1",
        "https://",
        "not a url",
    ],
)
def test_an_unsafe_url_is_ignored_with_a_warning(tmp_path, monkeypatch, caplog, url) -> None:
    with caplog.at_level(logging.WARNING):
        cfg = _load(tmp_path, monkeypatch, {"relay_viewer_cps_url": url})
    assert cfg.tunnel.relay_viewer_cps_url == ""
    assert any("tunnel.relay_viewer_cps_url ignored" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("ref", ["secret://viewer-test", "/run/viewer-credential"])
def test_a_credential_reference_is_kept(tmp_path, monkeypatch, ref) -> None:
    assert (
        _load(
            tmp_path, monkeypatch, {"relay_viewer_credential": ref}
        ).tunnel.relay_viewer_credential
        == ref
    )


@pytest.mark.parametrize("ref", [CREDENTIAL, "secret://", "relative/path"])
def test_a_credential_value_is_dropped_and_never_logged(tmp_path, monkeypatch, caplog, ref) -> None:
    with caplog.at_level(logging.DEBUG):
        cfg = _load(tmp_path, monkeypatch, {"relay_viewer_credential": ref})
    assert cfg.tunnel.relay_viewer_credential == ""
    messages = [r.getMessage() for r in caplog.records]
    assert any("tunnel.relay_viewer_credential ignored" in m for m in messages)
    assert not any(CREDENTIAL in m for m in messages)


@pytest.mark.parametrize(
    ("value", "kept"),
    [
        ("us-east-1.relay.example.invalid", "us-east-1.relay.example.invalid"),
        ("  Relay.Example.Invalid ", "relay.example.invalid"),
    ],
)
def test_a_relay_domain_is_kept_in_lower_case(tmp_path, monkeypatch, value, kept) -> None:
    cfg = _load(tmp_path, monkeypatch, {"relay_viewer_relay_domain": value})
    assert cfg.tunnel.relay_viewer_relay_domain == kept


@pytest.mark.parametrize(
    ("value", "kept"),
    [
        ("https://relay.example.test", "https://relay.example.test"),
        ("Relay.Example.Test:443", "relay.example.test:443"),
        ("relay.example.test/", "relay.example.test/"),
        (" Relaý.Example.Test ", "Relaý.Example.Test"),
        ("   ", ""),
    ],
)
def test_a_relay_domain_is_kept_as_written_with_no_format_check(
    tmp_path, monkeypatch, caplog, value, kept
) -> None:
    with caplog.at_level(logging.WARNING):
        cfg = _load(tmp_path, monkeypatch, {"relay_viewer_relay_domain": value})
    assert cfg.tunnel.relay_viewer_relay_domain == kept
    assert not any("relay_viewer_relay_domain" in r.getMessage() for r in caplog.records)


def test_an_upper_case_relay_domain_still_matches_a_listed_crew(tmp_path, monkeypatch) -> None:
    from kiro_crew.relay_viewer import cps

    endpoint = "teamaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    cfg = _load(tmp_path, monkeypatch, {"relay_viewer_relay_domain": "Relay.Example.Test"})
    raw = {
        "endpointId": endpoint,
        "crewUrl": f"https://{endpoint}.relay.example.test",
        "name": "Build team Crew",
        "crewType": "TEAM",
    }
    crew = cps._crew_endpoint(raw, cfg.tunnel.relay_viewer_relay_domain)
    assert crew is not None and crew.endpoint_id == endpoint
