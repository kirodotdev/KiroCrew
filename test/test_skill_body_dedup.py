"""A triggered skill's body is injected at most once per provider session.

With ``skills.max_triggered`` above 0, a matched skill's full body is injected
at most once per provider session: the first match sends the body, and a later
match in the same session sends the cheap pointer line ``inject_on_trigger:
false`` skills already use. A monitor loop that re-sends the same message
therefore does not re-send the same 8k–34k-char body every cycle — the copies
would add nothing, because the provider replays native history.

The record fails SAFE: whenever it is reset — a fresh session, the first turn
after a compaction, an agent switch, an edited skill body, or simply no
session key at all — the body re-injects rather than falling silent. Every test
here fails if a reset stops re-injecting, or if a repeat stops being demoted.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from kiro_crew.config.loader import KiroCrewConfig, SkillsConfig
from kiro_crew.context import ContextBuilder
from kiro_crew.memory import MemoryStore
from kiro_crew.messaging import dispatch as D
from kiro_crew.messaging.dispatch import drive_turn
from kiro_crew.session import HEARTBEAT_KEY
from kiro_crew.skills import SkillsLoader

BODY_SENTINEL = "STEP ONE: pour the concrete before the rebar."
HINT_HEADER = "[Relevant skills for this message]"
SKILL_BLOCK = "[Skill: foundation]"
SESSION = "chat-session-abc"


@pytest.fixture(autouse=True)
def _close_skills_loaders(close_skills_loaders):
    """Every test builds a ``SkillsLoader``: close it so its ``skill-catalog-refresh`` thread does not leak across teardown (``test/conftest.py``)."""


def _write_skill(root: Path, name: str, *, body: str = BODY_SENTINEL) -> Path:
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Lay a foundation\ntriggers: zebra quokka\n---\n{body}",
        encoding="utf-8",
    )
    return d / "SKILL.md"


def _loader(skills_path: Path, *, cap: int = 3) -> SkillsLoader:
    return SkillsLoader(
        skills_path=skills_path,
        install_builtins=False,
        config=KiroCrewConfig(skills=SkillsConfig(max_triggered=cap)),
    )


def _builder(tmp_path: Path, skills: SkillsLoader) -> ContextBuilder:
    return ContextBuilder(memory=MemoryStore(workspace=tmp_path / "ws"), skills=skills)


def _send(builder: ContextBuilder, **kw: object) -> str:
    """Build one turn's message. Dedup records at build time, so a second call
    for the same session demotes without any separate confirmation step."""
    defaults: dict = {"is_new_session": False, "session_key": SESSION}
    defaults.update(kw)
    msg, _ = builder.build_message("zebra quokka", **defaults)  # type: ignore[arg-type]
    return msg


class TestSecondMatchInSessionDemotesToPointer:
    def test_first_send_injects_body_second_sends_pointer(self, tmp_path: Path) -> None:
        skills = tmp_path / "skills"
        path = _write_skill(skills, "foundation")
        builder = _builder(tmp_path, _loader(skills))

        first = _send(builder)
        assert BODY_SENTINEL in first
        assert SKILL_BLOCK in first
        assert HINT_HEADER not in first

        second = _send(builder)
        assert BODY_SENTINEL not in second
        assert SKILL_BLOCK not in second
        assert HINT_HEADER in second
        assert str(path) in second

    def test_third_and_later_matches_also_stay_demoted(self, tmp_path: Path) -> None:
        skills = tmp_path / "skills"
        _write_skill(skills, "foundation")
        builder = _builder(tmp_path, _loader(skills))

        _send(builder)
        for _ in range(4):
            assert BODY_SENTINEL not in _send(builder)


class TestNoSessionKeyNeverDedups:
    """A None session key keeps no record — today's always-send behaviour."""

    def test_every_match_injects_the_body(self, tmp_path: Path) -> None:
        skills = tmp_path / "skills"
        _write_skill(skills, "foundation")
        builder = _builder(tmp_path, _loader(skills))

        for _ in range(3):
            assert BODY_SENTINEL in _send(builder, session_key=None)


class TestKeylessBuildOnASession:
    """A caller that builds without a session key on a real provider session
    names that session in ``skill_bodies_session``, as the heartbeat does.

    The record is then kept under that session, exactly as under a session key,
    while the rest of the prompt stays what a keyless build produces.
    """

    def test_a_second_build_on_the_session_sends_the_pointer(self, tmp_path: Path) -> None:
        skills = tmp_path / "skills"
        path = _write_skill(skills, "foundation")
        builder = _builder(tmp_path, _loader(skills))

        first = _send(builder, session_key=None, skill_bodies_session=HEARTBEAT_KEY)
        assert BODY_SENTINEL in first

        second = _send(builder, session_key=None, skill_bodies_session=HEARTBEAT_KEY)
        assert BODY_SENTINEL not in second
        assert HINT_HEADER in second
        assert str(path) in second

    def test_a_fresh_session_sends_the_body_again(self, tmp_path: Path) -> None:
        skills = tmp_path / "skills"
        _write_skill(skills, "foundation")
        builder = _builder(tmp_path, _loader(skills))

        _send(builder, session_key=None, skill_bodies_session=HEARTBEAT_KEY)
        again = _send(
            builder, session_key=None, skill_bodies_session=HEARTBEAT_KEY, is_new_session=True
        )
        assert BODY_SENTINEL in again

    def test_naming_the_session_changes_nothing_else_in_the_prompt(self, tmp_path: Path) -> None:
        skills = tmp_path / "skills"
        _write_skill(skills, "foundation")
        loader = _loader(skills)

        named = _send(
            _builder(tmp_path / "named", loader),
            session_key=None,
            skill_bodies_session=HEARTBEAT_KEY,
        )
        keyless = _send(_builder(tmp_path / "keyless", loader), session_key=None)
        assert named == keyless


class TestUnlandedTurnFallsBackToPointerNotSilence:
    """A rare turn that builds but never lands demotes to a pointer, not silence.

    Recording at build time means such a turn's body is recorded, so the next
    match sends the pointer line instead of the body. That still tells the agent
    the skill applies — the same fail-safe an ``inject_on_trigger: false`` skill
    already relies on — rather than dropping the skill entirely.
    """

    def test_repeat_after_any_build_sends_the_pointer(self, tmp_path: Path) -> None:
        skills = tmp_path / "skills"
        path = _write_skill(skills, "foundation")
        builder = _builder(tmp_path, _loader(skills))

        assert BODY_SENTINEL in _send(builder)
        second = _send(builder)
        assert BODY_SENTINEL not in second
        assert HINT_HEADER in second  # the agent still learns the skill applies
        assert str(path) in second


class TestRollbackReinjectsAfterAnUnlandedTurn:
    """`rollback_skill_bodies` undoes a build whose turn never reached the model.

    A build records the body at build time; if that turn then dies before the
    prompt reaches the provider window, the caller's `finally` rolls the record
    back so the NEXT turn re-injects the full body instead of demoting it to a
    pointer the model never saw the body for.
    """

    def test_rolled_back_build_reinjects_the_body(self, tmp_path: Path) -> None:
        skills = tmp_path / "skills"
        _write_skill(skills, "foundation")
        builder = _builder(tmp_path, _loader(skills))

        assert BODY_SENTINEL in _send(builder)  # recorded at build time
        builder.rollback_skill_bodies(SESSION)  # the turn never landed
        # Next turn re-injects the full body, not the pointer.
        assert BODY_SENTINEL in _send(builder)

    def test_landed_build_without_rollback_still_demotes(self, tmp_path: Path) -> None:
        skills = tmp_path / "skills"
        _write_skill(skills, "foundation")
        builder = _builder(tmp_path, _loader(skills))

        assert BODY_SENTINEL in _send(builder)
        builder.commit_skill_bodies(SESSION)  # turn landed
        # The repeat demotes as usual.
        assert BODY_SENTINEL not in _send(builder)

    def test_commit_after_a_landed_turn_blocks_a_later_rollback(self, tmp_path: Path) -> None:
        # GPT #2: a landed build's undo must not stay armed. After the turn
        # lands and commits, a later (spurious or non-building-turn) rollback
        # must NOT erase the recorded body — the body is in the window.
        skills = tmp_path / "skills"
        _write_skill(skills, "foundation")
        builder = _builder(tmp_path, _loader(skills))

        assert BODY_SENTINEL in _send(builder)  # turn 1 records
        builder.commit_skill_bodies(SESSION)  # turn 1 landed — undo dropped
        builder.rollback_skill_bodies(SESSION)  # a later rollback finds nothing armed
        # The committed body still demotes; it was NOT rolled back.
        assert BODY_SENTINEL not in _send(builder)

    def test_rollback_only_undoes_the_most_recent_build(self, tmp_path: Path) -> None:
        # Two landed builds record the body; a THIRD build that is rolled back
        # must not resurrect the body for the already-demoted skill — rollback
        # restores the state as of just before the rolled-back build, which had
        # the skill already recorded, so the next turn still demotes.
        skills = tmp_path / "skills"
        _write_skill(skills, "foundation")
        builder = _builder(tmp_path, _loader(skills))

        assert BODY_SENTINEL in _send(builder)  # turn 1 records, lands
        builder.commit_skill_bodies(SESSION)
        assert BODY_SENTINEL not in _send(builder)  # turn 2 demotes, lands
        builder.commit_skill_bodies(SESSION)
        builder.rollback_skill_bodies(SESSION)  # turn 2 was demote-only; nothing armed
        # The record still holds the body hash from turn 1, so turn 3 demotes.
        assert BODY_SENTINEL not in _send(builder)

    def test_commit_is_a_noop_without_a_session_key(self, tmp_path: Path) -> None:
        skills = tmp_path / "skills"
        _write_skill(skills, "foundation")
        builder = _builder(tmp_path, _loader(skills))
        builder.commit_skill_bodies(None)  # must not raise
        assert BODY_SENTINEL in _send(builder, session_key=None)

    def test_rollback_is_a_noop_without_a_session_key(self, tmp_path: Path) -> None:
        skills = tmp_path / "skills"
        _write_skill(skills, "foundation")
        builder = _builder(tmp_path, _loader(skills))
        # No record kept for a None key; rollback must not raise.
        builder.rollback_skill_bodies(None)
        assert BODY_SENTINEL in _send(builder, session_key=None)


class _SeamSpyBuilder:
    """A ctx_builder stand-in that records which settle method the turn's
    ``finally`` called. ``drive_turn`` touches only ``build_message`` and the
    two settle methods, so this is a faithful probe of the real seam call site
    without constructing a whole ``ContextBuilder``."""

    def __init__(self) -> None:
        self.committed: list[str] = []
        self.rolled_back: list[str] = []

    def build_message(self, text, is_new, session_key, **kw):  # type: ignore[no-untyped-def]
        return text, None

    def commit_skill_bodies(self, session_key: str) -> None:
        self.committed.append(session_key)

    def rollback_skill_bodies(self, session_key: str) -> None:
        self.rolled_back.append(session_key)


class TestTransportFinallySeamSettlesDedup:
    """Drive the REAL ``messaging.dispatch.drive_turn`` ``finally`` end to end.

    The reviewer's point: tests that call ``rollback_skill_bodies`` directly
    prove the helper, but deleting the ``rollback_skill_bodies(...)`` line from a
    transport's ``finally`` would fail no test. These drive the real
    ``drive_turn`` — the one ``finally`` every adopted channel reaches — with a
    builder that records which settle method the seam called, so removing that
    line from the ``finally`` breaks the assertion. ``_turn_landed`` is derived
    from the driver (``driver_turn_landed``): a completed turn commits, a
    cancelled one rolls back.
    """

    def test_drive_turn_finally_commits_a_landed_turn(self, monkeypatch) -> None:
        import asyncio

        from test_messaging_dispatch import _patch_pipeline, _Renderer, _Sessions, _turn

        _patch_pipeline(monkeypatch)
        spy = _SeamSpyBuilder()
        turn = _turn(_Renderer())
        asyncio.run(drive_turn(turn, sessions=_Sessions(), ctx_builder=spy))
        # The default _Driver reports a completed turn (completion_observed,
        # no cancel), so the finally commits — and never rolls back.
        assert spy.committed == [turn.session_key]
        assert spy.rolled_back == []

    def test_drive_turn_finally_rolls_back_a_cancelled_turn(self, monkeypatch) -> None:
        import asyncio

        from test_messaging_dispatch import _Driver, _patch_pipeline, _Renderer, _Sessions, _turn

        from kiro_crew.acp.types import STOP_REASON_CANCELLED

        _patch_pipeline(monkeypatch)

        class _CancelledDriver(_Driver):
            # A cancelled turn is NOT landed: the backend drops it from its
            # transcript, so the re-injected bodies never reached the window.
            last_stop_reason = STOP_REASON_CANCELLED

        monkeypatch.setattr("kiro_crew.messaging.dispatch.TurnDriver", _CancelledDriver)
        spy = _SeamSpyBuilder()
        turn = _turn(_Renderer())
        asyncio.run(drive_turn(turn, sessions=_Sessions(), ctx_builder=spy))
        # The finally sees landed=False and rolls back — never commits.
        assert spy.rolled_back == [turn.session_key]
        assert spy.committed == []


class TestEverySeamSiteSettlesTheSkillBodies:
    """Ratchet: every ``rearm_reinjection`` call site is paired with a
    ``rollback_skill_bodies`` call in the same function.

    ``rearm_reinjection`` and ``rollback_skill_bodies`` are two halves of one
    turn-seam contract: a turn that consumed state but never landed must put
    BOTH back (the re-injection flag AND the skill-body dedup record), or the
    retry/next turn gets a pointer for a body the model never saw. The dedup
    helper has unit tests and ``drive_turn`` has an end-to-end test above, but
    neither pins the OTHER call sites — the nine transport finallys and the cron
    transient-retry branch. This counts, per function, that the module re-arms
    the skill-body record at least as often as it re-arms re-injection, so
    deleting any single seam's ``rollback_skill_bodies`` (including the cron
    retry branch, where a bare ``rearm`` without the rollback was the reviewer's
    finding) drops the count below ``rearm`` and fails here.
    """

    def test_rollback_is_paired_with_every_rearm(self) -> None:
        import ast as _ast

        src = Path(D.__file__).resolve().parent.parent
        violations: list[str] = []
        rearm_sites = 0
        checked_fns = 0
        for path in sorted(src.rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            if "rearm_reinjection(" not in text:
                continue
            tree = _ast.parse(text)
            parents: dict = {}
            for node in _ast.walk(tree):
                for child in _ast.iter_child_nodes(node):
                    parents[child] = node

            def _nearest_fn(node):  # type: ignore[no-untyped-def]
                while node is not None and not isinstance(
                    node, (_ast.FunctionDef, _ast.AsyncFunctionDef)
                ):
                    node = parents.get(node)
                return node

            counts: dict = {}
            for node in _ast.walk(tree):
                if (
                    isinstance(node, _ast.Call)
                    and isinstance(node.func, _ast.Name)
                    and node.func.id in ("rearm_reinjection", "rollback_skill_bodies")
                ):
                    fn = _nearest_fn(node)
                    key = (fn.name, fn.lineno) if fn else ("<module>", 0)
                    re_, ro_ = counts.get(key, (0, 0))
                    if node.func.id == "rearm_reinjection":
                        counts[key] = (re_ + 1, ro_)
                    else:
                        counts[key] = (re_, ro_ + 1)
            for (name, lineno), (re_, ro_) in counts.items():
                if re_ == 0:
                    continue
                checked_fns += 1
                rearm_sites += re_
                if ro_ < re_:
                    rel = path.relative_to(src)
                    violations.append(f"{rel}:{lineno} {name} rearm={re_} rollback={ro_}")
        assert (
            not violations
        ), "every rearm_reinjection seam must settle the skill-body dedup too:\n" + "\n".join(
            violations
        )
        # Guard the guard: if the pipeline is refactored to zero seam sites this
        # test must fail loudly rather than pass vacuously.
        assert rearm_sites >= 9, f"expected the known seam sites, found {rearm_sites}"
        assert checked_fns >= 7, f"expected the known seam functions, found {checked_fns}"

    def test_seam_helper_is_a_noop_for_a_builder_without_the_methods(self) -> None:
        from kiro_crew.messaging.dispatch import rollback_skill_bodies

        # A builder stand-in that predates the settle methods must be a safe
        # no-op at the seam (defensive getattr), never raising into the turn's
        # own finally.
        class _OldBuilder:
            pass

        rollback_skill_bodies(_OldBuilder(), SESSION, landed=True)
        rollback_skill_bodies(_OldBuilder(), SESSION, landed=False)


class TestResetReinjects:
    def test_new_session_reinjects(self, tmp_path: Path) -> None:
        skills = tmp_path / "skills"
        _write_skill(skills, "foundation")
        builder = _builder(tmp_path, _loader(skills))

        _send(builder)
        assert BODY_SENTINEL not in _send(builder)
        # A fresh provider session does not hold the earlier body.
        assert BODY_SENTINEL in _send(builder, is_new_session=True)

    def test_first_turn_after_compaction_reinjects(self, tmp_path: Path) -> None:
        skills = tmp_path / "skills"
        _write_skill(skills, "foundation")
        builder = _builder(tmp_path, _loader(skills))

        _send(builder)
        assert BODY_SENTINEL not in _send(builder)
        # needs_reinjection is the post-compaction turn: the window was rebuilt.
        assert BODY_SENTINEL in _send(builder, needs_reinjection=True)

    def test_flag_on_a_no_match_turn_still_resets(self, tmp_path: Path) -> None:
        """A window-rebuild flag is one-shot and can land on a turn where no
        skill matches. The reset must fire on that turn anyway, so a later
        matching turn re-injects rather than demoting a body the rebuilt window
        never received."""
        skills = tmp_path / "skills"
        _write_skill(skills, "foundation")
        builder = _builder(tmp_path, _loader(skills))

        assert BODY_SENTINEL in _send(builder)
        assert BODY_SENTINEL not in _send(builder)

        # The compaction turn triggers no skill (its text does not match the
        # trigger), so it consumes needs_reinjection without touching the
        # skill-match branch. The record must still be cleared here.
        no_match, _ = builder.build_message(
            "unrelated message", is_new_session=False, session_key=SESSION, needs_reinjection=True
        )
        assert BODY_SENTINEL not in no_match  # nothing matched, so no body anyway

        # The next matching turn must re-inject: the window was rebuilt.
        assert BODY_SENTINEL in _send(builder)

    def test_flag_on_a_no_match_turn_clears_the_record(self, tmp_path: Path) -> None:
        """Same guarantee, asserted at the record: the no-match rebuild turn
        empties the session's stored hashes."""
        skills = tmp_path / "skills"
        _write_skill(skills, "foundation")
        builder = _builder(tmp_path, _loader(skills))

        _send(builder)
        key = builder._cap_memo_key(SESSION)
        assert builder._sent_skill_bodies.get(key)  # recorded

        builder.build_message(
            "unrelated message", is_new_session=False, session_key=SESSION, needs_reinjection=True
        )
        # The rebuild turn cleared the record even though no skill matched.
        assert not builder._sent_skill_bodies.get(key)

    def test_agent_switch_reinjects(self, tmp_path: Path) -> None:
        skills = tmp_path / "skills"
        # The default agent takes the body path; a custom agent skips triggered
        # skills, so switch between two names that both inject: None and the
        # canonical crew agent are both non-custom.
        _write_skill(skills, "foundation")
        builder = _builder(tmp_path, _loader(skills))

        assert BODY_SENTINEL in _send(builder, agent=None)
        assert BODY_SENTINEL not in _send(builder, agent=None)
        # Same session key, different agent: the window is a different one.
        assert BODY_SENTINEL in _send(builder, agent="kirocrew")

    def test_edited_body_reinjects_on_hash_change(self, tmp_path: Path) -> None:
        skills = tmp_path / "skills"
        path = _write_skill(skills, "foundation")
        builder = _builder(tmp_path, _loader(skills))

        assert BODY_SENTINEL in _send(builder)
        assert BODY_SENTINEL not in _send(builder)

        edited = "STEP ONE: cure the slab a full week."
        path.write_text(
            path.read_text(encoding="utf-8").replace(BODY_SENTINEL, edited),
            encoding="utf-8",
        )
        # A new hash means the agent has never seen this body: send it.
        assert edited in _send(builder)


class TestSessionsAreIndependent:
    def test_one_session_dedup_does_not_silence_another(self, tmp_path: Path) -> None:
        skills = tmp_path / "skills"
        _write_skill(skills, "foundation")
        builder = _builder(tmp_path, _loader(skills))

        assert BODY_SENTINEL in _send(builder, session_key="chat-a")
        assert BODY_SENTINEL not in _send(builder, session_key="chat-a")
        # A never-seen session still gets the body.
        assert BODY_SENTINEL in _send(builder, session_key="chat-b")


class TestConfinedSkillNeverDemoted:
    """A confined project skill has no pointer form, so it always re-injects."""

    def test_confined_skill_body_repeats_every_turn(self, tmp_path: Path) -> None:
        loader = MagicMock()
        loader.get_triggered_skills.return_value = ["proj-skill"]
        loader.split_triggered.return_value = (["proj-skill"], [])
        # Confinement is what forbids the pointer path; mark it confined.
        loader.confined_triggered.return_value = {"proj-skill"}
        loader.load_skill.return_value = "confined body ONE"
        loader.strip_frontmatter.return_value = "confined body ONE"
        loader.trigger_hint.return_value = ""
        builder = ContextBuilder(memory=MemoryStore(workspace=tmp_path / "ws"), skills=loader)

        first, _ = builder.build_message("trigger", is_new_session=False, session_key=SESSION)
        second, _ = builder.build_message("trigger", is_new_session=False, session_key=SESSION)

        # Demoting it would drop it entirely (no body, no pointer); instead it
        # re-injects both times.
        assert "confined body ONE" in first
        assert "confined body ONE" in second
        assert "[Skill: proj-skill]" in second


class TestRecordIsBounded:
    def test_session_record_evicts_oldest_past_the_bound(self, tmp_path: Path) -> None:
        skills = tmp_path / "skills"
        _write_skill(skills, "foundation")
        builder = _builder(tmp_path, _loader(skills))
        builder._SENT_SKILL_BODY_SESSIONS = 3  # type: ignore[misc]

        # First session sends the body and is recorded.
        assert BODY_SENTINEL in _send(builder, session_key="s0")
        assert BODY_SENTINEL not in _send(builder, session_key="s0")

        # Fill past the bound with newer sessions; s0 is the oldest and evicts.
        for i in range(1, 5):
            _send(builder, session_key=f"s{i}")

        assert len(builder._sent_skill_bodies) <= 3
        # Keys are digests of the session key, so check the digest is gone.
        assert builder._cap_memo_key("s0") not in builder._sent_skill_bodies
        assert len(builder._sent_skill_agents) <= 3
        # Evicted -> its next match re-injects (fail-safe direction).
        assert BODY_SENTINEL in _send(builder, session_key="s0")

    def test_per_session_entry_count_is_bounded(self, tmp_path: Path) -> None:
        """One session matching many distinct skills cannot grow without limit."""
        skills = tmp_path / "skills"
        # Every skill shares the same trigger word, so one message matches all.
        for i in range(6):
            _write_skill(skills, f"skill{i}", body=f"BODY NUMBER {i}")
        builder = _builder(tmp_path, _loader(skills, cap=6))
        builder._SENT_SKILL_BODY_ENTRIES = 3  # type: ignore[misc]

        _send(builder, session_key="cap-test")
        inner = builder._sent_skill_bodies[builder._cap_memo_key("cap-test")]
        assert len(inner) <= 3

    def test_undo_map_obeys_the_same_entry_cap(self, tmp_path: Path) -> None:
        """GPT F1 (context.py:3520): the undo map must not retain a row for a
        key the per-session entry cap already evicted from the record. A build
        matching more distinct skills than the cap evicts the oldest from
        ``sent``; the stashed undo entry must be filtered to the surviving keys
        so it cannot grow past the declared bound."""
        skills = tmp_path / "skills"
        # Every skill shares the trigger word, so one message matches all of
        # them in a single build — the case that fills undo before the cap.
        for i in range(6):
            _write_skill(skills, f"skill{i}", body=f"BODY NUMBER {i}")
        builder = _builder(tmp_path, _loader(skills, cap=6))
        builder._SENT_SKILL_BODY_ENTRIES = 3  # type: ignore[misc]

        _send(builder, session_key="undo-cap")
        key = builder._cap_memo_key("undo-cap")
        undo = builder._sent_skill_bodies_undo.get(key, {})
        sent = builder._sent_skill_bodies[key]
        # The undo map is bounded by the same cap AND holds no key the record
        # evicted — a bound bounds every field it retains.
        assert len(undo) <= 3
        assert set(undo).issubset(set(sent))

    def test_entry_cap_eviction_is_reported_not_silent(self, tmp_path: Path, caplog) -> None:
        """GPT #1: a forced eviction narrows dedup coverage, so it must be
        observable — the entry-cap path logs when it drops an oldest record."""
        import logging

        skills = tmp_path / "skills"
        for i in range(4):
            _write_skill(skills, f"skill{i}", body=f"BODY NUMBER {i}")
        builder = _builder(tmp_path, _loader(skills, cap=4))
        builder._SENT_SKILL_BODY_ENTRIES = 2  # type: ignore[misc]

        with caplog.at_level(logging.DEBUG, logger="kiro_crew.context"):
            _send(builder, session_key="overflow")

        assert any("per-session entry cap" in r.getMessage() for r in caplog.records)

    def test_keys_are_digested_not_raw_session_keys(self, tmp_path: Path) -> None:
        """A caller-supplied key is stored as a fixed-size digest, not verbatim."""
        skills = tmp_path / "skills"
        _write_skill(skills, "foundation")
        builder = _builder(tmp_path, _loader(skills))

        raw = "session/with:arbitrary\nlength-" + "x" * 500
        _send(builder, session_key=raw)

        assert raw not in builder._sent_skill_bodies
        assert builder._cap_memo_key(raw) in builder._sent_skill_bodies

    def test_the_stored_agent_is_digested_not_raw(self, tmp_path: Path) -> None:
        """The agent name is stored as a fixed-size digest, so a long agent name
        cannot stay retained at full length in the count-bounded agent map
        (a-bound-bounds-every-field-it-retains)."""
        skills = tmp_path / "skills"
        _write_skill(skills, "foundation")
        builder = _builder(tmp_path, _loader(skills))

        long_agent = "agent-" + "a" * 500
        builder._dedup_triggered_bodies("agent-digest", long_agent, reset=False, candidates=[])

        key = builder._cap_memo_key("agent-digest")
        stored = builder._sent_skill_agents[key]
        assert stored != long_agent
        assert stored == builder._cap_memo_key(long_agent)
        # None (no agent) stays distinct from any digest.
        builder._dedup_triggered_bodies("no-agent", None, reset=False, candidates=[])
        assert builder._sent_skill_agents[builder._cap_memo_key("no-agent")] is None

    def test_inner_skill_keys_are_digested_not_raw(self, tmp_path: Path) -> None:
        """A skill key is stored as a fixed-size digest, so a long key cannot
        stay retained at full length and defeat the byte bound."""
        long_name = "s" + "k" * 300
        loader = MagicMock()
        loader.get_triggered_skills.return_value = [long_name]
        loader.split_triggered.return_value = ([long_name], [])
        loader.confined_triggered.return_value = set()
        loader.load_skill.return_value = "some body"
        loader.strip_frontmatter.return_value = "some body"
        loader.trigger_hint.return_value = ""
        builder = ContextBuilder(memory=MemoryStore(workspace=tmp_path / "ws"), skills=loader)

        builder.build_message("trigger", is_new_session=False, session_key="digest-test")
        inner = builder._sent_skill_bodies[builder._cap_memo_key("digest-test")]
        assert long_name not in inner
        assert builder._cap_memo_key(long_name) in inner

    def test_a_non_utf8_skill_key_hashes_instead_of_aborting_the_turn(self, tmp_path: Path) -> None:
        """A skill directory whose name is not valid UTF-8 reaches dedup as a
        surrogate string (the filesystem layer round-trips it through
        ``os.fsdecode``). The digest must hash it, not raise
        ``UnicodeEncodeError`` and abort the matching turn before delivery."""
        # os.fsdecode(b"foundation-\xff") on a UTF-8 filesystem yields this
        # surrogate; str.encode("utf-8") on it raises without surrogatepass.
        surrogate_name = "foundation-\udcff"
        with pytest.raises(UnicodeEncodeError):
            surrogate_name.encode("utf-8")  # the pre-fix crash this guards against

        loader = MagicMock()
        loader.get_triggered_skills.return_value = [surrogate_name]
        loader.split_triggered.return_value = ([surrogate_name], [])
        loader.confined_triggered.return_value = set()
        loader.load_skill.return_value = "some body"
        loader.strip_frontmatter.return_value = "some body"
        loader.trigger_hint.return_value = ""
        builder = ContextBuilder(memory=MemoryStore(workspace=tmp_path / "ws"), skills=loader)

        # The turn must complete rather than abort on the surrogate name.
        builder.build_message("trigger", is_new_session=False, session_key="surrogate-test")
        inner = builder._sent_skill_bodies[builder._cap_memo_key("surrogate-test")]
        assert builder._cap_memo_key(surrogate_name) in inner


class TestDeliveryAuditReflectsDedup:
    """The delivery audit names what the prompt ACTUALLY carries after dedup.

    The matcher's ``skill_trigger`` row records the frontmatter-level split at
    match time, before this dedup runs, so a demoted body is still named a
    delivered body there. A ``skill_delivery`` correction row is emitted after
    dedup — and only when a demotion diverged from that claim — so an auditor is
    never told a demoted body was in the prompt.
    """

    def _delivery_rows(self, sel_mock: MagicMock) -> list[dict]:
        return [
            c.kwargs["metadata"]
            for c in sel_mock.log_tool_invocation.call_args_list
            if c.kwargs.get("tool_name") == "skill_delivery"
        ]

    def test_no_correction_row_on_first_send(self, tmp_path: Path, monkeypatch) -> None:
        skills = tmp_path / "skills"
        _write_skill(skills, "foundation")
        builder = _builder(tmp_path, _loader(skills))
        sel_mock = MagicMock()
        monkeypatch.setattr("kiro_crew.context.sel", lambda: sel_mock)

        _send(builder)
        # Nothing demoted -> the matcher row is already correct, no extra row.
        assert self._delivery_rows(sel_mock) == []

    def test_correction_row_names_demoted_skill_as_pointer(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        skills = tmp_path / "skills"
        _write_skill(skills, "foundation")
        builder = _builder(tmp_path, _loader(skills))
        sel_mock = MagicMock()
        monkeypatch.setattr("kiro_crew.context.sel", lambda: sel_mock)

        _send(builder)  # body delivered, recorded
        _send(builder)  # foundation demoted to pointer this turn

        rows = self._delivery_rows(sel_mock)
        assert len(rows) == 1
        assert rows[0]["demoted"] == "foundation"
        assert rows[0]["pointers"] == "foundation"
        assert rows[0]["bodies"] == ""

    def test_audit_name_lists_are_bounded_with_omitted_counts(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """GPT F1 (anchor a-bound-bounds-every-field-it-retains): the
        ``skill_delivery`` row persists comma-joined name lists whose only
        upstream bound is the operator-set ``max_triggered``, so each field must
        be truncated to the constant ``_SKILL_DELIVERY_AUDIT_NAMES`` and record
        how many names were dropped. The ``pointers`` field (demoted +
        ``inject_on_trigger: false`` opt-outs) is the one not already bounded by
        the per-session record cap, so drive it past the audit bound."""
        skills = tmp_path / "skills"
        cap = ContextBuilder._SKILL_DELIVERY_AUDIT_NAMES
        n_optout = cap + 6
        # One body skill (so a demotion occurs and the correction row fires),
        # plus > cap opt-out skills that each contribute a pointer line.
        _write_skill(skills, "foundation")
        for i in range(n_optout):
            d = skills / f"optout{i:03d}"
            d.mkdir(parents=True)
            (d / "SKILL.md").write_text(
                f"---\nname: optout{i:03d}\ndescription: Lay a foundation\n"
                f"triggers: zebra quokka\ninject_on_trigger: false\n---\nBODY",
                encoding="utf-8",
            )
        builder = _builder(tmp_path, _loader(skills, cap=n_optout + 1))
        sel_mock = MagicMock()
        monkeypatch.setattr("kiro_crew.context.sel", lambda: sel_mock)

        _send(builder)  # foundation body delivered + recorded; opt-outs pointer
        _send(builder)  # foundation demoted this turn -> correction row fires

        rows = self._delivery_rows(sel_mock)
        assert len(rows) == 1
        row = rows[0]
        # pointers = demoted(foundation) + opt-outs, so > cap before truncation.
        assert len(row["pointers"].split(",")) == cap
        assert int(row["pointers_omitted"]) > 0
        # Every field is bounded by the constant, never by max_triggered.
        assert len(row["demoted"].split(",")) <= cap
        for field in ("bodies", "pointers", "demoted"):
            names = row[field].split(",") if row[field] else []
            assert len(names) <= cap

    def test_a_single_long_audit_name_is_truncated_to_the_byte_budget(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """GPT F1 (anchor a-bound-bounds-every-field-it-retains, upheld):
        capping the NUMBER of names leaves one arbitrarily long name free to
        grow the row unboundedly. Each kept name must also be truncated to
        ``_SKILL_DELIVERY_AUDIT_NAME_LEN``. Drive one long opt-out pointer name
        (raw, not digested in the audit list) plus a demoted skill so the
        correction row fires, and assert the kept name is byte-bounded."""
        name_len = ContextBuilder._SKILL_DELIVERY_AUDIT_NAME_LEN
        long_name = "p" + "o" * (name_len * 3)  # far longer than the budget
        loader = MagicMock()
        loader.get_triggered_skills.return_value = ["foundation"]
        # foundation delivers a body (so it can demote next turn); the long name
        # is an opt-out pointer that enters the audit ``pointers`` list raw.
        loader.split_triggered.return_value = (["foundation"], [long_name])
        loader.confined_triggered.return_value = set()
        loader.load_skill.return_value = "some body"
        loader.strip_frontmatter.return_value = "some body"
        loader.trigger_hint.return_value = ""
        builder = ContextBuilder(memory=MemoryStore(workspace=tmp_path / "ws"), skills=loader)
        sel_mock = MagicMock()
        monkeypatch.setattr("kiro_crew.context.sel", lambda: sel_mock)

        builder.build_message("trigger", is_new_session=False, session_key="len-test")
        builder.build_message("trigger", is_new_session=False, session_key="len-test")

        rows = self._delivery_rows(sel_mock)
        assert len(rows) == 1
        kept = [n for n in rows[0]["pointers"].split(",") if n]
        long_kept = [n for n in kept if n.startswith("p")]
        assert long_kept, "the long opt-out name should be present in pointers"
        # No single name exceeds the per-name byte budget.
        assert all(len(n) <= name_len for n in kept)
        assert len(long_kept[0]) == name_len
