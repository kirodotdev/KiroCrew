"""Refusal, retry and teardown edges of ``kiro_crew.session`` the suites skip.

The behavioural session suites drive happy paths: a session is created, used,
reset. What they leave untested is the half of ``SessionManager`` that exists
only for things going wrong, and each of those branches is load-bearing:

* **Queue-race guards.** Six drain loops read ``_warm_pool`` with
  ``while not empty(): get_nowait()``. The ``except asyncio.QueueEmpty: break``
  arm is the only thing standing between a concurrent claim and an exception
  escaping a shutdown or config-reload path. It is driven here with a queue
  double that reports non-empty and then refuses to yield — the exact shape a
  racing ``_drain_and_claim`` produces.
* **Teardown that must not raise.** ``close_all``, ``reset`` and
  ``_discard_pool_provider`` each swallow failures from a provider, a runtime
  kill, a child sweep. A swallowed failure is indistinguishable from success
  unless a test makes the failure happen, so every one of those handlers is
  exercised with the underlying call raising.
* **Respawn retries.** ``get_bg_session`` re-spawns a dead ``_bg`` runtime once
  and then gives up. Both halves matter: retrying forever wedges a background
  caller, and not retrying at all drops chat titles on a runtime that died.
* **Delegation surface.** The ``_session_map`` wrappers are one-liners, which is
  why they are worth pinning: a wrong argument order or a dropped keyword is
  invisible at the call site and silently loses a mirror binding.

No test here starts a process, touches the OS sandbox, shells out to git, or
writes outside ``tmp_path``: every provider, runtime and queue is a double, and
the three functions that would reach the OS (``platform_compat.pid_exists``,
``acp.client._get_child_pids``, ``_sync_kill_provider``) are patched at the
chokepoint the product actually calls.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew import platform_compat
from kiro_crew.config import KiroCrewConfig
from kiro_crew.messaging.link import ChannelLink
from kiro_crew.session import (
    BACKGROUND_KEY,
    SessionManager,
    _Session,
)


@pytest.fixture
def cfg():
    c = KiroCrewConfig()
    c.session.timeout_secs = 60
    c.session.pool_size = 0
    return c


@pytest.fixture
def mgr(cfg):
    """A manager with no provider factory — nothing can cold-start by accident."""
    return SessionManager(cfg)


def _provider(**attrs):
    """A provider double carrying only the attributes a test names.

    ``SimpleNamespace`` rather than ``MagicMock``: the probe helpers
    (``_provider_has_unfinished_turn`` and friends) accept only an exact
    ``True``, and an auto-generated attribute would make a filter pass for the
    wrong reason.
    """
    base = {
        "shutdown": AsyncMock(),
        "context_usage_pct": lambda: 0.0,
        "cwd": "",
        "is_process_alive": lambda: True,
    }
    base.update(attrs)
    return SimpleNamespace(**base)


def _register(mgr: SessionManager, key: str, **kwargs) -> _Session:
    provider = kwargs.pop("provider", None) or _provider()
    sess = _Session(provider=provider, **kwargs)
    mgr._sessions[key] = sess
    return sess


class _RacedQueue:
    """Queue double that reports non-empty and then refuses to yield.

    Models the window a concurrent ``_drain_and_claim`` opens: the drain loops
    check ``empty()`` and call ``get_nowait()`` without holding a lock, so the
    entry can be gone by the time they ask for it.
    """

    def __init__(self, qsize: int = 1) -> None:
        self._qsize = qsize

    def empty(self) -> bool:
        return False

    def qsize(self) -> int:
        return self._qsize

    def get_nowait(self):
        raise asyncio.QueueEmpty

    def put_nowait(self, item) -> None:  # pragma: no cover — nothing was taken
        raise AssertionError("nothing was claimed, so nothing can be returned")


class _Bang(BaseException):
    """A BaseException that is NOT an Exception — the arm below ``except Exception``."""


# ── Session-map delegation ───────────────────────────────────────────────────


class TestSessionMapDelegation:
    """The thin wrappers must forward the caller's arguments verbatim.

    Each one is a single ``return self._session_map.X(...)``, so the only thing
    that can break is the plumbing: a dropped keyword or a swapped positional
    silently loses a link binding instead of failing loudly.
    """

    @pytest.fixture
    def smap(self, mgr):
        fake = MagicMock()
        mgr._session_map = fake
        return fake

    def test_resumable_hint_folds_the_key_before_asking(self, mgr, smap) -> None:
        _register(mgr, "slack:1.2")
        smap.has_hint.return_value = True
        assert mgr.resumable_hint("1.2") is True
        # The bare thread_ts folded onto the live canonical entry.
        smap.has_hint.assert_called_once_with("slack:1.2")

    def test_clear_slack_link_returns_the_maps_verdict(self, mgr, smap) -> None:
        smap.clear_slack_link.return_value = False
        assert mgr.clear_slack_link("dashboard:a") is False
        smap.clear_slack_link.assert_called_once_with("dashboard:a")

    def test_channel_key_for_stem_passes_the_stem_through(self, mgr, smap) -> None:
        smap.channel_key_for_stem.return_value = "channel:slack_C1"
        assert mgr.channel_key_for_stem("channel_slack_C1") == "channel:slack_C1"
        smap.channel_key_for_stem.assert_called_once_with("channel_slack_C1")

    def test_set_mirror_link_forwards_accepts_inbound_as_a_keyword(self, mgr, smap) -> None:
        link = ChannelLink(channel_type="discord", channel_id="C9")
        mgr.set_mirror_link("dashboard:a", link, accepts_inbound=True)
        smap.set_mirror_link.assert_called_once_with(
            "dashboard:a", link, accepts_inbound=True
        )

    def test_get_mirror_link_returns_the_stored_link(self, mgr, smap) -> None:
        link = ChannelLink(channel_type="discord", channel_id="C9")
        smap.get_mirror_link.return_value = link
        assert mgr.get_mirror_link("dashboard:a") is link

    def test_mirror_accepts_inbound_is_the_maps_answer(self, mgr, smap) -> None:
        smap.mirror_accepts_inbound.return_value = True
        assert mgr.mirror_accepts_inbound("dashboard:a") is True
        smap.mirror_accepts_inbound.assert_called_once_with("dashboard:a")

    def test_batched_save_hands_back_the_maps_context_manager(self, mgr, smap) -> None:
        sentinel = MagicMock()
        smap.batched_save.return_value = sentinel
        assert mgr.batched_save() is sentinel

    def test_find_mirror_sessions_forwards_inbound_only(self, mgr, smap) -> None:
        link = ChannelLink(channel_type="discord", channel_id="C9")
        smap.find_mirror_sessions.return_value = ["dashboard:a"]
        assert mgr.find_mirror_sessions(link, inbound_only=True) == ["dashboard:a"]
        smap.find_mirror_sessions.assert_called_once_with(link, inbound_only=True)

    def test_mirror_claim_blockers_forwards_key_link_and_flag(self, mgr, smap) -> None:
        link = ChannelLink(channel_type="discord", channel_id="C9")
        smap.mirror_claim_blockers.return_value = ["dashboard:b"]
        assert mgr.mirror_claim_blockers("dashboard:a", link, accepts_inbound=True) == [
            "dashboard:b"
        ]
        smap.mirror_claim_blockers.assert_called_once_with(
            "dashboard:a", link, accepts_inbound=True
        )

    def test_clear_mirror_link_returns_whether_one_was_present(self, mgr, smap) -> None:
        smap.clear_mirror_link.return_value = True
        assert mgr.clear_mirror_link("dashboard:a") is True

    def test_clear_mirror_links_at_returns_the_cleared_keys(self, mgr, smap) -> None:
        link = ChannelLink(channel_type="discord", channel_id="C9")
        smap.clear_mirror_links_at.return_value = ["dashboard:a", "dashboard:b"]
        assert mgr.clear_mirror_links_at(link) == ["dashboard:a", "dashboard:b"]

    def test_max_generation_returns_the_persisted_high_water_mark(self, mgr, smap) -> None:
        smap.max_generation.return_value = 7
        assert mgr.max_generation("unified:kirocrew") == 7
        smap.max_generation.assert_called_once_with("unified:kirocrew")


class TestMirrorOptOut:
    def test_a_key_with_no_generation_suffix_has_no_legacy_row_to_check(self, mgr) -> None:
        """When bucket and generation key are the SAME string there is no
        legacy row, so the read must answer False without a promoting write."""
        with patch.object(mgr._session_map, "batched_save") as batched:
            assert mgr.mirror_opt_out("dashboard:abc") is False
        batched.assert_not_called()

    def test_a_refusal_stored_on_the_bucket_is_honoured(self, mgr) -> None:
        mgr.set_mirror_opt_out("dashboard:abc", True)
        assert mgr.mirror_opt_out("dashboard:abc") is True


# ── Cancel / callback registration ───────────────────────────────────────────


class TestCancelAndCallbacks:
    @pytest.mark.asyncio
    async def test_cancel_current_on_an_unknown_key_reports_no_turn(self, mgr) -> None:
        assert await mgr.cancel_current("dashboard:ghost") == "no_turn"

    def test_replacing_a_recycle_callback_warns(self, mgr, caplog) -> None:
        async def cb(key, *, reason):  # pragma: no cover — never invoked
            return None

        mgr.set_recycle_callback(cb)
        with caplog.at_level("WARNING", logger="kiro_crew.session"):
            mgr.set_recycle_callback(cb)
        assert "Recycle callback already registered" in caplog.text
        assert mgr._on_recycled is cb

    @pytest.mark.asyncio
    async def test_a_raising_recycle_callback_never_escapes(self, mgr) -> None:
        mgr.set_recycle_callback(AsyncMock(side_effect=RuntimeError("boom")))
        await mgr._fire_recycle_callback("dashboard:a", reason="rss")


# ── context_info / _resolve_agent_model ──────────────────────────────────────


class TestContextInfoModelResolution:
    @pytest.mark.asyncio
    async def test_an_auto_model_on_a_named_agent_is_resolved_from_agent_json(
        self, mgr
    ) -> None:
        """``client._model == "auto"`` is not a model the dashboard can show, so
        a named (non-``kirocrew``) agent falls through to its JSON pin."""
        from kiro_crew.providers.acp import AcpProvider

        provider = MagicMock(spec=AcpProvider)
        provider.context_usage_pct = MagicMock(return_value=12.0)
        provider.context_window_tokens = MagicMock(return_value=200_000)
        provider.shutdown = AsyncMock()
        provider.client = MagicMock()
        provider.client._model = "auto"
        provider.client._agent = "researcher"
        _register(mgr, "dashboard:slot1", provider=provider)

        with patch.object(
            SessionManager, "_resolve_agent_model", staticmethod(lambda a: "sonnet-9")
        ):
            info = mgr.context_info()

        assert info[0]["model"] == "sonnet-9"
        assert info[0]["agent"] == "researcher"


class TestResolveAgentModel:
    @pytest.fixture(autouse=True)
    def _clear_class_cache(self):
        SessionManager._agent_model_cache = {}
        yield
        SessionManager._agent_model_cache = {}

    def test_a_missing_agents_dir_resolves_to_auto(self, tmp_path) -> None:
        """``stat()`` on an absent dir raises OSError; mtime 0.0 must be used as
        the cache stamp rather than the lookup blowing up."""
        missing = tmp_path / "nope"
        with patch("kiro_crew.agent.kiro_agents_dir_path", return_value=missing):
            assert SessionManager._resolve_agent_model("researcher") == "auto"
        assert SessionManager._agent_model_cache["researcher"][1] == 0.0

    def test_an_unparsable_agent_file_is_skipped_not_fatal(self, tmp_path) -> None:
        """A hand-edited agent JSON with a syntax error must not take the whole
        resolver down — every other agent's model would stop resolving."""
        agents = tmp_path / "agents"
        agents.mkdir()
        (agents / "researcher.json").write_text("{not json", encoding="utf-8")
        with patch("kiro_crew.agent.kiro_agents_dir_path", return_value=agents):
            assert SessionManager._resolve_agent_model("researcher") == "auto"

    def test_a_readable_agent_file_supplies_its_pinned_model(self, tmp_path) -> None:
        agents = tmp_path / "agents"
        agents.mkdir()
        (agents / "researcher.json").write_text(
            '{"name": "researcher", "model": "sonnet-9"}', encoding="utf-8"
        )
        with patch("kiro_crew.agent.kiro_agents_dir_path", return_value=agents):
            assert SessionManager._resolve_agent_model("researcher") == "sonnet-9"

    def test_a_raising_spec_reader_falls_back_to_auto(self, tmp_path) -> None:
        agents = tmp_path / "agents"
        agents.mkdir()
        (agents / "researcher.json").write_text('{"name": "researcher"}', encoding="utf-8")
        with patch("kiro_crew.agent.kiro_agents_dir_path", return_value=agents), patch(
            "kiro_crew.session.spec_model", side_effect=RuntimeError("bad spec")
        ):
            assert SessionManager._resolve_agent_model("researcher") == "auto"


# ── Runtime PID probes ───────────────────────────────────────────────────────


class TestRuntimePidProbes:
    def test_a_runtime_reporting_a_nonpositive_pid_is_omitted(self, mgr) -> None:
        mgr._bg_runtime = SimpleNamespace(
            is_alive=lambda: True, pid=0, _spawn_monotonic=1.0
        )
        assert [r for r in mgr.runtime_pids() if r["key"] == "Background runtime"] == []

    def test_a_raising_liveness_probe_drops_only_that_row(self, mgr) -> None:
        def boom():
            raise RuntimeError("probe exploded")

        mgr._bg_runtime = SimpleNamespace(is_alive=boom, pid=42)
        mgr._subagent_runtimes["dashboard:a"] = SimpleNamespace(
            is_alive=lambda: True, pid=99, _spawn_monotonic=None
        )
        rows = mgr.runtime_pids()
        labels = {r["key"] for r in rows}
        assert "Background runtime" not in labels
        assert "Subagent runtime (dashboard:a)" in labels

    def test_a_raising_bg_probe_does_not_break_the_sweep_shield(self, mgr) -> None:
        def boom():
            raise RuntimeError("probe exploded")

        mgr._bg_runtime = SimpleNamespace(is_alive=boom, pid=42)
        mgr._subagent_runtimes["dashboard:a"] = SimpleNamespace(
            is_alive=lambda: True, pid=99
        )
        assert mgr._companion_runtime_pids() == {99}


# ── Warm-pool queue races ────────────────────────────────────────────────────


class TestWarmPoolQueueRaces:
    def test_claim_loses_the_entry_between_check_and_take(self, mgr) -> None:
        mgr._warm_pool = _RacedQueue()
        mgr._pool_agent = "kirocrew"
        assert mgr._claim_from_pool("kirocrew") is None

    def test_pool_pid_peek_survives_a_lost_entry(self, mgr) -> None:
        mgr._warm_pool = _RacedQueue()
        mgr._pool_sweep_pids.add(77)
        assert mgr._pool_pids() == {77}

    @pytest.mark.asyncio
    async def test_drain_warm_pool_survives_a_lost_entry(self, mgr) -> None:
        mgr._warm_pool = _RacedQueue()
        assert await mgr.drain_warm_pool() == []

    @pytest.mark.asyncio
    async def test_refresh_defaults_survives_a_lost_entry(self, mgr) -> None:
        mgr._warm_pool = _RacedQueue()
        mgr._discard_pool_provider = AsyncMock()
        with patch.object(mgr, "start_pool", AsyncMock()), patch(
            "kiro_crew.session.build_provider_factory", return_value=MagicMock()
        ):
            await mgr.refresh_defaults()
        mgr._discard_pool_provider.assert_not_called()

    @pytest.mark.asyncio
    async def test_reload_provider_factory_survives_a_lost_entry(self, mgr) -> None:
        mgr._warm_pool = _RacedQueue()
        mgr._discard_pool_provider = AsyncMock()
        stale = _register(mgr, "dashboard:a")
        with patch.object(mgr, "start_pool", AsyncMock()), patch(
            "kiro_crew.session.build_provider_factory", return_value=MagicMock()
        ):
            await mgr.reload_provider_factory()
        assert mgr._sessions == {}
        stale.provider.shutdown.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_health_sweep_survives_a_lost_entry(self, mgr) -> None:
        mgr._pool_size = 1
        mgr._warm_pool = _RacedQueue()
        mgr._fill_warm_pool = AsyncMock()
        mgr._discard_pool_provider = AsyncMock()
        await mgr._sweep_warm_pool_once()
        mgr._discard_pool_provider.assert_not_called()

    @pytest.mark.asyncio
    async def test_health_sweep_no_ops_when_the_pool_is_disabled(self, mgr) -> None:
        """With ``pool_size == 0`` the sweep must not even look at the queue —
        reading it would make a disabled pool pay for a sweep it cannot use."""

        class _Tripwire(_RacedQueue):
            def qsize(self) -> int:
                raise AssertionError("the sweep read the queue on a disabled pool")

        mgr._pool_size = 0
        mgr._warm_pool = _Tripwire()
        await mgr._sweep_warm_pool_once()

    @pytest.mark.asyncio
    async def test_a_raising_liveness_probe_counts_as_dead(self, mgr) -> None:
        """The probe is the ONLY liveness signal; a provider that cannot answer
        must be discarded, not re-enqueued as healthy."""

        def boom():
            raise RuntimeError("rpc wedged")

        dead = _provider(is_process_alive=boom, exit_code=None)
        mgr._pool_size = 1
        mgr._pool_ttl_secs = 0
        await mgr._warm_pool.put((dead, 0.0))
        mgr._fill_warm_pool = AsyncMock()
        mgr._discard_pool_provider = AsyncMock()
        await mgr._sweep_warm_pool_once()
        mgr._discard_pool_provider.assert_awaited_once()
        assert mgr._discard_pool_provider.await_args.args[0] is dead


class TestPoolHealthLoop:
    @pytest.mark.asyncio
    async def test_a_failing_sweep_is_logged_and_the_loop_keeps_going(self, mgr) -> None:
        """A sweep failure must not end the loop — the pool would then never be
        swept again for the gateway's whole lifetime."""
        mgr._POOL_HEALTH_INTERVAL = 0
        sweep = AsyncMock(side_effect=[RuntimeError("sweep blew up"), asyncio.CancelledError])
        mgr._sweep_warm_pool_once = sweep
        with pytest.raises(asyncio.CancelledError):
            await mgr._pool_health_loop()
        assert sweep.await_count == 2


class TestPoolDecisionMetric:
    def test_a_failing_recorder_never_reaches_the_caller(self, mgr) -> None:
        with patch("kiro_crew.session.get_recorder", side_effect=RuntimeError("no otel")):
            mgr._record_pool_decision("hit", "dashboard:a")


# ── Pool provider discard ────────────────────────────────────────────────────


class TestDiscardPoolProvider:
    @pytest.mark.asyncio
    async def test_a_base_exception_during_shutdown_still_dispatches_the_kill(
        self, mgr
    ) -> None:
        """A non-``Exception`` BaseException (a cancellation-class escape) must
        not skip the hard kill — the provider would leak its whole process tree."""

        async def shutdown():
            raise _Bang()

        provider = _provider(shutdown=shutdown)
        with patch("kiro_crew.session._sync_kill_provider") as killer:
            with pytest.raises(_Bang):
                await mgr._discard_pool_provider(provider, "unit")
        # Dispatched to the executor; wait for the worker to pick it up.
        for _ in range(200):
            if killer.call_count:
                break
            await asyncio.sleep(0.001)
        assert killer.call_args.args[0] is provider

    @pytest.mark.asyncio
    async def test_an_unanswerable_liveness_probe_reads_as_dead(self, mgr) -> None:
        """With no tracked PID the provider's own view is the fallback; a raising
        probe must resolve to "gone" rather than trigger a kill on nothing."""

        def boom():
            raise RuntimeError("wedged")

        provider = _provider(is_process_alive=boom)
        with patch("kiro_crew.session._sync_kill_provider") as killer:
            await mgr._discard_pool_provider(provider, "unit")
        assert killer.call_count == 0


# ── Stale-session eviction ───────────────────────────────────────────────────


class TestEvictStaleSession:
    @pytest.mark.asyncio
    async def test_a_failing_shutdown_still_leaves_the_entry_evicted(self, mgr) -> None:
        sess = _register(mgr, "task:step1", provider=_provider(shutdown=AsyncMock(side_effect=OSError)))
        await mgr._evict_stale_session("task:step1", sess)
        assert "task:step1" not in mgr._sessions

    @pytest.mark.asyncio
    async def test_an_entry_owned_by_someone_else_is_left_alone(self, mgr) -> None:
        ours = _Session(provider=_provider())
        theirs = _register(mgr, "task:step1")
        await mgr._evict_stale_session("task:step1", ours)
        assert mgr._sessions["task:step1"] is theirs
        ours.provider.shutdown.assert_not_awaited()


# ── Background provider dispatch ─────────────────────────────────────────────


class TestBackgroundProviderDispatch:
    def test_an_unreadable_provider_setting_defaults_to_the_kiro_backend(self, mgr) -> None:
        """``_bg`` must keep working when the config object cannot answer — the
        alternative is losing chat titles and consolidation to a config edge."""

        class _Boom:
            @property
            def provider(self):
                raise RuntimeError("config exploded")

        mgr._cfg = SimpleNamespace(agent=_Boom())
        assert mgr._bg_provider_is_kiro() is True

    @pytest.mark.asyncio
    async def test_ensure_background_no_ops_without_a_factory(self, mgr) -> None:
        await mgr._ensure_background()
        assert BACKGROUND_KEY not in mgr._sessions

    @pytest.mark.asyncio
    async def test_losing_the_background_race_shuts_the_duplicate_down(self, cfg) -> None:
        """Two concurrent ``_ensure_background`` calls must leave ONE registered
        provider and no orphan: the loser tears its own provider down."""
        winner = _provider()
        loser = _provider()

        async def _start():
            # A racing coroutine registered the key while we were starting.
            mgr._sessions[BACKGROUND_KEY] = _Session(provider=winner, is_new=False)

        loser.start = _start
        mgr = SessionManager(cfg, provider_factory=lambda *a, **k: loser)
        await mgr._ensure_background()
        assert mgr._sessions[BACKGROUND_KEY].provider is winner
        loser.shutdown.assert_awaited_once()


class TestGetBgSessionRespawn:
    """``get_bg_session`` retries a dead ``_bg`` runtime exactly once."""

    @pytest.fixture
    def fake_runtime_cls(self):
        import kiro_crew.acp.runtime as runtime_mod

        created: list[object] = []

        class _FakeRuntime:
            def __init__(self, **kwargs) -> None:
                self.kwargs = kwargs
                self.pid = 4242
                created.append(self)

            async def spawn(self) -> None:
                return None

            def is_alive(self) -> bool:
                return True

            def has_active_sessions(self) -> bool:
                return True

            async def create_session(self, **kwargs):
                return SimpleNamespace(session_id="sid-fresh")

            async def kill(self) -> None:  # pragma: no cover — replacement only
                return None

        with patch.object(runtime_mod, "AcpRuntime", _FakeRuntime):
            yield created

    @pytest.mark.asyncio
    async def test_a_dead_runtime_is_reaped_before_being_replaced(
        self, mgr, fake_runtime_cls
    ) -> None:
        """Overwriting without ``kill()`` would leak the process AND its
        sweep-protected PID, so the reap must happen even when it fails."""
        doomed = SimpleNamespace(
            is_alive=lambda: False,
            pid=11,
            kill=AsyncMock(side_effect=RuntimeError("kill failed")),
        )
        mgr._bg_runtime = doomed
        handle = await mgr.get_bg_session()
        assert handle.session_id == "sid-fresh"
        doomed.kill.assert_awaited_once()
        assert len(fake_runtime_cls) == 1
        assert mgr._bg_runtime is fake_runtime_cls[0]

    @pytest.mark.asyncio
    async def test_a_still_live_runtime_that_keeps_failing_gives_up_after_one_retry(
        self, mgr, fake_runtime_cls
    ) -> None:
        from kiro_crew.acp.runtime import AcpRuntimeDead

        create = AsyncMock(side_effect=AcpRuntimeDead("gone"))
        mgr._bg_runtime = SimpleNamespace(
            is_alive=lambda: True,
            has_active_sessions=lambda: True,
            _stale_by_age=lambda: False,
            pid=11,
            create_session=create,
            kill=AsyncMock(),
        )
        with pytest.raises(AcpRuntimeDead):
            await mgr.get_bg_session()
        # One initial attempt plus exactly one retry — not an unbounded loop.
        assert create.await_count == 2
        assert fake_runtime_cls == []

    @pytest.mark.asyncio
    async def test_a_runtime_that_dies_mid_create_is_reaped_and_respawned(
        self, mgr, fake_runtime_cls
    ) -> None:
        from kiro_crew.acp.runtime import AcpRuntimeDead

        alive = [True]

        async def create_session(**kwargs):
            alive[0] = False
            raise AcpRuntimeDead("died mid-create")

        doomed = SimpleNamespace(
            is_alive=lambda: alive[0],
            has_active_sessions=lambda: True,
            _stale_by_age=lambda: False,
            pid=11,
            create_session=create_session,
            kill=AsyncMock(side_effect=RuntimeError("kill failed")),
        )
        mgr._bg_runtime = doomed
        handle = await mgr.get_bg_session()
        assert handle.session_id == "sid-fresh"
        doomed.kill.assert_awaited_once()
        assert len(fake_runtime_cls) == 1


# ── reset() teardown ─────────────────────────────────────────────────────────


@pytest.fixture
def no_child_scan():
    """Neutralize the three ``acp.client`` helpers ``reset`` calls.

    They read ``/proc`` (or spawn ``ps``/``pgrep`` on macOS) and are the reason
    a naive reset test cannot run on a CI runner.
    """
    with patch("kiro_crew.acp.client._get_child_pids", return_value=[]), patch(
        "kiro_crew.acp.client._capture_child_records", return_value={}
    ), patch("kiro_crew.acp.client._kill_escaped_children") as sweep:
        yield sweep


class TestResetTeardown:
    @pytest.mark.asyncio
    async def test_an_ephemeral_claude_process_supplies_the_pid_to_verify(
        self, mgr, no_child_scan, monkeypatch
    ) -> None:
        """With neither an ACP client nor a long-lived ``_proc``, the ephemeral
        ``_active_proc`` is the only handle to the process — reset must find it,
        or the post-shutdown liveness check silently probes nothing."""
        probed: list[int] = []
        monkeypatch.setattr(
            platform_compat, "pid_exists", lambda pid: probed.append(pid) or False
        )
        provider = _provider(_active_proc=SimpleNamespace(returncode=None, pid=4242))
        _register(mgr, "dashboard:a", provider=provider)

        assert await mgr.reset("dashboard:a") is True
        assert probed == [4242]

    @pytest.mark.asyncio
    async def test_a_survivor_whose_tree_and_pid_kills_both_fail_is_not_fatal(
        self, mgr, no_child_scan, monkeypatch
    ) -> None:
        monkeypatch.setattr(platform_compat, "pid_exists", lambda pid: True)
        tree = AsyncMock(side_effect=OSError("no such pgid"))
        single = AsyncMock(side_effect=ProcessLookupError)
        monkeypatch.setattr(platform_compat, "kill_process_tree_async", tree)
        monkeypatch.setattr(platform_compat, "kill_pid_async", single)
        provider = _provider(_proc=SimpleNamespace(returncode=None, pid=4242))
        _register(mgr, "dashboard:a", provider=provider)

        assert await mgr.reset("dashboard:a") is True
        tree.assert_awaited_once()
        single.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_failing_child_sweep_is_logged_not_raised(
        self, mgr, no_child_scan, monkeypatch
    ) -> None:
        monkeypatch.setattr(platform_compat, "pid_exists", lambda pid: False)
        no_child_scan.side_effect = RuntimeError("sweep exploded")
        provider = _provider(
            _client=SimpleNamespace(_pid=4242, _child_pids={99: object()}),
        )
        _register(mgr, "dashboard:a", provider=provider)

        assert await mgr.reset("dashboard:a") is True
        assert no_child_scan.called

    @pytest.mark.asyncio
    async def test_a_failing_subagent_runtime_release_still_completes_the_reset(
        self, mgr, no_child_scan
    ) -> None:
        _register(mgr, "dashboard:a")
        mgr._subagent_runtimes["dashboard:a"] = SimpleNamespace()
        mgr.release_subagent_runtime = AsyncMock(side_effect=RuntimeError("runtime wedged"))

        assert await mgr.reset("dashboard:a") is True
        assert "dashboard:a" not in mgr._sessions
        mgr.release_subagent_runtime.assert_awaited_once_with("dashboard:a")


# ── Drain of unfinished turns ────────────────────────────────────────────────


class TestDrainActiveTurns:
    @pytest.mark.asyncio
    async def test_every_uncooperative_shape_is_counted_and_none_escapes(self, mgr) -> None:
        """Four ways a provider can refuse to reach a safe boundary. Each is
        swallowed per-provider, and all four still count as drained so the
        caller's log reflects what it waited on."""
        no_cancel = _provider(has_unfinished_turn=lambda: True, cancel=None)
        raising = _provider(
            has_unfinished_turn=lambda: True,
            cancel=AsyncMock(side_effect=RuntimeError("cancel exploded")),
        )
        waiter_timeout = _provider(
            has_unfinished_turn=lambda: True,
            cancel=AsyncMock(return_value="no_turn"),
            wait_turn_done=AsyncMock(side_effect=asyncio.TimeoutError),
        )
        waiter_broken = _provider(
            has_unfinished_turn=lambda: True,
            cancel=AsyncMock(return_value="no_turn"),
            wait_turn_done=AsyncMock(side_effect=RuntimeError("waiter exploded")),
        )
        for i, p in enumerate((no_cancel, raising, waiter_timeout, waiter_broken)):
            _register(mgr, f"dashboard:{i}", provider=p)

        assert await mgr.drain_active_turns(timeout=0.05) == 4
        waiter_timeout.wait_turn_done.assert_awaited_once()
        waiter_broken.wait_turn_done.assert_awaited_once()


# ── close_all ────────────────────────────────────────────────────────────────


class TestCloseAll:
    @pytest.mark.asyncio
    async def test_every_teardown_step_can_fail_and_shutdown_still_completes(
        self, mgr
    ) -> None:
        """Shutdown is the last chance to release kiro-cli's native session
        locks, so no single failing step may abort the rest of it."""
        mgr.drain_active_turns = AsyncMock(side_effect=RuntimeError("drain exploded"))
        bg = SimpleNamespace(kill=AsyncMock(side_effect=RuntimeError("bg kill failed")))
        mgr._bg_runtime = bg
        mgr._subagent_runtimes["dashboard:a"] = SimpleNamespace()
        mgr.release_subagent_runtime = AsyncMock(side_effect=RuntimeError("release failed"))
        sess = _register(
            mgr, "dashboard:a", provider=_provider(shutdown=AsyncMock(side_effect=OSError))
        )

        await mgr.close_all()

        bg.kill.assert_awaited_once()
        assert mgr._bg_runtime is None
        assert mgr._sessions == {}
        sess.provider.shutdown.assert_awaited_once()
        assert mgr._closing is True

    @pytest.mark.asyncio
    async def test_the_pool_drain_survives_a_lost_entry(self, mgr) -> None:
        mgr.drain_active_turns = AsyncMock(return_value=0)
        mgr._warm_pool = _RacedQueue()
        await mgr.close_all()
        assert mgr._sessions == {}


# ── release(cleanup=True) ────────────────────────────────────────────────────


class TestReleaseCleanup:
    def test_an_unreadable_session_id_does_not_block_the_release(self, mgr) -> None:
        """The semaphore release is the important half: skipping it on a failed
        cleanup probe would wedge the key for the rest of the process."""

        class _Provider(SimpleNamespace):
            @property
            def session_id(self):
                raise RuntimeError("no session id")

        sess = _register(mgr, "subagent:run1", provider=_Provider())
        sess.semaphore._value = 0
        mgr.release("subagent:run1", cleanup=True)
        assert sess.semaphore._value == 1


# ── stop_turn hooks ──────────────────────────────────────────────────────────


class TestStopTurnHooks:
    @pytest.mark.asyncio
    async def test_a_failing_soft_hook_still_reports_a_soft_stop(self, mgr) -> None:
        provider = _provider(cancel=AsyncMock(return_value="acked"))
        sess = _register(mgr, "dashboard:a", provider=provider)
        outcome = await mgr.stop_turn(
            "dashboard:a", on_soft=AsyncMock(side_effect=RuntimeError("hook exploded"))
        )
        assert outcome == "soft"
        assert sess.prev_turn_cancelled is True

    @pytest.mark.asyncio
    async def test_a_failing_hard_hook_still_reports_a_hard_stop(
        self, mgr, no_child_scan
    ) -> None:
        provider = _provider(
            cancel=AsyncMock(return_value="acked"),
            runtime_info=lambda: (None, None),
        )
        _register(mgr, "dashboard:a", provider=provider)
        outcome = await mgr.stop_turn(
            "dashboard:a",
            force=True,
            on_hard=AsyncMock(side_effect=RuntimeError("hook exploded")),
        )
        assert outcome == "hard"
        assert "dashboard:a" not in mgr._sessions
        # The eager respawn task has no factory to use; drain it so the loop
        # does not report a pending task at teardown.
        await asyncio.gather(*mgr._background_tasks, return_exceptions=True)


# ── recycle_background refusals ──────────────────────────────────────────────


class TestRecycleBackgroundRefusals:
    @pytest.mark.asyncio
    async def test_a_dead_background_provider_is_not_recycled_under_us(self, mgr) -> None:
        """``_reacquire_and_validate`` failing means we own nothing — tearing
        anything down here would kill a session another path already owns."""
        provider = _provider(is_process_alive=lambda: False)
        sess = _register(mgr, BACKGROUND_KEY, provider=provider)
        await mgr.recycle_background()
        assert mgr._sessions[BACKGROUND_KEY] is sess
        assert sess.semaphore._value == 1

    @pytest.mark.asyncio
    async def test_a_full_background_session_with_no_factory_keeps_its_provider(
        self, mgr
    ) -> None:
        provider = _provider(context_usage_pct=lambda: 88.0)
        sess = _register(mgr, BACKGROUND_KEY, provider=provider)
        await mgr.recycle_background()
        assert sess.provider is provider
        provider.shutdown.assert_not_awaited()
        assert sess.semaphore._value == 1

    @pytest.mark.asyncio
    async def test_a_failing_shutdown_of_the_replaced_provider_is_swallowed(
        self, cfg
    ) -> None:
        old = _provider(
            context_usage_pct=lambda: 88.0,
            shutdown=AsyncMock(side_effect=OSError("shutdown exploded")),
        )
        new = _provider(start=AsyncMock())
        mgr = SessionManager(cfg, provider_factory=lambda *a, **k: new)
        sess = _register(mgr, BACKGROUND_KEY, provider=old)

        await mgr.recycle_background()

        assert sess.provider is new
        old.shutdown.assert_awaited_once()
        assert sess.semaphore._value == 1


# ── open_task_session ────────────────────────────────────────────────────────


class TestOpenTaskSession:
    @pytest.mark.asyncio
    async def test_reusing_a_live_session_adopts_the_callers_approval_policy(
        self, mgr
    ) -> None:
        """A later step of the same run may escalate to auto-approval; the
        reused session must adopt it rather than keep the first step's policy."""
        sess = _register(mgr, "taskrunner:run1:step2", approval_policy="")
        provider, is_new, resumed = await mgr.open_task_session(
            "taskrunner:run1", "taskrunner:run1:step2", approval_policy="auto"
        )
        assert (provider, is_new, resumed) == (sess.provider, False, False)
        assert sess.approval_policy == "auto"
        mgr.release("taskrunner:run1:step2")

    @pytest.mark.asyncio
    async def test_losing_the_cold_start_race_tears_down_the_duplicate(self, mgr) -> None:
        """Two steps cold-starting the same key must leave ONE registered
        session; the loser terminates its own extra session on the shared
        runtime even when that teardown fails."""
        key = "taskrunner:run1:step2"
        winner_provider = _provider()

        async def create_session(**kwargs):
            # A racing step registered the key while our RPC was in flight.
            mgr._sessions[key] = _Session(provider=winner_provider, is_new=False)
            return SimpleNamespace(session_id="sid-dup")

        runtime = SimpleNamespace(create_session=create_session)
        mgr._get_or_bootstrap_run_runtime = AsyncMock(return_value=runtime)

        dup = _provider(shutdown=AsyncMock(side_effect=RuntimeError("terminate failed")))
        with patch(
            "kiro_crew.acp.session_provider.AcpSessionProvider",
            side_effect=lambda handle, rt: dup,
        ):
            provider, is_new, resumed = await mgr.open_task_session("taskrunner:run1", key)

        assert (provider, is_new, resumed) == (winner_provider, False, False)
        dup.shutdown.assert_awaited_once()
        assert mgr._sessions[key].provider is winner_provider
        mgr.release(key)


# ── Signal escalation ────────────────────────────────────────────────────────


class TestResetSignalEscalation:
    """No ``skipif`` here on purpose: ``platform_compat.SIGKILL`` is defined on
    every platform (it falls back to the raw ``9`` where ``signal.SIGKILL`` is
    absent), and the kill entry points are doubles, so the assertion is about
    the product's escalation decision rather than about OS signal delivery."""

    @pytest.mark.asyncio
    async def test_a_surviving_process_is_force_killed_with_sigkill(
        self, mgr, no_child_scan, monkeypatch
    ) -> None:
        monkeypatch.setattr(platform_compat, "pid_exists", lambda pid: True)
        tree = AsyncMock()
        monkeypatch.setattr(platform_compat, "kill_process_tree_async", tree)
        provider = _provider(_client=SimpleNamespace(_pid=4242, _child_pids=None))
        _register(mgr, "dashboard:a", provider=provider)

        assert await mgr.reset("dashboard:a") is True
        tree.assert_awaited_once_with(4242, platform_compat.SIGKILL)
