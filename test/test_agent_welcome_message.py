"""An agent's ``welcomeMessage`` renders once when the agent becomes active.

Two halves, and the split matters:

* the READER (``agent_discovery.spec_welcome_message`` /
  ``agent_welcome_message``) — coercion, the length ceiling, and which of two
  specs declaring the same name is the live one;
* the EMITTER (``chat_runner._surface_agent_welcome`` and its two call sites) —
  that the hint lands as a durable ``notice`` row the model never replays, that
  it lands exactly once per activation, and that untrusted text is redacted
  before it reaches the persisted window.

``agent_welcome_message`` is the field's only reader, and the bundled
pptx-maker agents ship values for it, so both the reader and those agents are
covered here. Tests use a ``tmp_path`` fake ``$HOME`` so the real agents
directory is never read.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state

from kiro_crew.agent_discovery import (
    SCOPE_PROJECT,
    WELCOME_MESSAGE_MAX_CHARS,
    agent_welcome_message,
    clear_list_agents_cache,
    list_agents,
    spec_welcome_message,
)
from kiro_crew.dashboard.state import _TRANSIENT_ROLES

_HINT = "Drop a URL and I will turn it into slides."


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """A $HOME no test may reach outside of, matching test_agent_discovery.py."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


def _agents_dir(home: Path) -> Path:
    d = home / ".kiro" / "agents"
    d.mkdir(parents=True)
    return d


def _project_agents_dir(root: Path) -> Path:
    d = root / ".kiro" / "agents"
    d.mkdir(parents=True)
    return d


class TestSpecWelcomeMessage:
    """Coercion and the ceiling, on a parsed spec — no filesystem."""

    def test_plain_string_is_returned(self) -> None:
        assert spec_welcome_message({"welcomeMessage": _HINT}) == _HINT

    def test_absent_key_renders_nothing(self) -> None:
        assert spec_welcome_message({"name": "a"}) == ""

    @pytest.mark.parametrize(
        "value",
        [None, 7, {"text": "hi"}, ["hi"], True],
        ids=["null", "int", "object", "list", "bool"],
    )
    def test_non_string_is_absent_not_an_error(self, value: object) -> None:
        """``~/.kiro/agents`` is shared with other tools, which spell fields freely.

        A structured value must read as "no hint", the same rule ``spec_str``
        applies to ``description``/``model`` — never a raise, and never a
        stringified dict rendered at the user.
        """
        assert spec_welcome_message({"welcomeMessage": value}) == ""

    def test_whitespace_only_renders_nothing(self) -> None:
        """Otherwise an author's blank line would surface as an empty bubble."""
        assert spec_welcome_message({"welcomeMessage": "  \n\t "}) == ""

    def test_surrounding_whitespace_is_stripped(self) -> None:
        assert spec_welcome_message({"welcomeMessage": f"\n  {_HINT}  \n"}) == _HINT

    def test_at_the_ceiling_is_kept_verbatim(self) -> None:
        """The boundary is inclusive — exactly the cap is not truncated."""
        exact = "x" * WELCOME_MESSAGE_MAX_CHARS
        assert spec_welcome_message({"welcomeMessage": exact}) == exact

    def test_over_the_ceiling_is_truncated_with_an_ellipsis(self) -> None:
        """The row is persisted and re-broadcast on every restore, so an
        unbounded config value must not become an unbounded transcript row."""
        out = spec_welcome_message({"welcomeMessage": "y" * (WELCOME_MESSAGE_MAX_CHARS + 500)})
        # The ellipsis is INSIDE the budget: the ceiling is the promise, so a
        # result one character over it would break the only guarantee made.
        assert len(out) == WELCOME_MESSAGE_MAX_CHARS
        assert out.endswith("\u2026")
        assert out[:-1] == "y" * (WELCOME_MESSAGE_MAX_CHARS - 1)


class TestAgentWelcomeMessageResolution:
    """Which spec on disk is the one whose hint is shown."""

    def test_reads_the_user_level_spec(self, fake_home, tmp_path) -> None:
        d = _agents_dir(fake_home)
        (d / "helper.json").write_text(json.dumps({"name": "helper", "welcomeMessage": _HINT}))
        clear_list_agents_cache()
        assert agent_welcome_message("helper", agents_dir=d) == _HINT

    def test_unknown_agent_renders_nothing(self, fake_home) -> None:
        d = _agents_dir(fake_home)
        (d / "helper.json").write_text(json.dumps({"name": "helper", "welcomeMessage": _HINT}))
        clear_list_agents_cache()
        assert agent_welcome_message("absent", agents_dir=d) == ""

    def test_empty_agent_name_renders_nothing(self, fake_home) -> None:
        """The default crew has no spec of its own, so it can carry no hint."""
        d = _agents_dir(fake_home)
        clear_list_agents_cache()
        assert agent_welcome_message("", agents_dir=d) == ""

    def test_project_spec_shadows_the_user_level_one(self, fake_home, tmp_path) -> None:
        """``list_agents`` shows the PROJECT entry for a shadowed name because that
        is the spec kiro-cli resolves ``--agent`` against; the hint must come from
        the same file or the greeting describes an agent that is not running."""
        d = _agents_dir(fake_home)
        (d / "dup.json").write_text(json.dumps({"name": "dup", "welcomeMessage": "user level"}))
        proj = tmp_path / "repo"
        (_project_agents_dir(proj) / "dup.json").write_text(
            json.dumps({"name": "dup", "welcomeMessage": "project level"})
        )
        clear_list_agents_cache()
        assert agent_welcome_message("dup", project=str(proj), agents_dir=d) == "project level"

    def test_project_only_agent_is_found(self, fake_home, tmp_path) -> None:
        d = _agents_dir(fake_home)
        proj = tmp_path / "repo"
        (_project_agents_dir(proj) / "repobot.json").write_text(
            json.dumps({"name": "repobot", "welcomeMessage": _HINT})
        )
        clear_list_agents_cache()
        assert agent_welcome_message("repobot", project=str(proj), agents_dir=d) == _HINT

    def test_user_level_still_wins_when_the_project_does_not_define_it(
        self, fake_home, tmp_path
    ) -> None:
        """The project scan must not swallow the user-level answer for a name it
        does not carry."""
        d = _agents_dir(fake_home)
        (d / "helper.json").write_text(json.dumps({"name": "helper", "welcomeMessage": _HINT}))
        proj = tmp_path / "repo"
        (_project_agents_dir(proj) / "other.json").write_text(json.dumps({"name": "other"}))
        clear_list_agents_cache()
        assert agent_welcome_message("helper", project=str(proj), agents_dir=d) == _HINT

    def test_the_hint_is_read_from_the_file_the_roster_names(self, fake_home, tmp_path) -> None:
        """The law the reader now holds BY CONSTRUCTION, over a mixed directory.

        Nothing here re-states a precedence rule: it asserts only that the hint
        equals the ``welcomeMessage`` of whichever file ``list_agents`` selected.
        That is the whole contract, and it cannot drift from the roster the way a
        second copy of the rules could.
        """
        d = _agents_dir(fake_home)
        (d / "helper.json").write_text(json.dumps({"name": "helper", "welcomeMessage": "bare"}))
        (d / "pkg-helper.json").write_text(json.dumps({"name": "helper", "welcomeMessage": "pkg"}))
        (d / "solo.json").write_text(json.dumps({"name": "solo", "welcomeMessage": "solo hint"}))
        (d / "named-file.json").write_text(json.dumps({"welcomeMessage": "stem only"}))
        proj = tmp_path / "repo"
        pdir = _project_agents_dir(proj)
        (pdir / "shadow.json").write_text(
            json.dumps({"name": "helper", "welcomeMessage": "project wins"})
        )
        clear_list_agents_cache()

        roster = {a.name: a for a in list_agents(agents_dir=d, project_dir=str(proj))}
        assert {"helper", "solo", "named-file"} <= set(roster)
        for name, row in roster.items():
            directory = pdir if row.scope == SCOPE_PROJECT else d
            spec_file = directory / row.filename
            if not spec_file.is_file():
                continue  # an edition row with no file on disk
            expected = spec_welcome_message(json.loads(spec_file.read_text()))
            assert agent_welcome_message(name, project=str(proj), agents_dir=d) == expected

    def test_a_corrupt_spec_beside_a_good_one_does_not_hide_it(self, fake_home) -> None:
        """The reader is best-effort: an unparseable neighbour is skipped, not fatal."""
        d = _agents_dir(fake_home)
        (d / "broken.json").write_text("{ not json")
        (d / "helper.json").write_text(json.dumps({"name": "helper", "welcomeMessage": _HINT}))
        clear_list_agents_cache()
        assert agent_welcome_message("helper", agents_dir=d) == _HINT

    def test_missing_agents_directory_renders_nothing(self, fake_home, tmp_path) -> None:
        clear_list_agents_cache()
        assert agent_welcome_message("helper", agents_dir=tmp_path / "nope") == ""

    def test_ceiling_applies_to_a_spec_read_from_disk(self, fake_home) -> None:
        """The cap is the reader's contract, not something each caller re-checks."""
        d = _agents_dir(fake_home)
        (d / "loud.json").write_text(
            json.dumps({"name": "loud", "welcomeMessage": "z" * (WELCOME_MESSAGE_MAX_CHARS * 3)})
        )
        clear_list_agents_cache()
        out = agent_welcome_message("loud", agents_dir=d)
        assert len(out) == WELCOME_MESSAGE_MAX_CHARS


class TestBundledAgentsShipAReadableHint:
    """The project's own agents demonstrated the trap; they must now work."""

    def test_every_bundled_pptx_maker_hint_is_readable(self) -> None:
        import kiro_crew

        agents = Path(kiro_crew.__file__).parent / "apps/builtins/pptx_maker/agents"
        specs = sorted(agents.glob("*.json"))
        assert specs, f"no bundled pptx_maker agent specs under {agents}"
        rendered = {
            p.stem: spec_welcome_message(json.loads(p.read_text(encoding="utf-8"))) for p in specs
        }
        assert [s for s, text in rendered.items() if text], (
            "the bundled agents that ship a welcomeMessage must now render one: " f"{rendered}"
        )


class TestSurfaceAgentWelcome:
    """The emitter: role, one-shot semantics, redaction, and failure tolerance."""

    @staticmethod
    def _slot(monkeypatch, tmp_path, hint: str, *, agent: str = "helper"):
        """A slot plus a stubbed reader, so no test here touches a real spec."""
        import kiro_crew.dashboard.chat_runner as runner

        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = state.get_or_create_slot("welcome-slot")
        slot.agent = agent
        reads: list[tuple[str, object]] = []

        def _fake_read(name, *, project=None):
            reads.append((name, project))
            return hint

        monkeypatch.setattr(runner, "agent_welcome_message", _fake_read)
        return state, slot, reads

    @pytest.mark.asyncio
    async def test_the_row_is_durable_so_it_survives_a_reload(self, monkeypatch, tmp_path) -> None:
        """The issue asks for a transcript entry, not an ephemeral hint: the role
        must be outside ``_TRANSIENT_ROLES``, which is what the save path and
        every durable reader key off."""
        from kiro_crew.dashboard.chat_runner import _surface_agent_welcome

        state, slot, _ = self._slot(monkeypatch, tmp_path, _HINT)
        await _surface_agent_welcome(state, slot, "helper")

        row = next(m for m in slot.messages if m.get("content") == _HINT)
        assert row["role"] not in _TRANSIENT_ROLES

    @pytest.mark.asyncio
    async def test_the_row_is_not_a_role_the_model_replays(self, monkeypatch, tmp_path) -> None:
        """The whole point of a transcript entry over a system-prompt injection is
        that it costs no per-turn context. Only ``user``/``assistant`` rows are
        replayed as conversation, so the hint must be neither."""
        from kiro_crew.dashboard.chat_runner import _surface_agent_welcome

        state, slot, _ = self._slot(monkeypatch, tmp_path, _HINT)
        await _surface_agent_welcome(state, slot, "helper")

        row = next(m for m in slot.messages if m.get("content") == _HINT)
        assert row["role"] not in ("user", "assistant")

    @pytest.mark.asyncio
    async def test_a_second_activation_of_the_same_agent_emits_nothing(
        self, monkeypatch, tmp_path
    ) -> None:
        """A switch and the session start its own reset produces are two events
        for ONE activation; the hint must not appear twice, and the disk read
        must not repeat either."""
        from kiro_crew.dashboard.chat_runner import _surface_agent_welcome

        state, slot, reads = self._slot(monkeypatch, tmp_path, _HINT)
        await _surface_agent_welcome(state, slot, "helper")
        await _surface_agent_welcome(state, slot, "helper")

        assert len([m for m in slot.messages if m.get("content") == _HINT]) == 1
        assert len(reads) == 1

    @pytest.mark.asyncio
    async def test_switching_to_a_different_agent_emits_again(self, monkeypatch, tmp_path) -> None:
        """The guard is per ACTIVATION, not per slot lifetime: a new agent taking
        over is a new greeting."""
        from kiro_crew.dashboard.chat_runner import _surface_agent_welcome

        state, slot, reads = self._slot(monkeypatch, tmp_path, _HINT)
        await _surface_agent_welcome(state, slot, "helper")
        await _surface_agent_welcome(state, slot, "other")

        assert len([m for m in slot.messages if m.get("content") == _HINT]) == 2
        assert [name for name, _ in reads] == ["helper", "other"]

    @pytest.mark.asyncio
    async def test_concurrent_activations_emit_once(self, monkeypatch, tmp_path) -> None:
        """The guard is claimed before the offload, so two coroutines racing the
        same activation cannot both get past it."""
        from kiro_crew.dashboard.chat_runner import _surface_agent_welcome

        state, slot, reads = self._slot(monkeypatch, tmp_path, _HINT)
        await asyncio.gather(
            _surface_agent_welcome(state, slot, "helper"),
            _surface_agent_welcome(state, slot, "helper"),
        )

        assert len([m for m in slot.messages if m.get("content") == _HINT]) == 1
        assert len(reads) == 1

    @pytest.mark.asyncio
    async def test_no_hint_appends_no_row(self, monkeypatch, tmp_path) -> None:
        from kiro_crew.dashboard.chat_runner import _surface_agent_welcome

        state, slot, _ = self._slot(monkeypatch, tmp_path, "")
        before = len(slot.messages)
        await _surface_agent_welcome(state, slot, "helper")

        assert len(slot.messages) == before

    @pytest.mark.asyncio
    async def test_empty_agent_name_reads_nothing(self, monkeypatch, tmp_path) -> None:
        from kiro_crew.dashboard.chat_runner import _surface_agent_welcome

        state, slot, reads = self._slot(monkeypatch, tmp_path, _HINT, agent="")
        await _surface_agent_welcome(state, slot, "")

        assert reads == []
        assert not [m for m in slot.messages if m.get("content") == _HINT]

    @pytest.mark.asyncio
    async def test_returning_to_an_agent_after_the_default_greets_again(
        self, monkeypatch, tmp_path
    ) -> None:
        """A -> default -> A is a NEW activation of A, so the hint must return.

        Skipping the empty-agent case without releasing the claim left
        ``_welcomed_agent`` naming A while the default crew answered, so A's
        second activation read as already welcomed. One-shot is per activation,
        not per slot lifetime.
        """
        from kiro_crew.dashboard.chat_runner import _surface_agent_welcome

        state, slot, reads = self._slot(monkeypatch, tmp_path, _HINT)
        await _surface_agent_welcome(state, slot, "helper")
        await _surface_agent_welcome(state, slot, "")  # the default crew
        await _surface_agent_welcome(state, slot, "helper")

        assert len([m for m in slot.messages if m.get("content") == _HINT]) == 2
        assert [name for name, _ in reads] == ["helper", "helper"]

    @pytest.mark.asyncio
    async def test_the_slot_project_scopes_the_read(self, monkeypatch, tmp_path) -> None:
        """A project-local agent's hint lives in the checkout, so the slot's
        project must reach the reader."""
        from kiro_crew.dashboard.chat_runner import _surface_agent_welcome

        state, slot, reads = self._slot(monkeypatch, tmp_path, _HINT)
        slot.project = "/repo/checkout"
        await _surface_agent_welcome(state, slot, "helper")

        assert reads == [("helper", "/repo/checkout")]

    @pytest.mark.asyncio
    async def test_a_credential_in_the_hint_is_redacted(self, monkeypatch, tmp_path) -> None:
        """The field is untrusted config data reaching a PERSISTED window and
        every open tab, so it goes through the same display redactors as any
        other foreign string on that path."""
        from kiro_crew.dashboard.chat_runner import _surface_agent_welcome

        secret = "AKIA" + "I" * 16
        state, slot, _ = self._slot(monkeypatch, tmp_path, f"use {secret} to start")
        await _surface_agent_welcome(state, slot, "helper")

        rows = [m for m in slot.messages if m.get("role") == "notice"]
        assert len(rows) == 1
        assert secret not in rows[0]["content"]

    @pytest.mark.asyncio
    async def test_an_unreadable_hint_never_fails_the_turn(self, monkeypatch, tmp_path) -> None:
        """Decoration must not be able to abort a chat turn."""
        import kiro_crew.dashboard.chat_runner as runner
        from kiro_crew.dashboard.chat_runner import _surface_agent_welcome

        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = state.get_or_create_slot("welcome-boom")
        slot.agent = "helper"

        def _boom(name, *, project=None):
            raise OSError("agents dir is unsearchable")

        monkeypatch.setattr(runner, "agent_welcome_message", _boom)
        before = len(slot.messages)
        await _surface_agent_welcome(state, slot, "helper")

        assert len(slot.messages) == before

    @pytest.mark.asyncio
    async def test_a_failed_read_is_not_retried_for_the_same_agent(
        self, monkeypatch, tmp_path
    ) -> None:
        """The guard is claimed before the read, so a broken agents directory
        cannot re-scan on every later event of the same activation."""
        import kiro_crew.dashboard.chat_runner as runner
        from kiro_crew.dashboard.chat_runner import _surface_agent_welcome

        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = state.get_or_create_slot("welcome-boom-once")
        slot.agent = "helper"
        calls: list[str] = []

        def _boom(name, *, project=None):
            calls.append(name)
            raise OSError("agents dir is unsearchable")

        monkeypatch.setattr(runner, "agent_welcome_message", _boom)
        await _surface_agent_welcome(state, slot, "helper")
        await _surface_agent_welcome(state, slot, "helper")

        assert calls == ["helper"]


class TestWelcomeMessageInARunnerTurn:
    """The two call sites, driven through ``_run_chat`` — real reader, no stub.

    The agent is written into the live agents directory and the materialized
    snapshot refreshed, because ``_run_chat`` refuses a ``slot.agent`` it cannot
    resolve (``UnknownMemoryStore``) — so a stubbed reader over an unresolvable
    name would never reach either call site.
    """

    @staticmethod
    def _install_agent(name: str, hint: str) -> None:
        from kiro_crew.config.loader import refresh_materialized_agents
        from kiro_crew.config.paths import kiro_agents_dir

        d = kiro_agents_dir()
        d.mkdir(parents=True, exist_ok=True)
        payload = {"name": name}
        if hint:
            payload["welcomeMessage"] = hint
        (d / f"{name}.json").write_text(json.dumps(payload), encoding="utf-8")
        clear_list_agents_cache()
        refresh_materialized_agents()

    @classmethod
    def _harness(cls, monkeypatch, tmp_path, hint: str, *, is_new: bool, agent: str):
        import kiro_crew.dashboard.chat_runner as runner

        if agent:
            cls._install_agent(agent, hint)

        state = _make_state(tmp_path)
        client = MagicMock()
        client.context_usage_pct = MagicMock(return_value=50.0)
        client.shutdown = AsyncMock()
        state.sessions.get_or_create = AsyncMock(return_value=(client, is_new, False))
        state.sessions.release = MagicMock()
        state.sessions.reset = AsyncMock()
        state.sessions.set_approval_policy = MagicMock()
        state.sessions.check_context_usage = MagicMock()
        state.sessions.record_success = MagicMock()
        state.sessions.record_failure = AsyncMock()
        state.sessions.get_slack_link = MagicMock(return_value=(None, None))
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.is_yolo_active = MagicMock(return_value=False)
        state._background_tasks = set()

        slot = state.get_or_create_slot("welcome-turn-slot")
        slot.agent = agent
        return state, slot, client, runner._run_chat

    @staticmethod
    async def _drain(state) -> None:
        tasks = list(state._background_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    @staticmethod
    def _text_stream(client):
        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        async def _stream(msg):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="ok")
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

        client.stream = _stream
        client.stream_command = _stream

    @pytest.mark.asyncio
    async def test_a_new_session_on_a_named_agent_renders_the_hint(
        self, monkeypatch, tmp_path
    ) -> None:
        state, slot, client, _run_chat = self._harness(
            monkeypatch, tmp_path, _HINT, is_new=True, agent="helper"
        )
        self._text_stream(client)
        try:
            await _run_chat(state, slot, "hello")
        finally:
            await self._drain(state)

        assert [m for m in slot.messages if m.get("content") == _HINT and m["role"] == "notice"]

    @pytest.mark.asyncio
    async def test_a_warm_session_renders_nothing(self, monkeypatch, tmp_path) -> None:
        """ "Becomes active" is the activation, not every turn: a warm session
        already greeted (or never had a hint to greet with)."""
        state, slot, client, _run_chat = self._harness(
            monkeypatch, tmp_path, _HINT, is_new=False, agent="helper"
        )
        self._text_stream(client)
        try:
            await _run_chat(state, slot, "hello")
        finally:
            await self._drain(state)

        assert not [m for m in slot.messages if m.get("content") == _HINT]

    @pytest.mark.asyncio
    async def test_a_second_turn_on_the_same_session_does_not_repeat_it(
        self, monkeypatch, tmp_path
    ) -> None:
        """The regression the issue's ``welcomeMessage``-in-the-system-prompt
        workaround suffers: re-sent every turn."""
        state, slot, client, _run_chat = self._harness(
            monkeypatch, tmp_path, _HINT, is_new=True, agent="helper"
        )
        self._text_stream(client)
        try:
            await _run_chat(state, slot, "hello")
            await _run_chat(state, slot, "again")
        finally:
            await self._drain(state)

        assert len([m for m in slot.messages if m.get("content") == _HINT]) == 1

    @pytest.mark.asyncio
    async def test_a_crew_alias_greets_with_its_resolved_template(
        self, monkeypatch, tmp_path
    ) -> None:
        """The spec that ANSWERS carries the hint, not the name the human picked.

        On a crew slot ``slot.agent`` is the member alias, which names no agent
        spec, while ``kiro_agent`` is the template the turn actually runs. Passing
        the alias made the roster lookup miss and dropped the running template's
        hint silently.
        """
        state, slot, client, _run_chat = self._harness(
            monkeypatch, tmp_path, "", is_new=True, agent="template-agent"
        )
        # Only the TEMPLATE carries a hint, so a row can only have come from it.
        self._install_agent("template-agent", _HINT)
        slot.agent = "crew-alias"

        import kiro_crew.dashboard.chat_runner as runner

        seen: list[str] = []
        real = runner._surface_agent_welcome

        async def _spy(state_arg, slot_arg, agent_arg):
            seen.append(agent_arg)
            return await real(state_arg, slot_arg, agent_arg)

        monkeypatch.setattr(runner, "_surface_agent_welcome", _spy)

        def _fake_bindings(cfg, name=None, project=None, **kw):
            from kiro_crew.config.loader import resolve_agent_bindings as _real

            bindings = _real(cfg, "template-agent", project)
            return bindings

        monkeypatch.setattr(runner, "resolve_agent_bindings", _fake_bindings)

        self._text_stream(client)
        try:
            await _run_chat(state, slot, "hello")
        finally:
            await self._drain(state)

        assert seen == ["template-agent"], f"greeted for {seen!r}, not the resolved template"
        assert [m for m in slot.messages if m.get("content") == _HINT]

    @pytest.mark.asyncio
    async def test_an_in_turn_agent_switch_renders_the_new_agent_hint(
        self, monkeypatch, tmp_path
    ) -> None:
        """``/agent <name>`` arrives as a provider-side switch; the greeting must
        follow the "Switched to agent" line it belongs beside."""
        from kiro_crew.providers.base import EVENT_AGENT_SWITCHED, EVENT_COMPLETE, LLMEvent

        state, slot, client, _run_chat = self._harness(
            monkeypatch, tmp_path, "", is_new=False, agent="helper"
        )
        # Only the agent being switched TO carries a hint, so the row proven
        # below cannot have come from the session-start site.
        self._install_agent("other", _HINT)

        async def _stream(msg):
            yield LLMEvent(kind=EVENT_AGENT_SWITCHED, text="other")
            yield LLMEvent(kind=EVENT_COMPLETE)

        client.stream = _stream
        client.stream_command = _stream
        try:
            await _run_chat(state, slot, "/agent other")
        finally:
            await self._drain(state)

        contents = [m.get("content", "") for m in slot.messages]
        switched = next(i for i, c in enumerate(contents) if "Switched to agent: other" in c)
        greeting = next(i for i, c in enumerate(contents) if c == _HINT)
        assert greeting > switched, "the greeting must read after the line announcing the switch"
        assert slot.messages[greeting]["role"] == "notice"
