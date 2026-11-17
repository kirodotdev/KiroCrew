"""Tests for the by-name vault readers the Secrets usage scan covers."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from kiro_crew.dashboard.handlers import secrets_usage_readers as readers


def _collect(scan, config, stored: set[str]) -> dict[str, list[dict[str, str]]]:
    out: dict[str, list[dict[str, str]]] = {}
    scan(config, frozenset(stored), lambda name, place: out.setdefault(name, []).append(place))
    return out


def _config(**sections: object) -> SimpleNamespace:
    base = {
        "dashboard": SimpleNamespace(bitbucket_server_hosts=[]),
        "decisions": SimpleNamespace(provider=SimpleNamespace(api_key="", endpoint="")),
        "telemetry": SimpleNamespace(traces_enabled=False),
        "session_environments": SimpleNamespace(sprites_profiles=[]),
    }
    base.update(sections)
    return SimpleNamespace(**base)


class _Unreadable:
    def __getattr__(self, name: str) -> object:
        raise OSError("config unreadable")


# ── Bitbucket Server ──


def test_bitbucket_lists_the_name_the_provider_reads_for_each_listed_host() -> None:
    from kiro_crew.dashboard.source_providers.bitbucket_server import (
        GLOBAL_TOKEN_NAME,
        host_token_name,
    )

    one = _config(dashboard=SimpleNamespace(bitbucket_server_hosts=["git.example.com"]))
    place = {"kind": "integration", "name": "Bitbucket Server", "key": "git.example.com"}
    assert _collect(readers.scan_bitbucket_server, one, {GLOBAL_TOKEN_NAME}) == {
        GLOBAL_TOKEN_NAME: [place]
    }
    # A lone host's per-host entry may be unreadable, and the provider then reads
    # the shared one, so both count while both are stored.
    lone = host_token_name("git.example.com")
    assert _collect(readers.scan_bitbucket_server, one, {GLOBAL_TOKEN_NAME, lone}) == {
        lone: [place],
        GLOBAL_TOKEN_NAME: [place],
    }
    two = _config(dashboard=SimpleNamespace(bitbucket_server_hosts=["a.example", "b.example"]))
    per_host = host_token_name("a.example")
    # Two hosts: only the per-host name is read, never the shared one.
    assert set(_collect(readers.scan_bitbucket_server, two, {GLOBAL_TOKEN_NAME, per_host})) == {
        per_host
    }


def test_bitbucket_lists_nothing_with_no_host_or_for_a_lookalike_name() -> None:
    from kiro_crew.dashboard.source_providers.bitbucket_server import GLOBAL_TOKEN_NAME

    assert _collect(readers.scan_bitbucket_server, _config(), {GLOBAL_TOKEN_NAME}) == {}
    one = _config(dashboard=SimpleNamespace(bitbucket_server_hosts=["git.example.com"]))
    assert _collect(readers.scan_bitbucket_server, one, {GLOBAL_TOKEN_NAME + "_X"}) == {}


def test_bitbucket_unreadable_config_raises() -> None:
    with pytest.raises(OSError):
        readers.scan_bitbucket_server(_Unreadable(), frozenset(), lambda n, p: None)


# ── Linear ──


def test_linear_lists_its_key_only_under_its_own_name() -> None:
    from kiro_crew.dashboard.source_providers.linear import API_KEY_NAME

    assert _collect(readers.scan_linear, _config(), {API_KEY_NAME}) == {
        API_KEY_NAME: [{"kind": "integration", "name": "Linear", "key": ""}]
    }
    assert _collect(readers.scan_linear, _config(), {"LINEAR_API_KEY_OLD"}) == {}
    assert _collect(readers.scan_linear, _config(), set()) == {}


# ── Jev decisions ──


def _decisions(api_key: str) -> SimpleNamespace:
    return _config(
        decisions=SimpleNamespace(
            provider=SimpleNamespace(api_key=api_key, endpoint="https://api.example.test")
        )
    )


def test_decisions_lists_the_key_only_while_referenced_and_consented() -> None:
    from kiro_crew.decisions.impl_systemone import VAULT_SECRET_NAME

    ref = f"secret://{VAULT_SECRET_NAME}"
    with patch("kiro_crew.decisions.consent.permits", return_value=True):
        assert _collect(readers.scan_decisions, _decisions(ref), {VAULT_SECRET_NAME}) == {
            VAULT_SECRET_NAME: [{"kind": "integration", "name": "Jev", "key": ""}]
        }
        # A reference to any other name is never read by the provider.
        assert _collect(readers.scan_decisions, _decisions("secret://OTHER"), {"OTHER"}) == {}
    with patch("kiro_crew.decisions.consent.permits", return_value=False):
        assert _collect(readers.scan_decisions, _decisions(ref), {VAULT_SECRET_NAME}) == {}
    # A local model on a loopback endpoint is sent no key, consent or not.
    local = _config(
        decisions=SimpleNamespace(
            provider=SimpleNamespace(api_key=ref, endpoint="http://127.0.0.1:8080/v1/systemone")
        )
    )
    with patch("kiro_crew.decisions.consent.permits", return_value=True):
        assert _collect(readers.scan_decisions, local, {VAULT_SECRET_NAME}) == {}


def test_decisions_reads_the_name_as_the_provider_does() -> None:
    from kiro_crew.decisions.impl_systemone import VAULT_SECRET_NAME

    # Spaces around the name are dropped by ``resolve_api_key``, so it is read.
    spaced = _decisions(f"secret:// {VAULT_SECRET_NAME} ")
    # A SageMaker endpoint signs with AWS credentials and never reads the entry.
    sagemaker = _config(
        decisions=SimpleNamespace(
            provider=SimpleNamespace(
                api_key=f"secret://{VAULT_SECRET_NAME}",
                endpoint="sagemaker://us-east-1/jev-endpoint",
            )
        )
    )
    with patch("kiro_crew.decisions.consent.permits", return_value=True):
        assert _collect(readers.scan_decisions, spaced, {VAULT_SECRET_NAME}) == {
            VAULT_SECRET_NAME: [{"kind": "integration", "name": "Jev", "key": ""}]
        }
        assert _collect(readers.scan_decisions, sagemaker, {VAULT_SECRET_NAME}) == {}


def test_decisions_unreadable_consent_raises() -> None:
    from kiro_crew.decisions.impl_systemone import VAULT_SECRET_NAME

    cfg = _decisions(f"secret://{VAULT_SECRET_NAME}")
    with patch("kiro_crew.decisions.consent.permits", side_effect=OSError("unreadable")):
        with pytest.raises(OSError):
            readers.scan_decisions(cfg, frozenset({VAULT_SECRET_NAME}), lambda n, p: None)


# ── OpenTelemetry ──


def _destination(headers: tuple[tuple[str, str, str], ...]):
    from kiro_crew.tracing import otlp

    return otlp.Destination("https://otel.example.test/v1/traces", "config", headers)


def test_otlp_lists_vault_headers_of_the_resolved_destination_only() -> None:
    from kiro_crew.tracing import otlp

    dest = _destination(
        (("x-api-key", otlp.FROM_VAULT, "OTEL_KEY"), ("x-env", otlp.FROM_ENV, "OTEL_KEY_ENV"))
    )
    with patch.object(otlp, "resolve", return_value=dest):
        assert _collect(readers.scan_otlp, _config(), {"OTEL_KEY", "OTEL_KEY_ENV"}) == {
            "OTEL_KEY": [{"kind": "integration", "name": "OpenTelemetry", "key": "x-api-key"}]
        }
    with patch.object(otlp, "resolve", return_value=None):
        assert _collect(readers.scan_otlp, _config(), {"OTEL_KEY"}) == {}


def test_otlp_unreadable_config_raises() -> None:
    with pytest.raises(OSError):
        readers.scan_otlp(_Unreadable(), frozenset(), lambda n, p: None)


# ── Sprites ──


def test_sprites_lists_each_configured_profile_token() -> None:
    cfg = _config(
        session_environments=SimpleNamespace(
            sprites_profiles=[{"id": "p1", "name": "Build box", "token_secret": "SPRITES_TOKEN"}]
        )
    )
    assert _collect(readers.scan_sprites, cfg, {"SPRITES_TOKEN", "SPRITES_TOKEN_2"}) == {
        "SPRITES_TOKEN": [{"kind": "sprites_profile", "name": "Build box", "key": ""}]
    }
    assert _collect(readers.scan_sprites, _config(), {"SPRITES_TOKEN"}) == {}


def test_sprites_unreadable_config_raises() -> None:
    with pytest.raises(OSError):
        readers.scan_sprites(_Unreadable(), frozenset(), lambda n, p: None)


def test_a_saved_sprites_machine_keeps_its_token_listed_after_its_profile_is_gone(
    tmp_path: Path,
) -> None:
    from kiro_crew.session_environment import SessionEnvironmentUnavailable
    from kiro_crew.session_environments.persistent_vm import (
        SessionResource,
        SessionResourceRegistry,
    )
    from kiro_crew.sprites_session.launch import SpritesSessionEnvironments as cls

    def scan(stored: set[str]) -> dict[str, list[dict[str, str]]]:
        found: dict[str, list[dict[str, str]]] = {}
        readers.scan_sprites_resources(
            tmp_path, frozenset(stored), lambda n, p: found.setdefault(n, []).append(p)
        )
        return found

    # No registry yet: nothing saved, nothing listed.
    assert scan({"OLD_TOKEN"}) == {}
    registry = SessionResourceRegistry(tmp_path / cls.registry_file, cls.label)
    registry.put(
        SessionResource(
            session_key="dashboard:a", profile_id="p-old", name="kc-a", token_secret="OLD_TOKEN"
        )
    )
    assert scan({"OLD_TOKEN", "OTHER"}) == {
        "OLD_TOKEN": [{"kind": "sprites_profile", "name": "p-old", "key": "kc-a"}]
    }
    (tmp_path / cls.registry_file).write_text("{not json", encoding="utf-8")
    with pytest.raises(SessionEnvironmentUnavailable):
        scan({"OLD_TOKEN"})


# ── Approved APIs (`kirocrew secrets authorize`) ──


@pytest.fixture()
def signing(monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew.secrets_mediation import provenance

    monkeypatch.setattr(provenance, "_token_root", lambda: b"T" * 32)


def _write_policy(config_dir: Path, authorizations: dict) -> None:
    from kiro_crew.secrets_mediation.policy import policy_path
    from kiro_crew.secrets_mediation.provenance import SIGNATURE_FIELD, sign_secret_authorizations

    path = policy_path(config_dir)
    path.parent.mkdir(mode=0o700, exist_ok=True)
    payload = {
        "version": 1,
        "authorizations": authorizations,
        SIGNATURE_FIELD: sign_secret_authorizations(authorizations),
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def _approved(config_dir: Path, stored: set[str]) -> dict:
    out: dict[str, list[dict[str, str]]] = {}
    readers.scan_approved_apis(
        config_dir, frozenset(stored), lambda name, place: out.setdefault(name, []).append(place)
    )
    return out


def test_approved_api_lists_an_authorized_entry_with_its_origin(
    tmp_path: Path, signing: None
) -> None:
    entry = {"origin": "https://api.weather.example", "placement": {"type": "bearer"}}
    _write_policy(tmp_path, {"WEATHER_API_KEY": entry})
    assert _approved(tmp_path, {"WEATHER_API_KEY", "WEATHER_API_KEY_OLD"}) == {
        "WEATHER_API_KEY": [
            {"kind": "approved_api", "name": "https://api.weather.example", "key": ""}
        ]
    }


def test_approved_api_lists_nothing_without_a_policy_or_an_owner_signature(
    tmp_path: Path, signing: None
) -> None:
    assert _approved(tmp_path, {"WEATHER_API_KEY"}) == {}
    from kiro_crew.secrets_mediation.policy import policy_path

    path = policy_path(tmp_path)
    path.parent.mkdir(mode=0o700, exist_ok=True)
    entry = {"origin": "https://api.weather.example", "placement": {"type": "bearer"}}
    path.write_text(json.dumps({"version": 1, "authorizations": {"WEATHER_API_KEY": entry}}))
    assert _approved(tmp_path, {"WEATHER_API_KEY"}) == {}


def test_approved_api_unreadable_policy_raises(tmp_path: Path) -> None:
    from kiro_crew.secrets_mediation.policy import policy_path

    path = policy_path(tmp_path)
    path.parent.mkdir(mode=0o700, exist_ok=True)
    path.mkdir()  # a directory where the file belongs cannot be opened
    with pytest.raises(OSError):
        readers.scan_approved_apis(tmp_path, frozenset({"X"}), lambda n, p: None)
