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
    return _load_config(
        tmp_path,
        {
            "agent": {"model": GLOBAL_DEFAULT, "provider": "acp"},
            "agents": {"researcher": {"kiro_agent": "kirocrew", "model": crew_model}},
            "default_agent": "kirocrew",
        },
    )


def _turn_state(tmp_path: Path):
    state, client = _runner_state(tmp_path)
    _set_stream(client, [LLMEvent(kind=EVENT_TEXT_CHUNK, text="hi"), _complete()])
    return state, client


def _session_model(state) -> str | None:
    state.sessions.get_or_create.assert_awaited_once()
    return state.sessions.get_or_create.await_args.kwargs["model"]


@pytest.fixture
def _runner_config(tmp_path):
    """Serve the real config object to every ``KiroCrewConfig.load()`` in the turn."""

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
        would no longer reach it. An empty value must survive the turn.
        """
        _runner_config(_config(tmp_path))
        state, _client = _turn_state(tmp_path)
        slot = _slot()

        await _drive(state, slot)

        assert _session_model(state) == GLOBAL_DEFAULT
        assert slot.model == ""

    @pytest.mark.asyncio
    async def test_crew_pin_outranks_the_global_default(self, tmp_path, _runner_config):
        _runner_config(_config(tmp_path, crew_model=CREW_PIN))
        state, _client = _turn_state(tmp_path)
        slot = _slot()
        slot.agent = "researcher"

        await _drive(state, slot)

        assert _session_model(state) == CREW_PIN
        assert slot.model == ""

    @pytest.mark.asyncio
    async def test_explicit_slot_pin_is_untouched(self, tmp_path, _runner_config):
        _runner_config(_config(tmp_path, crew_model=CREW_PIN))
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
