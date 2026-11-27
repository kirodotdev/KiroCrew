"""The ``[AWS ACCESS]`` note: a chat is told once per context window what works there.

The common AWS failure is not a refusal, so advice that rides a refusal never
shows. The note rides the turn's context instead. It must not repeat on every
turn, must not be counted as told when its turn never reached the provider, and
must not go to a reader it is not written for.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew.context import ContextBuilder
from kiro_crew.context_blocks import split_blocks
from kiro_crew.mate_worker import MATE_CONDUCTOR_AGENT, MATE_WORKER_AGENT
from kiro_crew.memory import MemoryStore
from kiro_crew.messaging.turn_bracket import rollback_skill_bodies

pytestmark = pytest.mark.usefixtures("ample_host_resources")

SESSION = "dashboard:tab-1"
AWS_ACCESS_HEADER = "[AWS ACCESS]"


def _owner():
    """The module under test, imported when a test runs."""
    from kiro_crew.context_assembly import aws_access

    return aws_access


def turn_block(builder: ContextBuilder, session_key, agent, *, rebuilt: bool = False) -> str:
    return _owner().turn_block(builder, session_key, agent, rebuilt=rebuilt)


def _notes(prompt: str) -> int:
    """How many notes *prompt* carries. The shipped prompt names the header mid-line."""
    return sum(line.startswith(AWS_ACCESS_HEADER) for line in prompt.splitlines())


@pytest.fixture
def builder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ContextBuilder:
    """A builder on a host whose owner bound no folder."""
    monkeypatch.setattr(_owner(), "_folders_bound", lambda: False)
    return ContextBuilder(memory=MemoryStore(workspace=tmp_path / "ws"))


class TestOncePerContextWindow:
    def test_the_first_turn_carries_it_and_the_next_does_not(self, builder):
        first = turn_block(builder, SESSION, "kirocrew", rebuilt=True)
        rollback_skill_bodies(builder, SESSION, landed=True)

        assert first.startswith(f"{AWS_ACCESS_HEADER} What works for AWS in this chat.")
        assert first.endswith("\n\n")
        assert turn_block(builder, SESSION, "kirocrew") == ""

    def test_a_new_window_is_told_again(self, builder):
        first = turn_block(builder, SESSION, "kirocrew", rebuilt=True)
        rollback_skill_bodies(builder, SESSION, landed=True)

        assert turn_block(builder, SESSION, "kirocrew", rebuilt=True) == first

    def test_another_session_is_told_separately(self, builder):
        turn_block(builder, SESSION, "kirocrew", rebuilt=True)
        rollback_skill_bodies(builder, SESSION, landed=True)

        assert turn_block(builder, "dashboard:tab-2", "kirocrew")

    def test_the_record_is_bounded(self, builder, monkeypatch):
        monkeypatch.setattr(_owner(), "_TOLD_SESSIONS", 2)
        for tab in ("a", "b", "c"):
            turn_block(builder, f"dashboard:{tab}", "kirocrew")

        assert len(builder._aws_access_told) == 2


class TestToldOnlyWhenSent:
    def test_a_turn_that_did_not_land_leaves_the_session_untold(self, builder):
        first = turn_block(builder, SESSION, "kirocrew", rebuilt=True)
        rollback_skill_bodies(builder, SESSION, landed=False)

        assert turn_block(builder, SESSION, "kirocrew") == first

    def test_a_build_nobody_settled_is_sent_again(self, builder):
        """Two builds of one turn are the same, and a dropped prompt costs nothing."""
        first = turn_block(builder, SESSION, "kirocrew", rebuilt=True)

        assert turn_block(builder, SESSION, "kirocrew") == first
        rollback_skill_bodies(builder, SESSION, landed=True)
        assert turn_block(builder, SESSION, "kirocrew") == ""

    def test_a_later_failed_turn_does_not_take_back_a_note_that_landed(self, builder):
        turn_block(builder, SESSION, "kirocrew", rebuilt=True)
        rollback_skill_bodies(builder, SESSION, landed=True)
        assert turn_block(builder, SESSION, "kirocrew") == ""
        rollback_skill_bodies(builder, SESSION, landed=False)

        assert turn_block(builder, SESSION, "kirocrew") == ""

    def test_a_failed_turn_that_built_nothing_keeps_a_note_that_landed(self, builder):
        """A slash command or an early return builds no context, and its settle
        must not undo the turn before it."""
        turn_block(builder, SESSION, "kirocrew", rebuilt=True)
        rollback_skill_bodies(builder, SESSION, landed=True)
        rollback_skill_bodies(builder, SESSION, landed=False)

        assert turn_block(builder, SESSION, "kirocrew") == ""

    def test_settling_a_session_that_built_nothing_is_a_no_op(self, builder):
        rollback_skill_bodies(builder, SESSION, landed=False)
        rollback_skill_bodies(builder, SESSION, landed=True)

        assert builder._aws_access_told == {}


class TestWhoIsNotTold:
    def test_the_guest_agent_gets_none_and_no_record(self, builder):
        assert turn_block(builder, SESSION, "kirocrew-guest", rebuilt=True) == ""
        assert builder._aws_access_told == {}

    def test_a_sub_agents_session_gets_none_from_the_turn(self, builder):
        assert turn_block(builder, "subagent:run-1", "kirocrew", rebuilt=True) == ""

    def test_a_turn_with_no_session_key_gets_none(self, builder):
        assert turn_block(builder, None, "kirocrew", rebuilt=True) == ""

    def test_a_minimal_context_turn_gets_none_and_stays_untold(self, builder):
        minimal, _ = builder.build_message(
            "hello", is_new_session=True, session_key=SESSION, minimal_context=True
        )
        full, _ = builder.build_message("hello", is_new_session=False, session_key=SESSION)

        assert _notes(minimal) == 0
        assert _notes(full) == 1


class TestWhatEachReaderGets:
    def test_a_bots_own_chat_gets_its_own_line(self, builder):
        note = turn_block(builder, SESSION, MATE_CONDUCTOR_AGENT, rebuilt=True)

        assert note == f"{AWS_ACCESS_HEADER} Your worker does AWS work. This chat mounts only " + (
            "Kiro Crew's own servers, so hand an AWS task to your worker.\n\n"
        )

    def test_a_bots_worker_is_an_ordinary_session(self, builder):
        note = turn_block(builder, SESSION, MATE_WORKER_AGENT, rebuilt=True)

        assert "What works for AWS in this chat." in note

    def test_a_sub_agent_gets_one_line(self):
        line = _owner().subagent_line()

        assert line.startswith(f"{AWS_ACCESS_HEADER} Run `aws` plainly.")
        assert line.endswith("\n\n")
        assert "\n" not in line.rstrip("\n")

    def test_the_bound_folder_sentence_rides_only_when_a_folder_is_bound(
        self, builder, monkeypatch
    ):
        plain = turn_block(builder, SESSION, "kirocrew", rebuilt=True)
        monkeypatch.setattr(_owner(), "_folders_bound", lambda: True)
        bound = turn_block(builder, SESSION, "kirocrew", rebuilt=True)

        assert "aws.project_profiles" not in plain
        assert "That is a name, not a credential" in bound
        assert bound.startswith(plain.rstrip("\n"))

    def test_an_unreadable_config_counts_as_no_bound_folder(self, monkeypatch):
        from kiro_crew.config import loader

        def _boom():
            raise OSError("config unreadable")

        monkeypatch.setattr(loader.KiroCrewConfig, "load", staticmethod(_boom))

        assert _owner()._folders_bound() is False


class TestBuiltTurn:
    def test_the_prompt_carries_the_note_once_and_books_it_as_its_own_block(self, builder):
        first, _ = builder.build_message("hello", is_new_session=True, session_key=SESSION)
        rollback_skill_bodies(builder, SESSION, landed=True)
        second, _ = builder.build_message("hello", is_new_session=False, session_key=SESSION)

        assert _notes(first) == 1
        assert _notes(second) == 0
        assert split_blocks(first).get("aws_access", 0) > 0
