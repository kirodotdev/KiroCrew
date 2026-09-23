"""The eleven authorable hook triggers, and the line the six new ones do not cross.

A Kiro Agent session has eleven hook triggers where the gateway has five, so the
gateway's own five were the only ones a user could author -- a hook for "the agent
just created a file" had nowhere to be written down. The six others are authorable
now, and these tests pin the three things that makes true and the two it must not
break.

What it makes true: each of the six survives create, save and a cold reload; the
form offers it; the create and update schemas accept it.

What it must not break: an event name outside the eleven is still refused at every
entry, and none of the six reaches a generated kiro-cli agent spec. kiro-cli's
``hooks`` map is a closed enum -- measured against kiro-cli 2.23.1, a spec carrying
one of these keys fails to load with "data did not match any variant of untagged
enum Repr", which would cost the user their whole default agent rather than one
hook.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from kiro_crew.hooks import (
    HOOK_EVENT_AGENT_SPAWN,
    HOOK_EVENT_PRE_TOOL_USE,
    HOOK_EVENT_USER_PROMPT_SUBMIT,
    HOOK_EVENTS,
    HOOK_EVENTS_AGENT_REQUESTED,
    HOOK_EVENTS_ALL,
    HOOK_EVENTS_KAS_ONLY,
    ScriptHookStore,
    validate_hook_fields,
)
from kiro_crew.validation import (
    ALLOWED_HOOK_EVENTS,
    HOOK_CREATE_SCHEMA,
    HOOK_UPDATE_SCHEMA,
    ValidationError,
    validate_tool_args,
)

REPO = Path(__file__).resolve().parent.parent
HOOKS_PAGE = REPO / "website" / "src" / "pages" / "HooksPage.tsx"
WIRE_VALUES = REPO / "website" / "src" / "pages" / "hookEventWireValues.ts"

#: The camelCase spelling each new trigger carries on the Kiro Agent side, paired
#: with the PascalCase name the hook store persists. Written out rather than
#: derived so a rename on one side cannot quietly satisfy the other.
TWINS = {
    "preTaskExecution": "PreTaskExecution",
    "postTaskExecution": "PostTaskExecution",
    "fileCreated": "FileCreated",
    "fileEdited": "FileEdited",
    "fileDeleted": "FileDeleted",
    "userTriggered": "UserTriggered",
}


@pytest.fixture(scope="module")
def page() -> str:
    return HOOKS_PAGE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def wire_values() -> str:
    return WIRE_VALUES.read_text(encoding="utf-8")


def _skills_hook(event: str) -> None:
    """A skills-only hook definition, through the shared write-boundary validator."""
    validate_hook_fields(
        event=event,
        timeout=30,
        command="",
        skills=["deploy"],
        matcher="",
        matcher_mode="glob",
    )


def _valid(**overrides) -> dict:
    base = {"name": "h", "event": HOOK_EVENT_USER_PROMPT_SUBMIT, "command": "true"}
    base.update(overrides)
    return base


class TestTheVocabulary:
    """The three sets, and the boundary between them."""

    def test_the_fired_five_are_unchanged(self) -> None:
        """``HOOK_EVENTS`` is the promise that something calls ``fire``.

        The new triggers have no lifecycle call site, so widening this tuple
        would make the exit-code contract in ``steering-and-hooks.md`` a claim
        about events that never run.
        """
        assert HOOK_EVENTS == (
            "AgentSpawn",
            "UserPromptSubmit",
            "PreToolUse",
            "PostToolUse",
            "Stop",
        )

    def test_the_six_new_triggers_are_the_pascal_twins(self) -> None:
        assert set(HOOK_EVENTS_KAS_ONLY) == set(TWINS.values())
        assert len(HOOK_EVENTS_KAS_ONLY) == 6

    def test_the_two_groups_do_not_overlap(self) -> None:
        assert not set(HOOK_EVENTS) & set(HOOK_EVENTS_KAS_ONLY)
        assert len(set(HOOK_EVENTS_ALL)) == 11

    def test_the_requested_subset_is_the_two_task_triggers(self) -> None:
        """The distance to running differs inside the six.

        A Kiro Agent requests hooks by trigger name from a fixed set of seven,
        and only these two of the six are in it. The dashboard marks the two
        groups differently, so a name moving between them is a behaviour change
        and has to fail a test rather than pass quietly.
        """
        assert HOOK_EVENTS_AGENT_REQUESTED == (
            "PreTaskExecution",
            "PostTaskExecution",
        )
        assert set(HOOK_EVENTS_AGENT_REQUESTED) < set(HOOK_EVENTS_KAS_ONLY)

    def test_the_requested_subset_is_pinned_to_the_acp_trigger_set(self) -> None:
        """And it is pinned to the set it describes, not a second opinion of it.

        The literal above says WHICH two; this says WHY those two, against the
        source that decides it. ``ACP_HOOK_TRIGGERS`` is the fixed set a Kiro
        Agent requests from, in the camelCase spelling that side uses, so the
        membership question is "is this event's twin in that tuple". A trigger
        entering or leaving that set fails here, which is the whole point of
        reading the split from a tuple: the delivery round flips one set and the
        dashboard mark follows.
        """
        from kiro_crew.acp import kas_wire

        requested = {
            pascal for camel, pascal in TWINS.items() if camel in kas_wire.ACP_HOOK_TRIGGERS
        }
        assert set(HOOK_EVENTS_AGENT_REQUESTED) == requested
        assert {e for e in HOOK_EVENTS_KAS_ONLY if e not in requested} == {
            "FileCreated",
            "FileEdited",
            "FileDeleted",
            "UserTriggered",
        }

    def test_the_schemas_accept_exactly_the_authoring_vocabulary(self) -> None:
        """``ALLOWED_HOOK_EVENTS`` is spelled out in ``validation``, which
        ``hooks`` imports, so only a test can hold the two equal."""
        assert ALLOWED_HOOK_EVENTS == frozenset(HOOK_EVENTS_ALL)

    @pytest.mark.parametrize("camel,pascal", sorted(TWINS.items()))
    def test_each_twin_is_the_same_name_in_two_casings(self, camel: str, pascal: str) -> None:
        """The dashboard's ``normalizeEvent`` maps one to the other by
        upper-casing the first character; a pair that does not line up would
        render a Kiro-Agent-side event under an unstyled fallback badge."""
        assert camel[0].upper() + camel[1:] == pascal


class TestAuthoringTheNewTriggers:
    """Create, save, reload -- the round trip a new trigger has to survive."""

    @pytest.mark.parametrize("event", sorted(HOOK_EVENTS_KAS_ONLY))
    def test_create_accepts_the_trigger(self, event: str, tmp_path: Path) -> None:
        store = ScriptHookStore(tmp_path)
        hook = store.create(_valid(event=event, name=f"h-{event}"))
        assert hook.event == event
        assert store.get(hook.id) is not None

    @pytest.mark.parametrize("event", sorted(HOOK_EVENTS_KAS_ONLY))
    def test_the_trigger_reads_back_after_a_cold_reload(self, event: str, tmp_path: Path) -> None:
        """A second store instance parses the file from disk.

        The load path has its own event gate, and pointing that gate at the
        narrower fired set would quarantine the hook as unparseable -- saved,
        acknowledged, and gone on the next gateway start.
        """
        hook = ScriptHookStore(tmp_path).create(_valid(event=event, name=f"h-{event}"))
        reloaded = ScriptHookStore(tmp_path).get(hook.id)
        assert reloaded is not None, f"{event} hook did not survive the reload"
        assert reloaded.event == event
        assert reloaded.command == "true"

    @pytest.mark.parametrize("event", sorted(HOOK_EVENTS_KAS_ONLY))
    def test_the_persisted_file_records_the_trigger_verbatim(
        self, event: str, tmp_path: Path
    ) -> None:
        ScriptHookStore(tmp_path).create(_valid(event=event, name=f"h-{event}"))
        stored = json.loads((tmp_path / "hooks.json").read_text(encoding="utf-8"))
        assert [h["event"] for h in stored["hooks"]] == [event]

    @pytest.mark.parametrize("event", sorted(HOOK_EVENTS_KAS_ONLY))
    def test_update_can_move_a_hook_onto_the_trigger(self, event: str, tmp_path: Path) -> None:
        store = ScriptHookStore(tmp_path)
        hook = store.create(_valid())
        assert store.update(hook.id, {"event": event}).event == event

    @pytest.mark.parametrize("event", sorted(HOOK_EVENTS_KAS_ONLY))
    def test_both_dashboard_schemas_accept_the_trigger(self, event: str) -> None:
        assert validate_tool_args(_valid(event=event), HOOK_CREATE_SCHEMA)["event"] == event
        assert validate_tool_args({"event": event}, HOOK_UPDATE_SCHEMA)["event"] == event


class TestTheRefusalsThatMustSurvive:
    """Widening a closed set is only safe if it is still closed."""

    @pytest.mark.parametrize(
        "event", ["bogusEvent", "fileCreated", "filecreated", "PreTaskExec", "Manual", ""]
    )
    def test_a_name_outside_the_eleven_is_still_refused(self, event: str, tmp_path: Path) -> None:
        """Including the camelCase twins: the store's own vocabulary is the
        PascalCase one, so the Kiro-Agent-side spelling is not a second accepted
        alias for the same trigger."""
        with pytest.raises(ValueError, match="invalid event"):
            ScriptHookStore(tmp_path).create(_valid(event=event))
        with pytest.raises(ValidationError):
            validate_tool_args(_valid(event=event), HOOK_CREATE_SCHEMA)

    @pytest.mark.parametrize("event", sorted(HOOK_EVENTS_KAS_ONLY))
    def test_a_dormant_hook_is_saved_switched_off(self, event: str, tmp_path: Path) -> None:
        """The activation contract, and the only reason this PR can store these.

        Nothing fires these events today, so the mark is read from a tuple and
        clears itself when delivery lands. Saved enabled, that would hand the
        delivery round a population of hooks that begin running arbitrary shell
        commands months after they were written, with nobody reconfirming. Saved
        off, that change finds them already disabled.
        """
        hook = ScriptHookStore(tmp_path).create(_valid(event=event))
        assert hook.enabled is False

    @pytest.mark.parametrize("event", sorted(HOOK_EVENTS_KAS_ONLY))
    def test_an_explicit_enable_is_honoured(self, event: str, tmp_path: Path) -> None:
        """Turning it on IS the reconfirmation, so a caller that asks is obeyed."""
        store = ScriptHookStore(tmp_path)
        assert store.create(_valid(event=event, enabled=True)).enabled is True
        hook = store.create(_valid(event=event, name="h2"))
        assert store.update(hook.id, {"enabled": True}).enabled is True

    @pytest.mark.parametrize("event", sorted(HOOK_EVENTS))
    def test_the_five_are_still_saved_enabled(self, event: str, tmp_path: Path) -> None:
        """The default is narrowed for the six alone."""
        assert ScriptHookStore(tmp_path).create(_valid(event=event)).enabled is True

    @pytest.mark.parametrize("event", sorted(HOOK_EVENTS_KAS_ONLY))
    def test_the_contract_survives_the_schema_the_dashboard_creates_through(
        self, event: str, tmp_path: Path
    ) -> None:
        """Through ``HOOK_CREATE_SCHEMA``, which is the only path a user takes.

        ``validate_tool_args`` INJECTS a non-``None`` ``FieldSpec`` default for a
        field the caller omitted, so a ``default=True`` on ``enabled`` handed the
        store an explicit request on every create and the store's own rule --
        ``"enabled" not in data`` -- could never see an omission. Asserting the
        contract against the store alone missed that entirely: the store was right
        and the hook still persisted enabled. Drive the schema.
        """
        payload = validate_tool_args(_valid(event=event), HOOK_CREATE_SCHEMA)
        assert "enabled" not in payload, (
            "the create schema must not fabricate `enabled` -- the store decides "
            "the initial state from whether the caller named it"
        )
        assert ScriptHookStore(tmp_path).create(payload).enabled is False

    @pytest.mark.parametrize("event", sorted(HOOK_EVENTS))
    def test_a_live_trigger_through_the_schema_is_still_enabled(
        self, event: str, tmp_path: Path
    ) -> None:
        """Dropping the schema default must not switch the five off: an absent
        value still means on in ``ScriptHook.from_dict``."""
        payload = validate_tool_args(_valid(event=event), HOOK_CREATE_SCHEMA)
        assert ScriptHookStore(tmp_path).create(payload).enabled is True

    @pytest.mark.parametrize("event", sorted(HOOK_EVENTS_KAS_ONLY))
    def test_an_explicit_enable_through_the_schema_is_honoured(
        self, event: str, tmp_path: Path
    ) -> None:
        """A caller that names it is still obeyed -- that IS the reconfirmation."""
        payload = validate_tool_args(_valid(event=event, enabled=True), HOOK_CREATE_SCHEMA)
        assert payload["enabled"] is True
        assert ScriptHookStore(tmp_path).create(payload).enabled is True

    @pytest.mark.parametrize("schema", [HOOK_CREATE_SCHEMA, HOOK_UPDATE_SCHEMA])
    def test_an_explicit_null_enable_is_refused(self, schema: object) -> None:
        """``enabled: null`` is a third state, and every reader calls it off.

        ``validate_field`` answers ``spec.default`` for ``None`` BEFORE the type
        check, so a field with no default answers ``None`` while the key's presence
        carries that ``None`` through -- past the store's event-aware default, which
        is skipped because the key IS there. The hook then persists with ``enabled``
        neither true nor false, ``fire`` skips it and the row renders dimmed.

        Both schemas, because the update schema's ``enabled`` has never had a
        default: fixing only create would leave the same corruption one endpoint
        away.
        """
        body = _valid(event="FileEdited")
        body["enabled"] = None
        with pytest.raises(ValidationError, match="enabled"):
            validate_tool_args(body, schema)  # type: ignore[arg-type]

    @pytest.mark.parametrize("value", [True, False])
    def test_a_real_boolean_still_passes_both_schemas(self, value: bool) -> None:
        """The refusal is for ``null`` alone -- neither boolean is caught by it."""
        create = validate_tool_args(_valid(event="FileEdited", enabled=value), HOOK_CREATE_SCHEMA)
        assert create["enabled"] is value
        assert validate_tool_args({"enabled": value}, HOOK_UPDATE_SCHEMA)["enabled"] is value

    @pytest.mark.parametrize("event", sorted(HOOK_EVENTS_KAS_ONLY))
    def test_moving_a_live_hook_onto_the_trigger_switches_it_off(
        self, event: str, tmp_path: Path
    ) -> None:
        """The contract has to hold on BOTH write paths, not only on create.

        An edit is the cheap route to the very harm the off-by-default rule exists
        to stop: take a hook that already runs, point it at one of the six, and a
        create-only rule leaves it enabled -- so the change that starts firing these
        events inherits a command nobody reconfirmed for them.
        """
        store = ScriptHookStore(tmp_path)
        hook = store.create(_valid(event="PreToolUse"))
        assert hook.enabled is True
        assert store.update(hook.id, {"event": event}).enabled is False

    @pytest.mark.parametrize("event", sorted(HOOK_EVENTS_KAS_ONLY))
    def test_an_explicit_enable_on_that_move_is_honoured(self, event: str, tmp_path: Path) -> None:
        """Naming it IS the reconfirmation, on the update path as on create."""
        store = ScriptHookStore(tmp_path)
        hook = store.create(_valid(event="PreToolUse"))
        assert store.update(hook.id, {"event": event, "enabled": True}).enabled is True

    @pytest.mark.parametrize("event", sorted(HOOK_EVENTS_KAS_ONLY))
    def test_editing_a_dormant_hook_does_not_switch_it_off_again(
        self, event: str, tmp_path: Path
    ) -> None:
        """The mirror defect: only the TRANSITION switches off.

        The edit form always sends ``event``, so a rule keyed on that key's presence
        would switch off a hook the user had deliberately enabled every time they
        touched its command. Keyed on the transition, it does not.
        """
        store = ScriptHookStore(tmp_path)
        hook = store.create(_valid(event=event, enabled=True))
        assert hook.enabled is True
        same = store.update(hook.id, {"event": event, "command": "echo edited"})
        assert same.enabled is True

    @pytest.mark.parametrize("event", sorted(HOOK_EVENTS))
    def test_moving_between_live_events_leaves_enabled_alone(
        self, event: str, tmp_path: Path
    ) -> None:
        """The narrowing is for the six alone: a live-to-live edit is untouched."""
        store = ScriptHookStore(tmp_path)
        hook = store.create(_valid(event="PreToolUse"))
        assert store.update(hook.id, {"event": event}).enabled is True

    @pytest.mark.parametrize("event", sorted(HOOK_EVENTS_KAS_ONLY))
    def test_fire_skips_a_dormant_hook_while_test_still_runs_it(
        self, event: str, tmp_path: Path
    ) -> None:
        """The asymmetry the contract rests on, asserted rather than assumed.

        ``fire`` filters on ``enabled``; the dashboard's Test endpoint never reads
        it. If Test ever started honouring ``enabled``, saving these off would
        remove the only way they can run, so this pins both halves together.
        """
        import inspect

        from kiro_crew import hooks as hooks_mod

        fire_src = inspect.getsource(hooks_mod.ScriptHookStore.fire)
        assert "enabled" in fire_src, "fire must keep filtering on enabled"

        handler = Path(__file__).resolve().parent.parent / (
            "src/kiro_crew/dashboard/handlers/hooks.py"
        )
        body = handler.read_text(encoding="utf-8")
        start = body.index("async def api_hook_test")
        test_fn = body[start : body.index("\nasync def ", start + 1)]
        assert "enabled" not in test_fn, (
            "api_hook_test must not gate on enabled -- Test is the only way a dormant "
            "hook runs, and saving these off would otherwise make them unrunnable"
        )

    @pytest.mark.parametrize("event", sorted(HOOK_EVENTS_KAS_ONLY))
    def test_a_matcher_is_refused_on_the_new_triggers(self, event: str, tmp_path: Path) -> None:
        """A matcher filters the event's payload, and these events have no payload.

        Accepting one would store a filter written against whatever the author had
        in mind while the change that defines the payload is free to pick a
        different subject -- so the hook would later fire on the wrong things, with
        no way to tell the stored intent from a correct filter.
        """
        with pytest.raises(ValueError, match="a matcher cannot be set"):
            ScriptHookStore(tmp_path).create(_valid(event=event, matcher="*.py"))
        with pytest.raises(ValueError, match="a matcher cannot be set"):
            validate_hook_fields(
                event=event,
                timeout=30,
                command="true",
                skills=[],
                matcher="*.py",
                matcher_mode="glob",
            )

    @pytest.mark.parametrize("event", sorted(HOOK_EVENTS_KAS_ONLY))
    def test_an_empty_matcher_is_still_fine_on_the_new_triggers(
        self, event: str, tmp_path: Path
    ) -> None:
        """The refusal is of a VALUE, not of the field: the default stays saveable."""
        assert ScriptHookStore(tmp_path).create(_valid(event=event)).matcher == ""

    @pytest.mark.parametrize("event", sorted(HOOK_EVENTS))
    def test_a_matcher_is_still_accepted_on_the_five(self, event: str, tmp_path: Path) -> None:
        store = ScriptHookStore(tmp_path)
        assert store.create(_valid(event=event, name=f"h-{event}", matcher="fs_write")).matcher == (
            "fs_write"
        )

    @pytest.mark.parametrize("event", sorted(HOOK_EVENTS_KAS_ONLY))
    def test_a_hand_edited_matcher_is_dropped_on_load_not_trapped(
        self, event: str, tmp_path: Path
    ) -> None:
        """Refusing the pairing at the write boundary must not strand a stored one.

        ``hooks.json`` is hand-editable and the load gate admits all eleven events,
        so a file can carry this pairing even though create and update refuse it.
        Update re-validates the MERGED fields, so keeping the stored matcher would
        refuse every later edit -- the user could not even switch the hook off
        without editing the file again. Deserialization normalizes instead, which
        is the contract the timeout clamp beside it already follows.
        """
        (tmp_path / "hooks.json").write_text(
            json.dumps(
                {
                    "hooks": [
                        {
                            "id": "h1",
                            "name": "formatter",
                            "event": event,
                            "matcher": "*.py",
                            "matcher_mode": "glob",
                            "command": "formatter --changed",
                            "skills": [],
                            "timeout": 30,
                            "enabled": True,
                            "last_run": 0,
                            "last_status": "",
                            "last_error": "",
                            "run_count": 0,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        store = ScriptHookStore(tmp_path)

        loaded = store.get("h1")
        assert loaded is not None, "the hook must load, not be quarantined"
        assert loaded.event == event
        assert loaded.matcher == "", "the meaningless part is dropped, not the hook"
        assert loaded.command == "formatter --changed"

        # The edits the stored matcher would otherwise have refused.
        assert store.update("h1", {"enabled": False}).enabled is False
        assert store.update("h1", {"command": "true"}).command == "true"

    @pytest.mark.parametrize("event", sorted(HOOK_EVENTS))
    def test_a_hand_edited_matcher_survives_on_the_five(self, event: str, tmp_path: Path) -> None:
        """The normalization is scoped to the six: a real matcher is not touched."""
        (tmp_path / "hooks.json").write_text(
            json.dumps(
                {
                    "hooks": [
                        {
                            "id": "h1",
                            "name": "h",
                            "event": event,
                            "matcher": "fs_write",
                            "matcher_mode": "glob",
                            "command": "true",
                            "skills": [],
                            "timeout": 30,
                            "enabled": True,
                            "last_run": 0,
                            "last_status": "",
                            "last_error": "",
                            "run_count": 0,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        hook = ScriptHookStore(tmp_path).get("h1")
        assert hook is not None and hook.matcher == "fs_write"

    @pytest.mark.parametrize("event", sorted(HOOK_EVENTS_KAS_ONLY))
    def test_a_partial_move_onto_the_new_triggers_drops_the_stored_matcher(
        self, event: str, tmp_path: Path
    ) -> None:
        """A `PUT` carrying only `event` is not refused over a field it omits.

        The update schema gives `matcher` no default, so a partial request leaves
        the key absent and the merged hook carries the STORED filter into the
        pairing refusal -- a 400 naming a field the caller never sent, with no way
        to complete the move in one request. The move is what was asked for and the
        filter is the part with no meaning on these events, so it goes the same way
        deserialization drops one.
        """
        store = ScriptHookStore(tmp_path)
        hook = store.create(_valid(event=HOOK_EVENT_PRE_TOOL_USE, matcher="fs_write"))
        assert hook.matcher == "fs_write"

        moved = store.update(hook.id, validate_tool_args({"event": event}, HOOK_UPDATE_SCHEMA))
        assert moved is not None
        assert moved.event == event
        assert moved.matcher == "", "the meaningless part is dropped, not the move refused"
        assert moved.enabled is False, "and the activation contract still holds"

        reloaded = ScriptHookStore(tmp_path).get(hook.id)
        assert reloaded is not None and reloaded.matcher == ""

    @pytest.mark.parametrize("event", sorted(HOOK_EVENTS_KAS_ONLY))
    def test_a_matcher_sent_with_the_move_is_still_refused(
        self, event: str, tmp_path: Path
    ) -> None:
        """Dropping an omitted filter is not accepting a named one.

        `create` refuses a matcher a caller asks for on these events rather than
        saving the hook without the filter it named, and the update path draws the
        same line: the drop above is keyed on the field being ABSENT.
        """
        store = ScriptHookStore(tmp_path)
        hook = store.create(_valid(event=HOOK_EVENT_PRE_TOOL_USE, matcher="fs_write"))
        with pytest.raises(ValueError, match="a matcher cannot be set"):
            store.update(hook.id, {"event": event, "matcher": "*.py"})

    @pytest.mark.parametrize("event", sorted(HOOK_EVENTS))
    def test_a_partial_move_onto_the_five_keeps_the_stored_matcher(
        self, event: str, tmp_path: Path
    ) -> None:
        """The drop is scoped to the six, like the load-time one beside it."""
        store = ScriptHookStore(tmp_path)
        hook = store.create(_valid(event=HOOK_EVENT_PRE_TOOL_USE, matcher="fs_write"))
        moved = store.update(hook.id, {"event": event})
        assert moved is not None and moved.matcher == "fs_write"

    @pytest.mark.parametrize("event", sorted(HOOK_EVENTS_KAS_ONLY))
    def test_a_skills_hook_still_refuses_the_new_triggers(self, event: str) -> None:
        """The "Load skills:" directive has a reader on two events only, and a
        new trigger does not add one."""
        with pytest.raises(ValueError, match="skills hooks cannot fire"):
            _skills_hook(event)

    @pytest.mark.parametrize("event", [HOOK_EVENT_USER_PROMPT_SUBMIT, HOOK_EVENT_AGENT_SPAWN])
    def test_a_skills_hook_still_works_on_the_two_that_read_it(self, event: str) -> None:
        _skills_hook(event)

    def test_the_five_still_behave_as_before(self, tmp_path: Path) -> None:
        store = ScriptHookStore(tmp_path)
        for i, event in enumerate(HOOK_EVENTS):
            assert store.create(_valid(event=event, name=f"h{i}")).event == event
        with pytest.raises(ValueError, match="skills hooks cannot fire"):
            _skills_hook(HOOK_EVENT_PRE_TOOL_USE)


class TestTheKiroCliSpecBoundary:
    """None of the six may reach a generated kiro-cli agent spec.

    kiro-cli 2.23.1 treats the ``hooks`` map as a closed enum: ``agent
    validate`` on a spec with a sixth key answers "data did not match any
    variant of untagged enum Repr", and ``agent list`` refuses the same file. An
    unknown TOP-LEVEL key and an unknown hook-entry field are both accepted and
    ignored, so the closed set is the event map specifically.
    """

    def test_the_new_triggers_are_not_valid_kiro_cli_events(self) -> None:
        from kiro_crew.agent import _CREW_ONLY_HOOK_EVENTS, _VALID_HOOK_EVENTS

        assert _CREW_ONLY_HOOK_EVENTS == frozenset(TWINS)
        assert not _CREW_ONLY_HOOK_EVENTS & _VALID_HOOK_EVENTS

    def test_the_generation_filter_strips_them(self) -> None:
        from kiro_crew.agent import _kiro_hooks_only

        hooks = {"agentSpawn": [{"command": "/bin/true"}]}
        hooks.update({camel: [{"command": "/bin/true"}] for camel in TWINS})
        assert set(_kiro_hooks_only(hooks)) == {"agentSpawn"}

    @pytest.mark.parametrize("camel", sorted(TWINS))
    def test_a_user_authored_trigger_is_refused_as_a_kiro_agent_trigger(
        self, camel: str, monkeypatch, caplog
    ) -> None:
        """Reported as its own refusal, not as "unknown event type".

        The name IS known -- it just cannot travel in this file -- and reporting
        it as unknown sends the reader hunting a typo that is not there. The SEL
        audit still fires, because dropping a whole bucket is a decision either
        way.
        """
        import logging

        from kiro_crew import agent as agent_mod
        from kiro_crew.agent import _apply_user_kiro_hooks

        sel_calls: list[tuple[str, str, str]] = []
        monkeypatch.setattr(
            agent_mod,
            "_sel_hook_rejected",
            lambda event, command, reason: sel_calls.append((event, command, reason)),
        )

        config: dict = {"hooks": {}}
        mc_cfg = {
            "agent": {
                "kiro_hooks": {camel: [{"command": "/bin/true"}]},
                "kiro_hooks_autoimport": False,
            }
        }
        with caplog.at_level(logging.WARNING, logger="kiro_crew.agent"):
            _apply_user_kiro_hooks(config, mc_cfg)

        assert config["hooks"] == {}
        assert sel_calls, "dropping the bucket must still emit a SEL audit"
        event_tag, _command, reason = sel_calls[0]
        assert event_tag == camel
        assert reason == "Kiro Agent trigger, not emitted to kiro-cli"
        assert "unknown event type" not in caplog.text

    @pytest.mark.parametrize("camel", sorted(TWINS))
    def test_an_autoimported_script_is_refused_the_same_way(
        self, camel: str, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        """A ``# event:`` header naming a Kiro Agent trigger gets that reason too.

        The autoimport scan resolves the header through the same canonical map, so
        recognising the name is what keeps it out of the "unknown event" class. The
        merge gate, not the map, is what keeps it out of the generated spec.
        """
        import logging
        import stat

        from kiro_crew import agent as agent_mod
        from kiro_crew.agent import _apply_user_kiro_hooks

        pascal = TWINS[camel]
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()
        script = hooks_dir / "formatter.sh"
        script.write_text(f"#!/bin/sh\n# event: {pascal}\nexit 0\n", encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IXUSR)

        sel_calls: list[tuple[str, str, str]] = []
        monkeypatch.setattr(
            agent_mod,
            "_sel_hook_rejected",
            lambda event, command, reason: sel_calls.append((event, command, reason)),
        )
        monkeypatch.setattr(agent_mod, "_DEFAULT_KIRO_HOOKS_DIR", hooks_dir)

        config: dict = {"hooks": {}}
        mc_cfg = {"agent": {"kiro_hooks": {}, "kiro_hooks_autoimport": True}}
        with caplog.at_level(logging.WARNING, logger="kiro_crew.agent"):
            _apply_user_kiro_hooks(config, mc_cfg)

        assert camel not in config["hooks"]
        reasons = [r for _e, _c, r in sel_calls]
        assert "unknown event header" not in reasons, f"reported as unknown: {sel_calls!r}"
        assert (
            "Kiro Agent trigger, not emitted to kiro-cli" in reasons
        ), f"expected the Kiro Agent reason; got {sel_calls!r}"


class TestTheFormOffersThem:
    """The authoring surface, pinned to the code the way ``MATCHER_MODES`` is."""

    def test_the_picker_offers_every_authorable_event_in_order(self, wire_values: str) -> None:
        declared = re.search(r"export const EVENTS = \[(.*?)\]", wire_values, re.S)
        assert declared, "EVENTS no longer declared in hookEventWireValues.ts"
        offered = re.findall(r"'([^']+)'", declared.group(1))
        assert offered == list(HOOK_EVENTS_ALL), (
            f"hookEventWireValues offers {offered}; hooks.HOOK_EVENTS_ALL "
            f"ships {list(HOOK_EVENTS_ALL)}"
        )

    @pytest.mark.parametrize(
        "name,expected",
        [
            ("KAS_ONLY_EVENTS", HOOK_EVENTS_KAS_ONLY),
            ("AGENT_REQUESTED_EVENTS", HOOK_EVENTS_AGENT_REQUESTED),
        ],
    )
    def test_the_marker_reads_the_split_from_the_same_tuples(
        self, wire_values: str, name: str, expected: tuple[str, ...]
    ) -> None:
        """The dormant mark on the picker option and the row is derived, not typed.

        Two marks distinguish the six, and a hand-maintained copy of either set
        would let the dashboard claim a trigger is awaiting an agent that never
        asks for it. The round that ships delivery moves a name out of a tuple
        here and both surfaces follow.
        """
        declared = re.search(rf"export const {name} = \[(.*?)\]", wire_values, re.S)
        assert declared, f"{name} no longer declared in hookEventWireValues.ts"
        listed = re.findall(r"'([^']+)'", declared.group(1))
        assert listed == list(
            expected
        ), f"{name} lists {listed}; the backend tuple ships {list(expected)}"

    @pytest.mark.parametrize("const", ["EVENT_STYLE", "EVENT_BADGE"])
    def test_every_event_has_its_own_badge(self, page: str, const: str) -> None:
        """A missing key falls through to an unstyled grey pill, which reads as
        "this event is not recognised" on a row the form itself created."""
        block = re.search(rf"const {const}: Record<[^>]+> = \{{(.*?)\n\}}", page, re.S)
        assert block, f"{const} no longer declared in HooksPage.tsx"
        keyed = set(re.findall(r"(\w+):", block.group(1)))
        assert keyed == set(HOOK_EVENTS_ALL), f"{const} is missing {set(HOOK_EVENTS_ALL) - keyed}"
