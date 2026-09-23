"""Auto-created chat slots start their session on the global ``agent.model``.

Regression: a slot created implicitly by sending a message (``api_chat`` ->
``get_or_create_slot`` with no model) carries ``slot.model == ""``. ``_run_chat``
then passed ``model=slot.model or agent_model or None`` to ``get_or_create``,
where ``agent_model`` is only the crew's own pin, so a slot on a crew that pins
nothing handed ``None`` down and left the default to the session manager's
config snapshot — not to :func:`resolve_effective_model`, the documented
precedence chain the dashboard's model chip displays. The chip and the session
could disagree.

These tests drive the REAL ``_run_chat`` and ``_eager_spawn`` bodies with a
mocked session boundary and assert on the ``model`` kwarg ``get_or_create``
receives, so they fail if the runner stops resolving the default. They also pin
the persist-vs-not decision: the resolved default is passed to the session and
is NOT written into ``slot.model``, whose empty value keeps meaning "inherit".
"""

from __future__ import annotations

import asyncio
import json
import threading
import unittest.mock
from pathlib import Path

import pytest
from test_chat_runner_coverage import _complete, _drive, _runner_state, _set_stream, _slot

from kiro_crew.acp.types import EVENT_TEXT_CHUNK
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.chat_runner import _eager_spawn
from kiro_crew.member_memory_auth import bind_private_session_store
from kiro_crew.memory_stores import provision_member_memory
from kiro_crew.providers.base import LLMEvent

GLOBAL_DEFAULT = "claude-opus-5"
CREW_PIN = "claude-sonnet-5"
SLOT_PIN = "claude-haiku-5"


def _load_config(tmp_path: Path, data: dict) -> KiroCrewConfig:
    """A real ``KiroCrewConfig`` loaded from *data* (same shape as config.json)."""
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps(data))
    with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg_file):
        return KiroCrewConfig.load()


def _config(tmp_path: Path, *, crew_model: str = "") -> KiroCrewConfig:
    """Global ``agent.model`` set; the crews pin nothing unless *crew_model*."""
    cfg = _load_config(
        tmp_path,
        {
            "agent": {"model": GLOBAL_DEFAULT, "provider": "acp"},
            "agents": {
                "default": {"kiro_agent": "kirocrew", "memory_store": "default"},
                "researcher": {"kiro_agent": "kirocrew", "model": crew_model},
            },
            "default_agent": "default",
        },
    )
    provision_member_memory(cfg, "researcher")
    return cfg


def _grant_researcher(cfg: KiroCrewConfig, slot_key: str = "chat-cov-1") -> None:
    """Write the member's private session grant, as an owner-gated route would.

    A turn only confirms a grant that already exists; these turns exercise
    model selection, not admission, so the grant is written up front.
    """
    bind_private_session_store(f"dashboard:{slot_key}", cfg.agents["researcher"].memory_store)


def _pin_sync_accessors(client) -> None:
    """Give the provider double's remaining SYNC accessors sync stand-ins.

    ``_runner_state`` pins the context-usage trio; the turn also reads
    ``mcp_session_report``, ``available_models`` and the inner client's
    ``pop_pending_oauth_requests`` without ``await``. Left as ``AsyncMock``
    children each returns a coroutine nobody awaits, reported at garbage
    collection against whichever later test triggers it.
    """
    client.mcp_session_report = unittest.mock.MagicMock(return_value=None)
    client.available_models = unittest.mock.MagicMock(return_value=[])
    client.client.pop_pending_oauth_requests = unittest.mock.MagicMock(return_value=[])


def _turn_state(tmp_path: Path):
    builder = unittest.mock.MagicMock()
    builder.ensure_store = unittest.mock.AsyncMock(return_value=object())
    builder.build_message.return_value = ("fixture context", None)
    state, client = _runner_state(tmp_path, context_builder=builder)
    _pin_sync_accessors(client)
    _set_stream(client, [LLMEvent(kind=EVENT_TEXT_CHUNK, text="hi"), _complete()])
    return state, client


def _session_model(state) -> str | None:
    state.sessions.get_or_create.assert_awaited_once()
    return state.sessions.get_or_create.await_args.kwargs["model"]


@pytest.fixture
def _runner_config(tmp_path, monkeypatch):
    """Serve the real config object to every ``KiroCrewConfig.load()`` in the turn."""
    # These turns use a fake provider and exercise model selection. Supply only
    # the host capability result; member provisioning and binding remain real.
    pass  # Member routing does not depend on OS isolation.

    def _install(cfg: KiroCrewConfig):
        patcher = unittest.mock.patch.object(
            chat_runner.KiroCrewConfig, "load", unittest.mock.MagicMock(return_value=cfg)
        )
        patcher.start()
        return patcher

    patchers: list[unittest.mock._patch] = []

    def _use(cfg: KiroCrewConfig) -> None:
        patchers.append(_install(cfg))

    yield _use
    for p in patchers:
        p.stop()


class TestRunChatDefaultModel:
    @pytest.mark.asyncio
    async def test_fresh_slot_starts_on_the_global_default(self, tmp_path, _runner_config):
        """slot.model == "" and no crew pin -> the session gets ``agent.model``."""
        _runner_config(_config(tmp_path))
        state, _client = _turn_state(tmp_path)
        slot = _slot()
        assert slot.model == "" and slot.agent == ""

        await _drive(state, slot)

        assert _session_model(state) == GLOBAL_DEFAULT

    @pytest.mark.asyncio
    async def test_resolved_default_is_not_persisted_on_the_slot(self, tmp_path, _runner_config):
        """The default is a session-creation input, not a pin.

        ``slot.model`` is persisted and re-sent as a ``set_model`` override on
        every resume, so writing the resolved default there would turn an
        inheriting slot into a permanent pin: a later change to ``agent.model``
        would not reach it. An empty value must survive the turn.
        """
        _runner_config(_config(tmp_path))
        state, _client = _turn_state(tmp_path)
        slot = _slot()

        await _drive(state, slot)

        assert _session_model(state) == GLOBAL_DEFAULT
        assert slot.model == ""

    @pytest.mark.asyncio
    async def test_crew_pin_outranks_the_global_default(self, tmp_path, _runner_config):
        cfg = _config(tmp_path, crew_model=CREW_PIN)
        _runner_config(cfg)
        _grant_researcher(cfg)
        state, _client = _turn_state(tmp_path)
        slot = _slot()
        slot.agent = "researcher"

        await _drive(state, slot)

        assert _session_model(state) == CREW_PIN
        assert slot.model == ""

    @pytest.mark.asyncio
    async def test_explicit_slot_pin_is_untouched(self, tmp_path, _runner_config):
        cfg = _config(tmp_path, crew_model=CREW_PIN)
        _runner_config(cfg)
        _grant_researcher(cfg)
        state, _client = _turn_state(tmp_path)
        slot = _slot()
        slot.agent = "researcher"
        slot.model = SLOT_PIN

        await _drive(state, slot)

        assert _session_model(state) == SLOT_PIN
        assert slot.model == SLOT_PIN

    @pytest.mark.asyncio
    async def test_every_tier_deferring_leaves_the_backend_to_choose(
        self, tmp_path, _runner_config
    ):
        """``agent.model`` unset/auto and no installed pin -> ``None``, as before."""
        cfg = _load_config(
            tmp_path,
            {"agent": {"model": "auto", "provider": "acp"}, "default_agent": "kirocrew"},
        )
        _runner_config(cfg)
        state, _client = _turn_state(tmp_path)
        slot = _slot()

        with unittest.mock.patch.object(
            KiroCrewConfig, "_resolve_agent_model", staticmethod(lambda: "")
        ):
            await _drive(state, slot)

        assert _session_model(state) is None
        assert slot.model == ""

    @pytest.mark.asyncio
    async def test_resolver_failure_falls_back_to_the_old_shape(self, tmp_path, _runner_config):
        """A resolver error must not kill the turn; the session gets ``None``."""
        _runner_config(_config(tmp_path))
        state, _client = _turn_state(tmp_path)
        slot = _slot()

        with unittest.mock.patch.object(
            chat_runner, "resolve_effective_model", side_effect=RuntimeError("boom")
        ):
            await _drive(state, slot)

        assert _session_model(state) is None
        assert slot.model == ""

    @pytest.mark.asyncio
    async def test_resolver_stop_iteration_does_not_kill_the_turn(self, tmp_path, _runner_config):
        """``StopIteration`` from the resolver is converted INSIDE the worker.

        The resolve now runs through ``asyncio.to_thread``, and a
        ``StopIteration`` cannot be delivered through a Future
        (``set_exception`` rejects it with ``TypeError``). ``resolve_agent_bindings``
        can raise exactly that on a malformed config, so the helper must turn
        it into ``""`` before it reaches the thread boundary — otherwise the
        turn dies with a ``TypeError`` that names neither the config nor the
        resolver. The turn must complete and hand ``None`` to the session.
        """
        _runner_config(_config(tmp_path))
        state, client = _turn_state(tmp_path)
        slot = _slot()

        with unittest.mock.patch.object(
            chat_runner, "resolve_effective_model", side_effect=StopIteration("malformed")
        ):
            await _drive(state, slot)

        assert _session_model(state) is None
        assert slot.model == ""
        # The turn ran to the provider: the resolver failure was absorbed, not fatal.
        client.stream.assert_called()

    @pytest.mark.asyncio
    async def test_default_resolve_runs_off_the_event_loop(self, tmp_path, _runner_config):
        """The resolver globs and reads agent JSON; it must not run on the loop.

        Pins the ``asyncio.to_thread`` hop by observing the thread the resolver
        actually runs on: a worker thread has no running loop, and it is not the
        thread that drives the turn. A future inline call fails both checks.
        """
        _runner_config(_config(tmp_path))
        state, _client = _turn_state(tmp_path)
        slot = _slot()
        seen: dict[str, object] = {}

        def _resolver(cfg, agent):
            seen["thread"] = threading.get_ident()
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                seen["loop_on_thread"] = False
            else:
                seen["loop_on_thread"] = True
            return GLOBAL_DEFAULT

        with unittest.mock.patch.object(chat_runner, "resolve_effective_model", _resolver):
            await _drive(state, slot)

        assert _session_model(state) == GLOBAL_DEFAULT
        assert seen["thread"] != threading.get_ident()
        assert seen["loop_on_thread"] is False


class TestDefaultSessionModelThreadBoundary:
    """The helper's ``except`` is what makes ``asyncio.to_thread`` safe to use."""

    @pytest.mark.asyncio
    async def test_stop_iteration_is_converted_before_the_future(self, tmp_path):
        """``StopIteration`` from the resolver comes back as ``""``, not an error.

        Why this matters enough to pin: a ``StopIteration`` cannot be carried
        by a Future. On 3.12+ asyncio substitutes a ``RuntimeError``
        ("StopIteration interacts badly with generators") so the resolver's
        own message is lost; before 3.12 ``set_exception`` raised inside the
        loop callback and the destination future stayed PENDING, so an await
        on the hop hung until the test timeout. Either way the failure would
        name asyncio, not the malformed config. The helper's ``except
        Exception`` clause is what converts it inside the worker; narrowing
        that clause (say to ``RuntimeError``) turns this test red. No live
        negative control here on purpose: an unconverted ``StopIteration``
        through ``to_thread`` is exactly the hang described above on the
        older interpreters this repo still targets.
        """
        cfg = _config(tmp_path)
        slot = _slot()

        with unittest.mock.patch.object(
            chat_runner, "resolve_effective_model", side_effect=StopIteration("malformed")
        ):
            result = await asyncio.to_thread(chat_runner._default_session_model, cfg, slot, "")

        assert result == ""


class TestEagerSpawnDefaultModel:
    """The pre-warmed session must run the same model the first real turn would."""

    @pytest.fixture(autouse=True)
    def _no_debounce(self, monkeypatch):
        monkeypatch.setattr(chat_runner, "_EAGER_SPAWN_DEBOUNCE_SECS", 0)

    @pytest.mark.asyncio
    async def test_eager_session_starts_on_the_global_default(self, tmp_path, _runner_config):
        _runner_config(_config(tmp_path))
        state, _client = _runner_state(tmp_path)
        _pin_sync_accessors(_client)
        slot = _slot()
        state._slots[slot.key] = slot
        state.sessions.release = unittest.mock.MagicMock()
        state.sessions.remove_if_unclaimed = unittest.mock.AsyncMock(return_value=True)

        await _eager_spawn(state, slot)

        assert _session_model(state) == GLOBAL_DEFAULT
        assert slot.model == ""

    @pytest.mark.asyncio
    async def test_eager_resolve_runs_off_the_loop_and_survives_stop_iteration(
        self, tmp_path, _runner_config
    ):
        """Same two guarantees as the real turn, on the pre-warm path.

        The eager spawn is best-effort, so a resolver ``StopIteration`` must
        neither stall the loop (the resolve is off-loop) nor abort the spawn
        (converted to ``""`` in the worker, so the session is still created —
        on ``None``, as before the resolve existed).
        """
        _runner_config(_config(tmp_path))
        state, _client = _runner_state(tmp_path)
        _pin_sync_accessors(_client)
        slot = _slot()
        state._slots[slot.key] = slot
        state.sessions.release = unittest.mock.MagicMock()
        state.sessions.remove_if_unclaimed = unittest.mock.AsyncMock(return_value=True)
        seen: dict[str, object] = {}

        def _resolver(cfg, agent):
            seen["thread"] = threading.get_ident()
            raise StopIteration("malformed")

        with unittest.mock.patch.object(chat_runner, "resolve_effective_model", _resolver):
            await _eager_spawn(state, slot)

        assert _session_model(state) is None
        assert slot.model == ""
        assert seen["thread"] != threading.get_ident()


class TestSessionOpenedRecordsTheAllocationsSelection:
    """``session/opened.model_requested`` names the ALLOCATION's selection.

    The turn that observes a session is not always the one that allocated it. An
    eager allocation can outlive a config change, so re-resolving the selection at
    the first turn writes a model that session never used -- into an append-only
    entry nothing rewrites. These turns capture the kwarg the emitter receives, so
    they fail if the runner goes back to resolving it per turn.
    """

    @staticmethod
    def _capture():
        return unittest.mock.patch.object(
            chat_runner.crew_log_emit, "on_session_opened", unittest.mock.MagicMock()
        )

    @pytest.mark.asyncio
    async def test_an_allocating_turn_records_its_own_selection(self, tmp_path, _runner_config):
        _runner_config(_config(tmp_path))
        state, _client = _turn_state(tmp_path)
        slot = _slot()
        assert slot._session_requested_model is None

        with self._capture() as opened:
            await _drive(state, slot)

        assert slot._session_requested_model == GLOBAL_DEFAULT
        assert opened.call_args.kwargs["model_requested"] == GLOBAL_DEFAULT

    @pytest.mark.asyncio
    async def test_a_prewarmed_claim_keeps_the_allocations_selection(
        self, tmp_path, _runner_config
    ):
        """The regression: a claim of a pre-warmed session must not re-resolve.

        An eager allocation that RESUMED arms a ``resumed=True`` observation for the
        real turn, so this is the shape a resuming prewarmed first turn sees. (A
        prewarm that started fresh arms ``FRESH`` instead and arrives as
        ``is_new=True, resumed=False``; that shape is covered by
        ``TestSessionOpenedCoversTheTierBelowTheCaller``.) The config default has
        moved since that allocation; the entry must still name what was allocated.
        """
        _runner_config(_config(tmp_path))
        state, client = _turn_state(tmp_path)
        state.sessions.get_or_create = unittest.mock.AsyncMock(return_value=(client, False, True))
        slot = _slot()
        slot._session_requested_model = "model-the-allocation-chose"

        with self._capture() as opened:
            await _drive(state, slot)

        assert slot._session_requested_model == "model-the-allocation-chose"
        assert opened.call_args.kwargs["model_requested"] == "model-the-allocation-chose"

    @pytest.mark.asyncio
    async def test_a_fresh_allocation_replaces_a_dead_sessions_selection(
        self, tmp_path, _runner_config
    ):
        """A value left by a session that died without teardown must not outlive it.

        The live session stamped nothing, so the record falls back to this turn's own
        selection rather than reporting the dead session's provenance.
        """
        _runner_config(_config(tmp_path))
        state, _client = _turn_state(tmp_path)
        slot = _slot()
        slot._session_requested_model = "model-of-a-session-that-is-gone"

        with self._capture() as opened:
            await _drive(state, slot)

        assert slot._session_requested_model == GLOBAL_DEFAULT
        assert opened.call_args.kwargs["model_requested"] == GLOBAL_DEFAULT

    @pytest.mark.asyncio
    async def test_a_re_attach_with_no_provenance_records_nothing(self, tmp_path, _runner_config):
        """Absent beats inferred: the allocating process is gone."""
        _runner_config(_config(tmp_path))
        state, client = _turn_state(tmp_path)
        state.sessions.get_or_create = unittest.mock.AsyncMock(return_value=(client, False, True))
        slot = _slot()
        assert slot._session_requested_model is None

        with self._capture() as opened:
            await _drive(state, slot)

        assert slot._session_requested_model is None
        assert opened.call_args.kwargs["model_requested"] == ""


class TestSessionOpenedCoversTheTierBelowTheCaller:
    """A model resolved INSIDE ``get_or_create`` reaches ``model_requested``.

    The turn selects through the slot pin, the crew pin and the resolved default.
    When all three defer it asks for nothing, and the session manager resolves an
    id from its own config; that call reports the provider, ``is_new`` and
    ``resumed``, so the turn has no selection of its own to record. These turns
    pin the stamp the allocation leaves as the fourth tier, and pin that a session
    with no selection anywhere still writes no field.
    """

    @staticmethod
    def _capture():
        return unittest.mock.patch.object(
            chat_runner.crew_log_emit, "on_session_opened", unittest.mock.MagicMock()
        )

    @staticmethod
    def _all_tiers_defer(tmp_path: Path) -> KiroCrewConfig:
        """A config whose global pin is empty, so the turn resolves ``""``."""
        return _load_config(
            tmp_path,
            {
                "agent": {"model": "", "provider": "acp"},
                "agents": {"default": {"kiro_agent": "kirocrew", "memory_store": "default"}},
                "default_agent": "default",
            },
        )

    @pytest.mark.asyncio
    async def test_the_allocations_own_resolution_is_recorded(self, tmp_path, _runner_config):
        """The regression: the turn asks for nothing, the allocation resolves one."""
        _runner_config(self._all_tiers_defer(tmp_path))
        state, _client = _turn_state(tmp_path)
        state.sessions.allocation_requested_model = unittest.mock.MagicMock(
            return_value=GLOBAL_DEFAULT
        )
        slot = _slot()

        with self._capture() as opened:
            await _drive(state, slot)

        assert _session_model(state) is None
        assert slot._session_requested_model == GLOBAL_DEFAULT
        assert opened.call_args.kwargs["model_requested"] == GLOBAL_DEFAULT

    @pytest.mark.asyncio
    async def test_no_tier_anywhere_still_records_nothing(self, tmp_path, _runner_config):
        """An allocation that resolved nothing writes no field, not an empty one."""
        _runner_config(self._all_tiers_defer(tmp_path))
        state, _client = _turn_state(tmp_path)
        slot = _slot()

        with self._capture() as opened:
            await _drive(state, slot)

        assert slot._session_requested_model == ""
        assert opened.call_args.kwargs["model_requested"] == ""

    @pytest.mark.asyncio
    async def test_the_allocations_stamp_beats_a_fresh_re_resolution(
        self, tmp_path, _runner_config
    ):
        """The stamp wins, because the branch cannot tell prewarmed from cold.

        A prewarmed session arms ``FirstTurnState.FRESH``, whose ``is_new`` is True
        and ``resumed`` is False, so a claim of one arrives here exactly as a cold
        start does. Reading this turn's own resolution first would write a model the
        session never ran on whenever config moved between the allocation and the
        first turn -- into an entry nothing rewrites.
        """
        _runner_config(_config(tmp_path))
        state, _client = _turn_state(tmp_path)
        state.sessions.allocation_requested_model = unittest.mock.MagicMock(
            return_value="model-the-allocation-chose"
        )
        slot = _slot()

        with self._capture() as opened:
            await _drive(state, slot)

        assert _session_model(state) == GLOBAL_DEFAULT
        assert slot._session_requested_model == "model-the-allocation-chose"
        assert opened.call_args.kwargs["model_requested"] == "model-the-allocation-chose"

    @pytest.mark.asyncio
    async def test_a_session_that_stamped_nothing_falls_back_to_this_turn(
        self, tmp_path, _runner_config
    ):
        """A registration site that resolves no model stamps ``""``.

        The fallback is what keeps the top three tiers recorded for such a session
        rather than regressing them to absent.
        """
        _runner_config(_config(tmp_path))
        state, _client = _turn_state(tmp_path)
        slot = _slot()

        with self._capture() as opened:
            await _drive(state, slot)

        assert state.sessions.allocation_requested_model.return_value == ""
        assert slot._session_requested_model == GLOBAL_DEFAULT
        assert opened.call_args.kwargs["model_requested"] == GLOBAL_DEFAULT
