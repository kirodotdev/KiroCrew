"""The dynamic dashboard's own parts: what each refuses, and what it guarantees.

Organised by the thing under test rather than by contract part, because the
guarantees cross the parts: the type rule lives in the write path AND in the fold,
and both are here next to each other so a change to one that forgets the other
fails in one file.
"""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew import agent_panel, dashboard_agentic, dashboard_feed, dashboard_frame
from kiro_crew.crew_log import entry_types, projection
from kiro_crew.crew_log.schema import Entry
from kiro_crew.dashboard_templates.manifest import parse_manifest

#: The raw manifest the instance-seam cases hand the stubbed store. A dict rather
#: than a parsed manifest because that is what the record carries: the store keeps
#: the COPIED manifest undecoded so an unparseable one is still readable.
MANIFEST_RAW: dict[str, Any] = {
    "id": "demo",
    "version": 2,
    "title": "Demo",
    "description": "A demo dashboard",
    "source": "builtin",
    "fields": {"open_items": {"type": "number", "source": {"agentic": True}}},
}

AGENTIC = entry_types.DASHBOARD_AGENTIC_ENTRY_TYPE
REFUSED = entry_types.DASHBOARD_REFUSED_ENTRY_TYPE


def entry(etype: str, data: dict[str, Any], seq: int = 1) -> Entry:
    return Entry(type=etype, seq=seq, time=1_759_000_000_000 + seq, src="gateway", data=data)


def manifest(**fields: Any) -> Any:
    return parse_manifest(
        {
            "id": "demo",
            "version": 2,
            "title": "Demo",
            "description": "A demo dashboard",
            "source": "builtin",
            "fields": fields
            or {
                "open_items": {"type": "number", "source": {"agentic": True}},
                "risk_note": {"type": "string", "source": {"agentic": True}},
                "entries": {
                    "type": "number",
                    "source": {"fold": "work", "path": "conductor.entries"},
                },
            },
        }
    )


def instance(**fields: Any) -> dashboard_agentic.Instance:
    return dashboard_agentic.Instance(manifest=manifest(**fields), instance_version=7)


# -------------------------------------------------------------------------- #
# the type rule, which lives in two places on purpose
# -------------------------------------------------------------------------- #


#: One table, both implementations. Each row is ``(declared, value, holds)``.
TYPE_CASES: list[tuple[str, Any, bool]] = [
    ("number", 7, True),
    ("number", 7.5, True),
    ("number", "7", False),
    # Python says ``isinstance(True, int)``, so without the exclusion a crewmate
    # writing ``true`` into a number field would pass and the page would render
    # ``True`` where a count belongs.
    ("number", True, False),
    ("string", "two blocked", True),
    ("string", 7, False),
    ("boolean", True, False if False else True),
    ("boolean", 1, False),
    ("array", [1, 2], True),
    ("array", {"a": 1}, False),
    ("object", {"a": 1}, True),
    ("object", [1], False),
    ("mystery", 1, False),
]


class TestTheTypeRule:
    @pytest.mark.parametrize("declared,value,holds", TYPE_CASES)
    def test_the_write_path_applies_it(self, declared: str, value: Any, holds: bool) -> None:
        assert dashboard_agentic.type_holds(declared, value) is holds

    @pytest.mark.parametrize("declared,value,holds", TYPE_CASES)
    def test_the_fold_applies_the_same_rule(self, declared: str, value: Any, holds: bool) -> None:
        """Duplicated deliberately -- the fold cannot import the write path and the
        write path cannot live in ``projection`` (the manifest imports the fold
        names, so the cycle closes the other way). This is what keeps them equal."""
        assert projection._dashboard_type_holds(declared, value) is holds

    def test_every_manifest_type_has_a_branch(self) -> None:
        """A type added to the manifest without a branch would refuse every write to
        a field of the new type, which looks like a broken field rather than a
        missing rule."""
        for declared in dashboard_agentic.CHECKED_TYPES:
            sample = {"number": 1, "string": "s", "boolean": True, "array": [], "object": {}}[
                declared
            ]
            assert dashboard_agentic.type_holds(declared, sample) is True


# -------------------------------------------------------------------------- #
# part 6: what a write may say, and what a refusal teaches
# -------------------------------------------------------------------------- #


class TestTheAgenticWrite:
    def test_a_well_typed_write_to_an_agentic_field_is_accepted(self) -> None:
        written = dashboard_agentic.check_write(instance(), "open_items", 7)
        assert written == {
            "field": "open_items",
            "type": "number",
            # Wrapped: the crew-log entry declares ``value`` as an object because the
            # entry registry has no any-type. The FOLD unwraps it again, so a reader
            # of the fold never meets the box.
            "value": {"v": 7},
            "instance_version": 7,
        }

    def test_an_unknown_field_is_refused_and_told_the_real_names(self) -> None:
        with pytest.raises(dashboard_agentic.WriteRefused) as caught:
            dashboard_agentic.check_write(instance(), "credits_total", 7)
        assert caught.value.code == "unknown_field"
        # The names come from the MANIFEST, so a refusal cannot advise a field that
        # does not exist -- which is the failure a hand-written list produces.
        assert "open_items (number)" in str(caught.value)
        assert "risk_note (string)" in str(caught.value)

    def test_a_fold_sourced_field_is_refused_with_where_its_value_comes_from(self) -> None:
        """Telling the agent only "no" would leave it able to try again forever. The
        fold and path say the number is already recorded."""
        with pytest.raises(dashboard_agentic.WriteRefused) as caught:
            dashboard_agentic.check_write(instance(), "entries", 7)
        assert caught.value.code == "field_not_agentic"
        assert "'work'" in str(caught.value) and "conductor.entries" in str(caught.value)

    def test_a_wrong_type_is_refused_in_the_manifests_own_vocabulary(self) -> None:
        """So the sentence can be acted on without translating from Python's type
        names."""
        with pytest.raises(dashboard_agentic.WriteRefused) as caught:
            dashboard_agentic.check_write(instance(), "open_items", "seven")
        assert caught.value.code == "wrong_type"
        assert "wants number" in str(caught.value)
        assert "is string" in str(caught.value)

    def test_a_value_over_the_cap_is_refused_with_the_size_and_the_remedy(self) -> None:
        big = ["x" * 64] * 1000
        with pytest.raises(dashboard_agentic.WriteRefused) as caught:
            dashboard_agentic.check_write(
                instance(series={"type": "array", "source": {"agentic": True}}), "series", big
            )
        assert caught.value.code == "value_too_large"
        assert str(entry_types.DASHBOARD_VALUE_BYTES) in str(caught.value)

    def test_an_unserializable_value_is_refused_before_it_reaches_the_log(self) -> None:
        with pytest.raises(dashboard_agentic.WriteRefused) as caught:
            dashboard_agentic.check_write(instance(), "open_items", float("nan"))
        assert caught.value.code == "value_not_serializable"

    def test_a_crewmate_with_no_dashboard_is_told_so_rather_than_refused_per_field(self) -> None:
        with pytest.raises(dashboard_agentic.WriteRefused) as caught:
            dashboard_agentic.check_write(None, "open_items", 7)
        assert caught.value.code == "no_instance"
        assert "adopt a template" in str(caught.value)

    def test_a_template_with_no_agentic_field_says_so_instead_of_offering_nothing(self) -> None:
        """Telling an agent to choose from an empty list would send it round the
        retry budget for no reason."""
        only_folds = instance(
            entries={"type": "number", "source": {"fold": "work", "path": "conductor.entries"}}
        )
        with pytest.raises(dashboard_agentic.WriteRefused) as caught:
            dashboard_agentic.check_write(only_folds, "whatever", 1)
        assert "no agentic field" in str(caught.value)


class TestTheMistakeBookInARefusal:
    def book(self, **over: Any) -> dict[str, Any]:
        group = {
            "code": "unknown_field",
            "field": "credits_total",
            "count": 2,
            "reason": "no such field",
            "corrected_to": [],
        }
        group.update(over)
        return {"groups": [group], "refused": 2}

    def test_a_repeated_mistake_is_named_as_repeated(self) -> None:
        with pytest.raises(dashboard_agentic.WriteRefused) as caught:
            dashboard_agentic.check_write(instance(), "credits_total", 1, self.book())
        assert "2 times before" in str(caught.value)

    def test_a_mistake_with_a_known_answer_quotes_the_answer(self) -> None:
        """The correction is what makes it a mistake book rather than a scoreboard."""
        with pytest.raises(dashboard_agentic.WriteRefused) as caught:
            dashboard_agentic.check_write(
                instance(), "credits_total", 1, self.book(corrected_to=["open_items"])
            )
        assert "'open_items' worked" in str(caught.value)

    def test_a_first_time_mistake_gets_the_plain_refusal(self) -> None:
        """Silent when the book has nothing: the plain refusal is enough the first
        time, and a "you have done this before" on a first attempt is false."""
        message = ""
        with pytest.raises(dashboard_agentic.WriteRefused) as caught:
            dashboard_agentic.check_write(instance(), "nonesuch", 1, self.book())
        message = str(caught.value)
        assert "before" not in message

    def test_the_retry_budget_is_one_constant_and_reaches_the_agent(self) -> None:
        """Its VALUE is not decided; that it is one constant quoted everywhere is."""
        with pytest.raises(dashboard_agentic.WriteRefused) as caught:
            dashboard_agentic.check_write(instance(), "credits_total", 1, self.book())
        assert f"{dashboard_agentic.AGENTIC_RETRY_BUDGET} tries" in str(caught.value)


class TestWhatTheAgentReadsBeforeWriting:
    def test_every_field_is_listed_with_where_its_value_comes_from(self) -> None:
        """Fold fields are listed too: an agent that cannot see them has no way to
        know a number is already recorded, and would duplicate it under an agentic
        name so the page shows one quantity twice with two values."""
        listed = dashboard_agentic.fields_for_agent(instance())["fields"]
        by_name = {row["field"]: row for row in listed}
        assert by_name["open_items"]["source"] == "agentic"
        assert by_name["entries"]["source"] == "fold"
        assert by_name["entries"]["fold"] == "work"
        assert by_name["entries"]["path"] == "conductor.entries"

    def test_the_writable_names_are_reported_separately(self) -> None:
        assert dashboard_agentic.fields_for_agent(instance())["agentic"] == [
            "open_items",
            "risk_note",
        ]

    def test_the_mistake_book_is_trimmed_and_carries_the_answer(self) -> None:
        groups = [
            {"code": "unknown_field", "field": f"f{n}", "count": 9 - n, "reason": "r"}
            for n in range(9)
        ]
        groups[0]["corrected_to"] = ["open_items"]
        shown = dashboard_agentic.fields_for_agent(instance(), {"groups": groups})["mistakes"]
        assert len(shown) == dashboard_agentic.MISTAKES_SHOWN
        assert shown[0]["use_instead"] == "open_items"

    def test_a_crewmate_with_no_dashboard_still_reads_its_mistakes(self) -> None:
        """Its refused writes are the one thing it has, and they are what tell the
        human why nothing is on screen."""
        read = dashboard_agentic.fields_for_agent(
            None, {"groups": [{"code": "no_instance", "field": "", "count": 3, "reason": "r"}]}
        )
        assert read["template"] is None
        assert read["mistakes"][0]["count"] == 3


class TestTheCorrectionEntry:
    def test_an_accepted_write_corrects_an_outstanding_mistake(self) -> None:
        book = {"groups": [{"code": "unknown_field", "field": "credits_total", "count": 2}]}
        assert dashboard_agentic.correction_entry("open_items", book) == {
            # Coded: ``code`` is required on the entry type, and the fold decides
            # which of its two shapes a line is from this key.
            "code": entry_types.MISTAKE_CORRECTED_CODE,
            "field": "open_items",
            "corrects": ["credits_total"],
        }

    def test_a_mistake_already_answered_is_not_corrected_again(self) -> None:
        book = {"groups": [{"field": "credits_total", "count": 2, "corrected_to": ["open_items"]}]}
        assert dashboard_agentic.correction_entry("open_items", book) is None

    def test_an_ordinary_write_appends_no_second_entry(self) -> None:
        assert dashboard_agentic.correction_entry("open_items", {"groups": []}) is None

    def test_a_name_mistake_is_not_answered_by_a_write_to_that_same_name(self) -> None:
        """The lesson is which name to use, so the refused name cannot be its own
        answer -- the book would answer a question with the question."""
        book = {"groups": [{"code": "unknown_field", "field": "credits_total", "count": 1}]}
        assert dashboard_agentic.correction_entry("credits_total", book) is None

    def test_a_value_mistake_is_answered_by_a_write_to_the_same_field(self) -> None:
        """MEASURED ON A POD: a wrong_type group was never answered, because the
        rule "a write never corrects itself" is right for a name and wrong for a
        type. The name was right and the value was not, so the correct write to that
        same field IS the answer -- and it is the harder lesson of the two, since a
        wrong name is visible in the field list while a wrong type is only visible in
        the refusal."""
        book = {"groups": [{"code": "wrong_type", "field": "open_items", "count": 2}]}
        assert dashboard_agentic.correction_entry("open_items", book) == {
            "code": entry_types.MISTAKE_CORRECTED_CODE,
            "field": "open_items",
            "corrects": ["open_items"],
        }

    def test_a_value_mistake_is_not_answered_by_a_write_to_another_field(self) -> None:
        book = {"groups": [{"code": "wrong_type", "field": "open_items", "count": 2}]}
        assert dashboard_agentic.correction_entry("risk_note", book) is None

    def test_every_refusal_code_is_classified_as_a_name_or_a_value_mistake(self) -> None:
        """A code in neither set is a refusal the book can never answer, which looks
        like a mistake nothing fixes."""
        produced = set()
        for field, value in [
            ("credits_total", 1),
            ("entries", 1),
            ("open_items", "x"),
            ("open_items", float("nan")),
        ]:
            try:
                dashboard_agentic.check_write(instance(), field, value)
            except dashboard_agentic.WriteRefused as refused:
                produced.add(refused.code)
        classified = dashboard_agentic._NAME_CODES | dashboard_agentic._VALUE_CODES
        assert produced <= classified, sorted(produced - classified)


# -------------------------------------------------------------------------- #
# part 2: the two folds
# -------------------------------------------------------------------------- #


class TestTheAgenticFold:
    def fold(self, *entries: Entry) -> dict[str, Any]:
        state = projection._agentic_start()
        for item in entries:
            projection._agentic_step(state, item)
        return projection._agentic_render(state)

    def test_a_write_fills_one_field_and_leaves_the_others(self) -> None:
        """The fields are independent cells, which is where this fold parts company
        with ``panel``'s whole-document replacement."""
        read = self.fold(
            entry(AGENTIC, {"field": "a", "type": "number", "value": {"v": 1}}, 1),
            entry(AGENTIC, {"field": "b", "type": "string", "value": {"v": "x"}}, 2),
            entry(AGENTIC, {"field": "a", "type": "number", "value": {"v": 2}}, 3),
        )
        assert read["fields"]["a"]["value"] == 2
        assert read["fields"]["b"]["value"] == "x"
        assert read["wrote"] == 3

    def test_a_line_whose_type_and_value_disagree_is_dropped(self) -> None:
        """These are bytes off a file the reader does not control, so a planted or
        damaged line is exactly the input that ignores the writer's check -- and
        applying it would put a string in a cell a chart reads as a number."""
        read = self.fold(entry(AGENTIC, {"field": "a", "type": "number", "value": {"v": "seven"}}))
        assert read["fields"] == {}

    def test_a_line_naming_no_field_or_no_type_is_dropped(self) -> None:
        read = self.fold(
            entry(AGENTIC, {"type": "number", "value": 1}, 1),
            entry(AGENTIC, {"field": "a", "value": 1}, 2),
            entry(AGENTIC, {"field": "a", "type": "number"}, 3),
        )
        assert read["fields"] == {}

    def test_an_oversized_value_is_dropped_rather_than_retained(self) -> None:
        huge = "x" * (entry_types.DASHBOARD_VALUE_BYTES + 1)
        read = self.fold(entry(AGENTIC, {"field": "a", "type": "string", "value": {"v": huge}}))
        assert read["fields"] == {}

    def test_the_field_count_is_bounded_and_the_eviction_is_reported(self) -> None:
        """A wedged crewmate writing a generated field name per cycle must not put an
        unbounded set into retained state."""
        over = entry_types.DASHBOARD_VALUE_LIMIT + 5
        read = self.fold(
            *(
                entry(AGENTIC, {"field": f"f{n}", "type": "number", "value": {"v": n}}, n + 1)
                for n in range(over)
            )
        )
        assert len(read["fields"]) == entry_types.DASHBOARD_VALUE_LIMIT
        assert read["fields_omitted"] == 5
        # Least recently written goes, so what survives is what is being written now.
        assert "f0" not in read["fields"]
        assert f"f{over - 1}" in read["fields"]

    def test_the_folds_own_ranking_is_not_served_to_a_reader(self) -> None:
        """``order`` is the eviction key and means nothing to a reader, who gets
        ``at`` for the same question."""
        read = self.fold(entry(AGENTIC, {"field": "a", "type": "number", "value": {"v": 1}}))
        assert "order" not in read["fields"]["a"]
        assert read["fields"]["a"]["at"]

    def test_a_wrote_count_of_zero_distinguishes_never_from_empty(self) -> None:
        assert self.fold()["wrote"] == 0

    def test_another_entry_type_does_not_move_it(self) -> None:
        assert self.fold(entry("panel/published", {"template": "x", "data": {}}))["wrote"] == 0


class TestTheMistakeBookFold:
    def fold(self, *entries: Entry) -> dict[str, Any]:
        state = projection._mistakes_start()
        for item in entries:
            projection._mistakes_step(state, item)
        return projection._mistakes_render(state)

    def refusal(self, seq: int, **over: Any) -> Entry:
        data = {"code": "unknown_field", "field": "credits_total", "reason": "no such field"}
        data.update(over)
        return entry(REFUSED, data, seq)

    def test_the_same_mistake_twice_is_one_group_with_a_count_of_two(self) -> None:
        """THE POD DEMO'S SHAPE: two wrong writes, one group, count 2."""
        read = self.fold(self.refusal(1), self.refusal(2))
        assert len(read["groups"]) == 1
        assert read["groups"][0]["count"] == 2
        assert read["refused"] == 2

    def test_a_different_code_on_the_same_field_is_a_different_group(self) -> None:
        """Grouped on ``(code, field)``: reaching a name that does not exist and
        sending the wrong type for one that does are different lessons."""
        read = self.fold(self.refusal(1), self.refusal(2, code="wrong_type"))
        assert {group["code"] for group in read["groups"]} == {"unknown_field", "wrong_type"}

    def test_first_and_last_seen_are_both_kept(self) -> None:
        # Seconds apart, not milliseconds: the stamp is second-granularity, so two
        # refusals inside one second legitimately carry the same one.
        read = self.fold(self.refusal(1), self.refusal(5_000))
        group = read["groups"][0]
        assert group["first_seen"] and group["last_seen"]
        assert group["first_seen"] != group["last_seen"]

    def test_a_later_success_records_the_correction_without_counting_as_a_refusal(self) -> None:
        read = self.fold(
            self.refusal(1),
            self.refusal(2),
            entry(
                REFUSED,
                {
                    "code": entry_types.MISTAKE_CORRECTED_CODE,
                    "field": "open_items",
                    "corrects": ["credits_total"],
                },
                3,
            ),
        )
        group = read["groups"][0]
        assert group["corrected_to"] == ["open_items"]
        assert group["corrected_at"]
        # The correction is the OPPOSITE of a refusal and must not inflate the count.
        assert group["count"] == 2
        assert read["refused"] == 2

    def test_the_latest_wording_of_a_refusal_wins(self) -> None:
        """An updated message should be what the agent reads back; the first wording
        would teach a correction the gateway has since improved."""
        read = self.fold(self.refusal(1, reason="old"), self.refusal(2, reason="new"))
        assert read["groups"][0]["reason"] == "new"

    def test_the_groups_are_ordered_worst_first(self) -> None:
        """The reader is an agent with one short list to read before it writes."""
        read = self.fold(
            self.refusal(1, field="a"),
            self.refusal(2, field="b"),
            self.refusal(3, field="b"),
        )
        assert [group["field"] for group in read["groups"]] == ["b", "a"]

    def test_the_group_count_is_bounded_and_the_eviction_is_reported(self) -> None:
        over = entry_types.MISTAKE_GROUP_LIMIT + 3
        read = self.fold(*(self.refusal(n + 1, field=f"f{n}") for n in range(over)))
        assert len(read["groups"]) == entry_types.MISTAKE_GROUP_LIMIT
        assert read["groups_omitted"] == 3

    def test_the_corrections_per_group_are_bounded(self) -> None:
        entries = [self.refusal(1)]
        for n in range(entry_types.MISTAKE_CORRECTION_LIMIT + 2):
            entries.append(
                entry(
                    REFUSED,
                    {
                        "code": entry_types.MISTAKE_CORRECTED_CODE,
                        "field": f"fix{n}",
                        "corrects": ["credits_total"],
                    },
                    n + 2,
                )
            )
        read = self.fold(*entries)
        fixes = read["groups"][0]["corrected_to"]
        assert len(fixes) == entry_types.MISTAKE_CORRECTION_LIMIT
        # Newest kept: the useful answer is the latest one.
        assert fixes[-1] == f"fix{entry_types.MISTAKE_CORRECTION_LIMIT + 1}"

    def test_a_line_with_no_code_is_dropped(self) -> None:
        """The append validator refuses one too, so reaching the fold means a damaged
        or planted line."""
        assert self.fold(entry(REFUSED, {"reason": "nothing"}))["refused"] == 0

    def test_a_correction_with_no_list_is_dropped_rather_than_counted(self) -> None:
        coded = {"code": entry_types.MISTAKE_CORRECTED_CODE, "field": "x"}
        assert self.fold(entry(REFUSED, coded))["refused"] == 0

    def test_both_shapes_survive_the_real_append_validator(self) -> None:
        """THE CHECK THE FOLD CANNOT MAKE, and the one the pod demo caught.

        A fold test builds its own Entry, so it never meets the per-type validator
        the writer passes through. ``code`` is REQUIRED on this type, and an earlier
        draft gave a correction no code at all: every correction was refused at the
        append with bad_data_field, so the book stayed a list of errors with no
        answers in it while every fold test passed.
        """
        from kiro_crew.crew_log import emit

        refusal = dashboard_agentic.refusal_entry(
            dashboard_agentic.WriteRefused("unknown_field", "credits_total", "no such field")
        )
        correction = dashboard_agentic.correction_entry(
            "open_items", {"groups": [{"code": "unknown_field", "field": "credits_total"}]}
        )
        assert correction is not None
        for payload in (refusal, correction):
            assert emit.dashboard_entry_fits(REFUSED, payload)
            # The validator the writer passes through, asked directly, for the
            # SESSION kind -- which is the kind that owns the dashboard domain.
            entry_types.validate_data("session", REFUSED, payload)

    def test_it_is_registered_as_a_slot_fold_so_it_outlives_a_session(self) -> None:
        """A crewmate's refused writes are facts about the CREWMATE, so a fold that
        reset when its DM session was recreated would forget the lesson exactly when
        a fresh context most needs it."""
        assert entry_types.MISTAKES_FOLD_NAME in projection.SLOT_PROJECTION_NAMES
        assert entry_types.DASHBOARD_FOLD_NAME in projection.SLOT_PROJECTION_NAMES

    def test_both_folds_are_eager_so_a_read_is_a_memo_lookup(self) -> None:
        """Part 3's cost contract is O(fields) per read and never O(log)."""
        assert entry_types.MISTAKES_FOLD_NAME in projection.EAGER_SLOT_FOLD_NAMES
        assert entry_types.DASHBOARD_FOLD_NAME in projection.EAGER_SLOT_FOLD_NAMES

    def test_both_entry_types_wake_the_eager_folder(self) -> None:
        """A slot fold whose entry type is not in the wake set looks lazy: its value
        would be folded only when a reader happened to ask."""
        from kiro_crew.crew_log import eager

        assert AGENTIC in eager._WAKE_TYPES
        assert REFUSED in eager._WAKE_TYPES


# -------------------------------------------------------------------------- #
# part 3: the data channel
# -------------------------------------------------------------------------- #


class TestTheFoldScope:
    def test_a_slot_fold_is_keyed_by_the_slot(self) -> None:
        assert dashboard_feed.scope_for("work") == "slot"
        assert dashboard_feed.scope_for(entry_types.MISTAKES_FOLD_NAME) == "slot"

    def test_a_session_fold_is_keyed_by_the_unit(self) -> None:
        assert dashboard_feed.scope_for("usage") == "session"

    def test_a_fold_in_neither_family_is_refused_rather_than_guessed(self) -> None:
        """Guessing a scope would mean a subscription that silently never receives
        an event, which reads as an empty fold."""
        assert dashboard_feed.scope_for("nope") == ""

    def test_every_manifest_fold_name_has_a_scope(self) -> None:
        """A template may declare any of them, so a name the manifest accepts and
        this cannot key would be a dashboard that never fills."""
        from kiro_crew.dashboard_templates.manifest import FOLD_NAMES

        for name in FOLD_NAMES:
            assert dashboard_feed.scope_for(name), name


class TestThePathWalk:
    def test_a_dotted_path_walks_the_folded_value(self) -> None:
        assert dashboard_feed.resolve_path({"a": {"b": {"c": 3}}}, "a.b.c") == 3

    def test_an_absent_path_is_missing_and_a_null_value_is_not(self) -> None:
        """``timeline.first_seq`` is ``None`` for a fold with no moments, and
        rendering that as "unavailable" would report an empty timeline as a broken
        binding."""
        assert dashboard_feed.resolve_path({}, "a") is dashboard_feed.MISSING
        assert dashboard_feed.resolve_path({"a": None}, "a") is None

    def test_a_path_through_a_non_mapping_is_missing(self) -> None:
        assert dashboard_feed.resolve_path({"a": [1, 2]}, "a.b") is dashboard_feed.MISSING


class TestTheFeed:
    def event(self, value: Any, revision: int, seq: int = 1) -> Any:
        return SimpleNamespace(revision=revision, seq=seq, value=value)

    def feed(self, *folds: str) -> dashboard_feed.DashboardFeed:
        made = dashboard_feed.DashboardFeed(slot="member-atlas")
        for name in folds:
            made._cells[name] = dashboard_feed._Cell()
        return made

    def test_a_field_is_read_from_the_cached_fold_value(self) -> None:
        made = self.feed("work")
        made._accept("work", self.event({"conductor": {"entries": 3}}, 5, seq=11))
        read = made.read(manifest())
        assert read.fields["entries"] == 3
        assert read.seq == 11

    def test_an_older_revision_is_ignored(self) -> None:
        """``revision`` orders two values of one (key, fold) and ``seq`` cannot: a
        slot fold's seq can move DOWN when a unit is added."""
        made = self.feed("work")
        made._accept("work", self.event({"conductor": {"entries": 3}}, 5))
        made._accept("work", self.event({"conductor": {"entries": 999}}, 4, seq=99))
        assert made.read(manifest()).fields["entries"] == 3

    def test_a_newer_revision_replaces_the_value(self) -> None:
        made = self.feed("work")
        made._accept("work", self.event({"conductor": {"entries": 3}}, 5))
        made._accept("work", self.event({"conductor": {"entries": 8}}, 6))
        assert made.read(manifest()).fields["entries"] == 8

    def test_a_malformed_event_costs_this_update_and_nothing_else(self) -> None:
        """A bus carries whatever a publisher sends, and a malformed one must not
        break the fan-out to the next subscriber."""
        made = self.feed("work")
        made._accept("work", self.event({"conductor": {"entries": 3}}, 5))
        made._accept("work", SimpleNamespace(revision=9, seq=9, value="not a mapping"))
        made._accept("work", SimpleNamespace(revision=0, seq=9, value={"conductor": {}}))
        assert made.read(manifest()).fields["entries"] == 3

    def test_an_unresolved_field_is_named_and_the_read_is_stale(self) -> None:
        made = self.feed("work")
        made._accept("work", self.event({"nothing": {}}, 5))
        read = made.read(manifest())
        # The two agentic fields are missing too: no agentic fold value was passed,
        # and an agentic cell the crewmate has not written is not a zero.
        assert read.missing == ["open_items", "risk_note", "entries"]
        assert read.stale is True

    def test_an_agentic_field_comes_from_the_agentic_fold(self) -> None:
        read = self.feed("work").read(
            manifest(), {"fields": {"open_items": {"value": 7, "type": "number"}}}
        )
        assert read.fields["open_items"] == 7

    def test_an_agentic_field_the_crewmate_never_wrote_is_missing_not_zero(self) -> None:
        """An empty cell the crewmate has not filled is not a zero."""
        read = self.feed("work").read(manifest(), {"fields": {}})
        assert "open_items" in read.missing
        assert "open_items" not in read.fields

    def test_a_fold_that_has_not_been_seen_leaves_its_fields_missing(self) -> None:
        read = self.feed("work").read(manifest())
        assert "entries" in read.missing

    def test_a_change_notifies_once_per_accepted_event(self) -> None:
        seen: list[str] = []
        made = dashboard_feed.DashboardFeed(slot="s", on_change=seen.append)
        made._cells["work"] = dashboard_feed._Cell()
        made._accept("work", self.event({"a": 1}, 1))
        made._accept("work", self.event({"a": 2}, 1))  # not newer
        made._accept("work", self.event({"a": 3}, 2))
        assert seen == ["work", "work"]

    def test_a_failing_push_does_not_cost_the_cached_value(self) -> None:
        def boom(_fold: str) -> None:
            raise RuntimeError("socket gone")

        made = dashboard_feed.DashboardFeed(slot="s", on_change=boom)
        made._cells["work"] = dashboard_feed._Cell()
        made._accept("work", self.event({"conductor": {"entries": 4}}, 1))
        assert made.read(manifest()).fields["entries"] == 4

    def test_a_malformed_event_cannot_write_into_another_folds_cell(self) -> None:
        """The fold name comes from the closure, not from the event -- the one thing
        an untrusted publisher must not be able to do to a cache."""
        made = self.feed("work", "ledger")
        made._make_callback("ledger")(self.event({"conductor": {"entries": 3}}, 5))
        # The value landed in ``ledger``, which this manifest binds nothing from, so
        # ``entries`` is still missing rather than filled from the wrong fold.
        assert made.read(manifest()).missing == ["open_items", "risk_note", "entries"]


# -------------------------------------------------------------------------- #
# part 5: the frame
# -------------------------------------------------------------------------- #


class TestThePageContract:
    def test_an_empty_page_is_refused(self) -> None:
        with pytest.raises(dashboard_frame.PageError) as caught:
            dashboard_frame.validate_page("  \n ")
        assert caught.value.code == "page_empty"

    @pytest.mark.parametrize(
        "skeleton", ["<!doctype html><div>x</div>", "<html><p>x", "<div></div><body>", "<HEAD>"]
    )
    def test_a_document_skeleton_is_refused(self, skeleton: str) -> None:
        with pytest.raises(dashboard_frame.PageError) as caught:
            dashboard_frame.validate_page(skeleton)
        assert caught.value.code == "page_has_skeleton"

    def test_the_cap_is_measured_in_bytes_not_characters(self) -> None:
        page = "<p>" + "\u00e9" * (dashboard_frame.MAX_PAGE_BYTES // 2)
        assert len(page) < dashboard_frame.MAX_PAGE_BYTES
        with pytest.raises(dashboard_frame.PageError) as caught:
            dashboard_frame.validate_page(page)
        assert caught.value.code == "page_too_large"

    def test_the_cap_is_dsh_visualizes_own_default(self) -> None:
        assert dashboard_frame.MAX_PAGE_BYTES == 1_000_000


class TestTheFramesSecurityPolicy:
    def test_egress_is_closed(self) -> None:
        """This page holds real numbers from the operator's crew log, including a
        work ledger and a mistake book. A page that can both read those and open a
        channel is an exfiltration path with a data source attached."""
        assert "connect-src 'none'" in dashboard_frame.FRAME_CSP

    def test_nothing_is_allowed_by_default(self) -> None:
        assert dashboard_frame.FRAME_CSP.startswith("default-src 'none'")

    @pytest.mark.parametrize(
        "directive",
        ["frame-src 'none'", "object-src 'none'", "base-uri 'none'", "form-action 'none'"],
    )
    def test_the_closed_directives_stay_closed(self, directive: str) -> None:
        assert directive in dashboard_frame.FRAME_CSP

    def test_no_dynamic_execution_primitive_is_granted(self) -> None:
        assert "unsafe-eval" not in dashboard_frame.FRAME_CSP

    def test_the_cdn_allowlist_matches_the_browser_wrapper(self) -> None:
        """THE ONE DIRECTIVE THE TWO POLICIES MUST AGREE ON. They intersect, so an
        origin named only here is still blocked and the page fails to load its chart
        library with no clue why. Read out of that file rather than restated."""
        import re
        from pathlib import Path

        wrapper = (
            Path(__file__).resolve().parents[1] / "website" / "src" / "lib" / "widgetSrcdoc.ts"
        )
        policy = wrapper.read_text(encoding="utf-8")
        policy = policy.split("const cspFor")[1].split("const BASE_BODY_CSS")[0]
        assert set(dashboard_frame.RESOURCE_ORIGINS) == set(
            re.findall(r"https://[a-z0-9.\-]+", policy)
        )

    def test_the_sandbox_grant_is_scripts_only(self) -> None:
        """Read out of the component, so a grant added there reddens here."""
        from pathlib import Path

        component = (
            Path(__file__).resolve().parents[1]
            / "website"
            / "src"
            / "pages"
            / "members"
            / "CrewDynamicDashboard.tsx"
        )
        source = component.read_text(encoding="utf-8")
        assert "export const CREW_DASHBOARD_SANDBOX = 'allow-scripts'" in source
        # Every `sandbox=` attribute in the file must be the constant, so a grant
        # cannot be widened by one frame spelling its own. Checked on the
        # ATTRIBUTES rather than on the prose: the constant's own comment names the
        # withheld grants in order to explain why they are withheld.
        attributes = [line.strip() for line in source.splitlines() if "sandbox=" in line]
        assert attributes, "no sandboxed frame found; did the component change shape?"
        for line in attributes:
            assert line == "sandbox={CREW_DASHBOARD_SANDBOX}", line


class TestTheComposedDocument:
    def page(self) -> str:
        return (
            '<h1 data-dashboard-field="risk_note"></h1>'
            '<span data-dashboard-field="entries"></span>'
            "<canvas id=c></canvas>"
            "<script>var s = window.kirocrew.fields.entries;</script>"
        )

    def doc(self, **over: Any) -> str:
        read = dashboard_frame.read_payload(
            over.pop("fields", {"risk_note": "two blocked", "entries": 3}), **over
        )
        return dashboard_frame.compose(self.page(), read, title="Atlas")

    def test_the_host_writes_the_whole_document(self) -> None:
        doc = self.doc()
        assert doc.startswith("<!doctype html>")
        assert doc.rstrip().endswith("</html>")
        assert '<meta name="referrer" content="no-referrer">' in doc

    def test_the_page_is_placed_verbatim(self) -> None:
        """The template owns the body. Rewriting it would be the host quietly editing
        a page it then presents as the template's."""
        assert self.page() in self.doc()

    def test_the_data_island_precedes_the_page(self) -> None:
        """A page's own inline script runs during parse, so a bootstrap placed after
        it would leave the first script reading ``undefined`` -- and a chart that
        cannot see its data on first paint draws nothing."""
        doc = self.doc()
        assert doc.index("kirocrew-dashboard-data") < doc.index("<h1 data-dashboard-field")

    def test_the_ready_beacon_follows_the_page(self) -> None:
        """Only a page whose top-level scripts survived reaches it, which is exactly
        the condition the frame needs to tell a loaded page from a broken one."""
        doc = self.doc()
        assert doc.index("<h1 data-dashboard-field") < doc.index(dashboard_frame.READY_MESSAGE_TYPE)

    def test_a_value_cannot_end_the_island_and_open_a_tag(self) -> None:
        """Inside a document whose body is already template-authored, which is
        exactly where a second injection point must not exist."""
        doc = self.doc(fields={"risk_note": "</script><img src=x onerror=alert(1)>"})
        assert "</script><img" not in doc
        assert "\\u003c/script\\u003e" in doc

    def test_the_escaped_island_is_still_the_same_json(self) -> None:
        value = {"t": "<a>&</a>", "n": 1}
        blob = json.dumps(value)
        assert json.loads(dashboard_frame.escape_json_for_html(blob)) == value

    def test_values_are_bound_as_text_and_never_as_markup(self) -> None:
        """THE CONTAINMENT LINE of this surface. A fold value can hold any text the
        log recorded -- an issue title, a review comment, a command's output."""
        doc = self.doc()
        assert "textContent" in doc
        assert "innerHTML" not in doc

    def test_the_binding_attribute_matches_parity(self) -> None:
        """The dev-time gate that checks a template's bindings and the frame that
        fills them must not disagree about what a binding is."""
        from kiro_crew.dashboard_templates import parity

        assert dashboard_frame.BINDING_ATTRIBUTE == parity._BINDING

    def test_the_global_is_frozen_so_a_page_cannot_rewrite_its_numbers(self) -> None:
        doc = self.doc()
        assert "Object.freeze" in doc
        assert "writable: false" in doc

    def test_the_global_stays_redefinable_so_a_refill_can_replace_it(self) -> None:
        """Measured on a pod, not reasoned about: a non-configurable definition made
        the refill's own redefine throw, and made a second document in one window
        render with no bindings filled at all. The deep freeze is what protects the
        values; the descriptor only decides whether a refill is possible."""
        doc = self.doc()
        assert "configurable: false" not in doc
        assert doc.count("configurable: true") == 2

    def test_a_title_cannot_carry_markup_into_the_head(self) -> None:
        read = dashboard_frame.read_payload({})
        doc = dashboard_frame.compose("<p>x</p>", read, title="</title><script>evil()")
        assert "<script>evil()" not in doc
        assert "&lt;/title&gt;&lt;script&gt;evil()" in doc

    def test_the_stale_band_is_host_markup_above_the_page(self) -> None:
        """A template cannot suppress it: it is not in the fragment, and the page has
        no way to remove an element it did not create before the reader sees it."""
        doc = self.doc(stale=True, missing=["entries"])
        assert doc.index("kirocrew-stale-band") < doc.index("kirocrew-dashboard-data")

    def test_the_agentic_cells_are_marked(self) -> None:
        """A number the crewmate asserted and a number the log recorded are different
        kinds of claim, and a reader deciding whether to act is entitled to know
        which."""
        assert "data-dashboard-agentic" in self.doc(agentic=["risk_note"])

    def test_a_read_that_will_not_serialize_composes_an_empty_stale_one(self) -> None:
        """A value this module could not encode must not blank somebody's
        dashboard."""
        doc = dashboard_frame.compose("<p>x</p>", {"fields": {"a": object()}})
        island = doc.split('id="kirocrew-dashboard-data">')[1].split("</script>")[0]
        assert json.loads(island)["stale"] is True

    def test_an_unresolved_field_keeps_its_last_value_rather_than_blanking(self) -> None:
        """Blanking would turn one unresolved path into a page of em dashes and make a
        stale dashboard look like an empty one."""
        assert "data-dashboard-missing" in self.doc(stale=True, missing=["entries"])

    def test_the_read_payload_is_built_one_way_for_the_document_and_the_refill(self) -> None:
        """Two builders could describe the same read differently, which a reader
        cannot detect: a page filled once and refilled later looks the same."""
        read = dashboard_frame.read_payload({"a": 1}, agentic=["a"], seq=4, missing=["b"])
        assert read == {
            "fields": {"a": 1},
            "agentic": ["a"],
            "seq": 4,
            "stale": False,
            "missing": ["b"],
        }

    def test_the_refill_replaces_the_global_wholesale(self) -> None:
        """A page holding a reference to the old object keeps reading consistent
        numbers from it, and nothing half-updates."""
        doc = self.doc()
        assert dashboard_frame.DATA_MESSAGE_TYPE in doc
        assert dashboard_frame.PAGE_EVENT in doc

    def test_the_island_escapes_match_the_panel_store(self) -> None:
        assert dashboard_frame._JSON_HTML_ESCAPES == agent_panel._JSON_HTML_ESCAPES


# -------------------------------------------------------------------------- #
# the seam to the registry
# -------------------------------------------------------------------------- #


class TestTheInstanceSeam:
    """What the write path accepts as a dashboard to check a field against.

    The registry owns the instance (contract v3 part 4) and this is the one
    function that reads it, so these cases pin the NARROWING: which records resolve
    to a writable dashboard, and which resolve to ``None`` and are reported as
    ``no_instance``.
    """

    def handler(self) -> Any:
        from kiro_crew.dashboard.handlers import agent_panel as handlers

        return handlers

    def record(self, **over: Any) -> Any:
        base = {
            "slug": "atlas",
            "instance_version": 4,
            "state": "live",
            "manifest": dict(MANIFEST_RAW),
        }
        base.update(over)
        return SimpleNamespace(**base)

    def test_a_live_record_resolves_to_its_parsed_manifest_and_version(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        handlers = self.handler()
        store = SimpleNamespace(read=lambda slug: self.record(), STATE_LIVE="live")
        monkeypatch.setitem(sys.modules, "kiro_crew.dashboard_templates.instance", store)
        # The package attribute too: once the real registry module is imported,
        # ``from kiro_crew.dashboard_templates import instance`` reads it, not sys.modules.
        import kiro_crew.dashboard_templates as _templates_pkg

        monkeypatch.setattr(_templates_pkg, "instance", store, raising=False)
        resolved = handlers.read_instance("atlas", "atlas")
        assert resolved is not None
        assert resolved.instance_version == 4
        assert set(resolved.manifest.fields) == set(MANIFEST_RAW["fields"])

    @pytest.mark.parametrize("state", ["empty", "stale", "error"])
    def test_a_record_that_is_not_live_resolves_to_no_instance(
        self, monkeypatch: pytest.MonkeyPatch, state: str
    ) -> None:
        """All three mean a field name cannot be checked against a manifest a
        reader can trust, so the write is refused rather than accepted into a page
        that will not render it."""
        handlers = self.handler()
        store = SimpleNamespace(read=lambda slug: self.record(state=state), STATE_LIVE="live")
        monkeypatch.setitem(sys.modules, "kiro_crew.dashboard_templates.instance", store)
        # The package attribute too: once the real registry module is imported,
        # ``from kiro_crew.dashboard_templates import instance`` reads it, not sys.modules.
        import kiro_crew.dashboard_templates as _templates_pkg

        monkeypatch.setattr(_templates_pkg, "instance", store, raising=False)
        assert handlers.read_instance("atlas", "atlas") is None

    def test_a_live_record_whose_manifest_will_not_parse_resolves_to_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A disagreement between two readers, not a crewmate's mistake: refused
        rather than checked against a manifest this process could not read."""
        handlers = self.handler()
        store = SimpleNamespace(
            read=lambda slug: self.record(manifest={"id": "", "fields": {}}),
            STATE_LIVE="live",
        )
        monkeypatch.setitem(sys.modules, "kiro_crew.dashboard_templates.instance", store)
        # The package attribute too: once the real registry module is imported,
        # ``from kiro_crew.dashboard_templates import instance`` reads it, not sys.modules.
        import kiro_crew.dashboard_templates as _templates_pkg

        monkeypatch.setattr(_templates_pkg, "instance", store, raising=False)
        assert handlers.read_instance("atlas", "atlas") is None

    def test_a_store_that_raises_resolves_to_none_rather_than_a_500(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        handlers = self.handler()

        def _boom(slug: str) -> Any:
            raise RuntimeError("record unreadable")

        store = SimpleNamespace(read=_boom, STATE_LIVE="live")
        monkeypatch.setitem(sys.modules, "kiro_crew.dashboard_templates.instance", store)
        # The package attribute too: once the real registry module is imported,
        # ``from kiro_crew.dashboard_templates import instance`` reads it, not sys.modules.
        import kiro_crew.dashboard_templates as _templates_pkg

        monkeypatch.setattr(_templates_pkg, "instance", store, raising=False)
        assert handlers.read_instance("atlas", "atlas") is None

    def test_the_narrowed_view_carries_no_html_and_no_state(self) -> None:
        """An agent tool that could read its own page's markup is one that could be
        talked into reporting it, and the state is the frame's branch."""
        fields = set(dashboard_agentic.Instance.__dataclass_fields__)
        assert fields == {"manifest", "instance_version"}
