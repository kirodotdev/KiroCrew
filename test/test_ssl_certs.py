"""Tests for _ssl_compat SSL certificate bootstrap."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew import _ssl_compat
from kiro_crew._ssl_compat import _CA_CANDIDATES, _ensure_ssl_certs


@pytest.fixture(autouse=True)
def _reset_ssl_bootstrap(monkeypatch):
    """Keep tests independent and file-bootstrap cases platform-neutral."""
    monkeypatch.setattr(_ssl_compat, "_TRUSTSTORE_INJECTED", False)
    monkeypatch.setattr(sys, "platform", "linux")


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
            patch.dict("sys.modules", {"certifi": None}),
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
            patch.dict("sys.modules", {"certifi": mock_certifi}),
        ):
            _ensure_ssl_certs()

        import os

        assert os.environ["SSL_CERT_FILE"] == str(fake_certifi_bundle)
        assert os.environ["REQUESTS_CA_BUNDLE"] == str(fake_certifi_bundle)

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
            patch.dict(sys.modules, {"certifi": mock_certifi}),
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
