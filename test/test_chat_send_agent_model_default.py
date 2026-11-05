"""Auto-created chat slots start their session on the model the chip shows.

A slot created by sending a message has ``slot.model == ""``; the chip reads
:func:`resolve_effective_model`, and the session must start on that same answer.

The REAL ``_run_chat`` and ``_eager_spawn`` run against a session boundary that resolves
as ``SessionManager`` does (:func:`_allocation_resolves`). The resolved default is never
written into ``slot.model``, whose empty value means "inherit".
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
import unittest.mock
from pathlib import Path

import pytest
from test_chat_runner_coverage import _complete, _drive, _runner_state, _set_stream, _slot

from kiro_crew.acp.types import EVENT_TEXT_CHUNK, TurnUsage
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.chat_runner import _eager_spawn
from kiro_crew.member_memory_auth import bind_private_session_store
from kiro_crew.memory_stores import provision_member_memory
from kiro_crew.providers.base import LLMEvent
from kiro_crew.session import _session_model as _owner_session_model

#: The eager-spawn tests need a pre-warm allowance, which is zero while the
#: host's memory posture is critical (see ``ample_host_free_memory``).
pytestmark = pytest.mark.usefixtures("ample_host_free_memory")

GLOBAL_DEFAULT = "claude-opus-5"
CREW_PIN = "claude-sonnet-5"
SLOT_PIN = "claude-haiku-5"


def _load_config(tmp_path: Path, data: dict) -> KiroCrewConfig:
    """A real ``KiroCrewConfig`` loaded from *data* (same shape as config.json)."""
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps(data))
    with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg_file):
        return KiroCrewConfig.load()


def _config(
    tmp_path: Path, *, crew_model: str = "", default: str = GLOBAL_DEFAULT
) -> KiroCrewConfig:
    """Global ``agent.model`` is *default*; the crews pin nothing unless *crew_model*."""
    cfg = _load_config(
        tmp_path,
        {
            "agent": {"model": default, "provider": "acp"},
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

    The turn reads these without ``await``; as ``AsyncMock`` children each would leave an
    unawaited coroutine, reported at garbage collection against a later test.
    """
    client.mcp_session_report = unittest.mock.MagicMock(return_value=None)
    client.available_models = unittest.mock.MagicMock(return_value=[])
    client.client.pop_pending_oauth_requests = unittest.mock.MagicMock(return_value=[])


def _resolve_like_the_allocation(cfg: KiroCrewConfig, key: str, kwargs: dict) -> str | None:
    """The model ``get_or_create`` starts *key* on for these kwargs.

    The caller's own model when it passed one, else the session layer's own
    ladder over the session manager's config: the call the real allocation makes.
    """
    return kwargs.get("model") or _owner_session_model(
        cfg,
        kwargs.get("agent"),
        crew_agent=kwargs.get("crew_agent"),
        app_name=kwargs.get("app_name", ""),
        session_key=key,
    )


def _allocation_resolves(state, cfg: KiroCrewConfig) -> None:
    """Make the mocked session boundary resolve and stamp as ``SessionManager`` does.

    The session manager holds *cfg* (the watcher-refreshed config), a model-less
    allocation resolves the ladder from it, and ``allocation_requested_model``
    answers the stamp that allocation left.
    """
    state.sessions._cfg = cfg
    stamps: dict[str, str] = {}
    state.sessions.allocated_models = stamps
    answer = state.sessions.get_or_create.return_value

    async def _allocate(key, **kwargs):
        stamps[key] = _resolve_like_the_allocation(cfg, key, kwargs) or ""
        return answer

    state.sessions.get_or_create.side_effect = _allocate
    state.sessions.allocation_requested_model = unittest.mock.MagicMock(
        side_effect=lambda key: stamps.get(key, "")
    )


def _turn_state(tmp_path: Path):
    builder = unittest.mock.MagicMock()
    builder.ensure_store = unittest.mock.AsyncMock(return_value=object())
    builder.build_message.return_value = ("fixture context", None)
    state, client = _runner_state(tmp_path, context_builder=builder)
    _pin_sync_accessors(client)
    _set_stream(client, [LLMEvent(kind=EVENT_TEXT_CHUNK, text="hi"), _complete()])
    _allocation_resolves(state, chat_runner.KiroCrewConfig.load())
    return state, client


def _session_model(state) -> str | None:
    """The model the session this turn allocated starts on, resolved at allocation."""
    state.sessions.get_or_create.assert_awaited_once()
    return (
        state.sessions.allocated_models.get(state.sessions.get_or_create.await_args.args[0]) or None
    )


def _requested(state) -> str | None:
    """The ``model`` kwarg the turn itself handed ``get_or_create``."""
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
        """slot.model == "" and no crew pin -> the session gets ``agent.model``.

        The turn asks for no model: the allocation resolves the ladder itself.
        """
        _runner_config(_config(tmp_path))
        state, _client = _turn_state(tmp_path)
        slot = _slot()
        assert slot.model == "" and slot.agent == ""

        await _drive(state, slot)

        assert _requested(state) is None
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

        assert _requested(state) == CREW_PIN
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
        """A resolver error must not kill the turn; the session gets ``None``.

        ``None`` hands the choice to the provider factory's own fallback.
        """
        _runner_config(_config(tmp_path))
        state, client = _turn_state(tmp_path)
        slot = _slot()

        with unittest.mock.patch(
            "kiro_crew.config.loader.resolve_effective_model", side_effect=RuntimeError("boom")
        ):
            await _drive(state, slot)

        assert _requested(state) is None
        assert _session_model(state) is None
        assert slot.model == ""
        client.stream.assert_called()

    @pytest.mark.asyncio
    async def test_resolver_stop_iteration_does_not_kill_the_turn(self, tmp_path, _runner_config):
        """``StopIteration`` from the resolver is converted INSIDE the worker.

        A Future cannot carry ``StopIteration`` (``set_exception`` raises ``TypeError``),
        and ``resolve_agent_bindings`` raises it on a malformed config. The turn must
        complete with its session on ``None``.
        """
        _runner_config(_config(tmp_path))
        state, client = _turn_state(tmp_path)
        slot = _slot()

        with unittest.mock.patch(
            "kiro_crew.config.loader.resolve_effective_model",
            side_effect=StopIteration("malformed"),
        ):
            await _drive(state, slot)

        assert _session_model(state) is None
        assert slot.model == ""
        # The turn ran to the provider: the resolver failure was absorbed, not fatal.
        client.stream.assert_called()

    @pytest.mark.asyncio
    async def test_default_resolve_runs_off_the_event_loop(self, tmp_path, _runner_config):
        """The resolver globs and reads agent JSON; the turn must not run it on the loop.

        A worker thread has no running loop and is not the turn's thread. Only the turn's
        own call is observed: the mocked allocation resolves inline.
        """
        _runner_config(_config(tmp_path))
        state, _client = _turn_state(tmp_path)
        slot = _slot()
        seen: list[tuple[int, bool]] = []

        def _resolver(cfg, agent, **_kwargs):
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                loop_on_thread = False
            else:
                loop_on_thread = True
            seen.append((threading.get_ident(), loop_on_thread))
            return GLOBAL_DEFAULT

        with unittest.mock.patch("kiro_crew.config.loader.resolve_effective_model", _resolver):
            await _drive(state, slot)

        assert _session_model(state) == GLOBAL_DEFAULT
        turn_thread, turn_loop = seen[0]
        assert turn_thread != threading.get_ident()
        assert turn_loop is False


class TestLiveSessionFollowsDefaultChange:
    """An inheriting slot's live session follows a new chat default.

    The settings write leaves live sessions alone, so the next turn resets the session
    when its allocated model differs from the inherited default. A pin never does.
    """

    OLD_DEFAULT = "claude-sonnet-5"

    def _live_state(self, tmp_path: Path, stamped: str):
        state, client = _turn_state(tmp_path)
        client.has_active_turn = unittest.mock.MagicMock(return_value=False)
        state.sessions.get_provider = unittest.mock.MagicMock(return_value=client)
        state.sessions.allocation_requested_model = unittest.mock.MagicMock(return_value=stamped)
        state.sessions.reset = unittest.mock.AsyncMock(return_value=True)
        return state

    @staticmethod
    def _reset_keys(state) -> list[str]:
        return [c.args[0] for c in state.sessions.reset.await_args_list]

    @pytest.mark.asyncio
    async def test_unpinned_slot_resets_a_session_on_the_old_default(
        self, tmp_path, _runner_config
    ):
        _runner_config(_config(tmp_path))
        state = self._live_state(tmp_path, self.OLD_DEFAULT)
        slot = _slot()

        await _drive(state, slot)

        assert "dashboard:chat-cov-1" in self._reset_keys(state)
        assert _session_model(state) == GLOBAL_DEFAULT
        # Still inheriting: nothing was pinned by the reset.
        assert slot.model == ""

    @pytest.mark.asyncio
    async def test_reset_never_interrupts_a_busy_session(self, tmp_path, _runner_config):
        _runner_config(_config(tmp_path))
        state = self._live_state(tmp_path, self.OLD_DEFAULT)
        slot = _slot()

        await _drive(state, slot)

        ours = [
            c for c in state.sessions.reset.await_args_list if c.args[0] == "dashboard:chat-cov-1"
        ]
        assert ours
        assert all(c.kwargs.get("skip_if_busy") is True for c in ours)

    @pytest.mark.asyncio
    async def test_session_already_on_the_default_is_kept(self, tmp_path, _runner_config):
        _runner_config(_config(tmp_path))
        state = self._live_state(tmp_path, GLOBAL_DEFAULT)
        slot = _slot()

        await _drive(state, slot)

        assert "dashboard:chat-cov-1" not in self._reset_keys(state)

    @pytest.mark.asyncio
    async def test_slot_pin_is_never_reset(self, tmp_path, _runner_config):
        _runner_config(_config(tmp_path))
        state = self._live_state(tmp_path, self.OLD_DEFAULT)
        slot = _slot()
        slot.model = SLOT_PIN

        await _drive(state, slot)

        assert "dashboard:chat-cov-1" not in self._reset_keys(state)
        assert _session_model(state) == SLOT_PIN

    @pytest.mark.asyncio
    async def test_crew_pin_is_never_reset(self, tmp_path, _runner_config):
        cfg = _config(tmp_path, crew_model=CREW_PIN)
        _runner_config(cfg)
        _grant_researcher(cfg)
        state = self._live_state(tmp_path, self.OLD_DEFAULT)
        slot = _slot()
        slot.agent = "researcher"

        await _drive(state, slot)

        assert "dashboard:chat-cov-1" not in self._reset_keys(state)
        assert _session_model(state) == CREW_PIN

    @pytest.mark.asyncio
    async def test_serving_the_inherited_default_does_not_pin_the_slot(
        self, tmp_path, _runner_config
    ):
        """The backfill must not turn "inherit" into a pin on the default."""
        _runner_config(_config(tmp_path))
        state, client = _turn_state(tmp_path)
        client.client._model = GLOBAL_DEFAULT
        slot = _slot()

        await _drive(state, slot)

        assert slot.model == ""

    @pytest.mark.asyncio
    async def test_a_differently_spelled_served_model_does_not_pin_either(
        self, tmp_path, _runner_config
    ):
        """Any served id on an inheriting slot stays off ``slot.model``."""
        _runner_config(_config(tmp_path))
        state, client = _turn_state(tmp_path)
        client.client._model = SLOT_PIN
        slot = _slot()

        await _drive(state, slot)

        assert slot.model == ""

    @pytest.mark.asyncio
    async def test_activity_line_names_the_inherited_default(self, tmp_path, _runner_config):
        """The "Session created" line names the model run, not ``auto``."""
        _runner_config(_config(tmp_path))
        state, client = _turn_state(tmp_path)
        client.client._model = GLOBAL_DEFAULT
        client.served_model = GLOBAL_DEFAULT
        frames: list[dict] = []
        state.broadcast_ws = lambda kind, payload: frames.append(payload)
        slot = _slot()

        await _drive(state, slot)

        texts = [f.get("text", "") for f in frames if isinstance(f, dict)]
        created = [t for t in texts if t.startswith("Session created")]
        assert created and created[0].endswith(f"· {GLOBAL_DEFAULT}")
        assert slot.model == ""

    @pytest.mark.asyncio
    async def test_activity_line_names_the_served_model_when_the_default_is_withheld(
        self, tmp_path, _runner_config
    ):
        """Startup withheld the inherited default: the line names what runs."""
        _runner_config(_config(tmp_path))
        state, client = _turn_state(tmp_path)
        client.client._model = SLOT_PIN
        client.served_model = SLOT_PIN
        frames: list[dict] = []
        state.broadcast_ws = lambda kind, payload: frames.append(payload)
        slot = _slot()

        await _drive(state, slot)

        texts = [f.get("text", "") for f in frames if isinstance(f, dict)]
        created = [t for t in texts if t.startswith("Session created")]
        assert created and created[0].endswith(f"· {SLOT_PIN}")

    @pytest.mark.asyncio
    async def test_no_resolved_default_does_not_pin_either(self, tmp_path, _runner_config):
        """With every tier deferring to the backend, the served model stays off the slot."""
        _runner_config(_config(tmp_path))
        state, client = _turn_state(tmp_path)
        client.client._model = SLOT_PIN
        slot = _slot()

        with unittest.mock.patch(
            "kiro_crew.config.loader.resolve_effective_model", return_value=""
        ):
            await _drive(state, slot)

        assert slot.model == ""

    @pytest.mark.asyncio
    async def test_two_turns_across_a_default_change(self, tmp_path, _runner_config):
        """Turn 1 serves the old default; the default changes; turn 2 resets.

        Turn 1 resolves the old default through the resolver seam, so one real
        config serves both turns and turn 2 reads its new default.
        """
        _runner_config(_config(tmp_path))
        state, client = _turn_state(tmp_path)
        client.client._model = self.OLD_DEFAULT
        slot = _slot()

        with unittest.mock.patch(
            "kiro_crew.config.loader.resolve_effective_model", return_value=self.OLD_DEFAULT
        ):
            await _drive(state, slot)

        assert slot.model == ""
        assert _session_model(state) == self.OLD_DEFAULT

        # The session the reset cold-starts serves the new default.
        client.client._model = GLOBAL_DEFAULT
        client.has_active_turn = unittest.mock.MagicMock(return_value=False)
        state.sessions.get_provider = unittest.mock.MagicMock(return_value=client)
        state.sessions.allocation_requested_model = unittest.mock.MagicMock(
            return_value=self.OLD_DEFAULT
        )
        state.sessions.reset = unittest.mock.AsyncMock(return_value=True)
        state.sessions.get_or_create.reset_mock()
        _set_stream(client, [LLMEvent(kind=EVENT_TEXT_CHUNK, text="hi"), _complete()])

        await _drive(state, slot)

        assert "dashboard:chat-cov-1" in self._reset_keys(state)
        assert _session_model(state) == GLOBAL_DEFAULT
        assert slot.model == ""

    @pytest.mark.asyncio
    async def test_unknown_stamp_is_left_alone(self, tmp_path, _runner_config):
        """A session that recorded no selection is not reset on a guess."""
        _runner_config(_config(tmp_path))
        state = self._live_state(tmp_path, "")
        slot = _slot()

        await _drive(state, slot)

        assert "dashboard:chat-cov-1" not in self._reset_keys(state)


class TestInheritingSlotStaysUnpinned:
    """A slot with no slot pin and no crew pin stays unpinned after any turn.

    ``slot.model`` is re-sent as a ``set_model`` override on every resume, so writing the
    served model there would freeze the chat on it, Auto default included.
    """

    #: What the backend says it served: a concrete id that is none of the pins.
    SERVED = "claude-fable-5"

    @staticmethod
    def _no_installed_pin():
        """The installed agent file names no model, so Auto defers to the backend."""
        return unittest.mock.patch.object(
            KiroCrewConfig, "_resolve_agent_model", staticmethod(lambda: "")
        )

    @staticmethod
    def _learns_mid_turn(client, model: str) -> None:
        """The backend names *model* only once the prompt runs, on a billed turn.

        Only the late backfill site, just before the usage row, can see it then.
        """
        client.client._model = ""

        async def _events():
            client.client._model = model
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="hi")
            yield _complete(usage=TurnUsage(input_tokens=3, output_tokens=4))

        client.stream = unittest.mock.MagicMock(side_effect=lambda *_a, **_k: _events())

    @staticmethod
    @contextlib.contextmanager
    def _usage_rows():
        """Capture the model each usage row is written with, instead of writing it."""
        rows: list[str] = []

        async def _persist(_slot_key, model, _event, **_kwargs):
            rows.append(model)

        with unittest.mock.patch.object(chat_runner, "persist_token_record_async", _persist):
            yield rows

    @pytest.mark.asyncio
    async def test_an_auto_chat_follows_a_later_concrete_default(self, tmp_path, _runner_config):
        """Default Auto, one turn, then a concrete default: the next session runs it."""
        _runner_config(_config(tmp_path, default="auto"))
        state, client = _turn_state(tmp_path)
        client.client._model = self.SERVED
        slot = _slot()

        with self._no_installed_pin():
            await _drive(state, slot)

        assert slot.model == ""

        # The default becomes concrete; the next allocation must not be handed
        # the model the Auto turn was served.
        state.sessions.get_or_create.reset_mock()
        _set_stream(client, [LLMEvent(kind=EVENT_TEXT_CHUNK, text="hi"), _complete()])
        with unittest.mock.patch(
            "kiro_crew.config.loader.resolve_effective_model", return_value=GLOBAL_DEFAULT
        ):
            await _drive(state, slot)

        assert _requested(state) is None
        assert _session_model(state) == GLOBAL_DEFAULT
        assert slot.model == ""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("default", ["auto", GLOBAL_DEFAULT])
    async def test_a_model_learned_mid_turn_does_not_pin_either(
        self, tmp_path, _runner_config, default
    ):
        """The late site: the usage row names the served model, the slot stays unpinned."""
        _runner_config(_config(tmp_path, default=default))
        state, client = _turn_state(tmp_path)
        self._learns_mid_turn(client, self.SERVED)
        slot = _slot()

        with self._no_installed_pin(), self._usage_rows() as rows:
            await _drive(state, slot)

        assert rows == [self.SERVED]
        assert slot.model == ""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("agent", "pin"),
        [("", SLOT_PIN), ("researcher", CREW_PIN)],
        ids=["explicit-pick", "crewmate-child"],
    )
    async def test_a_pinned_slot_keeps_its_pin(self, tmp_path, _runner_config, agent, pin):
        """A user's pick, or the crewmate model a child chat was created with, survives."""
        cfg = _config(tmp_path, crew_model=CREW_PIN, default="auto")
        _runner_config(cfg)
        if agent:
            _grant_researcher(cfg)
        state, client = _turn_state(tmp_path)
        client.client._model = self.SERVED
        slot = _slot()
        slot.agent = agent
        slot.model = pin

        with self._no_installed_pin():
            await _drive(state, slot)

        assert _requested(state) == pin
        assert slot.model == pin

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mid_turn", [False, True], ids=["pre-turn-site", "late-site"])
    async def test_a_crew_pin_still_backfills_an_empty_slot(
        self, tmp_path, _runner_config, mid_turn
    ):
        """Out of this rule: a slot that pins nothing but whose crewmate does."""
        cfg = _config(tmp_path, crew_model=CREW_PIN, default="auto")
        _runner_config(cfg)
        _grant_researcher(cfg)
        state, client = _turn_state(tmp_path)
        if mid_turn:
            self._learns_mid_turn(client, self.SERVED)
        else:
            client.client._model = self.SERVED
        slot = _slot()
        slot.agent = "researcher"

        with self._no_installed_pin(), self._usage_rows():
            await _drive(state, slot)

        assert _requested(state) == CREW_PIN
        # The served id, not the pin: only the backfill writes what was served.
        assert slot.model == self.SERVED


class TestInheritedDefaultThreadBoundary:
    """The session resolver's ``except`` is what makes the turn's hop safe."""

    @pytest.mark.asyncio
    async def test_stop_iteration_is_converted_before_the_future(self, tmp_path):
        """``StopIteration`` from the resolver comes back as ``""``, not an error.

        A Future cannot carry ``StopIteration``: 3.12+ swaps in a ``RuntimeError``, older
        versions leave the future PENDING and the await hangs. The ``except Exception`` in
        ``session._session_model`` converts it inside the worker.
        """
        cfg = _config(tmp_path)
        sessions = unittest.mock.MagicMock()
        sessions._cfg = cfg

        with unittest.mock.patch(
            "kiro_crew.config.loader.resolve_effective_model",
            side_effect=StopIteration("malformed"),
        ):
            result = await chat_runner._inherited_default_model(
                sessions, _slot(), "dashboard:chat-cov-1", agent="kirocrew", crew_agent=""
            )

        assert result == ""

    @pytest.mark.asyncio
    async def test_a_manager_without_a_config_reads_nothing(self):
        """No config to resolve from means no inherited default, never a guess."""
        sessions = unittest.mock.MagicMock()

        result = await chat_runner._inherited_default_model(
            sessions, _slot(), "dashboard:chat-cov-1", agent="kirocrew", crew_agent=""
        )

        assert result == ""


class TestEagerSpawnDefaultModel:
    """The pre-warmed session must run the same model the first real turn would."""

    @pytest.fixture(autouse=True)
    def _no_debounce(self, monkeypatch):
        monkeypatch.setattr(chat_runner, "_EAGER_SPAWN_DEBOUNCE_SECS", 0)
        monkeypatch.setattr(chat_runner, "_eager_spawn_sem", asyncio.Semaphore(1))

    @pytest.mark.asyncio
    async def test_eager_session_starts_on_the_global_default(self, tmp_path, _runner_config):
        """The pre-warm asks for no model; the allocation resolves the ladder."""
        cfg = _config(tmp_path)
        _runner_config(cfg)
        state, _client = _runner_state(tmp_path)
        _pin_sync_accessors(_client)
        _allocation_resolves(state, cfg)
        slot = _slot()
        state._slots[slot.key] = slot
        state.sessions.release = unittest.mock.MagicMock()
        state.sessions.remove_if_unclaimed = unittest.mock.AsyncMock(return_value=True)

        await _eager_spawn(state, slot)

        assert _requested(state) is None
        assert _session_model(state) == GLOBAL_DEFAULT
        assert slot.model == ""


class TestSessionOpenedRecordsTheAllocationsSelection:
    """``session/opened.model_requested`` names the ALLOCATION's selection.

    An eager allocation can outlive a config change, so re-resolving at the first turn
    would write a model the session never used into an append-only entry.
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
        """A claim of a pre-warmed session that resumed must not re-resolve.

        The config default moved since the allocation; the entry still names what was
        allocated. The fresh-prewarm shape is in ``TestSessionOpenedCoversTheTierBelowTheCaller``.
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

    When slot pin, crew pin and default all defer, the session manager picks the id, so
    the allocation's stamp is the fourth tier. No selection anywhere writes no field.
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

        A prewarmed session arms ``FirstTurnState.FRESH`` and arrives exactly as a cold start
        does, so this turn's own resolution could name a model the session never ran on.
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

        The fallback is what keeps the turn's own selection (a slot or crew pin)
        recorded for such a session rather than regressing it to absent.
        """
        _runner_config(_config(tmp_path))
        state, _client = _turn_state(tmp_path)
        state.sessions.allocation_requested_model = unittest.mock.MagicMock(return_value="")
        slot = _slot()
        slot.model = SLOT_PIN

        with self._capture() as opened:
            await _drive(state, slot)

        assert slot._session_requested_model == SLOT_PIN
        assert opened.call_args.kwargs["model_requested"] == SLOT_PIN
