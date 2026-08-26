"""Tests for channel agent model inheritance.

Verifies that ``SessionManager.get_or_create()`` falls back to the global
``agent.model`` config when no explicit model is passed.  This ensures
channel agents (and any other caller that omits ``model=``) inherit the
user's configured model instead of silently defaulting to 'auto' (Sonnet).
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.session import SessionManager, _model_fallback


@pytest.fixture
def cfg():
    c = KiroCrewConfig()
    # The default provider is now ``acp`` (KiroACP/kiro-cli). These tests
    # exercise the ``agent.model`` fallback path, so pin the provider to
    # ``acp`` explicitly to be robust to future default changes.
    c.agent.provider = "acp"
    c.agent.model = "claude-opus-4.6"
    c.session.timeout_secs = 2  # short for testing
    return c


def _capturing_factory(captured: dict):
    """Factory that records kwargs passed to it."""

    def factory(session_key=None, agent=None, channel_id=None, **kwargs):
        captured.update(kwargs)
        m = AsyncMock()
        m.start = AsyncMock()
        m.shutdown = AsyncMock()
        m.context_usage_pct = lambda: 0.0
        m.is_alive.return_value = True
        return m

    return factory


class TestModelFallbackToGlobalConfig:
    """get_or_create() falls back to global model when caller omits it."""

    @pytest.mark.asyncio
    async def test_no_model_uses_global_config(self, cfg):
        """When caller omits model=, factory receives the global model."""
        captured: dict = {}
        mgr = SessionManager(cfg, provider_factory=_capturing_factory(captured))
        await mgr.get_or_create("test-fallback", agent="deep-researcher")
        assert captured["model_override"] == "claude-opus-4.6"
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_explicit_model_overrides_global(self, cfg):
        """Explicit model= wins over global config."""
        captured: dict = {}
        mgr = SessionManager(cfg, provider_factory=_capturing_factory(captured))
        await mgr.get_or_create("test-explicit", model="claude-haiku")
        assert captured["model_override"] == "claude-haiku"
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_channel_agent_call_pattern(self, cfg):
        """Mirror the channel.py call: agent name, no model arg."""
        captured: dict = {}
        mgr = SessionManager(cfg, provider_factory=_capturing_factory(captured))
        # This is exactly how run_channel_agent() calls get_or_create
        await mgr.get_or_create(
            "channel:abc:agent1",
            agent="deep-researcher",
            approval_policy="trusted",
        )
        # Without the fix, model_override would be None and kiro-cli
        # would default to 'auto' (Sonnet) for non-kirocrew agents.
        assert captured["model_override"] == "claude-opus-4.6"
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_kirocrew_agent_uses_global(self, cfg):
        """The default 'kirocrew' agent is excluded from per-agent resolution
        and inherits the global model (it intentionally tracks the global)."""
        captured: dict = {}
        mgr = SessionManager(cfg, provider_factory=_capturing_factory(captured))
        await mgr.get_or_create("kirocrew-sess", agent="kirocrew")
        assert captured["model_override"] == "claude-opus-4.6"
        await mgr.close_all()


class TestModelFallback:
    """Pure unit tests for the precedence decision — no I/O, no patching.

    A per-agent pin defers to kiro (None); a blank agent inherits the global;
    a sentinel/empty global also defers. This is the heart of the per-agent
    precedence fix, exercised here with plain inputs.
    """

    def test_per_agent_pin_defers_to_kiro(self):
        assert _model_fallback("claude-sonnet-3", "claude-opus-4.6") is None

    def test_blank_agent_uses_global_model(self):
        assert _model_fallback("", "claude-opus-4.6") == "claude-opus-4.6"

    def test_sentinel_global_returns_none(self):
        assert _model_fallback("", "auto") is None

    def test_empty_global_returns_none(self):
        assert _model_fallback("", "") is None


class TestResolveNamedAgentModel:
    """Real-file coverage for KiroCrewConfig._resolve_named_agent_model.

    Uses the ``agents_dir`` dependency-injection seam to point the resolver at
    a temp directory — real files, no patching.
    """

    def test_reads_model_by_name_field(self, tmp_path):
        # filename stem differs from the name field -> proves the name match
        (tmp_path / "file-stem.json").write_text(
            json.dumps({"name": "foo-agent", "model": "claude-sonnet-3"})
        )
        assert (
            KiroCrewConfig._resolve_named_agent_model("foo-agent", agents_dir=tmp_path)
            == "claude-sonnet-3"
        )

    def test_reads_model_by_filename_stem(self, tmp_path):
        (tmp_path / "bar.json").write_text(json.dumps({"model": "claude-haiku-4.5"}))
        assert (
            KiroCrewConfig._resolve_named_agent_model("bar", agents_dir=tmp_path)
            == "claude-haiku-4.5"
        )

    def test_returns_empty_when_not_found(self, tmp_path):
        (tmp_path / "bar.json").write_text(json.dumps({"model": "x"}))
        assert KiroCrewConfig._resolve_named_agent_model("nope", agents_dir=tmp_path) == ""

    def test_returns_empty_for_empty_agent(self, tmp_path):
        assert KiroCrewConfig._resolve_named_agent_model("", agents_dir=tmp_path) == ""

    def test_skips_non_dict_json(self, tmp_path):
        # stem matches "weird" but the content isn't an object -> skipped safely
        (tmp_path / "weird.json").write_text(json.dumps([1, 2, 3]))
        assert KiroCrewConfig._resolve_named_agent_model("weird", agents_dir=tmp_path) == ""

    def test_skips_malformed_json_and_finds_valid(self, tmp_path):
        (tmp_path / "broken.json").write_text("{not valid json")
        (tmp_path / "good.json").write_text(json.dumps({"model": "claude-opus-4.8"}))
        assert (
            KiroCrewConfig._resolve_named_agent_model("good", agents_dir=tmp_path)
            == "claude-opus-4.8"
        )

    def test_malformed_json_only_returns_empty(self, tmp_path):
        # Only a malformed file present -> json.loads raises, the entry is
        # skipped via the except/continue branch, and the resolver returns "".
        # (Deterministic: no valid file can match first and short-circuit.)
        (tmp_path / "broken.json").write_text("{not valid json")
        assert (
            KiroCrewConfig._resolve_named_agent_model("broken", agents_dir=tmp_path) == ""
        )

    def test_match_with_no_model_field_returns_empty(self, tmp_path):
        (tmp_path / "nomodel.json").write_text(json.dumps({"name": "nomodel"}))
        assert (
            KiroCrewConfig._resolve_named_agent_model("nomodel", agents_dir=tmp_path) == ""
        )


class TestAgentSpecReadsAreHardened:
    """Both model resolvers must read agent specs through the discovery
    module's hardened reader (#4962), not bare ``read_text``: the agents
    directory is user-writable and shared with other tools, so an oversized
    file is refused at the read cap and a link resolving into a sensitive
    path donates nothing."""

    def test_named_resolver_refuses_an_oversized_spec(self, tmp_path, monkeypatch):
        from kiro_crew import agent_discovery
        from kiro_crew.hooks import FileTooLargeError

        (tmp_path / "huge.json").write_text(json.dumps({"name": "huge", "model": "m"}))
        monkeypatch.setattr(
            agent_discovery,
            "safe_read_file_bytes",
            lambda _p: (_ for _ in ()).throw(FileTooLargeError()),
        )
        assert KiroCrewConfig._resolve_named_agent_model("huge", agents_dir=tmp_path) == ""

    def test_named_resolver_refuses_a_link_into_a_sensitive_path(
        self, tmp_path, monkeypatch
    ):
        # A directory junction needs no privilege on Windows and resolves
        # through pathlib exactly like a symlink, so the guard is exercised
        # here rather than skipped. The sensitive-path verdict is scoped to
        # the junction's TARGET so the test proves resolution follows the
        # link: a same-named spec OUTSIDE the link must still read fine.
        from conftest import make_dir_link
        from kiro_crew import agent_discovery

        outside = tmp_path / "sensitive-outside"
        outside.mkdir()
        (outside / "link.json").write_text(json.dumps({"name": "link", "model": "stolen"}))
        (tmp_path / "direct.json").write_text(json.dumps({"name": "direct", "model": "fine"}))
        make_dir_link(tmp_path / "link", outside)
        monkeypatch.setattr(
            agent_discovery,
            "is_sensitive_path",
            lambda p: "sensitive-outside" in str(p),
        )
        assert KiroCrewConfig._resolve_named_agent_model("link", agents_dir=tmp_path) == ""
        assert (
            KiroCrewConfig._resolve_named_agent_model("direct", agents_dir=tmp_path)
            == "fine"
        )

    def test_installed_spec_resolver_reads_through_the_hardened_reader(
        self, tmp_path, monkeypatch
    ):
        # The installed kirocrew.json path goes through the same reader: a
        # spec the reader refuses yields no model, and the resolver falls
        # through to the bundled default instead of trusting the file.
        import kiro_crew.config.loader as loader_mod
        from kiro_crew import agent_discovery
        from kiro_crew.hooks import FileTooLargeError

        (tmp_path / "kirocrew.json").write_text(json.dumps({"model": "pinned"}))
        monkeypatch.setattr(loader_mod, "kiro_agents_dir", lambda: tmp_path)
        seen = {}

        def _refusing_read(_path):
            seen["called"] = True
            raise FileTooLargeError()

        monkeypatch.setattr(agent_discovery, "safe_read_file_bytes", _refusing_read)
        result = KiroCrewConfig._resolve_agent_model()
        assert seen["called"] is True
        assert result != "pinned"
