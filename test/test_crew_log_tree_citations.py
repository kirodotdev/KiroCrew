"""``fold_citations``: the tree machinery with the session slot taken out of it.

The placement rules live in ``fold_citations``, which takes ``(node, cited_parent)``
pairs and knows nothing about session slots, so the orphan and cycle handling -- the
part that is hard to get right -- serves a tree of anything. These tests drive those
rules through node kinds that have nothing to do with sessions, and then pin that the
session caller ``fold_tree`` goes through the same code rather than keeping a second
copy of it.

What is NOT claimed here: that a v3 dashboard block can BIND to a tree. It cannot.
``fold_citations`` is a pure function and not a registered fold, and a block binds to a
fold name from the ``_FOLDS`` registry. Registering a tree fold needs a third key kind,
because a tree joins many logs and is keyed by neither one session nor one slot, and
that key kind needs its own measured row budget.
"""

from __future__ import annotations

import pytest

from kiro_crew.crew_log.session_tree import (
    EdgeRecord,
    OpenedRecord,
    TreePlacement,
    fold_citations,
    fold_tree,
)


def placed(citations: list[tuple[str, str | None]], **kwargs: object) -> dict[str, tuple]:
    """Each node as ``(parent, cycle)``, which is what every assertion here reads."""
    overrides = kwargs.get("overrides")
    result = fold_citations(citations, overrides)  # type: ignore[arg-type]
    return {node: (place.parent, place.cycle) for node, place in result.items()}


class TestItServesATreeThatIsNotSessionsAndNotWork:
    def test_a_tree_of_work_boards_hung_on_parent_item(self) -> None:
        """The node kind the item's own half B was about.

        A work BOARD hangs under one item of a parent board
        (``WorkBoardConductor.parent_item``), which is the only parent link the work
        vocabulary has -- a board's items are flat. Nothing about that reaches this
        function except the ids.
        """
        assert placed(
            [
                ("board:root", None),
                ("board:api", "board:root"),
                ("board:ui", "board:root"),
                ("board:ui-icons", "board:ui"),
            ]
        ) == {
            "board:root": (None, False),
            "board:api": ("board:root", False),
            "board:ui": ("board:root", False),
            "board:ui-icons": ("board:ui", False),
        }

    def test_a_tree_of_artifact_folders(self) -> None:
        """A second node kind, to show the first was not a special case."""
        assert placed([("/", None), ("/designs", "/"), ("/designs/v3", "/designs")]) == {
            "/": (None, False),
            "/designs": ("/", False),
            "/designs/v3": ("/designs", False),
        }

    def test_a_tree_of_issues_linked_by_blocked_by(self) -> None:
        """A third, whose ids are integers-as-strings and whose edge means something else.

        The function does not know what a parent MEANS, which is the generalization: a
        blocked-by link and a creator link fold the same way.
        """
        assert placed([("18642", None), ("21659", "18642"), ("21660", "21659")]) == {
            "18642": (None, False),
            "21659": ("18642", False),
            "21660": ("21659", False),
        }

    def test_the_result_is_the_node_kind_free_placement_type(self) -> None:
        """``node``/``parent``, not ``slot``/``parent_slot``: the caller's nodes are its own."""
        one = fold_citations([("n", None)])["n"]
        assert one == TreePlacement(node="n", parent=None, cycle=False)
        assert one.node == "n"


class TestOrphansDegradeToRootAndKeepTheirCitation:
    def test_a_cited_parent_that_is_not_a_node_leaves_a_root_carrying_its_parent(self) -> None:
        """Degrading to root rather than to an error, and the citation is the CHILD's record.

        Dropping the parent instead would destroy the one fact the child recorded, and a
        consumer could not tell "nobody created me" from "my creator is not here".
        """
        assert placed([("child", "a-parent-with-no-node-of-its-own")]) == {
            "child": ("a-parent-with-no-node-of-its-own", False)
        }

    def test_a_pair_naming_no_node_places_nothing(self) -> None:
        assert placed([("", "p"), ("", None)]) == {}

    def test_the_nodes_are_exactly_the_cited_subjects(self) -> None:
        """A parent is not made a node by being cited: that is what makes an edge orphan."""
        assert set(placed([("child", "parent")])) == {"child"}


class TestTheFirstCitationCarryingAParentWins:
    def test_a_later_citation_with_no_parent_does_not_retract_one(self) -> None:
        assert placed([("n", "first-parent"), ("n", None)]) == {"n": ("first-parent", False)}

    def test_a_later_citation_with_a_different_parent_does_not_replace_one(self) -> None:
        """Only an OVERRIDE replaces; a second citation is another record of the same fact."""
        assert placed([("n", "first-parent"), ("n", "second-parent"), ("p1", None)]) == {
            "n": ("first-parent", False),
            "p1": (None, False),
        }

    def test_a_citation_with_no_parent_first_does_not_block_a_later_one(self) -> None:
        """ "First carrying a parent", not "first at all"."""
        assert placed([("n", None), ("n", "the-parent")]) == {"n": ("the-parent", False)}

    def test_the_caller_owns_the_order_so_reversing_it_reverses_the_answer(self) -> None:
        """The one obligation the generalized signature moves outward, pinned.

        This is not a defect: a caller whose nodes have a succession history states it
        (``fold_tree`` sorts by ``log_rank_of`` first), and a caller with none gets an
        answer that depends on an order it chose rather than one invented for it.
        """
        forward: list[tuple[str, str | None]] = [("n", "early"), ("n", "late")]
        assert placed(forward)["n"] == ("early", False)
        assert placed(list(reversed(forward)))["n"] == ("late", False)


class TestOverridesReplaceTheCitationOutright:
    def test_an_override_replaces_rather_than_merges(self) -> None:
        assert placed(
            [("n", "cited"), ("cited", None), ("taken-over-by", None)],
            overrides={"n": "taken-over-by"},
        ) == {
            "n": ("taken-over-by", False),
            "cited": (None, False),
            "taken-over-by": (None, False),
        }

    def test_an_override_to_none_is_the_one_way_a_parent_is_taken_away(self) -> None:
        """Where ``None`` in a CITATION only means that citation did not repeat one."""
        assert placed([("n", "cited"), ("cited", None)], overrides={"n": None}) == {
            "n": (None, False),
            "cited": (None, False),
        }

    def test_an_override_onto_a_node_that_does_not_exist_is_dropped(self) -> None:
        """It would name a node this tree does not have.

        This asserts the OBSERVABLE contract, and deliberately does not claim to pin
        the ``node in exists`` guard that states it: two sibling rules enforce the same
        thing (see :class:`TestTheGuardsThatSiblingRulesAlreadyEnforce`), so removing
        that guard changes no answer. The guard stays because it says what the code
        means and because either sibling could move.
        """
        assert placed([("n", None)], overrides={"absent": "n"}) == {"n": (None, False)}

    def test_no_overrides_is_the_same_as_an_empty_mapping(self) -> None:
        assert placed([("n", "p"), ("p", None)]) == placed([("n", "p"), ("p", None)], overrides={})


class TestCycleColouring:
    def test_two_nodes_citing_each_other_are_both_on_the_cycle(self) -> None:
        assert placed([("p", "q"), ("q", "p")]) == {"p": ("q", True), "q": ("p", True)}

    def test_a_self_citation_is_a_cycle_of_one(self) -> None:
        assert placed([("n", "n")]) == {"n": ("n", True)}

    def test_a_node_merely_hanging_off_a_cycle_member_is_not_on_it(self) -> None:
        """Its chain ENDS at a member, which the fold treats as that chain's root.

        It keeps its edge to the member, so a consumer can still draw it; only the
        members themselves must not be nested.
        """
        assert placed([("p", "q"), ("q", "p"), ("leaf", "p")]) == {
            "p": ("q", True),
            "q": ("p", True),
            "leaf": ("p", False),
        }

    def test_a_longer_cycle_is_coloured_whole(self) -> None:
        assert placed([("a", "b"), ("b", "c"), ("c", "a")]) == {
            "a": ("b", True),
            "b": ("c", True),
            "c": ("a", True),
        }

    def test_an_unfollowable_citation_cannot_make_a_cycle(self) -> None:
        """The colouring runs over the FOLLOWED edges only.

        ``p`` cites a node that does not exist, so there is no edge for a walk to close
        a loop through, however the ids are spelled.
        """
        assert placed([("p", "not-a-node")]) == {"p": ("not-a-node", False)}

    def test_an_override_can_create_a_cycle_and_it_is_still_coloured(self) -> None:
        """The guard runs over the APPLIED edges, which is why it is here and not only
        where an override is written.

        A writer checks the tree as it stands at that moment; this sees a whole
        population whose decisions can arrive in an order no writer ever saw.
        """
        assert placed([("p", None), ("q", "p")], overrides={"p": "q"}) == {
            "p": ("q", True),
            "q": ("p", True),
        }


class TestTheGuardsThatSiblingRulesAlreadyEnforce:
    """Two of the function's guards cannot change any answer, and that is written down
    here rather than discovered by the next person who mutates them.

    Both were mutated away and every test above still passed. The honest reading is not
    that the tests are weak but that each guard is enforced a second time by a sibling
    rule -- so what is pinned here is the PROPERTY, which is what a reader actually
    depends on, not the line that happens to state it.
    """

    def test_the_result_names_exactly_the_cited_subjects(self) -> None:
        """Why a phantom override cannot surface however it is written.

        The result is built over the subject set, so an override keyed on anything else
        has nowhere to appear. That is the sibling rule standing behind the
        ``node in exists`` check on the override loop.
        """
        result = fold_citations(
            [("a", "ghost"), ("b", "a"), ("", "a")],
            {"ghost": "b", "another-phantom": None, "b": "ghost"},
        )
        assert set(result) == {"a", "b"}

    def test_a_node_citing_a_non_subject_is_never_on_a_cycle(self) -> None:
        """Why following an orphan edge could not invent a cycle either.

        A cycle needs a closed loop, and an orphan's parent is not a subject -- so it is
        never a key in the followed graph and the edge is a dead end. Equally, no real
        node can point AT a phantom, because that citation is itself unfollowable. Both
        directions are checked, since a loop needs them both.
        """
        result = fold_citations([("a", "ghost"), ("b", "a"), ("c", "ghost")])
        assert result["a"] == TreePlacement(node="a", parent="ghost", cycle=False)
        assert result["c"] == TreePlacement(node="c", parent="ghost", cycle=False)
        # And the node hanging off the orphan is not dragged onto a cycle either.
        assert result["b"] == TreePlacement(node="b", parent="a", cycle=False)

    def test_an_override_through_a_phantom_still_cannot_close_a_loop(self) -> None:
        """The combination that would have to exist for the guards to matter.

        An override hands the phantom an edge INTO the real tree; for a cycle, a real
        node would have to cite the phantom back, and that citation is unfollowable. So
        the loop cannot be closed from either side.
        """
        assert placed([("a", None), ("b", "a")], overrides={"ghost": "b", "a": "ghost"}) == {
            "a": ("ghost", False),
            "b": ("a", False),
        }


class TestTheSessionCallerGoesThroughTheGeneralizedForm:
    """The half that keeps this a generalization rather than a second implementation.

    If ``fold_tree`` kept its own copy of the rules, every test above could pass while
    the session tree drifted away from them. These drive the same populations through
    both entry points and require the same answer.
    """

    @staticmethod
    def _records(rows: list[tuple[str, str, str | None]]) -> list[OpenedRecord]:
        return [
            OpenedRecord(sid=sid, slot=slot, created_at=index + 1, parent_slot=parent)
            for index, (sid, slot, parent) in enumerate(rows)
        ]

    @pytest.mark.parametrize(
        "rows",
        [
            pytest.param([("s1", "a", None), ("s2", "b", "a")], id="ordinary-parent"),
            pytest.param([("s1", "c", "ghost")], id="orphan-keeps-its-citation"),
            pytest.param([("s1", "p", "q"), ("s2", "q", "p")], id="two-node-cycle"),
            pytest.param([("s1", "n", "n")], id="self-cycle"),
            pytest.param(
                [("s1", "p", "q"), ("s2", "q", "p"), ("s3", "leaf", "p")],
                id="hanging-off-a-cycle",
            ),
            pytest.param([("s1", "", "a"), ("s2", "a", None)], id="record-with-no-slot"),
        ],
    )
    def test_both_entry_points_agree_on_the_same_population(
        self, rows: list[tuple[str, str, str | None]]
    ) -> None:
        records = self._records(rows)
        through_session = {
            slot: (node.parent_slot, node.cycle) for slot, node in fold_tree(records).items()
        }
        through_core = placed([(record.slot, record.parent_slot) for record in records])
        assert through_session == through_core

    def test_a_release_edge_reaches_the_core_as_an_override_to_none(self) -> None:
        """The session caller's edges become overrides, and a release becomes ``None``."""
        records = self._records([("s1", "a", None), ("s2", "b", "a")])
        released = fold_tree(
            records, [EdgeRecord(slot="b", parent_slot=None, at=9, sid="s2", seq=5)]
        )
        assert released["b"].parent_slot is None
        assert placed([(r.slot, r.parent_slot) for r in records], overrides={"b": None}) == {
            slot: (node.parent_slot, node.cycle) for slot, node in released.items()
        }

    def test_the_session_node_type_keeps_its_own_field_names(self) -> None:
        """Its consumers read ``slot``/``parent_slot``, so the adapter is what renames.

        Pinned because a generalization that renamed them would have been a wide change
        to every consumer of the Sessions table, which is not what was asked for.
        """
        node = fold_tree(self._records([("s1", "a", None), ("s2", "b", "a")]))["b"]
        assert node.slot == "b"
        assert node.parent_slot == "a"
        assert not hasattr(node, "node")
