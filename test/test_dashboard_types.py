"""The data-type catalog: every fold listed, every shape honest.

The load-bearing test is :meth:`TestEveryFoldIsCovered.test_the_catalog_names_every
_registered_fold`. The catalog exists so a composing agent can be told which types it
may bind to, and a type missing from it is a capability nobody can reach -- so a fold
added to the registry with nothing else changed has to appear here, and this is what
fails if it ever stops doing so.

The second theme is the three-state rule the module is built on. A node can be a shape
whose keys are known, an ``opaque`` object whose keys are not this catalog's to list, or
a leaf typed ``unknown`` because the probe saw ``null`` and nothing declared otherwise.
Those three mean different things to a validator, and a test per state keeps them from
collapsing into "valid" and "invalid".
"""

from __future__ import annotations

from typing import Any

import pytest

from kiro_crew import dashboard_types as dt
from kiro_crew.crew_log.entry_types import DASHBOARD_FOLD_NAME
from kiro_crew.crew_log.projection import (
    _FOLDS,
    _SLOT_FOLD_ROW_BYTES,
    _TREE_FOLD_ROW_BYTES,
    FOLD_NAMES,
    OWNER_SERVED_SLOT_PROJECTION,
    SESSION_FOLD_NAMES,
    SLOT_PROJECTION_NAMES,
    TREE_PROJECTION_NAMES,
)
from kiro_crew.crew_log.schema import Entry
from kiro_crew.dashboard_templates.manifest import FIELD_TYPES


def _nodes(shape: Any, prefix: str = "") -> list[tuple[str, dict[str, Any]]]:
    """Every node in *shape*, as ``(dotted path, node)``, the root included."""
    if not isinstance(shape, dict):
        return []
    out = [(prefix, shape)]
    for key, sub in (shape.get("properties") or {}).items():
        out.extend(_nodes(sub, f"{prefix}.{key}" if prefix else str(key)))
    return out


def _probe_render(name: str) -> dict[str, Any]:
    """What the fold *name* renders from an empty state -- the catalog's own input."""
    fold = _FOLDS[name]
    state = fold.start()
    if fold.bind_slot is not None:
        fold.bind_slot(state, "test-probe")
    return fold.render(state)


def _nulls_in(value: Any, prefix: str = "") -> list[str]:
    """Every dotted path at which *value* holds ``None``."""
    if value is None:
        return [prefix]
    if not isinstance(value, dict):
        return []
    out: list[str] = []
    for key, sub in value.items():
        out.extend(_nulls_in(sub, f"{prefix}.{key}" if prefix else str(key)))
    return out


#: One entry list per fold that carries a nullable declaration, enough to make those
#: leaves hold real values. Only what a leaf needs: the point is the TYPE a fold writes
#: there, not a faithful session.
_DRIVERS: dict[str, list[tuple[str, dict[str, Any]]]] = {
    # The second ``turn/started`` is left open on purpose: ``session/closed`` does not
    # clear the open turn, so this is the only ordering that holds ``status.turn`` and
    # ``status.closed_at`` at once -- and it is a real session, one cut off mid-turn.
    "status": [
        (
            "session/opened",
            {"session_id": "s-1", "agent": "kirocrew", "previous": {"sid": "s-0"}},
        ),
        ("turn/started", {"turn": 1, "actor": "user", "depth": 0}),
        ("turn/completed", {"turn": 1, "stop_reason": "end_turn", "error": "a failure"}),
        ("turn/started", {"turn": 2, "actor": "user", "depth": 0}),
        ("session/closed", {"reason": "done"}),
    ],
    "approvals": [
        ("approval/requested", {"approval_id": "a-1", "tool": "fs_write", "turn": 1}),
        ("approval/decided", {"approval_id": "a-1", "decision": "allow", "by": "user"}),
    ],
    "timeline": [("session/opened", {"session_id": "s-1"})],
    "work": [
        (
            "work/recorded",
            {"item_id": "it_1", "title": "a board", "parent_item": "it_0", "state": "open"},
        )
    ],
    "outline": [
        ("message/received", {"turn": 1, "role": "user", "text": "a question"}),
        ("turn/started", {"turn": 1, "actor": "user", "depth": 0}),
        ("message/sent", {"turn": 1, "step": 1, "text": "an answer"}),
        ("turn/completed", {"turn": 1, "stop_reason": "end_turn"}),
    ],
}


def _driven_render(name: str) -> dict[str, Any]:
    """What the fold *name* renders after its driver has run."""
    fold = _FOLDS[name]
    state = fold.start()
    if fold.bind_slot is not None:
        fold.bind_slot(state, "test-probe")
    for seq, (entry_type, data) in enumerate(_DRIVERS[name], start=1):
        if entry_type not in fold.affects:
            raise AssertionError(f"{name} does not consume {entry_type!r}")
        fold.step(
            state,
            Entry(
                seq=seq,
                time=1_759_000_000_000 + seq,
                type=entry_type,
                src="gateway",
                data=data,
            ),
        )
    return fold.render(state)


def _declared_leaves(key: str, node: Any) -> list[tuple[str, str]]:
    """Every ``(dotted path, declared type)`` a ``_NULLABLE`` entry promises."""
    if not isinstance(node, dict):
        return []
    out: list[tuple[str, str]] = []
    properties = node.get("properties")
    if isinstance(properties, dict):
        for sub, child in properties.items():
            out.extend(_declared_leaves(f"{key}.{sub}", child))
    elif isinstance(node.get("type"), str):
        out.append((key, node["type"]))
    return out


def _value_at(value: Any, path: str) -> Any:
    """The value at dotted *path*, or ``None`` when the walk cannot finish."""
    node = value
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _by_name(folded_through: Any = None) -> dict[str, Any]:
    """:func:`dt.catalog` keyed by type name. A test convenience, not a product one.

    The module offers no such accessor on purpose: `catalog` is a tuple and nothing in
    `src/` resolves one entry by name, so a keyed view would be a surface with no
    caller.
    """
    return {entry.name: entry for entry in dt.catalog(folded_through)}


def _at(shape: Any, path: str) -> dict[str, Any] | None:
    """The shape at dotted *path*, or ``None``; the OPAQUE node itself when it hits one.

    Here rather than in the module for the same reason as `_by_name`: the walk has no
    production caller. The cases below are about what the CATALOG declares, and this is
    only how they reach a nested declaration.
    """
    node = shape
    if not path:
        return dict(node)
    for key in path.split("."):
        if node.get("opaque") is True:
            return dict(node)
        properties = node.get("properties")
        if not isinstance(properties, dict) or key not in properties:
            return None
        node = properties[key]
    return dict(node)


class TestEveryFoldIsCovered:
    """The catalog's reason to exist: a fold it omits is a type nobody can bind."""

    def test_the_catalog_names_every_registered_fold(self) -> None:
        """THE pin for this item.

        ``FOLD_NAMES`` is the registry's own tuple and the import-time check against
        ``_FOLDS`` makes it the single source of truth. A fold added later -- D2's
        ``custom``, D3's ``outline`` -- lands in the catalog with nothing in
        ``dashboard_types`` edited, and this fails if somebody replaces the derivation
        with a list.
        """
        assert tuple(entry.name for entry in dt.catalog()) == FOLD_NAMES

    def test_a_fold_the_registry_gains_appears_without_editing_the_catalog(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The derivation, proved by ADDING one rather than by reading the code.

        A fold is registered and the catalog is asked again: if the names came from a
        hand-written table the new one would be absent, and the shape would be missing
        too. Mutating the real registry under a monkeypatch is the only way to
        demonstrate that, and the patch is undone with the fixture.
        """
        added = "probe_only_fold"
        fold = dt._FOLDS[DASHBOARD_FOLD_NAME]
        monkeypatch.setitem(dt._FOLDS, added, fold)
        monkeypatch.setattr(dt, "FOLD_NAMES", FOLD_NAMES + (added,))

        names = [entry.name for entry in dt.catalog()]
        assert added in names
        new = _by_name()[added]
        assert new.shape["type"] == "object"
        assert new.source_fold == added

    def test_every_entry_carries_the_four_columns(self) -> None:
        for entry in dt.catalog():
            assert entry.name
            assert isinstance(entry.shape, dict) and entry.shape.get("type")
            assert entry.source_fold == entry.name
            assert "folded_through" in entry.to_dict()


class TestKeying:
    def test_each_fold_is_keyed_the_way_the_registry_keys_it(self) -> None:
        by_name = _by_name()
        for name in TREE_PROJECTION_NAMES:
            assert by_name[name].keyed_by == dt.KEYED_BY_TREE
        for name in SLOT_PROJECTION_NAMES:
            assert by_name[name].keyed_by == dt.KEYED_BY_SLOT
        for name in SESSION_FOLD_NAMES:
            assert by_name[name].keyed_by == dt.KEYED_BY_SESSION

    def test_the_three_families_partition_the_catalog(self) -> None:
        """No fold is in two and none is in none, or a reader could not tell what to
        read it against: a session fold reads one log, a slot fold joins every unit a
        slot ran under, a tree fold joins the logs of every slot the tree reaches.

        THREE, not two. The pair was the whole catalog until the tree-keyed fold, and a
        third kind that merely degraded to one of the other two would tell a composing
        agent to key a whole fleet's tree by one conversation -- a complete-looking
        wrong answer, which is why the partition is asserted both ways round.
        """
        kinds = {entry.keyed_by for entry in dt.catalog()}
        assert kinds == {dt.KEYED_BY_SESSION, dt.KEYED_BY_SLOT, dt.KEYED_BY_TREE}
        families = (
            set(SESSION_FOLD_NAMES),
            set(SLOT_PROJECTION_NAMES),
            set(TREE_PROJECTION_NAMES),
        )
        for index, family in enumerate(families):
            for other in families[index + 1 :]:
                assert not family & other, (family, other)
        assert set().union(*families) == {entry.name for entry in dt.catalog()}

    def test_the_three_keyed_by_words_are_distinct(self) -> None:
        """A composing agent branches on this string, so two kinds sharing a spelling
        would route a tree block down the slot path with nothing raised."""
        words = {dt.KEYED_BY_SESSION, dt.KEYED_BY_SLOT, dt.KEYED_BY_TREE}
        assert len(words) == 3

    def test_only_the_owner_served_fold_is_marked_owner_served(self) -> None:
        marked = {entry.name for entry in dt.catalog() if entry.owner_served}
        assert marked == {OWNER_SERVED_SLOT_PROJECTION}

    def test_row_bytes_comes_from_the_folds_own_key_kind_table(self) -> None:
        """Each measured table answers for its own kind, and a fold in neither is None.

        Two tables rather than one because each states its figure against its own
        largest member, and the fold a figure is read for decides which. A tree fold
        falling through to the slot table would be charged another kind's measurement.
        """
        for entry in dt.catalog():
            if entry.name in _SLOT_FOLD_ROW_BYTES:
                assert entry.row_bytes == _SLOT_FOLD_ROW_BYTES[entry.name]
            elif entry.name in _TREE_FOLD_ROW_BYTES:
                assert entry.row_bytes == _TREE_FOLD_ROW_BYTES[entry.name]
            else:
                assert entry.row_bytes is None

    def test_every_tree_fold_carries_a_measured_row_cost(self) -> None:
        """The third key kind's whole admission condition, pinned here as well as at
        import: a tree fold with no measured figure would publish ``row_bytes: null``
        to a composing agent sizing a block, which reads as "this fold retains nothing".
        """
        by_name = _by_name()
        assert TREE_PROJECTION_NAMES, "the tree key kind has no members to check"
        for name in TREE_PROJECTION_NAMES:
            assert by_name[name].row_bytes == _TREE_FOLD_ROW_BYTES[name]
            assert by_name[name].row_bytes > 0

    def test_state_version_comes_off_the_fold(self) -> None:
        for entry in dt.catalog():
            assert entry.state_version == _FOLDS[entry.name].state_version


class TestFoldedThrough:
    def test_an_omitted_fold_is_none_and_not_zero(self) -> None:
        """The distinction the whole live half rests on: ``None`` is 'not read',
        ``0`` is 'this fold has consumed nothing'. A reader that merged them would
        bind a block to a fold it cannot see and draw it as empty."""
        entries = _by_name({"status": 0})
        assert entries["status"].folded_through == 0
        assert entries["usage"].folded_through is None

    def test_a_supplied_seq_is_carried_through(self) -> None:
        entries = _by_name({"work": 4219, DASHBOARD_FOLD_NAME: 7})
        assert entries["work"].folded_through == 4219
        assert entries[DASHBOARD_FOLD_NAME].folded_through == 7
        assert entries["work"].to_dict()["folded_through"] == 4219

    def test_no_seq_at_all_still_answers_the_whole_catalog(self) -> None:
        """A caller with no key still gets every shape: shapes are not per-caller."""
        assert len(dt.catalog()) == len(FOLD_NAMES)
        assert all(entry.folded_through is None for entry in dt.catalog())


class TestShapeVocabulary:
    def test_every_type_is_one_a_model_field_can_declare_or_the_unknown_marker(
        self,
    ) -> None:
        """So a validator can compare a Model field's ``type`` to a catalog leaf's
        without translating between two vocabularies."""
        allowed = set(FIELD_TYPES) | {dt.UNKNOWN_TYPE}
        for entry in dt.catalog():
            for path, node in _nodes(entry.shape, entry.name):
                assert node.get("type") in allowed, f"{path} is typed {node.get('type')!r}"

    def test_the_unknown_marker_is_not_a_declarable_field_type(self) -> None:
        """Deliberate: a Model field can never declare it, so a type comparison
        cannot accidentally MATCH it and report a checkable path."""
        assert dt.UNKNOWN_TYPE not in FIELD_TYPES

    def test_an_array_is_a_leaf(self) -> None:
        """A manifest ``path`` is dotted keys with no index, so an element shape would
        describe something no Model field can name."""
        for entry in dt.catalog():
            for path, node in _nodes(entry.shape, entry.name):
                if node.get("type") == "array":
                    assert "properties" not in node and "items" not in node, path

    def test_a_boolean_is_never_reported_as_a_number(self) -> None:
        """``True`` is an ``int`` in Python. A flag typed ``number`` would let a page
        declare a number and draw a 1 where the reader wanted a yes."""
        assert dt._json_type(True) == "boolean"
        assert dt._json_type(1) == "number"
        by_name = _by_name()
        assert by_name["status"].shape["properties"]["resumed"]["type"] == "boolean"
        assert by_name["status"].shape["properties"]["entries"]["type"] == "number"


class TestTheThreeStates:
    """``properties``, ``opaque`` and ``unknown`` mean three different things."""

    def test_no_object_passes_as_having_no_keys(self) -> None:
        """An object with an EMPTY ``properties`` says 'no key below here is valid',
        which for a map keyed by a runtime id is simply false. Every one must be
        declared opaque instead, so a map added to a fold cannot land as a shape that
        refuses every real reading of it."""
        for entry in dt.catalog():
            for path, node in _nodes(entry.shape, entry.name):
                if node.get("type") != "object" or node.get("opaque") is True:
                    continue
                properties = node.get("properties")
                assert properties, f"{path} is an object with no keys and is not opaque"

    def test_an_opaque_node_says_what_its_keys_are(self) -> None:
        found = 0
        for entry in dt.catalog():
            for path, node in _nodes(entry.shape, entry.name):
                if node.get("opaque") is True:
                    found += 1
                    assert node["type"] == "object", path
                    assert node.get("keyed_by"), f"{path} is opaque and names no key"
                    assert "properties" not in node, path
        assert found == len(dt._OPAQUE), "an opaque declaration did not reach the shape"

    def test_every_opaque_declaration_is_a_real_path(self) -> None:
        """The other direction: a stale entry naming a path no fold renders would sit
        here forever claiming to describe something."""
        by_name = _by_name()
        for key in dt._OPAQUE:
            fold, _, path = key.partition(".")
            assert fold in by_name, key
            assert _at(by_name[fold].shape, path) is not None, key

    def test_no_leaf_is_typed_unknown_today(self) -> None:
        """The third state is REACHABLE and currently EMPTY, which is the claim worth
        pinning: every nullable leaf the registered folds render is declared, so an
        undeclared one is a fold added later and this fails rather than shipping a
        guess."""
        unknown = [
            path
            for entry in dt.catalog()
            for path, node in _nodes(entry.shape, entry.name)
            if node.get("type") == dt.UNKNOWN_TYPE
        ]
        assert unknown == []

    def test_an_undeclared_null_leaf_becomes_unknown_rather_than_a_guess(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The third state, exercised by removing a declaration.

        Without this the previous test's empty list is ambiguous: it could mean every
        leaf is declared, or that the ``unknown`` branch is dead code and a null leaf
        silently becomes a string.
        """
        monkeypatch.delitem(dt._NULLABLE, "status.closed_at")
        node = _at(_by_name()["status"].shape, "closed_at")
        assert node is not None
        assert node["type"] == dt.UNKNOWN_TYPE
        assert node["nullable"] is True
        assert node["why"]


class TestDeclaredNullables:
    def test_every_declared_nullable_leaf_is_a_path_the_fold_renders_null(self) -> None:
        """Both directions at once. A declaration for a path the fold does not render
        null is stale; the companion assertion below catches the reverse."""
        for key in dt._NULLABLE:
            fold, _, path = key.partition(".")
            assert fold in _FOLDS, key
            assert path in _nulls_in(_probe_render(fold)), key

    def test_every_declared_nullable_type_matches_what_the_fold_writes(self) -> None:
        """The half the path check cannot see: WHICH type the declaration promises.

        A ``_NULLABLE`` entry exists because the probe sees ``None`` there, so the probe
        can never check the type it declares -- and the sibling case above only asserts
        the PATH is one the fold renders null. A declaration can therefore name any type
        at all and nothing notices, which is how a leaf fed from ``entry.time`` (an int)
        came to be declared ``string``.

        So each fold is DRIVEN until its nullable leaves carry real values, and every
        leaf that now holds one is checked against its declaration with the same
        ``type_holds`` the write path uses. Leaves the driver below does not reach are
        not checked, so the coverage assertion at the end names the ones that must be:
        a leaf that stops being populated fails here rather than quietly dropping out.
        """
        from kiro_crew.dashboard_agentic import type_holds

        driven = {name: _driven_render(name) for name in sorted(_DRIVERS)}
        assert set(_DRIVERS) == {key.partition(".")[0] for key in dt._NULLABLE}, (
            "a fold gained a nullable declaration and no driver, so its types would go " "unchecked"
        )

        checked: list[str] = []
        wrong: list[str] = []
        for key, node in sorted(dt._NULLABLE.items()):
            fold = key.partition(".")[0]
            for leaf_path, declared in _declared_leaves(key, node):
                value = _value_at(driven[fold], leaf_path.partition(".")[2])
                if value is None:
                    continue
                checked.append(leaf_path)
                if not type_holds(declared, value):
                    wrong.append(
                        f"{leaf_path} is declared {declared!r} and the fold writes "
                        f"{type(value).__name__} ({value!r})"
                    )
        assert wrong == [], "\n".join(wrong)

        # The five the driver must reach, so this case cannot pass by checking nothing.
        for required in (
            "status.opened_at",
            "status.closed_at",
            "status.last_time",
            "status.turn.started_at",
            "approvals.last.time",
        ):
            assert required in checked, f"{required} was not populated, so its type went unchecked"

    def test_every_null_leaf_the_folds_render_is_declared(self) -> None:
        undeclared = [
            f"{name}.{path}" for name in FOLD_NAMES for path in _nulls_in(_probe_render(name))
        ]
        assert [key for key in undeclared if key not in dt._NULLABLE] == []

    def test_a_declared_nullable_is_marked_nullable_in_the_shape(self) -> None:
        node = _at(_by_name()["status"].shape, "closed_at")
        assert node == {"type": "number", "nullable": True}

    def test_a_declared_nullable_object_brings_its_own_keys(self) -> None:
        """``status.turn`` renders null on an empty fold, so the probe alone could
        never reach ``status.turn.actor`` -- which is a path a dashboard wants."""
        node = _at(_by_name()["status"].shape, "turn.actor")
        assert node == {"type": "string"}


class TestDescribe:
    def test_the_payload_carries_the_types_and_the_vocabulary(self) -> None:
        payload = dt.describe({DASHBOARD_FOLD_NAME: 12})
        names = [row["name"] for row in payload["types"]]
        assert names == list(FOLD_NAMES)
        assert payload["unknown_type"] == dt.UNKNOWN_TYPE
        assert set(payload["field_types"]) == set(FIELD_TYPES)
        assert payload["owner_served"] == OWNER_SERVED_SLOT_PROJECTION
        assert payload["session_keyed"] == list(SESSION_FOLD_NAMES)
        assert payload["slot_keyed"] == list(SLOT_PROJECTION_NAMES)
        dashboard = next(row for row in payload["types"] if row["name"] == DASHBOARD_FOLD_NAME)
        assert dashboard["folded_through"] == 12

    def test_the_payload_is_json_serializable(self) -> None:
        import json

        assert json.loads(json.dumps(dt.describe())) == dt.describe()


class TestItIsAReadTool:
    def test_building_the_catalog_does_not_mutate_the_registry(self) -> None:
        before = {name: _FOLDS[name].start() for name in FOLD_NAMES}
        dt.catalog()
        dt.describe()
        assert {name: _FOLDS[name].start() for name in FOLD_NAMES} == before

    def test_two_reads_answer_the_same_thing(self) -> None:
        assert [entry.to_dict() for entry in dt.catalog()] == [
            entry.to_dict() for entry in dt.catalog()
        ]
