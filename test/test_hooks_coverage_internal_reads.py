"""Coverage of ``kiro_crew.hooks``' audited internal reads of sensitive paths.

A read id is registered once, for a relative path that is still sensitive; an
unregistered or stale id is denied with an audit record first; and the read
itself answers every outcome: the bytes, an audit that cannot be recorded, a
non-regular target and an oversized one. Shared helpers live in
``hooks_coverage_helpers.py``.
"""

from __future__ import annotations

import os
import uuid

import pytest
from hooks_coverage_helpers import (
    _write,
)

from kiro_crew import hooks as hooks_mod
from kiro_crew import security
from kiro_crew.hooks import (
    _emit_internal_read_audit,
    emit_internal_read_audit,
    register_internal_read_path,
    safe_read_file_internal,
)


@pytest.fixture
def restore_internal_allowlist():
    """Snapshot/restore ``_INTERNAL_READ_ALLOWLIST`` around a registration."""
    saved = dict(hooks_mod._INTERNAL_READ_ALLOWLIST)
    yield
    hooks_mod._INTERNAL_READ_ALLOWLIST.clear()
    hooks_mod._INTERNAL_READ_ALLOWLIST.update(saved)


# ── internal (audited) reads of sensitive paths ──


class TestRegisterInternalReadPath:
    def test_blank_read_id_refused(self, restore_internal_allowlist):
        with pytest.raises(ValueError, match="non-empty string"):
            register_internal_read_path("", ".aws/x.json")
        with pytest.raises(ValueError, match="non-empty string"):
            register_internal_read_path(None, ".aws/x.json")  # type: ignore[arg-type]

    def test_repointing_an_existing_id_refused(self, restore_internal_allowlist):
        with pytest.raises(ValueError, match="refusing to repoint"):
            register_internal_read_path("kiro_usage_api.sso_token_cli", ".aws/other.json")

    def test_same_id_same_path_is_idempotent(self, restore_internal_allowlist):
        existing = hooks_mod._INTERNAL_READ_ALLOWLIST["kiro_usage_api.sso_token_cli"]
        register_internal_read_path("kiro_usage_api.sso_token_cli", existing)
        assert hooks_mod._INTERNAL_READ_ALLOWLIST["kiro_usage_api.sso_token_cli"] == existing

    @pytest.mark.parametrize(
        "rel",
        [
            "/etc/shadow",
            "../outside.json",
            ".aws/../../escape.json",
        ],
    )
    def test_non_relative_or_traversing_paths_refused(self, rel, restore_internal_allowlist):
        with pytest.raises(ValueError, match="must be relative"):
            register_internal_read_path("edition.probe", rel)

    def test_non_sensitive_target_refused(self, restore_internal_allowlist):
        with pytest.raises(ValueError, match="non-sensitive"):
            register_internal_read_path("edition.probe", "Documents/notes.txt")

    def test_valid_sensitive_registration_lands(self, restore_internal_allowlist):
        rel = ".aws/sso/cache/edition-probe.json"
        register_internal_read_path("edition.probe", rel)
        assert hooks_mod._INTERNAL_READ_ALLOWLIST["edition.probe"] == rel


class TestSafeReadFileInternal:
    def test_unregistered_read_id_is_denied(self):
        with pytest.raises(PermissionError, match="not in allowlist"):
            safe_read_file_internal("nope.not_registered")

    def test_allowlist_entry_that_is_no_longer_sensitive_is_denied(self, monkeypatch):
        # Defense in depth: the carve-out is only valid for a path the shared
        # file gate otherwise blocks.
        monkeypatch.setitem(hooks_mod._INTERNAL_READ_ALLOWLIST, "drifted", "Documents/plain.txt")
        with pytest.raises(PermissionError, match="non-sensitive"):
            safe_read_file_internal("drifted")

    def test_missing_file_returns_none_without_reading_anything(self, monkeypatch):
        rel = f".aws/sso/cache/absent-{uuid.uuid4().hex}.json"
        monkeypatch.setitem(hooks_mod._INTERNAL_READ_ALLOWLIST, "absent", rel)
        outcomes: list[tuple[str, str]] = []
        monkeypatch.setattr(
            hooks_mod,
            "_emit_internal_read_audit",
            lambda read_id, outcome: outcomes.append((read_id, outcome)) or True,
        )
        assert safe_read_file_internal("absent") is None
        assert outcomes == [("absent", "missing")]

    def test_unreadable_open_error_returns_none(self, monkeypatch):
        rel = f".aws/sso/cache/unreadable-{uuid.uuid4().hex}.json"
        monkeypatch.setitem(hooks_mod._INTERNAL_READ_ALLOWLIST, "unreadable", rel)
        outcomes: list[str] = []
        monkeypatch.setattr(
            hooks_mod,
            "_emit_internal_read_audit",
            lambda read_id, outcome: outcomes.append(outcome) or True,
        )
        from kiro_crew import platform_compat as _pc

        real_no_reparse = _pc.open_file_no_reparse

        # Patch the seam the read performs. os.open is only the POSIX arm of
        # open_file_no_reparse, so a patch there would leave the Windows shard
        # opening the real file and classifying it by contents.
        def _eacces(path, *args, **kwargs):
            if str(path).endswith(os.path.basename(rel)):
                raise PermissionError("denied")
            return real_no_reparse(path, *args, **kwargs)

        monkeypatch.setattr(_pc, "open_file_no_reparse", _eacces)
        assert safe_read_file_internal("unreadable") is None
        assert outcomes == ["unreadable"]

    def test_unregistered_read_emits_an_audit_before_raising(self, monkeypatch):
        seen: list[tuple[str, str]] = []
        monkeypatch.setattr(
            hooks_mod,
            "_emit_internal_read_audit",
            lambda read_id, outcome: seen.append((read_id, outcome)) or True,
        )
        with pytest.raises(PermissionError):
            safe_read_file_internal("also.not_registered")
        assert seen == [("also.not_registered", "not_allowlisted")]


class TestInternalReadAudit:
    def test_success_is_reported_when_sel_records_it(self, monkeypatch):
        calls: list[dict] = []

        class _Sel:
            def log_tool_invocation(self, **kwargs):
                calls.append(kwargs)

        import kiro_crew.sel as sel_mod

        monkeypatch.setattr(sel_mod, "sel", lambda: _Sel())
        assert _emit_internal_read_audit("some.read", "success") is True
        assert calls[0]["outcome"] == "success"
        # A success gates the return of live credential bytes, so it must be
        # written synchronously.
        assert calls[0]["critical"] is True

    def test_non_success_outcome_is_not_critical(self, monkeypatch):
        calls: list[dict] = []

        class _Sel:
            def log_tool_invocation(self, **kwargs):
                calls.append(kwargs)

        import kiro_crew.sel as sel_mod

        monkeypatch.setattr(sel_mod, "sel", lambda: _Sel())
        assert _emit_internal_read_audit("some.read", "missing") is True
        assert calls[0]["critical"] is False

    def test_a_raising_sel_reports_failure(self, monkeypatch):
        class _Sel:
            def log_tool_invocation(self, **kwargs):
                raise RuntimeError("sel down")

        import kiro_crew.sel as sel_mod

        monkeypatch.setattr(sel_mod, "sel", lambda: _Sel())
        assert _emit_internal_read_audit("some.read", "success") is False

    def test_audit_only_wrapper_enforces_its_own_allowlist(self, monkeypatch):
        calls: list[str] = []

        class _Sel:
            def log_tool_invocation(self, **kwargs):
                calls.append(kwargs["outcome"])

        import kiro_crew.sel as sel_mod

        monkeypatch.setattr(sel_mod, "sel", lambda: _Sel())
        assert emit_internal_read_audit("not.registered", "success") is False
        assert calls == []
        registered = next(iter(hooks_mod._AUDIT_ONLY_READ_IDS))
        assert emit_internal_read_audit(registered, "success") is True
        assert calls == ["success"]


# ── internal read: the remaining outcomes ──


class TestSafeReadFileInternalOutcomes:
    """Exercise the read outcomes against a redirected home directory.

    ``HOME``/``USERPROFILE`` are repointed at ``tmp_path`` so a real file can
    live at an allowlisted, genuinely sensitive location without touching the
    operator's own ``~/.aws``.
    """

    @pytest.fixture
    def sensitive_home(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        (home / ".aws" / "sso" / "cache").mkdir(parents=True)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        rel = ".aws/sso/cache/probe.json"
        monkeypatch.setitem(hooks_mod._INTERNAL_READ_ALLOWLIST, "probe", rel)
        target = home / ".aws" / "sso" / "cache" / "probe.json"
        if not security.is_sensitive_path(str(target)):
            pytest.skip("home redirection did not take effect for the path gate")
        return target

    def test_a_successful_read_returns_the_bytes(self, sensitive_home, monkeypatch):
        _write(sensitive_home, "opaque-value")
        outcomes: list[str] = []
        monkeypatch.setattr(
            hooks_mod,
            "_emit_internal_read_audit",
            lambda read_id, outcome: outcomes.append(outcome) or True,
        )
        assert safe_read_file_internal("probe") == b"opaque-value"
        assert outcomes == ["success"]

    def test_a_read_whose_audit_cannot_be_recorded_is_denied(self, sensitive_home, monkeypatch):
        _write(sensitive_home, "opaque-value")
        monkeypatch.setattr(hooks_mod, "_emit_internal_read_audit", lambda read_id, outcome: False)
        # audit-or-deny: the carve-out's validity depends on the audit landing.
        assert safe_read_file_internal("probe") is None

    def test_a_non_regular_target_is_refused(self, sensitive_home, monkeypatch):
        sensitive_home.mkdir()
        outcomes: list[str] = []
        monkeypatch.setattr(
            hooks_mod,
            "_emit_internal_read_audit",
            lambda read_id, outcome: outcomes.append(outcome) or True,
        )
        assert safe_read_file_internal("probe") is None
        assert outcomes and outcomes[0] in ("not_regular", "unreadable")

    def test_an_oversized_target_is_refused(self, sensitive_home, monkeypatch):
        _write(sensitive_home, "0123456789")
        monkeypatch.setattr(hooks_mod, "MAX_FILE_BYTES", 4)
        outcomes: list[str] = []
        monkeypatch.setattr(
            hooks_mod,
            "_emit_internal_read_audit",
            lambda read_id, outcome: outcomes.append(outcome) or True,
        )
        assert safe_read_file_internal("probe") is None
        assert outcomes == ["too_large"]
