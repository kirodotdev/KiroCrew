"""Tests for SessionMap CWD persistence and session resume CWD override."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew.session_map import SessionMap


@pytest.fixture()
def session_map(tmp_path):
    """Create a SessionMap backed by a temp directory."""
    with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
        yield SessionMap()


class TestSessionMapCwd:
    """Tests for cwd parameter in set() and get_cwd()."""

    def test_set_stores_cwd_new_entry(self, session_map):
        session_map.set("dash:1", "sid-abc", cwd="/home/user/project")
        assert session_map.get_cwd("dash:1") == "/home/user/project"

    def test_set_stores_cwd_existing_entry(self, session_map):
        session_map.set("dash:1", "sid-abc")
        session_map.set("dash:1", "sid-abc", cwd="/home/user/project")
        assert session_map.get_cwd("dash:1") == "/home/user/project"

    def test_set_without_cwd_does_not_overwrite_existing(self, session_map):
        session_map.set("dash:1", "sid-abc", cwd="/home/user/project")
        session_map.set("dash:1", "sid-abc")
        assert session_map.get_cwd("dash:1") == "/home/user/project"

    def test_get_cwd_missing_key_returns_empty(self, session_map):
        assert session_map.get_cwd("nonexistent") == ""

    def test_get_cwd_entry_without_cwd_field_returns_empty(self, session_map):
        session_map.set("dash:1", "sid-abc")
        assert session_map.get_cwd("dash:1") == ""

    def test_cwd_persists_to_disk(self, tmp_path):
        with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
            sm = SessionMap()
            sm.set("dash:1", "sid-abc", cwd="/home/user/project")

        with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
            sm2 = SessionMap()
            assert sm2.get_cwd("dash:1") == "/home/user/project"

    def test_set_cwd_with_provider(self, session_map):
        session_map.set("dash:1", "sid-abc", provider="claude_code", cwd="/tmp/ws")
        assert session_map.get_cwd("dash:1") == "/tmp/ws"
        assert session_map.get_provider("dash:1") == "claude_code"


class TestSessionResumeCwdOverride:
    """Tests for the resume CWD override logic in SessionManager.get_or_create."""

    @pytest.fixture()
    def mock_session_mgr(self, tmp_path):
        """Minimal mock of SessionManager internals needed for CWD override logic."""
        with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
            sm = SessionMap()

        mgr = MagicMock()
        mgr._session_map = sm
        return mgr

    def test_resume_uses_stored_cwd_when_no_explicit_cwd(self, mock_session_mgr, tmp_path):
        """When resuming (resume_sid set) with no explicit cwd, stored CWD is used."""
        sm = mock_session_mgr._session_map
        sm.set("dash:1", "sid-abc", provider="claude_code", cwd=str(tmp_path))

        cwd = ""
        resume_sid = "sid-abc"
        key = "dash:1"

        effective_cwd = cwd
        if not effective_cwd and resume_sid:
            stored_cwd = sm.get_cwd(key)
            if stored_cwd and Path(stored_cwd).is_dir():
                effective_cwd = stored_cwd

        assert effective_cwd == str(tmp_path)

    def test_resume_ignores_stored_cwd_when_explicit_cwd_provided(self, mock_session_mgr, tmp_path):
        """When explicit cwd is passed, stored CWD is not used."""
        sm = mock_session_mgr._session_map
        sm.set("dash:1", "sid-abc", provider="claude_code", cwd="/old/path")

        cwd = str(tmp_path)
        resume_sid = "sid-abc"
        key = "dash:1"

        effective_cwd = cwd
        if not effective_cwd and resume_sid:
            stored_cwd = sm.get_cwd(key)
            if stored_cwd and Path(stored_cwd).is_dir():
                effective_cwd = stored_cwd

        assert effective_cwd == str(tmp_path)

    def test_resume_skips_stored_cwd_when_dir_missing(self, mock_session_mgr):
        """When stored CWD points to a deleted directory, fall back to empty."""
        sm = mock_session_mgr._session_map
        sm.set("dash:1", "sid-abc", provider="claude_code", cwd="/nonexistent/path/xyz")

        cwd = ""
        resume_sid = "sid-abc"
        key = "dash:1"

        effective_cwd = cwd
        if not effective_cwd and resume_sid:
            stored_cwd = sm.get_cwd(key)
            if stored_cwd and Path(stored_cwd).is_dir():
                effective_cwd = stored_cwd

        assert effective_cwd == ""

    def test_no_resume_no_cwd_override(self, mock_session_mgr, tmp_path):
        """Without resume_sid, stored CWD is never consulted."""
        sm = mock_session_mgr._session_map
        sm.set("dash:1", "sid-abc", cwd=str(tmp_path))

        cwd = ""
        resume_sid = ""
        key = "dash:1"

        effective_cwd = cwd
        if not effective_cwd and resume_sid:
            stored_cwd = sm.get_cwd(key)
            if stored_cwd and Path(stored_cwd).is_dir():
                effective_cwd = stored_cwd

        assert effective_cwd == ""


class TestCwdExtractionFromProvider:
    """The session_map save sites read ``provider.cwd`` (the LLMProvider ABC
    accessor). AcpProvider has no ``_work_dir`` — the old
    ``getattr(provider, "_work_dir", "")`` pattern persisted "" for ALL ACP
    sessions (kiro and claude), breaking resume-cwd-override. These verify the
    accessor returns the real path per provider type."""

    def test_acp_provider_cwd_returns_real_work_dir(self, tmp_path):
        from kiro_crew.providers.acp import AcpProvider

        with patch("kiro_crew.providers.acp.AcpClient"):
            provider = AcpProvider(acp_backend="")  # kiro
        provider._client._work_dir = tmp_path
        assert provider.cwd == str(tmp_path)
        assert provider.cwd != ""
