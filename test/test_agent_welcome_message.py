"""An agent's ``welcomeMessage`` renders once when the agent becomes active.

Three parts, and the split matters:

* the READER (``agent_discovery.spec_welcome_message`` /
  ``agent_welcome_message``) — coercion, the length ceiling, and which of two
  specs declaring the same name is the live one;
* the EMITTER (``chat_runner._surface_agent_welcome`` and its two call sites) —
  that the hint lands as a durable ``notice`` row the model never replays, that
  it lands exactly once per activation, and that untrusted text is redacted
  before it reaches the persisted window;
* the WELCOME PAGE (``dashboard.agent_welcome``): an unstarted chat writes no
  row, its slot summary carries the greeting instead, and the row is tagged so
  the website's turn fold keeps it in view.

``agent_welcome_message`` is the field's only reader, so the reader is covered
here; an app whose bundled agents ship values for it pins those in its own
``tests/``. Tests use a ``tmp_path`` fake ``$HOME`` so the real agents directory is
never read.
"""

from __future__ import annotations

import asyncio
import json
import os
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
_WEBSITE = Path(__file__).resolve().parents[1] / "website" / "src"


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
        """A provider-side switch (kiro-cli's own ``/agent swap``, which Crew
        passes through); the greeting must follow the "Switched to agent" line it
        belongs beside."""
        from chat_test_helpers import pin_kiro_cli_harness

        pin_kiro_cli_harness()
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
            await _run_chat(state, slot, "/agent swap other")
        finally:
            await self._drain(state)

        contents = [m.get("content", "") for m in slot.messages]
        switched = next(i for i, c in enumerate(contents) if "Switched to agent: other" in c)
        greeting = next(i for i, c in enumerate(contents) if c == _HINT)
        assert greeting > switched, "the greeting must read after the line announcing the switch"
        assert slot.messages[greeting]["role"] == "notice"


class TestTheWelcomeRowIsTagged:
    """The row names its kind, so the dashboard's turn fold can keep it in view."""

    @pytest.mark.asyncio
    async def test_the_row_carries_the_agent_welcome_kind(self, monkeypatch, tmp_path) -> None:
        """Under the default collapse-all setting every untagged row before the
        answer folds, and the cold start writes this one inside the first turn."""
        from kiro_crew.dashboard.agent_welcome import AGENT_WELCOME_KIND
        from kiro_crew.dashboard.chat_runner import _surface_agent_welcome

        state, slot, _ = TestSurfaceAgentWelcome._slot(monkeypatch, tmp_path, _HINT)
        await _surface_agent_welcome(state, slot, "helper")

        row = next(m for m in slot.messages if m.get("content") == _HINT)
        assert row["meta"]["kind"] == AGENT_WELCOME_KIND == "agent_welcome"

    def test_the_website_keeps_the_kind_out_of_the_turn_fold(self) -> None:
        """A wire token: nothing links the two sides but the string."""
        from kiro_crew.dashboard.agent_welcome import AGENT_WELCOME_KIND

        source = (_WEBSITE / "pages" / "chat" / "TurnBlock.tsx").read_text(encoding="utf-8")
        predicate = source.split("const isAlwaysVisible", 1)[1].split("\n)\n", 1)[0]
        assert f"it.msg.meta?.kind === '{AGENT_WELCOME_KIND}'" in predicate


class TestTheWelcomePageShowsTheGreeting:
    """An unstarted chat writes no row; its slot summary carries the greeting.

    A pick on an empty chat is no boundary, and a row there would replace the
    welcome page, stack one greeting per agent tried and count as history for
    the member-memory gates. So the slot create and the picker commit write
    nothing into the transcript and ship the greeting as ``welcome_message``,
    and the workspace and project switches read it again for the new project.
    """

    @staticmethod
    def _state(monkeypatch, tmp_path):
        from kiro_crew.dashboard import chat_handlers

        TestWelcomeMessageInARunnerTurn._install_agent("helper", _HINT)
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock(return_value=True)
        monkeypatch.setattr(chat_handlers, "schedule_eager_spawn", lambda *a, **kw: None)
        return state

    @staticmethod
    async def _post(state, path: str, body: dict) -> tuple[int, dict]:
        from aiohttp.test_utils import TestClient, TestServer
        from chat_test_helpers import _make_app_with_agent_routes
        from dashboard_owner_helpers import as_owner

        from kiro_crew.dashboard.chat import api_chat_slot_project
        from kiro_crew.testing.wait import await_until

        app = as_owner(_make_app_with_agent_routes(state))
        app.router.add_post("/api/chat/slots/{slot}/project", api_chat_slot_project)
        async with TestClient(TestServer(app)) as client:
            response = await await_until(asyncio.ensure_future(client.post(path, json=body)))
            return response.status, await response.json()

    @pytest.mark.asyncio
    async def test_a_chat_created_on_the_agent_shows_it_and_writes_no_row(
        self, monkeypatch, tmp_path
    ) -> None:
        state = self._state(monkeypatch, tmp_path)
        status, body = await self._post(
            state,
            "/api/chat/slots",
            {"name": "welcome-new", "agent": "helper", "agent_kind": "template"},
        )

        assert status == 200, body
        slot = state._slots["welcome-new"]
        assert slot.messages == []
        assert body["welcome_message"] == _HINT
        assert state.serialize_slot(slot)["welcome_message"] == _HINT

    @pytest.mark.asyncio
    async def test_picking_the_agent_on_an_empty_chat_shows_it_and_writes_no_row(
        self, monkeypatch, tmp_path
    ) -> None:
        state = self._state(monkeypatch, tmp_path)
        status, body = await self._post(state, "/api/chat/slots", {"name": "welcome-pick"})
        assert status == 200, body
        assert body["welcome_message"] == ""

        status, body = await self._post(
            state,
            "/api/chat/slots/welcome-pick/agent",
            {"agent": "helper", "agent_kind": "template"},
        )

        assert status == 200 and body["ok"], body
        slot = state._slots["welcome-pick"]
        assert slot.messages == [], "a pick on an unstarted chat writes no row"
        assert state.serialize_slot(slot)["welcome_message"] == _HINT

    @pytest.mark.asyncio
    async def test_picking_the_default_again_takes_the_greeting_away(
        self, monkeypatch, tmp_path
    ) -> None:
        state = self._state(monkeypatch, tmp_path)
        await self._post(
            state,
            "/api/chat/slots",
            {"name": "welcome-back", "agent": "helper", "agent_kind": "template"},
        )
        status, body = await self._post(state, "/api/chat/slots/welcome-back/agent", {"agent": ""})

        assert status == 200 and body["ok"], body
        slot = state._slots["welcome-back"]
        assert slot.messages == []
        assert state.serialize_slot(slot)["welcome_message"] == ""

    @staticmethod
    def _project(tmp_path, name: str, hint: str = "") -> str:
        """A project directory, holding its own ``helper`` spec when *hint* is set."""
        root = tmp_path / name
        root.mkdir()
        if hint:
            (_project_agents_dir(root) / "helper.json").write_text(
                json.dumps({"name": "helper", "welcomeMessage": hint}), encoding="utf-8"
            )
        clear_list_agents_cache()
        return os.path.realpath(root)

    async def _switch_project(self, monkeypatch, tmp_path, route: str, project: str):
        """Create a chat on ``helper``, then move it to *project* through *route*.

        The workspace route sets the workspace's own directory as the project, so
        the workspace ``ws2`` is pointed at *project*.
        """
        from kiro_crew.dashboard import chat_handlers

        state = self._state(monkeypatch, tmp_path)
        key = f"welcome-{route}"
        status, body = await self._post(
            state, "/api/chat/slots", {"name": key, "agent": "helper", "agent_kind": "template"}
        )
        assert status == 200 and body["welcome_message"] == _HINT, body
        slot = state._slots[key]
        assert slot.project != project
        if route == "workspace":
            real = chat_handlers.default_project_dir
            monkeypatch.setattr(
                chat_handlers,
                "default_project_dir",
                lambda workspace=None: project if workspace == "ws2" else real(workspace),
            )
            status, body = await self._post(
                state, f"/api/chat/slots/{key}/workspace", {"workspace": "ws2"}
            )
        else:
            monkeypatch.setattr(chat_handlers, "_save_recent_project", lambda _project: None)
            status, body = await self._post(
                state, f"/api/chat/slots/{key}/project", {"project": project}
            )
        assert status == 200 and body["ok"], body
        assert slot.project == project
        return state, slot

    @pytest.mark.asyncio
    @pytest.mark.parametrize("route", ["workspace", "project"])
    async def test_a_switch_on_an_empty_chat_keeps_the_greeting(
        self, monkeypatch, tmp_path, route
    ) -> None:
        """The reading is kept for the project it was read in, so a switch that
        moves the project must read again, or the page loses the greeting."""
        project = self._project(tmp_path, "plain")
        state, slot = await self._switch_project(monkeypatch, tmp_path, route, project)

        assert slot.messages == [], "a switch on an unstarted chat writes no row"
        assert state.serialize_slot(slot)["welcome_message"] == _HINT

    @pytest.mark.asyncio
    @pytest.mark.parametrize("route", ["workspace", "project"])
    async def test_the_new_projects_own_spec_gives_the_greeting(
        self, monkeypatch, tmp_path, route
    ) -> None:
        """A project's own spec of the agent shadows the user-level one, so after
        the switch the page shows the greeting of the spec that will answer."""
        project = self._project(tmp_path, "repo", "Project level greeting.")
        state, slot = await self._switch_project(monkeypatch, tmp_path, route, project)

        assert slot.messages == []
        assert state.serialize_slot(slot)["welcome_message"] == "Project level greeting."

    @staticmethod
    def _park_the_first_greeting_read(monkeypatch) -> tuple[asyncio.Event, asyncio.Event]:
        """Hold the next greeting read until the test releases it.

        A pick reads the greeting after both switch locks are released, so a
        second switch can commit while the first one waits here.
        """
        from kiro_crew.dashboard import agent_welcome
        from kiro_crew.testing.wait import await_until

        real = agent_welcome.refresh_welcome_preview
        parked, release = asyncio.Event(), asyncio.Event()
        reads: list[str] = []

        async def _held(state, slot) -> None:
            reads.append(slot.key)
            if len(reads) == 1:
                parked.set()
                await await_until(release, what="the test releasing the parked greeting read")
            await real(state, slot)

        monkeypatch.setattr(agent_welcome, "refresh_welcome_preview", _held)
        return parked, release

    @pytest.mark.asyncio
    @pytest.mark.parametrize("second", ["agent", "workspace"])
    async def test_a_pick_answers_the_binding_the_slot_holds_after_its_greeting_read(
        self, monkeypatch, tmp_path, second
    ) -> None:
        """The acting tab writes the pick's answer into its store, after any slots
        frame a newer switch sent while the pick read its greeting. So the answer
        names the agent, namespace and workspace the slot holds once that read
        returns, not the ones the pick committed before it."""
        from aiohttp.test_utils import TestClient, TestServer
        from chat_test_helpers import _make_app_with_agent_routes
        from dashboard_owner_helpers import as_owner

        from kiro_crew.dashboard import chat_handlers
        from kiro_crew.testing.wait import await_until

        state = self._state(monkeypatch, tmp_path)
        TestWelcomeMessageInARunnerTurn._install_agent("other", "Another greeting.")
        key = "welcome-race"
        status, body = await self._post(state, "/api/chat/slots", {"name": key})
        assert status == 200, body
        project = self._project(tmp_path, "ws2-root")
        real = chat_handlers.default_project_dir
        monkeypatch.setattr(
            chat_handlers,
            "default_project_dir",
            lambda workspace=None: project if workspace == "ws2" else real(workspace),
        )
        parked, release = self._park_the_first_greeting_read(monkeypatch)

        async with TestClient(TestServer(as_owner(_make_app_with_agent_routes(state)))) as client:
            first = asyncio.ensure_future(
                client.post(
                    f"/api/chat/slots/{key}/agent",
                    json={"agent": "helper", "agent_kind": "template"},
                )
            )
            try:
                await await_until(parked, what="the first pick reaching its greeting read")
                if second == "agent":
                    path, payload = f"/api/chat/slots/{key}/agent", {
                        "agent": "other",
                        "agent_kind": "template",
                    }
                else:
                    path, payload = f"/api/chat/slots/{key}/workspace", {"workspace": "ws2"}
                newer = await await_until(asyncio.ensure_future(client.post(path, json=payload)))
                assert newer.status == 200, await newer.json()
            finally:
                release.set()
            answer = await await_until(first)
            body = await answer.json()

        slot = state._slots[key]
        assert answer.status == 200 and body["ok"], body
        assert slot.agent == ("other" if second == "agent" else "helper")
        assert (body["agent"], body["agent_kind"], body["workspace"]) == (
            slot.agent,
            slot.agent_kind,
            slot.workspace or "default",
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("route", ["workspace", "project"])
    async def test_a_switch_answers_the_value_the_slot_holds_after_its_greeting_read(
        self, monkeypatch, tmp_path, route
    ) -> None:
        """The workspace and project switches read the greeting after their locks
        too, so a newer switch of the same field can commit during that read. The
        answer names the value the slot holds once the read returns."""
        from aiohttp.test_utils import TestClient, TestServer
        from chat_test_helpers import _make_app_with_agent_routes
        from dashboard_owner_helpers import as_owner

        from kiro_crew.dashboard import chat_handlers
        from kiro_crew.dashboard.chat import api_chat_slot_project
        from kiro_crew.testing.wait import await_until

        state = self._state(monkeypatch, tmp_path)
        key = f"welcome-race-{route}"
        status, body = await self._post(
            state, "/api/chat/slots", {"name": key, "agent": "helper", "agent_kind": "template"}
        )
        assert status == 200, body
        older, newer = self._project(tmp_path, "older"), self._project(tmp_path, "newer")
        real = chat_handlers.default_project_dir
        roots = {"ws2": older, "ws3": newer}
        monkeypatch.setattr(
            chat_handlers,
            "default_project_dir",
            lambda workspace=None: roots.get(workspace) or real(workspace),
        )
        monkeypatch.setattr(chat_handlers, "_save_recent_project", lambda _project: None)
        if route == "workspace":
            first_value, second_value = "ws2", "ws3"
        else:
            first_value, second_value = older, newer
        parked, release = self._park_the_first_greeting_read(monkeypatch)

        app = as_owner(_make_app_with_agent_routes(state))
        app.router.add_post("/api/chat/slots/{slot}/project", api_chat_slot_project)
        path = f"/api/chat/slots/{key}/{route}"
        async with TestClient(TestServer(app)) as client:
            first = asyncio.ensure_future(client.post(path, json={route: first_value}))
            try:
                await await_until(
                    parked, what=f"the first {route} switch reaching its greeting read"
                )
                second = await await_until(
                    asyncio.ensure_future(client.post(path, json={route: second_value}))
                )
                assert second.status == 200, await second.json()
            finally:
                release.set()
            answer = await await_until(first)
            body = await answer.json()

        slot = state._slots[key]
        assert answer.status == 200 and body["ok"], body
        assert getattr(slot, route) == second_value
        assert body[route] == getattr(slot, route)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("route", ["agent", "workspace", "project"])
    async def test_a_switch_whose_chat_is_replaced_during_its_greeting_read_answers_404(
        self, monkeypatch, tmp_path, route
    ) -> None:
        """A chat closed and opened again under its name while a switch reads the
        greeting is another slot, and the acting tab writes the answer onto the row
        of that name. So the switch answers the 404 a missing chat gets, never the
        binding of the slot it switched."""
        from aiohttp.test_utils import TestClient, TestServer
        from chat_test_helpers import _make_app_with_agent_routes
        from dashboard_owner_helpers import as_owner

        from kiro_crew.dashboard import chat_handlers
        from kiro_crew.dashboard.chat import api_chat_slot_project
        from kiro_crew.testing.wait import await_until

        state = self._state(monkeypatch, tmp_path)
        key = f"welcome-replaced-{route}"
        status, body = await self._post(state, "/api/chat/slots", {"name": key})
        assert status == 200, body
        switched = state._slots[key]
        project = self._project(tmp_path, "picked")
        real = chat_handlers.default_project_dir
        monkeypatch.setattr(
            chat_handlers,
            "default_project_dir",
            lambda workspace=None: project if workspace == "ws2" else real(workspace),
        )
        monkeypatch.setattr(chat_handlers, "_save_recent_project", lambda _project: None)
        payload = {
            "agent": {"agent": "helper", "agent_kind": "template"},
            "workspace": {"workspace": "ws2"},
            "project": {"project": project},
        }[route]
        parked, release = self._park_the_first_greeting_read(monkeypatch)

        app = as_owner(_make_app_with_agent_routes(state))
        app.router.add_post("/api/chat/slots/{slot}/project", api_chat_slot_project)
        async with TestClient(TestServer(app)) as client:
            first = asyncio.ensure_future(
                client.post(f"/api/chat/slots/{key}/{route}", json=payload)
            )
            try:
                await await_until(parked, what=f"the {route} switch reaching its greeting read")
                closed = await await_until(
                    asyncio.ensure_future(client.delete(f"/api/chat/slots/{key}"))
                )
                assert closed.status == 200, await closed.json()
                reopened = await await_until(
                    asyncio.ensure_future(client.post("/api/chat/slots", json={"name": key}))
                )
                assert reopened.status == 200, await reopened.json()
            finally:
                release.set()
            answer = await await_until(first)
            body = await answer.json()

        assert state._slots[key] is not switched
        assert (answer.status, body.get("code")) == (404, "slot_not_found"), body

    @pytest.mark.asyncio
    async def test_a_create_whose_chat_is_closed_during_its_greeting_read_answers_404(
        self, monkeypatch, tmp_path
    ) -> None:
        """The slots frame lists a new chat before its create answers, so it can be
        closed while the create reads the greeting. The browser adds the create's
        answer to the list and drops the close's tombstone for that key, so a
        success there would bring back a chat the gateway has closed. The create
        answers the 404 a missing chat gets instead."""
        from aiohttp.test_utils import TestClient, TestServer
        from chat_test_helpers import _make_app_with_agent_routes
        from dashboard_owner_helpers import as_owner

        from kiro_crew.testing.wait import await_until

        state = self._state(monkeypatch, tmp_path)
        key = "welcome-closed-on-create"
        parked, release = self._park_the_first_greeting_read(monkeypatch)

        async with TestClient(TestServer(as_owner(_make_app_with_agent_routes(state)))) as client:
            created = asyncio.ensure_future(
                client.post(
                    "/api/chat/slots",
                    json={"name": key, "agent": "helper", "agent_kind": "template"},
                )
            )
            try:
                await await_until(parked, what="the create reaching its greeting read")
                assert key in state._slots
                closed = await await_until(
                    asyncio.ensure_future(client.delete(f"/api/chat/slots/{key}"))
                )
                assert closed.status == 200, await closed.json()
            finally:
                release.set()
            answer = await await_until(created)
            body = await answer.json()

        assert key not in state._slots
        assert (answer.status, body.get("code")) == (404, "slot_not_found"), body

    @pytest.mark.asyncio
    async def test_the_first_turn_writes_the_greeting_once_and_the_page_lets_go(
        self, monkeypatch, tmp_path
    ) -> None:
        """From the first row on the summary ships no greeting: the cold start's
        tagged row carries it, exactly once."""
        state = self._state(monkeypatch, tmp_path)
        await self._post(
            state,
            "/api/chat/slots",
            {"name": "welcome-turn-slot", "agent": "helper", "agent_kind": "template"},
        )
        slot = state._slots["welcome-turn-slot"]
        assert state.serialize_slot(slot)["welcome_message"] == _HINT

        import kiro_crew.dashboard.chat_runner as runner

        # The runner harness above, on this state: a new session that answers "ok".
        client = MagicMock()
        client.context_usage_pct = MagicMock(return_value=50.0)
        client.shutdown = AsyncMock()
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
        state.sessions.release = MagicMock()
        state.sessions.set_approval_policy = MagicMock()
        state.sessions.check_context_usage = MagicMock()
        state.sessions.record_success = MagicMock()
        state.sessions.record_failure = AsyncMock()
        state.sessions.get_slack_link = MagicMock(return_value=(None, None))
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.is_yolo_active = MagicMock(return_value=False)
        state._background_tasks = set()
        TestWelcomeMessageInARunnerTurn._text_stream(client)
        try:
            await runner._run_chat(state, slot, "hello")
        finally:
            await TestWelcomeMessageInARunnerTurn._drain(state)

        rows = [m for m in slot.messages if m.get("content") == _HINT]
        assert [(m["role"], m["meta"]["kind"]) for m in rows] == [("notice", "agent_welcome")]
        assert state.serialize_slot(slot)["welcome_message"] == ""

    def test_a_reading_for_another_binding_shows_nothing(self, tmp_path) -> None:
        """A path that moves the agent or the project without a fresh read leaves
        a stale reading, which must read as no greeting, never another agent's."""
        from kiro_crew.dashboard.agent_welcome import welcome_preview

        slot = _make_state(tmp_path).get_or_create_slot("welcome-stale")
        slot.agent = "helper"
        slot._welcome_preview = ("helper", "", slot.project or "", _HINT)
        assert welcome_preview(slot) == _HINT
        slot.agent = "other"
        assert welcome_preview(slot) == ""
        slot.agent = "helper"
        slot.project = "/elsewhere"
        assert welcome_preview(slot) == ""

    @pytest.mark.asyncio
    async def test_the_page_and_the_row_show_the_same_redacted_text(
        self, monkeypatch, tmp_path
    ) -> None:
        """Untrusted spec text: the page reading goes through the redactors the
        row does, so a credential in the hint reaches neither."""
        from kiro_crew.dashboard.agent_welcome import refresh_welcome_preview
        from kiro_crew.dashboard.chat_runner import _surface_agent_welcome

        secret = "AKIA" + "I" * 16
        state, slot, _ = TestSurfaceAgentWelcome._slot(monkeypatch, tmp_path, f"use {secret} now")
        state.push_slots_update = MagicMock()
        await refresh_welcome_preview(state, slot)
        page = state.serialize_slot(slot)["welcome_message"]
        await _surface_agent_welcome(state, slot, "helper")
        row = next(m for m in slot.messages if m.get("role") == "notice")

        assert secret not in page
        assert page == row["content"]
        state.push_slots_update.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_a_chat_that_has_rows_reads_nothing(self, monkeypatch, tmp_path) -> None:
        from kiro_crew.dashboard.agent_welcome import refresh_welcome_preview

        state, slot, reads = TestSurfaceAgentWelcome._slot(monkeypatch, tmp_path, _HINT)
        slot.append("user", "hello", "msg msg-u")
        await refresh_welcome_preview(state, slot)

        assert reads == []
        assert state.serialize_slot(slot)["welcome_message"] == ""

    def test_the_welcome_page_reads_the_summary_field(self) -> None:
        """The field name crosses the wire: the transcript view hands the
        summary's ``welcome_message`` to the welcome page."""
        view = (_WEBSITE / "pages" / "chat" / "page" / "ChatTranscriptView.tsx").read_text(
            encoding="utf-8"
        )
        assert "agentWelcome={currentSlot?.welcome_message}" in view
