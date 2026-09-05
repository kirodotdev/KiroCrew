"""Tests for _ssl_compat SSL certificate bootstrap."""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, call, patch

import pytest

from kiro_crew import _ssl_compat
from kiro_crew._ssl_compat import _CA_CANDIDATES, _ensure_ssl_certs
from kiro_crew.atomic_write import atomic_write


@pytest.fixture(autouse=True)
def _reset_ssl_bootstrap(monkeypatch):
    """Keep tests independent and file-bootstrap cases platform-neutral."""
    original_platform = sys.platform
    monkeypatch.setattr(_ssl_compat, "_TRUSTSTORE_INJECTED", False)
    monkeypatch.setattr(_ssl_compat, "_WINDOWS_CA_REFRESHED", False)
    monkeypatch.delenv(_ssl_compat._WINDOWS_CA_BUNDLE_ENV, raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    yield
    # Restore before broader conftest teardowns import platform-sensitive
    # dependencies such as numpy. The monkeypatch fixture itself unwinds later.
    monkeypatch.setattr(sys, "platform", original_platform)


def _bundle_ders(payload: bytes) -> list[bytes]:
    """Decode every PEM certificate in a generated bundle back to DER."""
    text = payload.decode("ascii")
    begin = "-----BEGIN CERTIFICATE-----"
    end = "-----END CERTIFICATE-----"
    ders: list[bytes] = []
    cursor = 0
    while True:
        start = text.find(begin, cursor)
        if start < 0:
            return ders
        finish = text.find(end, start)
        assert finish >= 0, "generated bundle contains a truncated PEM certificate"
        finish += len(end)
        ders.append(_ssl_compat.ssl.PEM_cert_to_DER_cert(text[start:finish]))
        cursor = finish


class TestEnsureSslCerts:
    """Tests for _ensure_ssl_certs()."""

    def test_noop_when_ssl_cert_file_already_set(self, monkeypatch):
        """Should return immediately if SSL_CERT_FILE is already set."""
        monkeypatch.setenv("SSL_CERT_FILE", "/custom/ca.pem")
        monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)

        _ensure_ssl_certs()

        import os

        assert os.environ["SSL_CERT_FILE"] == "/custom/ca.pem"
        assert os.environ.get("REQUESTS_CA_BUNDLE") is None

    def test_windows_operator_ssl_cert_file_is_never_treated_as_managed(
        self, monkeypatch, tmp_path
    ):
        """An operator override wins even when it happens to run on Windows."""
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setenv("SSL_CERT_FILE", "/operator/custom-ca.pem")
        monkeypatch.delenv(_ssl_compat._WINDOWS_CA_BUNDLE_ENV, raising=False)

        with (
            patch("kiro_crew._ssl_compat.config_paths.config_dir") as config_dir,
            patch("kiro_crew._ssl_compat._build_windows_ca_bundle") as build,
        ):
            _ensure_ssl_certs()

        config_dir.assert_not_called()
        build.assert_not_called()
        assert os.environ["SSL_CERT_FILE"] == "/operator/custom-ca.pem"

    def test_noop_when_default_cafile_exists(self, monkeypatch, tmp_path):
        """Should return if ssl.get_default_verify_paths().cafile exists."""
        monkeypatch.delenv("SSL_CERT_FILE", raising=False)
        monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)

        ca_file = tmp_path / "system-ca.pem"
        ca_file.write_text("fake cert bundle")

        mock_paths = type("P", (), {"cafile": str(ca_file), "capath": None})()
        with patch("ssl.get_default_verify_paths", return_value=mock_paths):
            _ensure_ssl_certs()

        import os

        assert os.environ.get("SSL_CERT_FILE") is None

    def test_sets_env_from_first_existing_candidate(self, monkeypatch, tmp_path):
        """Should set SSL_CERT_FILE and REQUESTS_CA_BUNDLE from the first candidate found."""
        monkeypatch.delenv("SSL_CERT_FILE", raising=False)
        monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)

        # Simulate: cafile is None (no default bundle)
        mock_paths = type("P", (), {"cafile": None, "capath": None})()

        # Make the second candidate exist
        fake_bundle = tmp_path / "ca-bundle.crt"
        fake_bundle.write_text("fake cert bundle")

        candidates = (
            "/nonexistent/cert.pem",
            str(fake_bundle),
            "/also/nonexistent.crt",
        )

        with (
            patch("ssl.get_default_verify_paths", return_value=mock_paths),
            patch("kiro_crew._ssl_compat._CA_CANDIDATES", candidates),
        ):
            _ensure_ssl_certs()

        import os

        assert os.environ["SSL_CERT_FILE"] == str(fake_bundle)
        assert os.environ["REQUESTS_CA_BUNDLE"] == str(fake_bundle)

    def test_does_not_overwrite_existing_requests_ca_bundle(self, monkeypatch, tmp_path):
        """REQUESTS_CA_BUNDLE should not be overwritten if already set."""
        monkeypatch.delenv("SSL_CERT_FILE", raising=False)
        monkeypatch.setenv("REQUESTS_CA_BUNDLE", "/existing/bundle.crt")

        mock_paths = type("P", (), {"cafile": None, "capath": None})()

        fake_bundle = tmp_path / "cert.pem"
        fake_bundle.write_text("fake cert bundle")
        candidates = (str(fake_bundle),)

        with (
            patch("ssl.get_default_verify_paths", return_value=mock_paths),
            patch("kiro_crew._ssl_compat._CA_CANDIDATES", candidates),
        ):
            _ensure_ssl_certs()

        import os

        assert os.environ["SSL_CERT_FILE"] == str(fake_bundle)
        assert os.environ["REQUESTS_CA_BUNDLE"] == "/existing/bundle.crt"

    def test_no_env_set_when_no_candidate_exists(self, monkeypatch):
        """Should leave env vars unset if no candidate file exists and certifi is unavailable."""
        monkeypatch.delenv("SSL_CERT_FILE", raising=False)
        monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)

        mock_paths = type("P", (), {"cafile": None, "capath": None})()
        candidates = ("/nonexistent/a.pem", "/nonexistent/b.crt")

        with (
            patch("ssl.get_default_verify_paths", return_value=mock_paths),
            patch("kiro_crew._ssl_compat._CA_CANDIDATES", candidates),
            patch.object(_ssl_compat, "certifi", None),
        ):
            _ensure_ssl_certs()

        import os

        assert os.environ.get("SSL_CERT_FILE") is None
        assert os.environ.get("REQUESTS_CA_BUNDLE") is None

    def test_falls_back_to_certifi_when_no_system_path_exists(self, monkeypatch, tmp_path):
        """macOS has none of the Linux system paths — should fall back to certifi's bundle."""
        monkeypatch.delenv("SSL_CERT_FILE", raising=False)
        monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)

        mock_paths = type("P", (), {"cafile": None, "capath": None})()
        candidates = ("/nonexistent/a.pem", "/nonexistent/b.crt")

        fake_certifi_bundle = tmp_path / "certifi-cacert.pem"
        fake_certifi_bundle.write_text("fake certifi bundle")
        mock_certifi = type("M", (), {"where": staticmethod(lambda: str(fake_certifi_bundle))})()

        with (
            patch("ssl.get_default_verify_paths", return_value=mock_paths),
            patch("kiro_crew._ssl_compat._CA_CANDIDATES", candidates),
            patch.object(_ssl_compat, "certifi", mock_certifi),
        ):
            _ensure_ssl_certs()

        import os

        assert os.environ["SSL_CERT_FILE"] == str(fake_certifi_bundle)
        assert os.environ["REQUESTS_CA_BUNDLE"] == str(fake_certifi_bundle)

    def test_windows_bundle_sets_child_env_without_overwriting_requests(
        self, monkeypatch, tmp_path
    ):
        """Windows exports system trust while preserving an operator requests bundle."""
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.delenv("SSL_CERT_FILE", raising=False)
        monkeypatch.setenv("REQUESTS_CA_BUNDLE", "/operator/requests-ca.pem")
        bundle_path = tmp_path / _ssl_compat._WINDOWS_CA_BUNDLE_RELATIVE_PATH

        with (
            patch("kiro_crew.config.paths.config_dir", return_value=tmp_path),
            patch("kiro_crew._ssl_compat._build_windows_ca_bundle", return_value=True) as build,
            patch("ssl.get_default_verify_paths") as defaults,
        ):
            _ensure_ssl_certs()
            # ``python -m kiro_crew`` reaches the bootstrap through both
            # __main__ and cli in one interpreter; only the first call refreshes.
            _ensure_ssl_certs()

        import os

        build.assert_called_once_with(bundle_path)
        defaults.assert_not_called()
        assert os.environ["SSL_CERT_FILE"] == str(bundle_path)
        assert os.environ["REQUESTS_CA_BUNDLE"] == "/operator/requests-ca.pem"

    def test_windows_bundle_failure_keeps_verification_and_falls_back_to_certifi(
        self, monkeypatch, tmp_path
    ):
        """A store failure keeps verified HTTPS, without claiming Windows parity."""
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.delenv("SSL_CERT_FILE", raising=False)
        monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
        mock_paths = type("P", (), {"cafile": None, "capath": None})()
        fake_certifi_bundle = tmp_path / "certifi-cacert.pem"
        fake_certifi_bundle.write_text("fake certifi bundle")
        mock_certifi = type("M", (), {"where": staticmethod(lambda: str(fake_certifi_bundle))})()

        with (
            patch("kiro_crew.config.paths.config_dir", return_value=tmp_path),
            patch("kiro_crew._ssl_compat._build_windows_ca_bundle", return_value=False),
            patch("ssl.get_default_verify_paths", return_value=mock_paths),
            patch("kiro_crew._ssl_compat._CA_CANDIDATES", ("/nonexistent/a.pem",)),
            patch.object(_ssl_compat, "certifi", mock_certifi),
        ):
            _ensure_ssl_certs()

        import os

        assert os.environ["SSL_CERT_FILE"] == str(fake_certifi_bundle)
        assert os.environ["REQUESTS_CA_BUNDLE"] == str(fake_certifi_bundle)

    def test_managed_windows_bundle_is_refreshed_after_reexec(self, monkeypatch, tmp_path):
        """An inherited managed path is refreshed, not mistaken for an override."""
        monkeypatch.setattr(sys, "platform", "win32")
        bundle_path = tmp_path / _ssl_compat._WINDOWS_CA_BUNDLE_RELATIVE_PATH
        monkeypatch.setenv("SSL_CERT_FILE", str(bundle_path))
        monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(bundle_path))
        monkeypatch.setenv(_ssl_compat._WINDOWS_CA_BUNDLE_ENV, str(bundle_path))

        def build(path):
            assert path == bundle_path
            assert "SSL_CERT_FILE" not in os.environ
            assert "REQUESTS_CA_BUNDLE" not in os.environ
            assert _ssl_compat._WINDOWS_CA_BUNDLE_ENV not in os.environ
            return True

        with (
            patch("kiro_crew._ssl_compat.config_paths.config_dir", return_value=tmp_path),
            patch("kiro_crew._ssl_compat._build_windows_ca_bundle", side_effect=build) as refresh,
            patch("ssl.get_default_verify_paths") as defaults,
        ):
            _ensure_ssl_certs()

        refresh.assert_called_once_with(bundle_path)
        defaults.assert_not_called()
        assert os.environ["SSL_CERT_FILE"] == str(bundle_path)
        assert os.environ["REQUESTS_CA_BUNDLE"] == str(bundle_path)
        assert os.environ[_ssl_compat._WINDOWS_CA_BUNDLE_ENV] == str(bundle_path)

    def test_managed_refresh_failure_preserves_lkg_and_uses_verified_fallback(
        self, monkeypatch, tmp_path
    ):
        """A failed refresh neither uses nor damages the last-known-good mirror."""
        monkeypatch.setattr(sys, "platform", "win32")
        bundle_path = tmp_path / _ssl_compat._WINDOWS_CA_BUNDLE_RELATIVE_PATH
        bundle_path.parent.mkdir()
        bundle_path.write_bytes(b"last-known-good")
        monkeypatch.setenv("SSL_CERT_FILE", str(bundle_path))
        monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(bundle_path))
        monkeypatch.setenv(_ssl_compat._WINDOWS_CA_BUNDLE_ENV, str(bundle_path))
        fake_certifi_bundle = tmp_path / "certifi-cacert.pem"
        fake_certifi_bundle.write_text("verified certifi bundle")
        mock_certifi = type("M", (), {"where": staticmethod(lambda: str(fake_certifi_bundle))})()
        mock_paths = type("P", (), {"cafile": None, "capath": None})()

        with (
            patch("kiro_crew._ssl_compat.config_paths.config_dir", return_value=tmp_path),
            patch("kiro_crew._ssl_compat._build_windows_ca_bundle", return_value=False),
            patch("ssl.get_default_verify_paths", return_value=mock_paths),
            patch("kiro_crew._ssl_compat._CA_CANDIDATES", ("/nonexistent/a.pem",)),
            patch.object(_ssl_compat, "certifi", mock_certifi),
        ):
            _ensure_ssl_certs()

        assert bundle_path.read_bytes() == b"last-known-good"
        assert os.environ["SSL_CERT_FILE"] == str(fake_certifi_bundle)
        assert os.environ["REQUESTS_CA_BUNDLE"] == str(fake_certifi_bundle)
        assert _ssl_compat._WINDOWS_CA_BUNDLE_ENV not in os.environ

    @pytest.mark.parametrize("error", [OSError("denied"), RuntimeError("unsafe home")])
    def test_windows_config_dir_failure_falls_back_to_certifi(
        self, monkeypatch, tmp_path, caplog, error
    ):
        """Home resolution failures must not make CLI import or --help crash."""
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.delenv("SSL_CERT_FILE", raising=False)
        monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
        mock_paths = type("P", (), {"cafile": None, "capath": None})()
        fake_certifi_bundle = tmp_path / "certifi-cacert.pem"
        fake_certifi_bundle.write_text("fake certifi bundle")
        mock_certifi = type("M", (), {"where": staticmethod(lambda: str(fake_certifi_bundle))})()

        with (
            patch("kiro_crew._ssl_compat.config_paths.config_dir", side_effect=error),
            patch("kiro_crew._ssl_compat._build_windows_ca_bundle") as build,
            patch("ssl.get_default_verify_paths", return_value=mock_paths),
            patch("kiro_crew._ssl_compat._CA_CANDIDATES", ("/nonexistent/a.pem",)),
            patch.object(_ssl_compat, "certifi", mock_certifi),
            caplog.at_level("WARNING", logger="kiro_crew._ssl_compat"),
        ):
            _ensure_ssl_certs()

        build.assert_not_called()
        assert os.environ["SSL_CERT_FILE"] == str(fake_certifi_bundle)
        assert os.environ["REQUESTS_CA_BUNDLE"] == str(fake_certifi_bundle)
        assert "falling back" in caplog.text

    def test_cafile_missing_on_disk_falls_through(self, monkeypatch, tmp_path):
        """If cafile is set but the file doesn't exist, should fall through to candidates."""
        monkeypatch.delenv("SSL_CERT_FILE", raising=False)
        monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)

        # cafile points to a nonexistent path
        mock_paths = type("P", (), {"cafile": "/ghost/cert.pem", "capath": None})()

        fake_bundle = tmp_path / "ca-bundle.crt"
        fake_bundle.write_text("fake cert bundle")
        candidates = (str(fake_bundle),)

        with (
            patch("ssl.get_default_verify_paths", return_value=mock_paths),
            patch("kiro_crew._ssl_compat._CA_CANDIDATES", candidates),
        ):
            _ensure_ssl_certs()

        import os

        assert os.environ["SSL_CERT_FILE"] == str(fake_bundle)

    def test_candidates_match_expected_paths(self):
        """Verify the candidate list covers AL2 and Debian/Ubuntu paths."""
        assert "/etc/pki/tls/cert.pem" in _CA_CANDIDATES
        assert "/etc/pki/tls/certs/ca-bundle.crt" in _CA_CANDIDATES
        assert "/etc/ssl/certs/ca-certificates.crt" in _CA_CANDIDATES

    def test_cli_invokes_ensure_ssl_certs(self):
        """Reloading cli.py must trigger _ensure_ssl_certs()."""
        import importlib
        from unittest.mock import MagicMock
        from unittest.mock import patch as _patch

        mock_fn = MagicMock()
        with _patch("kiro_crew._ssl_compat._ensure_ssl_certs", mock_fn):
            import kiro_crew.cli

            importlib.reload(kiro_crew.cli)
        mock_fn.assert_called()

    def test_macos_injects_system_trust(self, monkeypatch, tmp_path):
        """macOS should delegate TLS validation to Security.framework."""
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.delenv("SSL_CERT_FILE", raising=False)
        inject = MagicMock()
        mock_truststore = type("Truststore", (), {"inject_into_ssl": inject})()
        ca_file = tmp_path / "system-ca.pem"
        ca_file.write_text("fake cert bundle")
        mock_paths = type("P", (), {"cafile": str(ca_file), "capath": None})()

        with (
            patch.object(_ssl_compat, "truststore", mock_truststore),
            patch("ssl.get_default_verify_paths", return_value=mock_paths),
        ):
            _ensure_ssl_certs()

        inject.assert_called_once_with()
        assert _ssl_compat._TRUSTSTORE_INJECTED is True

    def test_macos_explicit_bundle_wins(self, monkeypatch):
        """An operator-supplied SSL_CERT_FILE must bypass system injection."""
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setenv("SSL_CERT_FILE", "/operator/ca.pem")
        inject = MagicMock()
        mock_truststore = type("Truststore", (), {"inject_into_ssl": inject})()

        with patch.object(_ssl_compat, "truststore", mock_truststore):
            _ensure_ssl_certs()

        inject.assert_not_called()
        assert _ssl_compat._TRUSTSTORE_INJECTED is False

    def test_macos_injection_is_idempotent(self, monkeypatch, tmp_path):
        """Both application entry points may call the prelude in one process."""
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.delenv("SSL_CERT_FILE", raising=False)
        inject = MagicMock()
        mock_truststore = type("Truststore", (), {"inject_into_ssl": inject})()
        ca_file = tmp_path / "system-ca.pem"
        ca_file.write_text("fake cert bundle")
        mock_paths = type("P", (), {"cafile": str(ca_file), "capath": None})()

        with (
            patch.object(_ssl_compat, "truststore", mock_truststore),
            patch("ssl.get_default_verify_paths", return_value=mock_paths),
        ):
            _ensure_ssl_certs()
            _ensure_ssl_certs()

        inject.assert_called_once_with()

    def test_macos_injection_still_exports_child_env(self, monkeypatch, tmp_path):
        """Injection covers this process only; children still need the env vars.

        MCP subprocesses (kiro-cli, Node servers) inherit ``os.environ`` and
        cannot inherit a process-local monkey-patch, so a successful macOS
        injection must not short-circuit the file-based export they rely on.
        """
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.delenv("SSL_CERT_FILE", raising=False)
        monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
        inject = MagicMock()
        mock_truststore = type("Truststore", (), {"inject_into_ssl": inject})()

        mock_paths = type("P", (), {"cafile": None, "capath": None})()
        fake_certifi_bundle = tmp_path / "certifi-cacert.pem"
        fake_certifi_bundle.write_text("fake certifi bundle")
        mock_certifi = type("M", (), {"where": staticmethod(lambda: str(fake_certifi_bundle))})()

        with (
            patch.object(_ssl_compat, "truststore", mock_truststore),
            patch.object(_ssl_compat, "certifi", mock_certifi),
            patch("ssl.get_default_verify_paths", return_value=mock_paths),
            patch("kiro_crew._ssl_compat._CA_CANDIDATES", ("/nonexistent/a.pem",)),
        ):
            _ensure_ssl_certs()

        import os

        inject.assert_called_once_with()
        assert os.environ["SSL_CERT_FILE"] == str(fake_certifi_bundle)
        assert os.environ["REQUESTS_CA_BUNDLE"] == str(fake_certifi_bundle)

    def test_macos_injection_failure_falls_back(self, monkeypatch, tmp_path, caplog):
        """A system-trust failure must not prevent the prior CA bootstrap."""
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.delenv("SSL_CERT_FILE", raising=False)
        ca_file = tmp_path / "fallback-ca.pem"
        ca_file.write_text("fake cert bundle")
        mock_paths = type("P", (), {"cafile": str(ca_file), "capath": None})()
        inject = MagicMock(side_effect=RuntimeError("unavailable"))
        mock_truststore = type("Truststore", (), {"inject_into_ssl": inject})()

        with (
            patch.object(_ssl_compat, "truststore", mock_truststore),
            patch("ssl.get_default_verify_paths", return_value=mock_paths),
            caplog.at_level("WARNING", logger="kiro_crew._ssl_compat"),
        ):
            _ensure_ssl_certs()

        inject.assert_called_once_with()
        assert _ssl_compat._TRUSTSTORE_INJECTED is False
        assert "falling back" in caplog.text

        import os

        assert os.environ.get("SSL_CERT_FILE") is None

    def test_macos_missing_truststore_falls_back(self, monkeypatch, tmp_path, caplog):
        """An absent truststore package degrades to file discovery, not a crash."""
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.delenv("SSL_CERT_FILE", raising=False)
        ca_file = tmp_path / "fallback-ca.pem"
        ca_file.write_text("fake cert bundle")
        mock_paths = type("P", (), {"cafile": str(ca_file), "capath": None})()

        with (
            patch.object(_ssl_compat, "truststore", None),
            patch("ssl.get_default_verify_paths", return_value=mock_paths),
            caplog.at_level("WARNING", logger="kiro_crew._ssl_compat"),
        ):
            _ensure_ssl_certs()

        assert _ssl_compat._TRUSTSTORE_INJECTED is False
        assert "falling back" in caplog.text

    def test_gatewayd_invokes_ensure_ssl_certs(self):
        """The separate gateway process must install process-local trust."""
        import importlib

        mock_fn = MagicMock()
        with patch("kiro_crew._ssl_compat._ensure_ssl_certs", mock_fn):
            import kiro_crew.mcp_gateway.gatewayd

            importlib.reload(kiro_crew.mcp_gateway.gatewayd)
        mock_fn.assert_called()

    def test_context_trust_uses_openssl_ca_count(self, monkeypatch):
        """Regular OpenSSL contexts retain the concrete CA-count check."""
        context = MagicMock()
        context.cert_store_stats.return_value = {"x509_ca": 1}

        assert _ssl_compat._ssl_context_has_ca_trust(context) is True
        context.cert_store_stats.assert_called_once_with()

    def test_context_trust_accepts_injected_dynamic_store(self, monkeypatch):
        """Security.framework trust is valid even though it cannot list CAs."""
        monkeypatch.setattr(_ssl_compat, "_TRUSTSTORE_INJECTED", True)
        context = MagicMock()
        context.cert_store_stats.side_effect = NotImplementedError

        assert _ssl_compat._ssl_context_has_ca_trust(context) is True


class TestWindowsCaBundle:
    """Tests for exporting Windows trust without widening its meaning."""

    def test_successful_store_read_replaces_a_stale_bundle_even_without_extra_roots(
        self, monkeypatch, tmp_path
    ):
        """A stable-path mirror cannot retain a ROOT entry removed from Windows."""
        certifi_bundle = tmp_path / "certifi.pem"
        certifi_bundle.write_bytes(b"source certifi bytes are parsed, not copied\n")
        destination = tmp_path / "ca-bundle.pem"
        destination.write_bytes(b"stale corporate root")
        mozilla_der = b"mozilla-root"
        mock_certifi = type("M", (), {"where": staticmethod(lambda: str(certifi_bundle))})()

        with (
            patch.object(_ssl_compat, "certifi", mock_certifi),
            patch(
                "kiro_crew._ssl_compat._windows_ca_certs",
                return_value=([mozilla_der], [], 0),
            ),
            patch("kiro_crew._ssl_compat.atomic_write", wraps=atomic_write) as write,
        ):
            assert _ssl_compat._build_windows_ca_bundle(destination) is True

        payload = destination.read_bytes()
        write.assert_called_once_with(destination, payload, restrict_to_owner=True)
        assert _bundle_ders(payload) == [mozilla_der]
        assert b"stale corporate root" not in payload

    def test_exports_only_https_roots_absent_from_certifi(self, monkeypatch, tmp_path):
        mozilla_der = b"mozilla-root"
        corporate_der = b"corporate-root"
        server_only_der = b"server-only-root"
        code_signing_der = b"code-signing-root"
        context = MagicMock()
        context.get_ca_certs.return_value = [mozilla_der]
        enum_certificates = MagicMock(
            side_effect=[
                [],
                [
                    (mozilla_der, "x509_asn", True),
                    (corporate_der, "x509_asn", True),
                    (
                        server_only_der,
                        "x509_asn",
                        {_ssl_compat._WINDOWS_SERVER_AUTH_OID},
                    ),
                    (code_signing_der, "x509_asn", {"1.3.6.1.5.5.7.3.3"}),
                    (b"certificate-list", "pkcs_7_asn", True),
                    (corporate_der, "x509_asn", True),
                ],
            ]
        )
        monkeypatch.setattr(_ssl_compat.ssl, "enum_certificates", enum_certificates, raising=False)
        certifi_path = tmp_path / "certifi.pem"

        with patch("ssl.create_default_context", return_value=context) as create_context:
            certifi_roots, extra_roots, removed = _ssl_compat._windows_ca_certs(certifi_path)

        create_context.assert_called_once_with(cafile=str(certifi_path))
        assert enum_certificates.call_args_list == [call("Disallowed"), call("ROOT")]
        assert certifi_roots == [mozilla_der]
        assert extra_roots == [corporate_der, server_only_der]
        assert removed == 0

    def test_explicitly_distrusted_root_is_not_exported(self, monkeypatch, tmp_path):
        trusted_der = b"trusted-root"
        distrusted_der = b"distrusted-root"
        context = MagicMock()
        context.get_ca_certs.return_value = []
        enum_certificates = MagicMock(
            side_effect=[
                [(distrusted_der, "x509_asn", True)],
                [
                    (trusted_der, "x509_asn", True),
                    (distrusted_der, "x509_asn", True),
                ],
            ]
        )
        monkeypatch.setattr(_ssl_compat.ssl, "enum_certificates", enum_certificates, raising=False)

        with patch("ssl.create_default_context", return_value=context):
            certifi_roots, extra_roots, removed = _ssl_compat._windows_ca_certs(
                tmp_path / "certifi.pem"
            )

        assert certifi_roots == []
        assert extra_roots == [trusted_der]
        assert removed == 0

    def test_disallowed_certifi_root_is_filtered_without_any_extra_root(
        self, monkeypatch, tmp_path
    ):
        """A Windows deny must rewrite certifi even when ROOT adds nothing.

        This is the blocking-review shape: publishing was previously gated on
        an extra ROOT certificate, so a certifi-only deny silently retained the
        distrusted anchor. Assertions decode the generated PEM back to DER;
        textual PEM deletion cannot satisfy the identity contract by accident.
        """
        allowed_der = b"allowed-certifi-root"
        distrusted_der = b"distrusted-certifi-root"
        context = MagicMock()
        # The duplicate also pins DER-level de-duplication in the certifi half.
        context.get_ca_certs.return_value = [
            allowed_der,
            distrusted_der,
            allowed_der,
        ]
        enum_certificates = MagicMock(
            side_effect=[
                [(distrusted_der, "x509_asn", True)],
                # The same identity appearing in ROOT must not re-add it.
                [(distrusted_der, "x509_asn", True)],
            ]
        )
        monkeypatch.setattr(_ssl_compat.ssl, "enum_certificates", enum_certificates, raising=False)
        certifi_bundle = tmp_path / "certifi.pem"
        certifi_bundle.write_text("source bytes are not copied", encoding="ascii")
        destination = tmp_path / "ca-bundle.pem"
        mock_certifi = type("M", (), {"where": staticmethod(lambda: str(certifi_bundle))})()

        with (
            patch.object(_ssl_compat, "certifi", mock_certifi),
            patch("ssl.create_default_context", return_value=context),
            patch("kiro_crew._ssl_compat.atomic_write", wraps=atomic_write) as write,
        ):
            assert _ssl_compat._build_windows_ca_bundle(destination) is True

        payload = destination.read_bytes()
        write.assert_called_once_with(destination, payload, restrict_to_owner=True)
        assert _bundle_ders(payload) == [allowed_der]
        assert distrusted_der not in _bundle_ders(payload)
        assert enum_certificates.call_args_list == [call("Disallowed"), call("ROOT")]

    def test_unavailable_disallowed_store_aborts_bundle_publish(
        self, monkeypatch, tmp_path, caplog
    ):
        """An unknown deny set aborts the mirror and declares fallback degradation."""
        certifi_bundle = tmp_path / "certifi.pem"
        certifi_bundle.write_bytes(b"mozilla roots\n")
        destination = tmp_path / "ca-bundle.pem"
        mock_certifi = type("M", (), {"where": staticmethod(lambda: str(certifi_bundle))})()
        enum_certificates = MagicMock(side_effect=OSError("store unavailable"))
        monkeypatch.setattr(_ssl_compat.ssl, "enum_certificates", enum_certificates, raising=False)

        with (
            patch.object(_ssl_compat, "certifi", mock_certifi),
            patch("kiro_crew._ssl_compat.atomic_write") as write,
            caplog.at_level("WARNING", logger="kiro_crew._ssl_compat"),
        ):
            assert _ssl_compat._build_windows_ca_bundle(destination) is False

        enum_certificates.assert_called_once_with("Disallowed")
        write.assert_not_called()
        assert not destination.exists()
        assert "falling back" in caplog.text
        assert "may not reflect Windows Disallowed entries" in caplog.text

    def test_missing_module_scope_certifi_skips_bundle_publish(self, monkeypatch, tmp_path):
        """The guarded module import preserves minimal/source-only installs."""
        enum_certificates = MagicMock()
        monkeypatch.setattr(_ssl_compat.ssl, "enum_certificates", enum_certificates, raising=False)

        with (
            patch.object(_ssl_compat, "certifi", None),
            patch("kiro_crew._ssl_compat.atomic_write") as write,
        ):
            assert _ssl_compat._build_windows_ca_bundle(tmp_path / "ca-bundle.pem") is False

        enum_certificates.assert_not_called()
        write.assert_not_called()

    def test_generated_bundle_is_inside_the_sensitive_trust_root(self, monkeypatch, tmp_path):
        """The generated trust input cannot be read or written by an agent."""
        from kiro_crew import security

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "crew-home"))
        bundle_path = str(
            _ssl_compat.config_paths.config_dir() / _ssl_compat._WINDOWS_CA_BUNDLE_RELATIVE_PATH
        )

        assert security.is_sensitive_write_path(bundle_path) is True
        assert security.is_sensitive_path(bundle_path) is True
        for command in (
            "echo rogue > ~/.kiro/crew/trust/ca-bundle.pem",
            "echo rogue > $HOME/.kiro/crew/trust/ca-bundle.pem",
            "tee ~/.kirocrew/trust/ca-bundle.pem",
            "cp /tmp/rogue.pem ~/.kiro/crew/trust/ca-bundle.pem",
            "cd ~/.kiro/crew && type rogue.pem >> trust/ca-bundle.pem",
        ):
            assert security.is_sensitive_bash_command(command) is not None, command

        from kiro_crew.hooks import HookManager, HooksConfig

        hooks = HookManager(HooksConfig())
        edit = hooks.on_tool_call(
            "Editing generated trust bundle",
            tool_kind="edit",
            raw_params={"path": bundle_path},
        )
        read = hooks.on_tool_call(
            "Reading generated trust bundle",
            tool_kind="read",
            raw_params={"path": bundle_path},
        )
        assert edit.action == "deny"
        assert "sensitive path" in (edit.reason or "")
        assert read.action == "deny"

    @pytest.mark.parametrize(
        "command",
        (
            "Add-Content -Path $env:SSL_CERT_FILE -Value rogue",
            "Set-Content -Path ${env:REQUESTS_CA_BUNDLE} -Value rogue",
            "echo rogue >> %SSL_CERT_FILE%",
            "echo rogue >> !KIROCREW_MANAGED_CA_BUNDLE!",
            'printf rogue >> "$SSL_CERT_FILE"',
            'printf rogue >> "${REQUESTS_CA_BUNDLE}"',
            "python -c \"import os; open(os.environ['SSL_CERT_FILE'], 'a').write('rogue')\"",
        ),
    )
    def test_generated_bundle_environment_aliases_are_refused(self, command):
        """An inherited path alias cannot bypass the trust-root path gate."""
        from kiro_crew import security
        from kiro_crew.hooks import HookManager, HooksConfig

        assert security.is_sensitive_bash_command(command) is not None
        result = HookManager(HooksConfig()).on_tool_call(
            "Modify a file",
            command=command,
            is_shell=True,
        )
        assert result.action == "deny"
        assert "protected TLS trust path" in (result.reason or "")

    def test_generated_bundle_alias_names_are_identifier_bounded(self):
        """Unrelated longer environment names do not trip the trust-path gate."""
        from kiro_crew import security

        assert security.is_sensitive_bash_command("echo $MY_SSL_CERT_FILE_BACKUP") is None

    def test_publishes_owner_only_atomic_bundle_without_proxy_secrets(self, monkeypatch, tmp_path):
        certifi_bundle = tmp_path / "certifi.pem"
        certifi_bundle.write_bytes(b"source certifi bytes are parsed, not copied\n")
        destination = tmp_path / "ca-bundle.pem"
        mozilla_der = b"mozilla-root"
        corporate_der = b"corporate-root"
        mock_certifi = type("M", (), {"where": staticmethod(lambda: str(certifi_bundle))})()
        monkeypatch.setenv("HTTPS_PROXY", "http://employee:proxy-secret@proxy.example")

        with (
            patch.object(_ssl_compat, "certifi", mock_certifi),
            patch(
                "kiro_crew._ssl_compat._windows_ca_certs",
                return_value=([mozilla_der], [corporate_der], 0),
            ),
            patch("kiro_crew._ssl_compat.atomic_write", wraps=atomic_write) as write,
        ):
            assert _ssl_compat._build_windows_ca_bundle(destination) is True

        payload = destination.read_bytes()
        write.assert_called_once_with(destination, payload, restrict_to_owner=True)
        assert _bundle_ders(payload) == [mozilla_der, corporate_der]
        assert b"Windows trusted Root certificates" in payload
        assert b"proxy-secret" not in payload
        assert b"PRIVATE KEY" not in payload

    def test_publish_failure_returns_false_without_partial_destination(
        self, monkeypatch, tmp_path, caplog
    ):
        certifi_bundle = tmp_path / "certifi.pem"
        certifi_bundle.write_bytes(b"mozilla roots\n")
        destination = tmp_path / "ca-bundle.pem"
        mock_certifi = type("M", (), {"where": staticmethod(lambda: str(certifi_bundle))})()

        with (
            patch.object(_ssl_compat, "certifi", mock_certifi),
            patch(
                "kiro_crew._ssl_compat._windows_ca_certs",
                return_value=([b"mozilla-root"], [b"corporate-root"], 0),
            ),
            patch("kiro_crew._ssl_compat.atomic_write", side_effect=OSError("denied")),
            caplog.at_level("WARNING", logger="kiro_crew._ssl_compat"),
        ):
            assert _ssl_compat._build_windows_ca_bundle(destination) is False

        assert not destination.exists()
        assert "falling back" in caplog.text
