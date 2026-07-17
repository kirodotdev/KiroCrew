"""Regression tests for the Slack session-key alias fold (Mesh-2732).

Since the channel-neutral transport refactor (commit 67602204), Slack thread
sessions have two key forms: the legacy bare ``thread_ts`` and the canonical
``slack:<ts>`` form (``messaging/link.py``). The ``SessionMap`` thread index
returns canonical keys while first-message derivation historically registered
the bare form, so the second in-thread message missed the live session in
``SessionManager._sessions``, the disk resume was rejected by kiro-cli
("Session is active in another process"), and a brand-new context-free session
silently split the thread.

The fix: ``SessionManager._fold_key`` resolves the two alias forms onto the
live registry entry at every public method boundary, and the Slack handler
derives the canonical form at message entry.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from kiro_crew.config import KiroCrewConfig
from kiro_crew.history import ConversationLog
from kiro_crew.messaging.link import canonical_key
from kiro_crew.session import SessionManager

_BARE_TS = "1783733803.877979"
_CANON = f"slack:{_BARE_TS}"


def _alive_provider_factory():
    """Providers that pass the fast-path is_process_alive reuse gate."""

    def factory(session_key=None, agent=None, channel_id=None, **kwargs):
        m = AsyncMock()
        m.start = AsyncMock()
        m.shutdown = AsyncMock()
        m.is_process_alive = lambda: True
        m.is_alive = lambda: True
        m.context_usage_pct = lambda: 0.0
        return m

    return factory


@pytest.fixture()
def cfg():
    return KiroCrewConfig()


class TestFoldKey:
    """_fold_key resolves bare/canonical Slack aliases onto the live entry."""

    @pytest.mark.asyncio
    async def test_canonical_lookup_finds_bare_registered_session(self, cfg) -> None:
        """The incident shape: session registered bare, looked up canonical.

        First message registered the live session under the bare thread_ts;
        the second message arrives with the canonical key from the thread
        index. get_or_create must return the SAME provider with is_new=False —
        no cold start, no resume attempt.
        """
        mgr = SessionManager(cfg, provider_factory=_alive_provider_factory())
        provider1, is_new1, _ = await mgr.get_or_create(_BARE_TS)
        assert is_new1
        mgr.release(_BARE_TS)

        provider2, is_new2, resumed2 = await mgr.get_or_create(_CANON)
        assert provider2 is provider1
        assert not is_new2
        assert not resumed2
        mgr.release(_CANON)
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_bare_lookup_finds_canonical_registered_session(self, cfg) -> None:
        """Reverse alias: session registered canonical, legacy caller uses bare."""
        mgr = SessionManager(cfg, provider_factory=_alive_provider_factory())
        provider1, is_new1, _ = await mgr.get_or_create(_CANON)
        assert is_new1
        mgr.release(_CANON)

        provider2, is_new2, _ = await mgr.get_or_create(_BARE_TS)
        assert provider2 is provider1
        assert not is_new2
        mgr.release(_BARE_TS)
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_fold_covers_read_accessors(self, cfg) -> None:
        """has_session / get_provider / get_pid / get_agent fold aliases."""
        mgr = SessionManager(cfg, provider_factory=_alive_provider_factory())
        provider, _, _ = await mgr.get_or_create(_BARE_TS, agent="kirocrew")
        mgr.release(_BARE_TS)

        assert mgr.has_session(_CANON)
        assert mgr.has_session(_BARE_TS)
        assert mgr.get_provider(_CANON) is provider
        assert mgr.get_agent(_CANON) == "kirocrew"
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_fold_covers_approval_policy(self, cfg) -> None:
        """Trust set under one alias is visible under the other (trust click
        paths pass bare keys while the handler runs under canonical)."""
        mgr = SessionManager(cfg, provider_factory=_alive_provider_factory())
        await mgr.get_or_create(_CANON)
        mgr.release(_CANON)

        mgr.set_approval_policy(_BARE_TS, "auto")
        assert mgr.get_approval_policy(_CANON) == "auto"
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_fold_covers_queue(self, cfg) -> None:
        """enqueue under bare key lands on the canonical-registered session."""
        mgr = SessionManager(cfg, provider_factory=_alive_provider_factory())
        await mgr.get_or_create(_CANON)
        # Semaphore still held (no release) — session is "busy".
        assert mgr.enqueue(_BARE_TS, "1783733999.000001", "queued text")
        popped = mgr.dequeue(_CANON)
        assert popped is not None
        assert popped[1] == "queued text"
        mgr.release(_CANON)
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_non_slack_namespaces_never_rewritten(self, cfg) -> None:
        """dashboard:/cron: keys pass through the fold unchanged."""
        mgr = SessionManager(cfg, provider_factory=_alive_provider_factory())
        await mgr.get_or_create("dashboard:chat-1-abc")
        mgr.release("dashboard:chat-1-abc")
        assert mgr.has_session("dashboard:chat-1-abc")
        assert not mgr.has_session("chat-1-abc")
        assert mgr._fold_key("cron:j1") == "cron:j1"
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_unknown_key_passes_through(self, cfg) -> None:
        """A key with no live entry registers under the caller's form."""
        mgr = SessionManager(cfg, provider_factory=_alive_provider_factory())
        assert mgr._fold_key(_CANON) == _CANON
        assert mgr._fold_key(_BARE_TS) == _BARE_TS
        await mgr.close_all()


class TestThreadIndexRegistryAgreement:
    """After the self-link, the thread index and the live registry agree."""

    @pytest.mark.asyncio
    async def test_second_message_flow_no_split(self, cfg) -> None:
        """End-to-end key flow of two in-thread Slack messages.

        Mirrors handle_message: message 1 derives the canonical key, creates
        the session, and self-links the bare thread_ts; message 2 resolves the
        thread index and must land on the same live session.
        """
        mgr = SessionManager(cfg, provider_factory=_alive_provider_factory())

        # Message 1
        session_key = canonical_key(_BARE_TS)
        provider1, is_new1, _ = await mgr.get_or_create(session_key, channel_id="D123")
        assert is_new1
        mgr.set_slack_link(session_key, _BARE_TS, "D123")
        mgr.release(session_key)

        # Thread index and registry agree on the canonical key
        linked = mgr.get_session_for_thread(_BARE_TS)
        assert linked == session_key
        assert mgr.has_session(linked)

        # Message 2 — routed via the thread index
        provider2, is_new2, resumed2 = await mgr.get_or_create(linked, channel_id="D123")
        assert provider2 is provider1
        assert not is_new2
        assert not resumed2
        mgr.release(linked)
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_legacy_bare_session_map_entry_still_resolves(self, cfg) -> None:
        """A pre-existing self-link written with bare key args still resolves
        (SessionMap canonicalizes the session key; the thread index stays
        keyed by the bare ts)."""
        mgr = SessionManager(cfg, provider_factory=_alive_provider_factory())
        # Legacy writer: both args bare (pre-fix handler behavior)
        mgr.set_slack_link(_BARE_TS, _BARE_TS, "D123")
        linked = mgr.get_session_for_thread(_BARE_TS)
        assert linked == _CANON

        # A live session registered bare is still reachable via that link
        provider1, _, _ = await mgr.get_or_create(_BARE_TS)
        mgr.release(_BARE_TS)
        provider2, is_new2, _ = await mgr.get_or_create(linked)
        assert provider2 is provider1
        assert not is_new2
        mgr.release(linked)
        await mgr.close_all()


class TestConversationLogLegacyFallback:
    """ConversationLog reads/appends the legacy bare-ts file for old threads."""

    def test_canonical_key_falls_back_to_legacy_file(self, tmp_path) -> None:
        log = ConversationLog(base_dir=tmp_path)
        log.init()
        # Pre-migration thread: log file named after the bare thread_ts
        log.append(_BARE_TS, "user", "hello from before the migration")
        legacy_file = tmp_path / f"{_BARE_TS}.jsonl"
        assert legacy_file.exists()

        # Post-migration access under the canonical key hits the same file
        assert log.has_log(_CANON)
        log.append(_CANON, "assistant", "reply after the migration")
        assert not (tmp_path / f"slack_{_BARE_TS}.jsonl").exists()
        assert "reply after the migration" in legacy_file.read_text()

    def test_new_thread_creates_canonical_file(self, tmp_path) -> None:
        log = ConversationLog(base_dir=tmp_path)
        log.init()
        log.append(_CANON, "user", "brand new thread")
        assert (tmp_path / f"slack_{_BARE_TS}.jsonl").exists()
        assert not (tmp_path / f"{_BARE_TS}.jsonl").exists()

    def test_non_slack_keys_unaffected(self, tmp_path) -> None:
        log = ConversationLog(base_dir=tmp_path)
        log.init()
        log.append("dashboard:chat-1-abc", "user", "dashboard message")
        assert (tmp_path / "dashboard_chat-1-abc.jsonl").exists()
