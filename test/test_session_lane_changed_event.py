"""`SessionLaneChanged` is an ordinary fired event: armable, filterable, dispatched.

Each assertion here is the positive form of a property the store could get wrong in
only one direction, and every one is kept rather than assumed: an event that is
dispatched but still cannot be armed, or still refuses a matcher, is unusable in
exactly the way these detect. Nothing else would catch it, because dispatch and
authoring are exercised on opposite sides of the store.

What the EMITTER does lives beside the writers that call it --
`test_session_lane_changed_emitter.py` for the two tag routes and
`test_session_lane_changed_on_create.py` for folder inheritance. This file is about
the event's standing in the store.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew.hooks import (
    HOOK_EVENT_SESSION_LANE_CHANGED,
    HOOK_EVENTS,
    HOOK_EVENTS_ALL,
    HOOK_EVENTS_KAS_ONLY,
    ScriptHookStore,
    validate_hook_fields,
)

EVENT = HOOK_EVENT_SESSION_LANE_CHANGED
MATCHER = "*added:tag-done;*"


def _store(tmp_path: Path) -> ScriptHookStore:
    return ScriptHookStore(tmp_path)


def _validate(event: str, matcher: str = "") -> None:
    validate_hook_fields(
        event=event,
        timeout=30,
        command="true",
        skills=[],
        matcher=matcher,
        matcher_mode="glob",
    )


class TestWhereTheEventSits:
    def test_it_is_a_fired_event(self) -> None:
        """Membership in ``HOOK_EVENTS`` is the promise that something calls ``fire``.

        Asserted together with its ABSENCE from the unfired set, because the store
        reads that one for the activation contract: a name in both would be
        dispatched and still stored switched off.
        """
        assert EVENT in HOOK_EVENTS
        assert EVENT in HOOK_EVENTS_ALL
        assert EVENT not in HOOK_EVENTS_KAS_ONLY

    def test_it_is_not_one_of_the_kiro_agent_triggers(self) -> None:
        assert EVENT not in HOOK_EVENTS_KAS_ONLY

    def test_the_validator_accepts_it(self) -> None:
        _validate(EVENT)


class TestItArmsLikeAnyOtherFiredEvent:
    """All three write paths arm a hook on this event, as they do for any other.

    `create`, `update` and `toggle` are pinned separately because they are three
    code paths rather than one: a refusal surviving on any single one of them
    leaves the event authorable and unarmable from the UI, which a user cannot
    tell apart from a hook they forgot to switch on.
    """

    def test_a_new_hook_honours_the_default(self, tmp_path: Path) -> None:
        hook = _store(tmp_path).create({"name": "n", "event": EVENT, "command": "true"})
        assert hook.enabled is True

    def test_an_explicit_enable_is_accepted(self, tmp_path: Path) -> None:
        hook = _store(tmp_path).create(
            {"name": "n", "event": EVENT, "command": "true", "enabled": True}
        )
        assert hook.enabled is True

    def test_an_explicit_disable_is_still_honoured(self, tmp_path: Path) -> None:
        """The inverse control: accepting an enable must not mean ignoring the field."""
        hook = _store(tmp_path).create(
            {"name": "n", "event": EVENT, "command": "true", "enabled": False}
        )
        assert hook.enabled is False

    def test_an_edit_can_arm_it(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        hook = store.create({"name": "n", "event": EVENT, "command": "true", "enabled": False})
        assert store.update(hook.id, {"enabled": True}).enabled is True

    def test_the_table_toggle_can_arm_it(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        hook = store.create({"name": "n", "event": EVENT, "command": "true", "enabled": False})
        assert store.toggle(hook.id).enabled is True

    def test_moving_a_live_hook_onto_it_keeps_it_armed(self, tmp_path: Path) -> None:
        """A hook moved onto this event keeps the armed state it already had.

        Nothing about the event requires disarming a row the author switched on,
        and doing so silently would read as the move having failed.
        """
        store = _store(tmp_path)
        hook = store.create(
            {"name": "n", "event": "UserPromptSubmit", "command": "true", "enabled": True}
        )
        assert store.update(hook.id, {"event": EVENT}).enabled is True


class TestItTakesAMatcher:
    """A matcher filters what the event carries, and the emitter defines that.

    It is defined now -- direction-tagged lane tokens, `added:<id>;` and
    `removed:<id>;` -- so the refusal is lifted. The six Kiro Agent triggers still
    refuse one, which is the control proving the refusal was lifted for this event
    specifically rather than removed from the validator.
    """

    def test_the_validator_accepts_a_matcher(self) -> None:
        _validate(EVENT, matcher=MATCHER)

    @pytest.mark.parametrize("kas_event", sorted(HOOK_EVENTS_KAS_ONLY))
    def test_the_six_still_refuse_one(self, kas_event: str) -> None:
        with pytest.raises(ValueError, match="a matcher cannot be set"):
            _validate(kas_event, matcher=MATCHER)

    def test_the_store_accepts_one_on_create(self, tmp_path: Path) -> None:
        hook = _store(tmp_path).create(
            {"name": "n", "event": EVENT, "command": "true", "matcher": MATCHER}
        )
        assert hook.matcher == MATCHER

    def test_a_row_on_disk_carrying_one_keeps_it(self, tmp_path: Path) -> None:
        """A stored filter survives a cold load.

        A loader that dropped it would silently widen the hook to every lane change
        it was written to exclude, and for a close-out hook that is an irreversible
        action on sessions it was never registered for.
        """
        (tmp_path / "hooks.json").write_text(
            json.dumps(
                {
                    "hooks": [
                        {
                            "id": "h0",
                            "name": "n",
                            "event": EVENT,
                            "command": "true",
                            "timeout": 30,
                            "enabled": False,
                            "matcher": MATCHER,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        assert _store(tmp_path).list_all()[0].matcher == MATCHER


class TestARowOnDiskIsLoadedRatherThanDropped:
    """An authorable event the loader does not know is not loaded, but is kept.

    The loader's property, for any name outside the vocabulary; the lane row is just
    the vehicle. The unknown-event row is the negative control: without it, a green
    assertion here would not distinguish "kept" from "the loader keeps everything".
    """

    def _write(self, tmp_path: Path, events: list[str]) -> None:
        rows = [
            {
                "id": f"h{i}",
                "name": f"n{i}",
                "event": event,
                "command": "true",
                "timeout": 30,
                "enabled": False,
            }
            for i, event in enumerate(events)
        ]
        (tmp_path / "hooks.json").write_text(json.dumps({"hooks": rows}), encoding="utf-8")

    def test_a_lane_row_survives_a_cold_load(self, tmp_path: Path) -> None:
        self._write(tmp_path, [EVENT])
        assert [h.event for h in _store(tmp_path).list_all()] == [EVENT]

    def test_a_row_naming_an_unknown_event_is_dropped(self, tmp_path: Path) -> None:
        self._write(tmp_path, ["SessionLaneChangedTypo"])
        assert [h.event for h in _store(tmp_path).list_all()] == []

    def test_the_lane_row_survives_beside_one_that_is_dropped(self, tmp_path: Path) -> None:
        self._write(tmp_path, [EVENT, "NotAnEventAtAll"])
        assert [h.event for h in _store(tmp_path).list_all()] == [EVENT]

    def test_a_dropped_row_is_kept_in_the_file_rather_than_erased(self, tmp_path: Path) -> None:
        """Not loaded is not lost.

        If a name later changes or goes away, a row saved against it stops loading --
        but a WRITE must not then erase it, or a renamed event would silently cost
        people their hooks. `_load` keeps it and `_save` writes it back, so the row
        returns if the name does.
        """
        self._write(tmp_path, [EVENT, "NotAnEventAtAll"])
        store = _store(tmp_path)
        store.create({"name": "fresh", "event": "UserPromptSubmit", "command": "true"})

        on_disk = json.loads((tmp_path / "hooks.json").read_text(encoding="utf-8"))["hooks"]
        assert "NotAnEventAtAll" in [row["event"] for row in on_disk]

    def test_the_warning_names_the_row_that_stopped_loading(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The recovery is to rename the row by hand, which needs a pointer to it.

        The warning itself is the generic "Skipping unparseable hook entry", so the
        id and the event have to travel in the raised message that `exc_info`
        renders beneath it. Asserting the generic line alone would pass without
        either, which is the whole gap.
        """
        self._write(tmp_path, ["NotAnEventAtAll"])
        with caplog.at_level("WARNING"):
            _store(tmp_path).list_all()

        logged = caplog.text
        assert "Skipping unparseable hook entry" in logged
        assert "NotAnEventAtAll" in logged
        assert "'h0'" in logged
