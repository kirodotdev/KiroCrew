"""Tests for mcp_core session key routing."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from kiro_crew.mcp_core import _call_tool


class TestSpawnRunSessionKeyRouting:
    def test_uses_env_var_when_set(self):
        """KIROCREW_SESSION_KEY env var is used as parent_session."""
        with patch("kiro_crew.mcp_core._post") as mock_post, patch.dict(
            "os.environ", {"KIROCREW_SESSION_KEY": "sess-from-env"}
        ):
            mock_post.return_value = {"id": "agent1"}

            _call_tool("spawn_run", {"task": "test"})

            call_body = mock_post.call_args[0][1]
            assert call_body["parent_session"] == "sess-from-env"

    def test_falls_back_to_pid_file(self, tmp_path):
        import os

        with patch("kiro_crew.mcp_core._post") as mock_post, patch(
            "pathlib.Path.home", return_value=tmp_path / "fake_home"
        ):
            env = os.environ.copy()
            env.pop("KIROCREW_SESSION_KEY", None)
            env.pop("KIROCREW_HOME", None)  # ensure config_dir() uses patched Path.home()
            with patch.dict("os.environ", env, clear=True):
                kirocrew_dir = tmp_path / "fake_home" / ".kirocrew"
                kirocrew_dir.mkdir(parents=True)
                (kirocrew_dir / f"session_pid_{os.getppid()}.txt").write_text("sess-from-pid")

                mock_post.return_value = {"id": "agent1"}
                _call_tool("spawn_run", {"task": "test"})

                assert mock_post.call_args[0][1]["parent_session"] == "sess-from-pid"


class TestSendMessageUnfurlForwarding:
    def test_unfurl_params_forwarded_in_payload(self):
        """unfurl_links and unfurl_media are forwarded to /api/send-message."""
        with patch("kiro_crew.mcp_core._post") as mock_post:
            mock_post.return_value = {"ok": True}

            _call_tool(
                "send_message",
                {
                    "text": "test",
                    "unfurl_links": False,
                    "unfurl_media": False,
                },
            )

            payload = mock_post.call_args[0][1]
            assert payload["unfurl_links"] is False
            assert payload["unfurl_media"] is False

    def test_unfurl_params_omitted_when_absent(self):
        """unfurl params are not in payload when not provided."""
        with patch("kiro_crew.mcp_core._post") as mock_post:
            mock_post.return_value = {"ok": True}

            _call_tool("send_message", {"text": "test"})

            payload = mock_post.call_args[0][1]
            assert "unfurl_links" not in payload
            assert "unfurl_media" not in payload


class TestSendMessageCronSession:
    """session param is explicit opt-in only — no auto-default.
    Default delivery is notification-only; session="slack" adds Slack DM.
    """

    @pytest.fixture(autouse=True)
    def _permit_messaging(self, monkeypatch):
        """Stub governance vets so a real ~/.kirocrew/profiles/cron.json that
        disables messaging doesn't block these payload-routing tests."""
        monkeypatch.setattr("kiro_crew.mcp_core._vet_messaging_governance", lambda _sk: None)
        monkeypatch.setattr("kiro_crew.mcp_core._vet_channel_governance", lambda _sk, _t: None)

    def test_default_notification_only(self):
        """Non-cron bare send_message(text=...) → no session in payload, notification only."""
        with patch("kiro_crew.mcp_core._post") as mock_post, patch.dict(
            "os.environ", {"KIROCREW_SESSION_KEY": "dashboard:chat-1"}
        ):
            mock_post.return_value = {"ok": True}
            result = _call_tool("send_message", {"text": "build passed"})

            payload = mock_post.call_args[0][1]
            assert "session" not in payload
            assert "caller_session" not in payload
            assert "Notification delivered" in result

    def test_cron_bare_send_attaches_caller_session(self):
        """A cron bare send attaches caller_session so the gateway can apply
        the cron→Slack default, and reports the Slack landing site."""
        with patch("kiro_crew.mcp_core._post") as mock_post, patch.dict(
            "os.environ", {"KIROCREW_SESSION_KEY": "cron:abc123"}
        ):
            mock_post.return_value = {"ok": True, "slack": True, "delivered_to": "slack", "ts": "9.9"}
            result = _call_tool("send_message", {"text": "sweep done"})

            payload = mock_post.call_args[0][1]
            assert payload["caller_session"] == "cron:abc123"
            assert "session" not in payload
            assert "Slack" in result and "9.9" in result

    def test_cron_send_notification_only_warns(self):
        """When a cron send only reaches the dashboard (no Slack), surface a
        loud warning instead of a success string."""
        with patch("kiro_crew.mcp_core._post") as mock_post, patch.dict(
            "os.environ", {"KIROCREW_SESSION_KEY": "cron:abc123"}
        ):
            mock_post.return_value = {"ok": True, "delivered_to": "notification"}
            result = _call_tool("send_message", {"text": "sweep done"})

            assert "⚠️" in result
            assert "NOT posted to Slack" in result

    def test_explicit_session_origin_passes_through(self):
        """LLM explicitly passes session=origin → origin in payload."""
        with patch("kiro_crew.mcp_core._post") as mock_post, patch.dict(
            "os.environ", {"KIROCREW_SESSION_KEY": "cron:abc123"}
        ):
            mock_post.return_value = {"ok": True}
            _call_tool("send_message", {"text": "hi", "session": "origin"})

            payload = mock_post.call_args[0][1]
            assert payload.get("session") == "origin"

    def test_explicit_session_slack(self):
        """session='slack' routes to Slack DM + notification."""
        with patch("kiro_crew.mcp_core._post") as mock_post, patch.dict(
            "os.environ", {"KIROCREW_SESSION_KEY": "cron:abc123"}
        ):
            mock_post.return_value = {"ok": True, "slack": True, "ts": "123.456"}
            result = _call_tool("send_message", {"text": "hi", "session": "slack"})

            payload = mock_post.call_args[0][1]
            assert payload.get("session") == "slack"
            assert "Slack" in result

    def test_invalid_session_value_rejected(self):
        """session must be 'origin' or 'slack'; other values rejected."""
        with patch("kiro_crew.mcp_core._post") as mock_post, patch.dict(
            "os.environ", {"KIROCREW_SESSION_KEY": "cron:abc123"}
        ):
            result = _call_tool("send_message", {"text": "hi", "session": "bogus"})
            assert "session" in result.lower() or "error" in result.lower()
            mock_post.assert_not_called()


class TestKnowledgeSearchCache:
    """_get_knowledge_search reuses store+embedder until the DB/config changes."""

    def _reset(self):
        import kiro_crew.mcp_core as mc

        mc._KNOWLEDGE_CACHE = None

    def test_reuses_store_when_db_unchanged(self, tmp_path):
        from kiro_crew.mcp_core import _get_knowledge_search

        self._reset()
        db_path = tmp_path / "knowledge.db"
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text("{}")

        store1, _ = _get_knowledge_search(db_path, cfg_path)
        store2, _ = _get_knowledge_search(db_path, cfg_path)
        # Same object — not rebuilt — when nothing changed.
        assert store1 is store2
        self._reset()

    def test_rebuilds_after_ingest(self, tmp_path):
        from kiro_crew.mcp_core import _get_knowledge_search

        self._reset()
        db_path = tmp_path / "knowledge.db"
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text("{}")

        store1, _ = _get_knowledge_search(db_path, cfg_path)
        # Simulate ingestion: write through the store so the DB file changes.
        store1.add_item("Title", "body text", "doc")
        # Force a checkpoint so the main DB file's mtime/size move (WAL mode).
        store1.db.execute("PRAGMA wal_checkpoint(FULL)")
        store2, _ = _get_knowledge_search(db_path, cfg_path)
        # A changed DB signature must yield a freshly-built store.
        assert store1 is not store2
        self._reset()

    def test_config_change_rebuilds(self, tmp_path):
        from kiro_crew.mcp_core import _get_knowledge_search

        self._reset()
        db_path = tmp_path / "knowledge.db"
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text('{"memory": {"embedding_provider": "none"}}')

        _, emb1 = _get_knowledge_search(db_path, cfg_path)
        assert emb1 is None
        cfg_path.write_text(
            '{"memory": {"embedding_provider": "ollama", "embedding_model": "m"}}'
        )
        _, emb2 = _get_knowledge_search(db_path, cfg_path)
        assert emb2 is not None  # embedder rebuilt from new config
        self._reset()

    def test_failed_rebuild_keeps_old_store_usable(self, tmp_path, monkeypatch):
        """If a rebuild's KnowledgeStore() raises, the cached store must NOT be
        left with a closed connection — the old store stays usable."""
        import kiro_crew.mcp_core as mc
        from kiro_crew.mcp_core import _get_knowledge_search

        self._reset()
        db_path = tmp_path / "knowledge.db"
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text("{}")

        store1, _ = _get_knowledge_search(db_path, cfg_path)
        # Change the config so the next call enters the rebuild branch.
        cfg_path.write_text('{"memory": {"embedding_provider": "none"}}')

        def _boom(*a, **k):
            raise RuntimeError("db locked")

        monkeypatch.setattr(mc, "KnowledgeStore", _boom)
        with pytest.raises(RuntimeError):
            _get_knowledge_search(db_path, cfg_path)
        # The old cached store's connection must still be open (not closed before
        # the failed build) — a query succeeds rather than raising ProgrammingError.
        assert store1.db.execute("SELECT 1").fetchone()[0] == 1
        self._reset()


class TestSessionKeyHeaderError:
    """The header-safety guard for session keys (Mesh-2241)."""

    def test_ascii_key_is_header_safe(self):
        from kiro_crew.mcp_core import _session_key_header_error

        assert _session_key_header_error("dashboard:Plain ASCII Title") is None
        assert _session_key_header_error("") is None

    def test_non_latin1_key_returns_actionable_error(self):
        from kiro_crew.mcp_core import _session_key_header_error

        # Em-dash (U+2014) and emoji are non-latin-1 and crash http.client.
        for sk in ("dashboard:A — B", "dashboard:done \U0001f680"):
            err = _session_key_header_error(sk)
            assert err is not None
            assert "rename" in err.lower()

    def test_latin1_supplement_is_allowed(self):
        from kiro_crew.mcp_core import _session_key_header_error

        # Chars in latin-1 range (e.g. é, U+00E9) encode fine — not flagged.
        assert _session_key_header_error("dashboard:café") is None

    def test_post_short_circuits_on_non_latin1_key(self):
        from kiro_crew import mcp_core

        # The actual user-facing fix path: a non-latin-1 resolved key makes
        # _post early-return the error dict WITHOUT issuing the HTTP request.
        with (
            patch.object(mcp_core, "_resolve_session_key", return_value="dashboard:A — B"),
            patch.object(mcp_core, "_internal_secret", return_value="secret"),
            patch("urllib.request.urlopen") as mock_urlopen,
        ):
            result = mcp_core._post("/api/lessons", {"text": "x"})
        assert "error" in result
        assert "rename" in result["error"].lower()
        mock_urlopen.assert_not_called()
