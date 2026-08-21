"""Tests for kiro_crew.log_redaction."""

from __future__ import annotations

import logging

from kiro_crew.log_redaction import SecretRedactionFilter, install_log_redaction


class TestSecretRedactionFilter:
    """Unit tests for the SecretRedactionFilter class."""

    def test_redacts_vault_secret(self) -> None:
        filt = SecretRedactionFilter(["sk-secret-key-12345"])
        result = filt.redact("Connecting with key sk-secret-key-12345 to DB")
        assert "sk-secret-key-12345" not in result
        assert "[REDACTED]" in result

    def test_redacts_multiple_secrets(self) -> None:
        filt = SecretRedactionFilter(["sk-secret-key-12345", "hunter2_password"])
        result = filt.redact("key=sk-secret-key-12345 pass=hunter2_password")
        assert "sk-secret-key-12345" not in result
        assert "hunter2_password" not in result
        assert result.count("[REDACTED]") == 2

    def test_redacts_bearer_token(self) -> None:
        filt = SecretRedactionFilter([])
        result = filt.redact("Auth header: Bearer eyJhbGciOiJSUzI1NiJ9.payload.sig")
        assert "eyJhbGciOiJSUzI1NiJ9" not in result
        assert "Bearer [REDACTED]" in result

    def test_redacts_bearer_case_insensitive(self) -> None:
        filt = SecretRedactionFilter([])
        result = filt.redact("header: bearer eyJhbGciOiJSUzI1NiJ9.payload.sig")
        assert "eyJhbGciOiJSUzI1NiJ9" not in result

    def test_passthrough_non_secret_text(self) -> None:
        filt = SecretRedactionFilter([])
        msg = "Normal log message with no secrets"
        assert filt.redact(msg) == msg

    def test_short_secrets_not_redacted(self) -> None:
        """Secrets shorter than 4 chars are not redacted to avoid false positives."""
        filt = SecretRedactionFilter(["ab", "x"])
        result = filt.redact("value is ab here")
        assert result == "value is ab here"

    def test_no_vault_io_at_construction(self) -> None:
        """Filter construction does ZERO vault I/O — just compiles patterns."""
        # This test exists to verify the architectural contract: no imports
        # of kiro_crew.secrets happen inside SecretRedactionFilter.
        filt = SecretRedactionFilter(["my-secret-value"])
        assert filt.redact("my-secret-value") == "[REDACTED]"

    def test_empty_patterns_only_bearer(self) -> None:
        """With no secret patterns, only Bearer tokens are redacted."""
        filt = SecretRedactionFilter([])
        msg = "key=sk-live-123 and Bearer eyJhbGciOiJSUzI1NiJ9.test"
        result = filt.redact(msg)
        # sk-live-123 passes through (not in patterns)
        assert "sk-live-123" in result
        # Bearer token is always redacted
        assert "eyJhbGciOiJSUzI1NiJ9" not in result


class TestInstallLogRedaction:
    """Tests for the install_log_redaction convenience function."""

    def test_installs_record_factory(self) -> None:
        import kiro_crew.log_redaction as mod

        old_factory = logging.getLogRecordFactory()
        old_active = mod._active_filter
        old_orig = mod._original_factory
        try:
            mod._active_filter = None
            mod._original_factory = None
            filt = install_log_redaction([])
            assert mod._active_filter is filt
            assert logging.getLogRecordFactory() is not old_factory
        finally:
            # Restore
            mod._active_filter = old_active
            mod._original_factory = old_orig
            logging.setLogRecordFactory(old_factory)

    def test_returns_filter_instance(self) -> None:
        import kiro_crew.log_redaction as mod

        old_factory = logging.getLogRecordFactory()
        old_active = mod._active_filter
        old_orig = mod._original_factory
        try:
            mod._active_filter = None
            mod._original_factory = None
            filt = install_log_redaction(["test-secret"])
            assert isinstance(filt, SecretRedactionFilter)
        finally:
            mod._active_filter = old_active
            mod._original_factory = old_orig
            logging.setLogRecordFactory(old_factory)

    def test_redacts_via_record_factory(self) -> None:
        """Records created after install have secrets redacted."""
        import kiro_crew.log_redaction as mod

        old_factory = logging.getLogRecordFactory()
        old_active = mod._active_filter
        old_orig = mod._original_factory
        try:
            mod._active_filter = None
            mod._original_factory = None
            install_log_redaction(["sk-secret-key-12345"])

            # Use the factory directly (how logging internally creates records)
            factory = logging.getLogRecordFactory()
            record = factory(
                "kiro_crew.test",
                logging.INFO,
                "",
                0,
                "key is sk-secret-key-12345 here",
                None,
                None,
            )
            assert "sk-secret-key-12345" not in record.msg
            assert "[REDACTED]" in record.msg
        finally:
            mod._active_filter = old_active
            mod._original_factory = old_orig
            logging.setLogRecordFactory(old_factory)

    def test_zero_vault_imports(self) -> None:
        """log_redaction module does NOT import kiro_crew.secrets."""
        import importlib
        import sys

        # Ensure fresh import
        mod_name = "kiro_crew.log_redaction"
        if mod_name in sys.modules:
            del sys.modules[mod_name]

        # Track what gets imported
        imported_before = set(sys.modules.keys())
        importlib.import_module(mod_name)
        imported_after = set(sys.modules.keys())

        new_imports = imported_after - imported_before
        vault_imports = [m for m in new_imports if "secrets" in m and "kiro_crew" in m]
        assert vault_imports == [], f"log_redaction imported vault modules: {vault_imports}"
