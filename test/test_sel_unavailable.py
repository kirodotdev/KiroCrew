"""``sel()`` in a process that cannot build the security event log.

A sandboxed MCP server can be unable to create or see ``<config>/trust``, so the
first ``sel()`` of that process fails inside ``_load_or_create_hmac_key``. A
regular file at ``trust`` reproduces the same ``FileExistsError`` on Linux.
"""

from __future__ import annotations

import asyncio
import io
import logging
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

import kiro_crew.mcp_shared as mcp_shared
from kiro_crew import sel as sel_mod


@pytest.fixture
def unreachable_trust_root(tmp_path_factory, monkeypatch):
    home = tmp_path_factory.mktemp("sel-unreachable")
    (home / "trust").write_text("not a directory")
    monkeypatch.setattr(sel_mod, "_default_dir", lambda: home)
    monkeypatch.setattr(sel_mod, "_unavailable_warned", False, raising=False)
    monkeypatch.setattr(sel_mod, "_unavailable_dropped", 0, raising=False)
    monkeypatch.setattr(sel_mod.SecurityEventLog, "_instance", None)
    return home


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="http://localhost/api/session-tool-policy",
        code=code,
        msg=f"HTTP {code}",
        hdrs=None,
        fp=io.BytesIO(b""),
    )


def test_a_403_policy_read_returns_policy_forbidden(unreachable_trust_root, monkeypatch, tmp_path):
    monkeypatch.setenv("KIROCREW_SESSION_KEY", "subagent:abc")
    monkeypatch.setattr(mcp_shared, "resolve_client_port_src", lambda port: (5476, "config"))
    monkeypatch.setattr(mcp_shared, "config_dir", lambda: tmp_path)
    (tmp_path / ".local_secret").write_text("test-secret")
    for name in ("_last_failure_time", "_last_probe_time", "_last_startup_race_time"):
        monkeypatch.setattr(mcp_shared, name, 0.0)
    monkeypatch.setattr(mcp_shared, "_last_startup_race_key", "")
    mcp_shared._excluded_tools_by_session.clear()

    urlopen = MagicMock(side_effect=_http_error(403))
    with patch.object(mcp_shared, "loopback_urlopen", urlopen):
        try:
            policy = mcp_shared._resolve_tool_policy()
        except OSError as exc:
            pytest.fail(f"the policy read raised {exc!r} instead of answering the 403")

    assert urlopen.call_count == 1
    assert policy.unresolved == "policy_forbidden"
    assert mcp_shared._last_failure_time == 0.0


def test_a_non_critical_audit_is_dropped_with_one_warning(unreachable_trust_root, caplog):
    with caplog.at_level(logging.WARNING, logger="kiro_crew.sel"):
        try:
            sel_mod.sel().log_api_access(caller="mcp", operation="probe", outcome="ok")
            sel_mod.sel().log_tool_invocation(
                session_key="subagent:abc", tool_name="t", outcome="ok"
            )
        except OSError as exc:
            pytest.fail(f"a non-critical audit raised {exc!r}")

    warnings = [r for r in caplog.records if "security event log is unavailable" in r.getMessage()]
    assert len(warnings) == 1
    assert "FileExistsError" in warnings[0].getMessage()


def test_a_critical_audit_still_raises(unreachable_trust_root):
    with pytest.raises(FileExistsError):
        sel_mod.sel().log_api_access(caller="mcp", operation="probe", outcome="ok", critical=True)
    with pytest.raises(FileExistsError):
        sel_mod.sel().log_governance_degraded(
            session_key="subagent:abc", chokepoint="probe", failed_closed=True
        )


def test_reading_the_log_still_raises(unreachable_trust_root):
    with pytest.raises(FileExistsError):
        sel_mod.sel().recent()
    with pytest.raises(FileExistsError):
        sel_mod.sel().verify_integrity()


def test_sel_builds_the_real_log_once_the_trust_dir_is_usable(unreachable_trust_root):
    assert type(sel_mod.sel()) is not sel_mod.SecurityEventLog

    (unreachable_trust_root / "trust").unlink()

    assert type(sel_mod.sel()) is sel_mod.SecurityEventLog
    assert (unreachable_trust_root / "trust" / "sel_hmac.key").is_file()


def test_events_dropped_before_the_log_builds_are_counted(unreachable_trust_root):
    sel_mod.sel().log_api_access(caller="mcp", operation="probe", outcome="ok")
    sel_mod.sel().log_api_access(caller="mcp", operation="probe", outcome="ok")
    (unreachable_trust_root / "trust").unlink()

    assert sel_mod.sel().dropped_events == 2
    assert sel_mod.sel().dropped_events == 2


def test_the_startup_warm_reports_a_log_that_cannot_build(unreachable_trust_root, caplog):
    with caplog.at_level(logging.WARNING, logger="kiro_crew.sel"):
        asyncio.run(sel_mod.warm_sel_singleton())

    warm = [r for r in caplog.records if "SEL startup warm failed" in r.getMessage()]
    assert len(warm) == 1
    assert isinstance(warm[0].exc_info[1], FileExistsError)


def test_a_construction_error_that_is_not_an_os_error_still_raises(
    unreachable_trust_root, monkeypatch
):
    (unreachable_trust_root / "trust").unlink()

    def _bad_key(self):
        raise RuntimeError("trust root too short")

    monkeypatch.setattr(sel_mod.SecurityEventLog, "_load_or_create_hmac_key", _bad_key)
    with pytest.raises(RuntimeError, match="trust root too short"):
        sel_mod.sel()


def test_a_missing_attribute_is_an_attribute_error(unreachable_trust_root):
    log = sel_mod.sel()

    assert not hasattr(log, "missing_public")
    assert getattr(log, "_missing_private", "default") == "default"


def test_a_later_failure_warns_again_after_the_log_recovered(
    unreachable_trust_root, monkeypatch, caplog
):
    sel_mod.sel().log_api_access(caller="mcp", operation="probe", outcome="ok")
    (unreachable_trust_root / "trust").unlink()
    sel_mod.sel()
    monkeypatch.setattr(sel_mod.SecurityEventLog, "_instance", None)
    (unreachable_trust_root / "trust" / "sel_hmac.key").unlink()
    (unreachable_trust_root / "trust").rmdir()
    (unreachable_trust_root / "trust").write_text("not a directory")
    caplog.clear()

    with caplog.at_level(logging.WARNING, logger="kiro_crew.sel"):
        sel_mod.sel().log_api_access(caller="mcp", operation="probe", outcome="ok")

    warnings = [r for r in caplog.records if "security event log is unavailable" in r.getMessage()]
    assert len(warnings) == 1


def test_a_critical_audit_raises_the_construction_error_with_both_paths(
    unreachable_trust_root, monkeypatch
):
    (unreachable_trust_root / "trust").unlink()
    error = PermissionError(13, "Access is denied", "sel_hmac.key.tmp", None, "sel_hmac.key")

    def _replace_fails(self):
        raise error

    monkeypatch.setattr(sel_mod.SecurityEventLog, "_load_or_create_hmac_key", _replace_fails)
    with pytest.raises(PermissionError) as raised:
        sel_mod.sel().log_api_access(caller="mcp", operation="probe", outcome="ok", critical=True)

    assert raised.value is not error
    assert (raised.value.filename, raised.value.filename2) == ("sel_hmac.key.tmp", "sel_hmac.key")


class _NoArgsOSError(OSError):
    def __init__(self):
        super().__init__(13, "denied")


def test_a_critical_audit_raises_an_error_that_cannot_be_copied(
    unreachable_trust_root, monkeypatch
):
    (unreachable_trust_root / "trust").unlink()
    error = _NoArgsOSError()

    def _fails(self):
        raise error

    monkeypatch.setattr(sel_mod.SecurityEventLog, "_load_or_create_hmac_key", _fails)
    with pytest.raises(_NoArgsOSError) as raised:
        sel_mod.sel().log_api_access(caller="mcp", operation="probe", outcome="ok", critical=True)

    assert raised.value is error
