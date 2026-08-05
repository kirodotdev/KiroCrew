"""Tests pinning Mochi's four small ported modules.

These modules are too small for the differential-harness treatment (pure
tables and a few branches); they were instead cross-checked one-shot against
the TypeScript at port time (all 96 state-machine pairs;
parse inputs) and are pinned here. The bigger modules were instead pinned by a
differential harness run against the TypeScript during the port — migration-only
tooling that is not part of the repository.
"""

from __future__ import annotations

from kiro_crew.apps.builtins.mochi.pet_state_machine import (
    ALL_EVENTS,
    ALL_STATES,
    ERROR_TIMEOUT_MS,
    TRANSITIONS,
    is_legal_transition,
    legal_events,
    transition,
)
from kiro_crew.apps.builtins.mochi.pet_state_manager import (
    WALK_RESTORE_DELAY_MS,
    PetStateManager,
)
from kiro_crew.apps.builtins.mochi.soul_loader import (
    BUILTIN_PET_NAMES,
    DEFAULT_SOUL,
    SoulLoader,
    persona_for,
)


class TestPetStateMachine:
    def test_happy_paths(self) -> None:
        assert transition("idle", "user_input") == "thinking"
        assert transition("thinking", "tool_call") == "working"
        assert transition("working", "task_complete") == "idle"
        assert transition("idle", "walk_start") == "walking"
        assert transition("walking", "walk_done") == "idle"
        assert transition("error", "timeout") == "idle"
        assert transition("offline", "connect") == "idle"

    def test_unmapped_event_keeps_state(self) -> None:
        assert transition("idle", "walk_done") == "idle"
        assert transition("offline", "user_input") == "offline"

    def test_unknown_state_is_identity(self) -> None:
        assert transition("nonsense", "user_input") == "nonsense"

    def test_table_shape(self) -> None:
        # Every mapped state/event is in the published vocabulary, and every
        # target is a real state — the exhaustive TS cross-check (96 pairs)
        # was done at port time; this keeps the table internally consistent.
        assert set(TRANSITIONS) == set(ALL_STATES)
        for state, events in TRANSITIONS.items():
            for event, target in events.items():
                assert event in ALL_EVENTS, (state, event)
                assert target in ALL_STATES, (state, event, target)

    def test_helpers(self) -> None:
        assert is_legal_transition("idle", "user_input")
        assert not is_legal_transition("idle", "walk_done")
        assert legal_events("offline") == ["connect", "error"]


class TestPetStateManager:
    def _mk(self) -> tuple[PetStateManager, list[tuple[str, str]]]:
        broadcasts: list[tuple[str, str]] = []
        mgr = PetStateManager(lambda ch, state: broadcasts.append((ch, state)))
        return mgr, broadcasts

    def test_walking_buffers_requested_state(self) -> None:
        mgr, broadcasts = self._mk()
        mgr.set_pet_state("idle", 0)
        mgr.start_walking(0)
        mgr.set_pet_state("thinking", 100)  # buffered, not applied
        assert mgr.current == "walking"
        mgr.finish_walking(1000)  # normal walk: restore is delayed
        assert mgr.current == "walking"
        mgr.tick(1000 + WALK_RESTORE_DELAY_MS)
        assert mgr.current == "thinking"
        assert broadcasts[-1] == ("pet:state-change", "thinking")

    def test_instant_walk_restores_immediately(self) -> None:
        mgr, _ = self._mk()
        mgr.set_pet_state("idle", 0)
        mgr.start_walking(0)
        mgr.finish_walking(299)  # < 300ms: no delay
        assert mgr.current == "idle"

    def test_error_auto_recovers_after_3s(self) -> None:
        mgr, _ = self._mk()
        mgr.set_pet_state("idle", 0)
        mgr.apply_event("error", 0)
        assert mgr.current == "error"
        mgr.tick(ERROR_TIMEOUT_MS - 1)
        assert mgr.current == "error"
        mgr.tick(ERROR_TIMEOUT_MS)
        assert mgr.current == "idle"

    def test_start_walking_cancels_error_recovery(self) -> None:
        mgr, _ = self._mk()
        mgr.set_pet_state("idle", 0)
        mgr.apply_event("error", 0)
        mgr.start_walking(100)  # shares the timer slot: recovery cancelled
        mgr.tick(ERROR_TIMEOUT_MS + 100)
        assert mgr.current == "walking"

    def test_thinking_edges_feed_stats(self) -> None:
        calls: list[tuple[str, int]] = []

        class Stats:
            def record_thinking_start(self, now_ms: int) -> None:
                calls.append(("start", now_ms))

            def record_thinking_end(self, now_ms: int) -> None:
                calls.append(("end", now_ms))

        mgr = PetStateManager(lambda *a: None, stats=Stats())
        mgr.set_pet_state("idle", 0)
        mgr.set_pet_state("thinking", 10)
        mgr.set_pet_state("thinking", 20)  # no re-entry edge
        mgr.set_pet_state("working", 30)
        assert calls == [("start", 10), ("end", 30)]

    def test_transient_mood_auto_resets(self) -> None:
        from kiro_crew.apps.builtins.mochi.pet_state_manager import (
            MOOD_DURATION_MS,
            PET_MOOD_CHANNEL,
        )

        mgr, broadcasts = self._mk()
        mgr.set_mood("happy", 0)
        assert mgr.current_mood == "happy"
        assert broadcasts[-1] == (PET_MOOD_CHANNEL, "happy")
        mgr.tick(MOOD_DURATION_MS - 1)
        assert mgr.current_mood == "happy"
        mgr.tick(MOOD_DURATION_MS)  # deadline reached → reset to neutral
        assert mgr.current_mood == "neutral"
        assert broadcasts[-1] == (PET_MOOD_CHANNEL, "neutral")

    def test_persistent_mood_lingers_until_cleared(self) -> None:
        from kiro_crew.apps.builtins.mochi.pet_state_manager import MOOD_DURATION_MS

        mgr, _ = self._mk()
        mgr.set_mood("busy", 0)
        mgr.tick(MOOD_DURATION_MS + 5_000)  # no auto-reset for persistent moods
        assert mgr.current_mood == "busy"
        mgr.clear_persistent_mood(0)
        assert mgr.current_mood == "neutral"

    def test_thinking_clears_a_persistent_mood(self) -> None:
        mgr, _ = self._mk()
        mgr.set_pet_state("idle", 0)
        mgr.set_mood("busy", 0)
        mgr.set_pet_state("thinking", 10)  # entering thinking clears persistent mood
        assert mgr.current_mood == "neutral"

    def test_thinking_leaves_a_transient_mood_alone(self) -> None:
        mgr, _ = self._mk()
        mgr.set_pet_state("idle", 0)
        mgr.set_mood("happy", 0)
        mgr.set_pet_state("thinking", 10)  # transient rides its own timer
        assert mgr.current_mood == "happy"

    def test_mood_reset_timer_is_independent_of_error_recovery(self) -> None:
        # A transient mood armed while an error recovery is pending: the two use
        # SEPARATE deadline slots, so arming the mood must not cancel the error
        # recovery, and the error state change must not cancel the mood timer.
        # Staggered so the deadlines don't coincide (both default to 3000ms).
        from kiro_crew.apps.builtins.mochi.pet_state_machine import ERROR_TIMEOUT_MS
        from kiro_crew.apps.builtins.mochi.pet_state_manager import MOOD_DURATION_MS

        mgr, _ = self._mk()
        mgr.set_pet_state("idle", 0)
        mgr.apply_event("error", 0)  # error recovers at ERROR_TIMEOUT_MS (3000)
        mgr.set_mood("scared", 2_000)  # mood resets at 5000
        mgr.tick(ERROR_TIMEOUT_MS)  # error recovers; mood timer untouched
        assert mgr.current == "idle"
        assert mgr.current_mood == "scared"
        mgr.tick(2_000 + MOOD_DURATION_MS)  # now the mood deadline fires
        assert mgr.current_mood == "neutral"


class TestSoulLoader:
    def test_default_and_config_priority(self) -> None:
        soul = SoulLoader()
        assert soul.get() == DEFAULT_SOUL
        soul.set_config_soul("  \n ")  # blank: still default
        assert soul.get() == DEFAULT_SOUL
        soul.set_config_soul("Be a dragon.")
        assert soul.get() == "Be a dragon."

    def test_pet_name_falsy_fallback(self) -> None:
        soul = SoulLoader()
        assert soul.pet_name == "Mochi"
        soul.set_pet_name("Nori")
        assert soul.pet_name == "Nori"
        soul.set_pet_name("")
        assert soul.pet_name == "Mochi"

    def test_default_soul_never_names_a_creature(self) -> None:
        # The user can swap appearance packs; the persona must fit any of them.
        for word in ("ghost", "cat", "dog"):
            assert word not in DEFAULT_SOUL.lower()


class TestAppearancePersona:
    """The active PACK carries the personality — the soul editor is gone.

    Keyed by pack id, which is the single identity key. A user-imported pack is
    not in the built-in table and contributes its own description instead — the
    original's rule, and what stops an imported robot describing itself as a cat.
    """

    def test_each_built_in_extends_the_shared_base(self) -> None:
        """The base holds the response-length rules; a pack must not lose them."""
        for pack in ("kiro-ghost", "default-mochi"):
            text = persona_for(pack)
            assert DEFAULT_SOUL in text, f"{pack} persona dropped the shared base"
            assert len(text) > len(DEFAULT_SOUL), f"{pack} added no personality"

    def test_the_two_built_ins_read_differently(self) -> None:
        assert persona_for("kiro-ghost") != persona_for("default-mochi")

    def test_ghost_is_kiro_and_cat_is_mochi(self) -> None:
        assert "ghost" in persona_for("kiro-ghost").lower()
        assert "cat" in persona_for("default-mochi").lower()

    def test_unset_pack_is_a_generic_companion(self) -> None:
        assert persona_for(None) == DEFAULT_SOUL

    def test_an_imported_pack_uses_its_own_description(self) -> None:
        """The bug this replaced: a robot pack whose prompt claimed to be a cat.

        The two-key model let the art and the persona come from different
        places. Now an unknown pack id with a description describes THAT.
        """
        text = persona_for("some-imported-pack", "a small copper robot with tread wheels")
        assert DEFAULT_SOUL in text
        assert "copper robot" in text
        assert "cat" not in text.lower()

    def test_a_built_in_ignores_a_supplied_description(self) -> None:
        """Curated text wins for the built-ins; they are not user art."""
        assert persona_for("default-mochi", "a robot") == persona_for("default-mochi")

    def test_an_unknown_pack_with_no_description_is_generic(self) -> None:
        assert persona_for("some-imported-pack") == DEFAULT_SOUL

    def test_loader_follows_the_selected_pack(self) -> None:
        loader = SoulLoader()
        loader.set_appearance("kiro-ghost")
        assert loader.get() == persona_for("kiro-ghost")
        loader.set_appearance("default-mochi")
        assert loader.get() == persona_for("default-mochi")

    def test_loader_carries_an_imported_packs_description(self) -> None:
        loader = SoulLoader()
        loader.set_appearance("my-pack", "a tiny paper crane")
        assert "paper crane" in loader.get()

    def test_switching_pack_switches_the_default_name(self) -> None:
        """Picking the ghost must not leave it introducing itself as Mochi."""
        loader = SoulLoader()
        loader.set_appearance("kiro-ghost")
        assert loader.pet_name == BUILTIN_PET_NAMES["kiro-ghost"]

    def test_an_explicit_pet_name_survives_a_pack_switch(self) -> None:
        loader = SoulLoader()
        loader.set_pet_name("Biscuit")
        loader.set_appearance("kiro-ghost")
        assert loader.pet_name == "Biscuit"

    def test_config_soul_still_wins(self) -> None:
        """The field survives for installs that carry one; only the EDITOR is gone."""
        loader = SoulLoader()
        loader.set_appearance("default-mochi")
        loader.set_config_soul("You are a very serious accountant.")
        assert loader.get() == "You are a very serious accountant."
