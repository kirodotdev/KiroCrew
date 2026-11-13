"""Which CA set HTTPS clients trust on macOS, and how a fallback is reported."""

from __future__ import annotations

import ssl
import sys
from unittest.mock import MagicMock

import pytest

from conftest import forget_env_at_teardown
from kiro_crew import _ssl_compat
from kiro_crew.doctor_checks import install as doctor_install

_SSL_MARKER = "KIROCREW_EXPORTED_SSL_CERT_FILE"


@pytest.fixture(autouse=True)
def _darwin_without_trust(_floor_monkeypatch):
    mp = _floor_monkeypatch
    mp.setattr(sys, "platform", "darwin")
    mp.setattr(_ssl_compat, "_TRUSTSTORE_INJECTED", False)
    mp.setattr(_ssl_compat, "_TRUSTSTORE_FAILURE", None)
    mp.setattr(_ssl_compat, "_TRUSTSTORE_CONTEXT_BUILT", None)
    forget_env_at_teardown(mp, "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", _SSL_MARKER)
    mp.delenv("SSL_CERT_FILE", raising=False)
    mp.delenv("REQUESTS_CA_BUNDLE", raising=False)
    mp.delenv(_SSL_MARKER, raising=False)


def _truststore(context_factory) -> object:
    return type(
        "Truststore",
        (),
        {"SSLContext": staticmethod(context_factory), "inject_into_ssl": MagicMock()},
    )()


def test_the_context_is_built_from_truststore_on_macos(monkeypatch) -> None:
    built: list[int] = []

    def factory(protocol: int) -> ssl.SSLContext:
        built.append(protocol)
        return ssl.SSLContext(protocol)

    monkeypatch.setattr(_ssl_compat, "truststore", _truststore(factory))
    ctx = _ssl_compat.system_trust_context()
    assert built == [ssl.PROTOCOL_TLS_CLIENT]
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True


def test_an_operator_bundle_keeps_its_place_as_the_only_trust_source(monkeypatch, tmp_path) -> None:
    factory = MagicMock()
    monkeypatch.setattr(_ssl_compat, "truststore", _truststore(factory))
    bundle = tmp_path / "ca.pem"
    monkeypatch.setenv("SSL_CERT_FILE", str(bundle))
    sentinel = ssl.create_default_context()
    monkeypatch.setattr(_ssl_compat.ssl, "create_default_context", lambda: sentinel)
    assert _ssl_compat.system_trust_context() is sentinel
    factory.assert_not_called()
    assert _ssl_compat.macos_trust_state() == (_ssl_compat.TRUST_OPERATOR_BUNDLE, str(bundle))


def test_the_runtimes_own_export_is_not_read_as_an_operator_bundle(monkeypatch) -> None:
    monkeypatch.setattr(_ssl_compat, "truststore", _truststore(ssl.SSLContext))
    monkeypatch.setenv("SSL_CERT_FILE", "/install/certifi/cacert.pem")
    monkeypatch.setenv(_SSL_MARKER, "/install/certifi/cacert.pem")
    monkeypatch.setattr(_ssl_compat, "_TRUSTSTORE_INJECTED", True)
    assert _ssl_compat.macos_trust_state() == (_ssl_compat.TRUST_SYSTEM, "")
    assert _ssl_compat.system_trust_note() is None


def test_a_context_that_cannot_be_built_falls_back_to_a_verifying_default(monkeypatch) -> None:
    def factory(protocol: int) -> ssl.SSLContext:
        raise OSError("Security.framework unavailable")

    monkeypatch.setattr(_ssl_compat, "truststore", _truststore(factory))
    ctx = _ssl_compat.system_trust_context()
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname is True
    state, detail = _ssl_compat.macos_trust_state()
    assert state == _ssl_compat.TRUST_FALLBACK
    assert "Security.framework unavailable" in detail


def test_a_failed_injection_is_recorded_and_named_in_the_note(monkeypatch) -> None:
    ts = _truststore(ssl.SSLContext)
    ts.inject_into_ssl = MagicMock(side_effect=RuntimeError("boom"))
    monkeypatch.setattr(_ssl_compat, "truststore", ts)
    assert _ssl_compat._inject_macos_system_trust() is False
    note = _ssl_compat.system_trust_note()
    assert note is not None and "RuntimeError: boom" in note and "kirocrew doctor" in note


def test_a_missing_truststore_is_recorded(monkeypatch) -> None:
    monkeypatch.setattr(_ssl_compat, "truststore", None)
    monkeypatch.setattr(_ssl_compat, "_TRUSTSTORE_IMPORT_ERROR", "No module named 'truststore'")
    _ssl_compat._inject_macos_system_trust()
    state, detail = _ssl_compat.macos_trust_state()
    assert state == _ssl_compat.TRUST_FALLBACK and "No module named" in detail


def test_off_macos_nothing_is_reported(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    assert _ssl_compat.system_trust_note() is None
    issues: list[str] = []
    doctor_install._doctor_tls_trust(issues)
    assert capsys.readouterr().out == "" and issues == []


def test_doctor_reports_the_system_trust_store(monkeypatch, capsys) -> None:
    monkeypatch.setattr(_ssl_compat, "_TRUSTSTORE_INJECTED", True)
    issues: list[str] = []
    doctor_install._doctor_tls_trust(issues)
    assert "✅ macOS system trust store" in capsys.readouterr().out
    assert issues == []


def test_doctor_flags_the_fallback_with_its_reason(monkeypatch, capsys) -> None:
    monkeypatch.setattr(_ssl_compat, "_TRUSTSTORE_FAILURE", "enabling it raised RuntimeError: boom")
    issues: list[str] = []
    doctor_install._doctor_tls_trust(issues)
    out = capsys.readouterr().out
    assert "❌ macOS system trust store not in use" in out
    assert "RuntimeError: boom" in out
    assert issues == ["tls trust"]


def test_a_built_context_clears_the_note_after_a_failed_startup_patch(monkeypatch) -> None:
    ts = _truststore(ssl.SSLContext)
    ts.inject_into_ssl = MagicMock(side_effect=RuntimeError("boom"))
    monkeypatch.setattr(_ssl_compat, "truststore", ts)
    _ssl_compat._inject_macos_system_trust()
    _ssl_compat.system_trust_context()
    assert _ssl_compat.macos_trust_state() == (_ssl_compat.TRUST_SYSTEM, "")
    assert _ssl_compat.system_trust_note() is None


def test_a_failed_build_is_reported_even_after_a_successful_startup_patch(monkeypatch) -> None:
    def factory(protocol: int) -> ssl.SSLContext:
        raise OSError("no Security.framework")

    monkeypatch.setattr(_ssl_compat, "truststore", _truststore(factory))
    monkeypatch.setattr(_ssl_compat, "_TRUSTSTORE_INJECTED", True)
    _ssl_compat.system_trust_context()
    note = _ssl_compat.system_trust_note()
    assert note is not None and "no Security.framework" in note
