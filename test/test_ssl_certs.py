"""Tests for _ssl_compat SSL certificate bootstrap."""

from __future__ import annotations

from unittest.mock import patch

from kiro_crew._ssl_compat import _CA_CANDIDATES, _ensure_ssl_certs


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
        """Should leave env vars unset if no candidate file exists."""
        monkeypatch.delenv("SSL_CERT_FILE", raising=False)
        monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)

        mock_paths = type("P", (), {"cafile": None, "capath": None})()
        candidates = ("/nonexistent/a.pem", "/nonexistent/b.crt")

        with (
            patch("ssl.get_default_verify_paths", return_value=mock_paths),
            patch("kiro_crew._ssl_compat._CA_CANDIDATES", candidates),
        ):
            _ensure_ssl_certs()

        import os

        assert os.environ.get("SSL_CERT_FILE") is None
        assert os.environ.get("REQUESTS_CA_BUNDLE") is None

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
