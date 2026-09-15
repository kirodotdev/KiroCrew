"""Coverage for ``kiro_crew.session`` edges the behavioural suites skip.

Three groups, and the seam between them is what this file is for:

* The module-level **probe helpers** (``_provider_has_active_turn`` and its two
  siblings) are the defensive boundary between the manager and a provider
  double. Their contract is "anything that is not exactly ``True`` reads as no",
  which only holds if the missing-attribute, raising, and awaitable-returning
  shapes are each exercised — an awaitable slipping through is what leaks an
  un-awaited-coroutine warning into an unrelated test.
* The **pure resolvers** (``_model_fallback``, ``_session_model``,
  ``detect_provider_switch``, ``_is_continuable_key``) decide what a session
  becomes before any process exists, so they are testable without spawning one.
* The **lifecycle edges** (``remove_if_unclaimed``, ``recycle_heartbeat``,
  ``_expire_idle``, ``_stuck_turn_check``, ``_send_abort_for_session``) are the
  refusal branches: each one's job is to decline to act on a session that is
  busy, persistent, or already claimed. A test that only drives the happy path
  cannot tell a working guard from an absent one.

Every session here is injected directly into the registry as a ``_Session``
over a stub provider, so no test starts a real process, writes outside
``tmp_path``, or opens a socket.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.acp.types import ACP_BACKEND_CLAUDE, ACP_BACKEND_KIRO
from kiro_crew.config import KiroCrewConfig
from kiro_crew.config.loader import KiroCrewAgentConfig
from kiro_crew.messaging.link import ChannelLink
from kiro_crew.session import (
    _MAX_ORIGIN_LINKS,
    _STUCK_TURN_REPORT_SECS,
    BACKGROUND_KEY,
    HEARTBEAT_KEY,
    FirstTurnState,
    SessionManager,
    _context_pct_is_unknown,
    _model_fallback,
    _provider_has_active_turn,
    _provider_has_unfinished_turn,
    _ProviderBgSession,
    _Session,
    _session_model,
    detect_provider_switch,
)


@pytest.fixture
def cfg():
    c = KiroCrewConfig()
    c.session.timeout_secs = 60
    return c


@pytest.fixture
def mgr(cfg):
    """A manager with no provider factory — nothing can cold-start by accident."""
    return SessionManager(cfg)


def _stub_provider(**attrs):
    """A provider double with only the attributes a test names.

    ``MagicMock`` is deliberately avoided for the probe surfaces: an
    auto-generated ``has_active_turn`` returns a truthy Mock, which would make
    the drain filters pass for the wrong reason.
    """
    base = {
        "shutdown": AsyncMock(),
        "context_usage_pct": lambda: 0.0,
    }
    base.update(attrs)
    return SimpleNamespace(**base)


def _register(mgr: SessionManager, key: str, **session_kwargs) -> _Session:
    provider = session_kwargs.pop("provider", None) or _stub_provider()
    sess = _Session(provider=provider, **session_kwargs)
    mgr._sessions[key] = sess
    return sess


# ── Probe helpers ────────────────────────────────────────────────────────────


class _AwaitableWithoutClose:
    """Awaitable that exposes no ``close`` — the guard must not assume one."""

    def __await__(self):
        yield from ()


class _AwaitableWithBrokenClose:
    def __await__(self):
        yield from ()

    def close(self):
        raise RuntimeError("close blew up")


PROBES = [
    (_provider_has_active_turn, "has_active_turn"),
    (_context_pct_is_unknown, "context_usage_unknown"),
    (_provider_has_unfinished_turn, "has_unfinished_turn"),
]


@pytest.mark.parametrize("probe,attr", PROBES, ids=[a for _, a in PROBES])
class TestProviderProbeHelpers:
    """Each probe is a strict opt-in: only an exact ``True`` counts."""

    def test_missing_attribute_reads_as_no(self, probe, attr) -> None:
        assert probe(SimpleNamespace()) is False

    def test_non_callable_attribute_reads_as_no(self, probe, attr) -> None:
        """A stub that sets the name to a plain bool must not be called."""
        assert probe(SimpleNamespace(**{attr: True})) is False

    def test_raising_probe_reads_as_no(self, probe, attr) -> None:
        def boom():
            raise RuntimeError("provider is wedged")

        assert probe(SimpleNamespace(**{attr: boom})) is False

    def test_true_is_the_only_yes(self, probe, attr) -> None:
        assert probe(SimpleNamespace(**{attr: lambda: True})) is True

    def test_truthy_non_bool_is_still_no(self, probe, attr) -> None:
        """``is True``, not truthiness — a Mock return must not read as yes."""
        assert probe(SimpleNamespace(**{attr: lambda: MagicMock()})) is False

    def test_coroutine_returning_double_is_closed_not_awaited(self, probe, attr) -> None:
        """The AsyncMock shape. Left un-closed this emits a RuntimeWarning that
        surfaces in whichever unrelated test happens to run next."""

        async def _coro():  # pragma: no cover — never awaited, only closed
            return True

        assert probe(SimpleNamespace(**{attr: _coro})) is False

    def test_awaitable_without_close_reads_as_no(self, probe, attr) -> None:
        assert probe(SimpleNamespace(**{attr: _AwaitableWithoutClose})) is False

    def test_awaitable_whose_close_raises_reads_as_no(self, probe, attr) -> None:
        """A double that fails on cleanup must not propagate out of a probe."""
        assert probe(SimpleNamespace(**{attr: _AwaitableWithBrokenClose})) is False


# ── Model resolution ─────────────────────────────────────────────────────────


class TestModelFallback:
    def test_agent_pin_defers_to_native_resolution(self) -> None:
        """A per-agent pin returns None so the factory reads the agent JSON."""
        assert _model_fallback("opus", "sonnet") is None

    def test_global_default_applies_when_agent_defers(self) -> None:
        assert _model_fallback("", "sonnet") == "sonnet"

    def test_sentinel_global_defers(self) -> None:
        assert _model_fallback("", "auto") is None

    def test_empty_global_defers(self) -> None:
        assert _model_fallback("", "") is None


class TestSessionModel:
    def test_no_agent_uses_global_default(self, cfg) -> None:
        cfg.agent.model = "sonnet"
        assert _session_model(cfg, None) == "sonnet"

    def test_crew_pin_wins_verbatim(self, cfg) -> None:
        """The factory never sees the crew name, so its pin must be returned
        as-is rather than left for a lower tier to rediscover."""
        cfg.agent.model = "sonnet"
        cfg.agents["research"] = KiroCrewAgentConfig(model="opus")
        assert _session_model(cfg, "research") == "opus"

    def test_crew_inherit_spelling_is_not_a_pin(self, cfg) -> None:
        """``auto`` on the crew tier means inherit, not "pin the default"."""
        cfg.agent.model = "sonnet"
        cfg.agents["research"] = KiroCrewAgentConfig(model="auto")
        assert _session_model(cfg, "research") == "sonnet"

    def test_crew_that_defers_continues_on_its_bound_template(self, cfg, monkeypatch) -> None:
        seen: list[str] = []

        def _resolve(agent: str) -> str:
            seen.append(agent)
            return "haiku-from-template"

        cfg.agent.model = "sonnet"
        cfg.agents["research"] = KiroCrewAgentConfig(model="", kiro_agent="tmpl")
        monkeypatch.setattr(cfg, "_resolve_named_agent_model", _resolve)
        # A template pin returns None: the factory resolves the JSON itself.
        assert _session_model(cfg, "research") is None
        assert seen == ["tmpl"], "the crew's bound template, not the crew name"

    def test_base_agent_name_skips_the_per_agent_lookup(self, cfg, monkeypatch) -> None:
        """``kirocrew`` is the built-in template; globbing the agents dir for it
        would be a disk read with a known-empty answer."""
        calls: list[str] = []
        cfg.agent.model = "sonnet"
        monkeypatch.setattr(
            cfg,
            "_resolve_named_agent_model",
            lambda agent: calls.append(agent) or "",
        )
        assert _session_model(cfg, "kirocrew") == "sonnet"
        assert calls == []


# ── Provider-switch detection ────────────────────────────────────────────────


def _map_stub(provider: str = "", sid: str | None = None):
    return SimpleNamespace(get_provider=lambda key: provider, get=lambda key: sid)


class TestDetectProviderSwitch:
    def test_same_provider_is_not_a_switch(self) -> None:
        assert detect_provider_switch(_map_stub("acp", "sid-1"), "k", "acp") is False

    def test_unset_stored_provider_defaults_to_acp(self) -> None:
        """An entry written before the provider column existed reads as acp."""
        assert detect_provider_switch(_map_stub("", "sid-1"), "k", "acp") is False

    def test_different_provider_without_a_stored_sid_is_not_a_switch(self) -> None:
        """Nothing to discard, so nothing to audit."""
        assert detect_provider_switch(_map_stub("claude_code", None), "k", "acp") is False

    def test_different_provider_with_a_stored_sid_is_a_switch(self) -> None:
        with patch("kiro_crew.session.sel") as sel_mock:
            assert detect_provider_switch(_map_stub("claude_code", "sid-1"), "k", "acp") is True
        call = sel_mock.return_value.log_tool_invocation.call_args.kwargs
        assert call["tool_name"] == "provider_switch_detected"
        assert call["metadata"] == {"stored_provider": "claude_code", "new_provider": "acp"}


# ── _Session record ──────────────────────────────────────────────────────────


class TestAdoptProvider:
    def test_transcript_state_is_reset_but_role_state_survives(self) -> None:
        """Everything reset describes the OLD transcript. ``agent`` and
        ``approval_policy`` describe the session's role, so they must carry."""
        old, new = _stub_provider(), _stub_provider()
        sess = _Session(
            provider=old,
            agent="researcher",
            approval_policy="auto",
            prompt_count=17,
            consecutive_failures=3,
            prev_turn_cancelled=True,
            provider_switch_replay=True,
            needs_context_reinjection=True,
            first_turn=FirstTurnState.RESUMED,
        )

        sess.adopt_provider(new)

        assert sess.provider is new
        assert sess.prompt_count == 0
        assert sess.consecutive_failures == 0
        assert sess.prev_turn_cancelled is False
        assert sess.provider_switch_replay is False
        assert sess.needs_context_reinjection is False
        assert sess.first_turn is FirstTurnState.FRESH, "a replacement is fresh, not resumed"
        assert sess.agent == "researcher"
        assert sess.approval_policy == "auto"

    @pytest.mark.parametrize(
        "start", [FirstTurnState.NOTHING_ARMED, FirstTurnState.FRESH]
    )
    def test_non_resumed_first_turn_states_survive_adoption(self, start) -> None:
        """Only the RESUMED half of the observation is stale on a replacement.
        The production caller recycles a mid-life, already-claimed session
        (NOTHING_ARMED): re-arming it here would make the next turn report
        ``is_new=True`` and re-inject Kiro Crew history onto a live
        transcript — the exact failure shape the one-shot state exists to
        prevent."""
        sess = _Session(provider=_stub_provider(), first_turn=start)

        sess.adopt_provider(_stub_provider())

        assert sess.first_turn is start


# ── _ProviderBgSession ───────────────────────────────────────────────────────


class _StreamingProvider:
    def __init__(self, events=("a", "b"), sid="sid-9"):
        self._events = events
        self._sid = sid
        self.rejected: list[object] = []

    @property
    def session_id(self) -> str:
        return self._sid

    async def stream(self, message: str):
        for e in self._events:
            yield f"{message}:{e}"

    async def reject_tool(self, request_id) -> None:
        self.rejected.append(request_id)


class _SidRaisingProvider(_StreamingProvider):
    @property
    def session_id(self) -> str:
        raise RuntimeError("no native session yet")


class TestProviderBgSession:
    """The non-kiro ``_bg`` adapter. All callers share ONE provider session, so
    the ``Semaphore(1)`` is the only thing serializing them."""

    def test_session_id_is_read_through(self) -> None:
        sess = _Session(provider=_StreamingProvider())
        assert _ProviderBgSession(sess).session_id == "sid-9"

    def test_session_id_swallows_a_provider_error(self) -> None:
        """Reading the id must never break a caller that only wants to log it."""
        sess = _Session(provider=_SidRaisingProvider())
        assert _ProviderBgSession(sess).session_id == ""

    @pytest.mark.asyncio
    async def test_prompt_holds_the_semaphore_and_releases_on_exhaustion(self) -> None:
        sess = _Session(provider=_StreamingProvider())
        handle = _ProviderBgSession(sess)

        seen = []
        async for event in handle.prompt("hi", timeout=1.0):
            assert sess.semaphore.locked(), "the turn must hold the shared lock"
            seen.append(event)

        assert seen == ["hi:a", "hi:b"]
        assert not sess.semaphore.locked(), "released on generator exhaustion"

    @pytest.mark.asyncio
    async def test_destroy_releases_the_shared_semaphore_but_keeps_the_session(self) -> None:
        """The BACKGROUND_KEY session is persistent and shared: destroy must
        release the turn lock without tearing the provider down."""
        provider = _StreamingProvider()
        sess = _Session(provider=provider)
        handle = _ProviderBgSession(sess)
        await sess.semaphore.acquire()
        handle._sem_held = True

        await handle.destroy()

        assert not sess.semaphore.locked()
        assert sess.provider is provider

    @pytest.mark.asyncio
    async def test_destroy_is_idempotent_when_no_turn_is_held(self) -> None:
        """A double release would over-count the semaphore and let two _bg
        callers into the provider at once."""
        sess = _Session(provider=_StreamingProvider())
        handle = _ProviderBgSession(sess)

        await handle.destroy()
        await handle.destroy()

        assert not sess.semaphore.locked()
        await sess.semaphore.acquire()
        assert sess.semaphore.locked(), "the counter was never inflated"

    @pytest.mark.asyncio
    async def test_reject_tool_delegates_to_the_provider(self) -> None:
        provider = _StreamingProvider()
        handle = _ProviderBgSession(_Session(provider=provider))
        await handle.reject_tool("req-1")
        assert provider.rejected == ["req-1"]


class TestGetBgSessionNonKiro:
    @pytest.mark.asyncio
    async def test_non_runtime_backend_gets_the_provider_backed_adapter(self, cfg) -> None:
        # A backend the multiplexed AcpRuntime cannot serve must fall through
        # to the provider path even though agent.provider is still "acp" (a
        # one-member enum) — dispatch is keyed on acp_backend, not provider.
        cfg.agent.acp_backend = ACP_BACKEND_CLAUDE
        mgr = SessionManager(cfg)
        sess = _register(mgr, BACKGROUND_KEY, provider=_StreamingProvider())

        with patch.object(mgr, "_ensure_background", AsyncMock()):
            handle = await mgr.get_bg_session()

        assert isinstance(handle, _ProviderBgSession)
        assert handle._sess is sess

    @pytest.mark.asyncio
    async def test_missing_background_session_is_a_named_error(self, cfg) -> None:
        """Silently returning None here surfaces much later as an
        AttributeError inside a chat-title turn."""
        cfg.agent.acp_backend = ACP_BACKEND_CLAUDE
        mgr = SessionManager(cfg)

        with patch.object(mgr, "_ensure_background", AsyncMock()):
            with pytest.raises(RuntimeError, match="background session unavailable"):
                await mgr.get_bg_session()

    @pytest.mark.asyncio
    async def test_provider_path_retires_a_drained_runtime_left_by_a_switch(self, cfg) -> None:
        """After a switch to a backend the runtime cannot serve, the runtime
        branch is never taken again — so the provider path itself must finish
        a retirement that refresh_defaults deferred while handles were live."""
        cfg.agent.acp_backend = ACP_BACKEND_CLAUDE
        mgr = SessionManager(cfg)
        _register(mgr, BACKGROUND_KEY, provider=_StreamingProvider())

        stranded = AsyncMock()
        stranded.acp_backend = ACP_BACKEND_KIRO  # spawned before the switch
        stranded.has_active_or_initializing_sessions = lambda: False
        stranded.kill = AsyncMock()
        stranded.pid = 555
        mgr._bg_runtime = stranded

        with patch.object(mgr, "_ensure_background", AsyncMock()):
            handle = await mgr.get_bg_session()

        assert isinstance(handle, _ProviderBgSession)
        stranded.kill.assert_awaited_once()
        assert mgr._bg_runtime is None


# ── Registry reads ───────────────────────────────────────────────────────────


class TestTryAcquire:
    @pytest.mark.asyncio
    async def test_unknown_key_is_refused(self, mgr) -> None:
        assert await mgr.try_acquire("nope") is False

    @pytest.mark.asyncio
    async def test_busy_session_is_refused(self, mgr) -> None:
        sess = _register(mgr, "d1")
        await sess.semaphore.acquire()
        assert await mgr.try_acquire("d1") is False

    @pytest.mark.asyncio
    async def test_idle_session_is_taken(self, mgr) -> None:
        """Every True must be paired with release(), so the semaphore is held
        on return — that IS the contract."""
        sess = _register(mgr, "d1")
        assert await mgr.try_acquire("d1") is True
        assert sess.semaphore.locked()
        mgr.release("d1")


class TestSessionAgentLookup:
    def test_unknown_key_is_empty(self, mgr) -> None:
        assert mgr._get_session_agent("nope") == ""

    def test_agentless_session_is_empty_not_none(self, mgr) -> None:
        _register(mgr, "d1")
        assert mgr._get_session_agent("d1") == ""

    def test_agent_is_returned(self, mgr) -> None:
        _register(mgr, "d1", agent="researcher")
        assert mgr._get_session_agent("d1") == "researcher"


class TestParentRuntimeKwargs:
    """A companion subagent runtime must inherit the parent's security posture,
    never spawn as a bare unsandboxed process."""

    def test_unknown_parent_yields_no_kwargs(self, mgr) -> None:
        assert mgr._parent_runtime_kwargs("nope") == {}

    def test_provider_without_a_client_yields_no_kwargs(self, mgr) -> None:
        _register(mgr, "d1", provider=_stub_provider())
        assert mgr._parent_runtime_kwargs("d1") == {}

    def test_posture_is_copied_from_the_private_client(self, mgr) -> None:
        client = SimpleNamespace(
            _sandbox_mode="strict",
            _extra_env={"A": "1"},
            _mcp_gateway_overlay={"servers": {}},
            _mcp_gateway_socket="/tmp/gw.sock",
        )
        _register(mgr, "d1", provider=_stub_provider(client=client))

        assert mgr._parent_runtime_kwargs("d1") == {
            "sandbox_mode": "strict",
            "extra_env": {"A": "1"},
            "mcp_gateway_overlay": {"servers": {}},
            "mcp_gateway_socket": "/tmp/gw.sock",
        }

    def test_unset_posture_fields_are_omitted_not_nulled(self, mgr) -> None:
        """A None must not be forwarded: the runtime would read it as an
        explicit "no sandbox" rather than "inherit the default"."""
        client = SimpleNamespace(_sandbox_mode="strict", _extra_env=None)
        _register(mgr, "d1", provider=_stub_provider(client=client))
        assert mgr._parent_runtime_kwargs("d1") == {"sandbox_mode": "strict"}

    def test_legacy_underscore_client_attribute_is_honoured(self, mgr) -> None:
        client = SimpleNamespace(_sandbox_mode="off")
        provider = _stub_provider()
        provider._client = client
        _register(mgr, "d1", provider=provider)
        assert mgr._parent_runtime_kwargs("d1") == {"sandbox_mode": "off"}


class TestSessionSharingEligible:
    def test_unknown_parent_is_ineligible(self, mgr) -> None:
        assert mgr.is_session_sharing_eligible("nope") is False

    def test_provider_that_does_not_advertise_the_capability_is_ineligible(self, mgr) -> None:
        _register(mgr, "d1", provider=_stub_provider())
        assert mgr.is_session_sharing_eligible("d1") is False

    def test_kiro_backed_provider_is_eligible(self, mgr) -> None:
        _register(mgr, "d1", provider=_stub_provider(is_session_sharing_eligible=True))
        assert mgr.is_session_sharing_eligible("d1") is True


class TestContinuableKeys:
    def test_cache_hit_needs_no_disk_read(self, mgr) -> None:
        mgr._continuable_keys.add("subagent:a")
        mgr._continuable_fallback = MagicMock(
            side_effect=AssertionError("must not consult disk")
        )
        assert mgr._is_continuable_key("subagent:a") is True

    def test_miss_without_a_fallback_is_stateless(self, mgr) -> None:
        assert mgr._is_continuable_key("subagent:a") is False

    def test_disk_hit_rewarms_the_cache(self, mgr) -> None:
        """A gateway restart empties the cache; the disk answer must be
        promoted so a continuable conversation is not demoted to stateless."""
        mgr.set_continuable_fallback(lambda folded: folded == "subagent:a")

        assert mgr._is_continuable_key("subagent:a") is True
        assert "subagent:a" in mgr._continuable_keys
        assert mgr.is_continuable("subagent:b") is False

    def test_a_broken_fallback_reads_as_stateless(self, mgr) -> None:
        def boom(folded: str) -> bool:
            raise OSError("state.json is unreadable")

        mgr.set_continuable_fallback(boom)
        assert mgr._is_continuable_key("subagent:a") is False
        assert "subagent:a" not in mgr._continuable_keys


# ── Origin links ─────────────────────────────────────────────────────────────


class TestOriginLinks:
    def test_link_round_trips(self, mgr) -> None:
        link = ChannelLink(channel_type="slack", channel_id="C1", thread_id="1.2")
        mgr.set_origin_link("slack:1.2", link)
        assert mgr.get_origin_link("slack:1.2") is link

    def test_unknown_key_has_no_link(self, mgr) -> None:
        assert mgr.get_origin_link("nope") is None

    def test_the_map_is_bounded_fifo(self, mgr) -> None:
        """Sessions dropped without reset()/remove() would otherwise grow this
        map without limit for the life of the gateway."""
        for i in range(_MAX_ORIGIN_LINKS + 5):
            mgr.set_origin_link(f"k{i}", ChannelLink(channel_type="slack", channel_id=f"C{i}"))

        assert len(mgr._origin_links) == _MAX_ORIGIN_LINKS
        assert mgr.get_origin_link("k0") is None, "oldest evicted first"
        assert mgr.get_origin_link(f"k{_MAX_ORIGIN_LINKS + 4}") is not None


# ── Lifecycle refusals ──────────────────────────────────────────────────────


class TestRemoveIfUnclaimed:
    """The TTL backstop for resume prefetch. A speculative session holds
    kiro-cli's native per-session lock, so an unclaimed one must be released —
    but a claimed one must survive."""

    @pytest.mark.asyncio
    async def test_unknown_key_removes_nothing(self, mgr) -> None:
        assert await mgr.remove_if_unclaimed("nope") is False

    @pytest.mark.asyncio
    async def test_a_consumed_session_is_kept(self, mgr) -> None:
        sess = _register(mgr, "dashboard:1", first_turn=FirstTurnState.NOTHING_ARMED)
        assert await mgr.remove_if_unclaimed("dashboard:1") is False
        assert mgr._sessions["dashboard:1"] is sess
        sess.provider.shutdown.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_session_mid_turn_is_kept(self, mgr) -> None:
        sess = _register(mgr, "dashboard:1", first_turn=FirstTurnState.FRESH)
        await sess.semaphore.acquire()
        assert await mgr.remove_if_unclaimed("dashboard:1") is False
        assert "dashboard:1" in mgr._sessions

    @pytest.mark.asyncio
    async def test_an_unclaimed_session_is_shut_down_and_forgotten(self, mgr) -> None:
        sess = _register(mgr, "dashboard:1", first_turn=FirstTurnState.FRESH)
        mgr._compact_cooldown_until["dashboard:1"] = 123.0
        mgr.set_origin_link("dashboard:1", ChannelLink(channel_type="dashboard"))

        assert await mgr.remove_if_unclaimed("dashboard:1") is True

        assert "dashboard:1" not in mgr._sessions
        assert "dashboard:1" not in mgr._compact_cooldown_until
        assert mgr.get_origin_link("dashboard:1") is None
        sess.provider.shutdown.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_an_unclaimed_session_unlinks_its_queued_temp_files(
        self, mgr, tmp_path
    ) -> None:
        img = tmp_path / "img.png"
        img.write_bytes(b"fake")
        sess = _register(mgr, "dashboard:1", first_turn=FirstTurnState.FRESH)
        sess.queue.append(("ts1", "queued", {"image_temp_paths": [str(img)]}))

        assert await mgr.remove_if_unclaimed("dashboard:1") is True

        assert not img.exists()


class TestRecycleHeartbeat:
    @pytest.mark.asyncio
    async def test_no_heartbeat_session_is_a_noop(self, mgr) -> None:
        await mgr.recycle_heartbeat()
        assert HEARTBEAT_KEY not in mgr._sessions

    @pytest.mark.asyncio
    async def test_recycle_is_unconditional(self, mgr) -> None:
        """Heartbeat's published contract is a fresh context each cycle, so the
        teardown must not be gated on context usage the way recycle_background
        is."""
        sess = _register(mgr, HEARTBEAT_KEY, provider=_stub_provider(context_usage_pct=lambda: 1.0))

        await mgr.recycle_heartbeat()

        assert HEARTBEAT_KEY not in mgr._sessions
        sess.provider.shutdown.assert_awaited_once()


class TestExpireIdle:
    @pytest.mark.asyncio
    async def test_persistent_and_channel_sessions_are_never_swept(self, mgr) -> None:
        for key in (BACKGROUND_KEY, HEARTBEAT_KEY, "channel:slack:C1"):
            _register(mgr, key, last_used=0.0)
        with patch.object(mgr, "reset", AsyncMock(return_value=True)) as reset:
            await mgr._expire_idle(1)
        reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_turn_in_flight_is_never_idle(self, mgr) -> None:
        """``last_used`` is bumped once per turn, so a turn running longer than
        the timeout looks idle to the arithmetic — the semaphore is the guard."""
        sess = _register(mgr, "dashboard:1", last_used=0.0)
        await sess.semaphore.acquire()
        with patch.object(mgr, "reset", AsyncMock(return_value=True)) as reset:
            await mgr._expire_idle(1)
        reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_idle_session_is_reset_not_removed(self, mgr) -> None:
        """reset() preserves the session-map entry so the next open can
        session/load the transcript back."""
        _register(mgr, "dashboard:1", last_used=0.0)
        with patch.object(mgr, "reset", AsyncMock(return_value=True)) as reset:
            await mgr._expire_idle(1)
        reset.assert_awaited_once_with("dashboard:1", skip_if_busy=True)

    @pytest.mark.asyncio
    async def test_an_orphaned_dashboard_session_ignores_the_clock(self, mgr) -> None:
        """A closed tab is reaped immediately; the slot set is the authority."""
        _register(mgr, "dashboard:gone")
        mgr._active_dashboard_slots = {"dashboard:live"}
        _register(mgr, "dashboard:live")

        with patch.object(mgr, "reset", AsyncMock(return_value=True)) as reset:
            await mgr._expire_idle(10_000)

        assert [c.args[0] for c in reset.await_args_list] == ["dashboard:gone"]

    @pytest.mark.asyncio
    async def test_uninitialised_slot_set_orphans_nothing(self, mgr) -> None:
        """None means "not yet reported", which must not read as "all tabs
        closed" — that would reap every dashboard session on startup."""
        _register(mgr, "dashboard:1")
        assert mgr._active_dashboard_slots is None
        with patch.object(mgr, "reset", AsyncMock(return_value=True)) as reset:
            await mgr._expire_idle(10_000)
        reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_session_that_turns_busy_before_reset_is_left_running(self, mgr) -> None:
        """The collect->reset window: reset() re-checks atomically and returns
        False, and the sweep must accept that rather than retry or raise."""
        _register(mgr, "dashboard:1", last_used=0.0)
        with patch.object(mgr, "reset", AsyncMock(return_value=False)):
            await mgr._expire_idle(1)
        # No exception, and the entry was left for the next pass to reconsider.

    @pytest.mark.asyncio
    async def test_a_broken_expire_callback_does_not_stop_the_sweep(self, mgr) -> None:
        _register(mgr, "dashboard:1", last_used=0.0)
        mgr.on_session_expire = MagicMock(side_effect=RuntimeError("consolidator down"))
        with patch.object(mgr, "reset", AsyncMock(return_value=True)) as reset:
            await mgr._expire_idle(1)
        mgr.on_session_expire.assert_called_once_with("dashboard:1")
        reset.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_the_hook_honours_the_disable_gate(self, mgr) -> None:
        _register(mgr, "dashboard:1", last_used=0.0)
        mgr._idle_sweep_enabled = False
        with patch.object(mgr, "_expire_idle", AsyncMock()) as expire:
            await mgr._expire_idle_hook()
        expire.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_hook_swallows_a_sweep_crash(self, mgr) -> None:
        mgr._idle_sweep_enabled = True
        mgr._idle_timeout = 60
        with patch.object(mgr, "_expire_idle", AsyncMock(side_effect=RuntimeError("boom"))):
            await mgr._expire_idle_hook()  # must not propagate into the loop


# ── Stuck-turn observer ─────────────────────────────────────────────────────


def _parked_provider(parked: float, *, since: object = 1.0, awaiting: bool = False):
    handle = SimpleNamespace(
        parked_for_secs=lambda: parked,
        parked_since=since,
        awaiting_permission=awaiting,
    )
    provider = _stub_provider()
    provider._handle = handle
    return provider


async def _busy(mgr: SessionManager, key: str, provider) -> _Session:
    sess = _register(mgr, key, provider=provider)
    await sess.semaphore.acquire()
    return sess


class TestStuckTurnCheck:
    """Detection only — the hook reports and never terminates."""

    @pytest.mark.asyncio
    async def test_an_idle_session_is_not_a_park(self, mgr) -> None:
        _register(mgr, "d1", provider=_parked_provider(_STUCK_TURN_REPORT_SECS + 10))
        mgr.on_stuck_turn = MagicMock()
        await mgr._stuck_turn_check()
        mgr.on_stuck_turn.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_provider_without_a_handle_is_skipped(self, mgr) -> None:
        await _busy(mgr, "d1", _stub_provider())
        mgr.on_stuck_turn = MagicMock()
        await mgr._stuck_turn_check()
        mgr.on_stuck_turn.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_handle_without_the_accessor_is_skipped(self, mgr) -> None:
        """Duck-typed on the capability, so a transport that lacks it is
        ignored rather than crashing the cleanup pass."""
        provider = _stub_provider()
        provider._handle = SimpleNamespace(parked_for_secs=None)
        await _busy(mgr, "d1", provider)
        mgr.on_stuck_turn = MagicMock()
        await mgr._stuck_turn_check()
        mgr.on_stuck_turn.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_short_park_is_below_the_report_threshold(self, mgr) -> None:
        await _busy(mgr, "d1", _parked_provider(1.0))
        mgr.on_stuck_turn = MagicMock()
        await mgr._stuck_turn_check()
        mgr.on_stuck_turn.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_turn_waiting_for_a_human_is_excluded(self, mgr) -> None:
        """That wait has its own budget (tool_approval_timeout_secs); reporting
        it here would put two components on different clocks."""
        await _busy(
            mgr, "d1", _parked_provider(_STUCK_TURN_REPORT_SECS + 10, awaiting=True)
        )
        mgr.on_stuck_turn = MagicMock()
        await mgr._stuck_turn_check()
        mgr.on_stuck_turn.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_long_park_is_reported_once_per_park(self, mgr) -> None:
        parked = _STUCK_TURN_REPORT_SECS + 42
        await _busy(mgr, "d1", _parked_provider(parked, since=1000.0))
        mgr.on_stuck_turn = MagicMock()

        await mgr._stuck_turn_check()
        await mgr._stuck_turn_check()

        mgr.on_stuck_turn.assert_called_once_with("d1", parked)
        assert mgr._stuck_reported == {"d1": 1000.0}

    @pytest.mark.asyncio
    async def test_a_new_park_on_the_same_session_reports_afresh(self, mgr) -> None:
        """Latched on the park's identity, not the session, so a stale entry
        cannot silence a later stall."""
        provider = _parked_provider(_STUCK_TURN_REPORT_SECS + 5, since=1000.0)
        await _busy(mgr, "d1", provider)
        mgr.on_stuck_turn = MagicMock()
        await mgr._stuck_turn_check()

        provider._handle.parked_since = 2000.0
        await mgr._stuck_turn_check()

        assert mgr.on_stuck_turn.call_count == 2
        assert mgr._stuck_reported == {"d1": 2000.0}

    @pytest.mark.asyncio
    async def test_a_handle_without_a_park_identity_is_reported_per_tick(self, mgr) -> None:
        """A missed stall is worse than a repeated line, so a handle exposing
        the duration but not the instant falls back to the duration."""
        parked = _STUCK_TURN_REPORT_SECS + 7
        await _busy(mgr, "d1", _parked_provider(parked, since=None))
        mgr.on_stuck_turn = MagicMock()

        await mgr._stuck_turn_check()
        assert mgr._stuck_reported == {"d1": parked}
        await mgr._stuck_turn_check()
        mgr.on_stuck_turn.assert_called_once()

    @pytest.mark.asyncio
    async def test_the_latch_is_dropped_when_the_park_ends(self, mgr) -> None:
        sess = await _busy(mgr, "d1", _parked_provider(_STUCK_TURN_REPORT_SECS + 5))
        mgr.on_stuck_turn = MagicMock()
        await mgr._stuck_turn_check()
        assert mgr._stuck_reported

        sess.semaphore.release()
        await mgr._stuck_turn_check()

        assert mgr._stuck_reported == {}

    @pytest.mark.asyncio
    async def test_a_broken_callback_does_not_break_the_cleanup_pass(self, mgr) -> None:
        await _busy(mgr, "d1", _parked_provider(_STUCK_TURN_REPORT_SECS + 5))
        mgr.on_stuck_turn = MagicMock(side_effect=RuntimeError("notifier down"))
        await mgr._stuck_turn_check()  # an observer must never break its host

    @pytest.mark.asyncio
    async def test_the_observer_swallows_its_own_crash(self, mgr, caplog) -> None:
        provider = _stub_provider()
        provider._handle = SimpleNamespace(
            parked_for_secs=lambda: "not-a-number",
            parked_since=1.0,
            awaiting_permission=False,
        )
        await _busy(mgr, "d1", provider)
        with caplog.at_level(logging.ERROR, logger="kiro_crew.session"):
            await mgr._stuck_turn_check()
        assert any("_stuck_turn_check crashed" in r.message for r in caplog.records)


# ── Abort push ───────────────────────────────────────────────────────────────


class TestSendAbortForSession:
    @pytest.mark.asyncio
    async def test_runtime_info_drives_the_abort(self, mgr) -> None:
        sess = _Session(provider=_stub_provider(runtime_info=lambda: (4242, "/tmp/gw.sock")))
        with patch("kiro_crew.session.schedule_abort") as abort:
            await mgr._send_abort_for_session("d1", sess)
        abort.assert_called_once()
        assert abort.call_args.args[0] == "/tmp/gw.sock"
        assert abort.call_args.args[1] == [4242]

    @pytest.mark.asyncio
    async def test_private_client_fields_are_the_fallback(self, mgr) -> None:
        """For providers that never overrode runtime_info()."""
        provider = _stub_provider(runtime_info=lambda: (None, None))
        provider._client = SimpleNamespace(_pid=99, _mcp_gateway_socket="/tmp/b.sock")
        with patch("kiro_crew.session.schedule_abort") as abort:
            await mgr._send_abort_for_session("d1", _Session(provider=provider))
        assert abort.call_args.args[1] == [99]

    @pytest.mark.asyncio
    async def test_an_unresolvable_runtime_warns_rather_than_failing_silently(
        self, mgr, caplog
    ) -> None:
        """Visible by default: if provider internals get renamed the abort push
        stops firing, and a silent skip would hide the regression."""
        provider = _stub_provider(runtime_info=lambda: (None, None))
        with caplog.at_level(logging.WARNING, logger="kiro_crew.session"):
            with patch("kiro_crew.session.schedule_abort") as abort:
                await mgr._send_abort_for_session("d1", _Session(provider=provider))
        abort.assert_not_called()
        assert any("abort-push skipped" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_a_reaped_pid_is_not_aborted(self, mgr) -> None:
        """pid<=1 would target init, not a kiro-cli process."""
        sess = _Session(provider=_stub_provider(runtime_info=lambda: (1, "/tmp/gw.sock")))
        with patch("kiro_crew.session.schedule_abort") as abort:
            await mgr._send_abort_for_session("d1", sess)
        abort.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_raising_provider_never_blocks_the_kill_path(self, mgr) -> None:
        def boom():
            raise RuntimeError("provider is gone")

        sess = _Session(provider=_stub_provider(runtime_info=boom))
        await mgr._send_abort_for_session("d1", sess)  # best-effort, must not raise

    @pytest.mark.asyncio
    async def test_a_failing_audit_does_not_block_the_abort(self, mgr) -> None:
        sess = _Session(provider=_stub_provider(runtime_info=lambda: (7, "/tmp/gw.sock")))
        with patch("kiro_crew.session.sel", side_effect=RuntimeError("sel down")):
            with patch("kiro_crew.session.schedule_abort") as abort:
                await mgr._send_abort_for_session("d1", sess)
        abort.assert_called_once()


# ── Orphan-MCP hook ─────────────────────────────────────────────────────────


class TestOrphanMcpHook:
    @pytest.mark.asyncio
    async def test_a_sweep_result_is_logged(self, mgr, caplog) -> None:
        with patch("kiro_crew.session._cleanup_orphaned_mcp_servers", return_value=3):
            with caplog.at_level(logging.INFO, logger="kiro_crew.session"):
                await mgr._orphan_mcp_hook()
        assert any("cleaned 3 orphaned MCP servers" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_a_clean_sweep_logs_nothing(self, mgr, caplog) -> None:
        with patch("kiro_crew.session._cleanup_orphaned_mcp_servers", return_value=0):
            with caplog.at_level(logging.INFO, logger="kiro_crew.session"):
                await mgr._orphan_mcp_hook()
        assert not any("orphaned MCP servers" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_a_sweep_failure_is_swallowed(self, mgr) -> None:
        with patch(
            "kiro_crew.session._cleanup_orphaned_mcp_servers",
            side_effect=OSError("procfs unreadable"),
        ):
            await mgr._orphan_mcp_hook()  # preserves the silent-swallow contract


# ── Conversation map seams ──────────────────────────────────────────────────


class _MapSpy:
    def __init__(self, sid: str | None = None, provider: str = "") -> None:
        self.entries: dict[str, tuple[str, str, str]] = {}
        self.deleted: list[str] = []
        self._sid = sid
        self._provider = provider

    def set(self, key: str, sid: str, *, provider: str = "", cwd: str = "") -> None:
        self.entries[key] = (sid, provider, cwd)

    def get(self, key: str) -> str | None:
        return self._sid

    def get_provider(self, key: str) -> str:
        return self._provider

    def delete(self, key: str) -> None:
        self.deleted.append(key)


class TestConversationMapSeams:
    def test_seeding_without_a_sid_writes_nothing(self, mgr) -> None:
        """Retain-by-default: a full-file rewrite per spawn is O(n) churn at
        wave scale, so an empty sid must not touch the map."""
        spy = _MapSpy()
        mgr._session_map = spy
        mgr.seed_conversation("subagent:a", "")
        assert spy.entries == {}

    def test_seeding_records_provider_and_cwd(self, mgr) -> None:
        spy = _MapSpy()
        mgr._session_map = spy
        mgr.seed_conversation("subagent:a", "sid-1", provider="acp", cwd="/tmp/wt")
        assert spy.entries == {"subagent:a": ("sid-1", "acp", "/tmp/wt")}

    def test_forgetting_returns_the_sid_and_clears_the_mark(self, mgr) -> None:
        mgr._session_map = _MapSpy(sid="sid-1")
        mgr._continuable_keys.add("subagent:a")

        assert mgr.forget_conversation("subagent:a") == "sid-1"

        assert mgr._session_map.deleted == ["subagent:a"]
        assert "subagent:a" not in mgr._continuable_keys

    def test_forgetting_an_unmapped_key_returns_none(self, mgr) -> None:
        mgr._session_map = _MapSpy(sid=None)
        assert mgr.forget_conversation("subagent:a") is None

    def test_resumable_sid_and_provider_read_through(self, mgr) -> None:
        mgr._session_map = _MapSpy(sid="sid-1", provider="claude_code")
        assert mgr.resumable_sid("subagent:a") == "sid-1"
        assert mgr.conversation_provider("subagent:a") == "claude_code"


# ── Registry snapshots ──────────────────────────────────────────────────────


class TestRegistrySnapshots:
    def test_active_providers_lists_every_live_backend(self, mgr) -> None:
        a = _register(mgr, "d1").provider
        b = _register(mgr, "d2").provider
        assert set(map(id, mgr.active_providers())) == {id(a), id(b)}

    def test_any_active_turn_filters_on_the_real_turn_signal(self, mgr) -> None:
        """A provider that does not implement the probe contributes nothing,
        rather than a false positive that would keep the host awake."""
        _register(mgr, "d1", provider=_stub_provider())
        assert mgr.any_active_turn() is False

        _register(mgr, "d2", provider=_stub_provider(has_active_turn=lambda: True))
        assert mgr.any_active_turn() is True

    def test_get_pid_reads_the_client(self, mgr) -> None:
        provider = _stub_provider()
        provider.client = SimpleNamespace(_pid=555)
        _register(mgr, "d1", provider=provider)
        assert mgr.get_pid("d1") == 555

    def test_get_pid_is_none_for_an_unknown_key(self, mgr) -> None:
        assert mgr.get_pid("nope") is None

    def test_get_pid_is_none_when_the_provider_has_no_client(self, mgr) -> None:
        _register(mgr, "d1", provider=_stub_provider())
        assert mgr.get_pid("d1") is None

    def test_get_provider_is_none_for_an_unknown_key(self, mgr) -> None:
        assert mgr.get_provider("nope") is None
