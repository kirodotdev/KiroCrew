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
from kiro_crew.crew_log.projection import (
    _FOLDS,
    _SLOT_FOLD_ROW_BYTES,
    FOLD_NAMES,
    OWNER_SERVED_SLOT_PROJECTION,
    SESSION_FOLD_NAMES,
    SLOT_PROJECTION_NAMES,
)
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
        fold = dt._FOLDS["agentic"]
        monkeypatch.setitem(dt._FOLDS, added, fold)
        monkeypatch.setattr(dt, "FOLD_NAMES", FOLD_NAMES + (added,))

        names = [entry.name for entry in dt.catalog()]
        assert added in names
        new = dt.catalog_by_name()[added]
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
        by_name = dt.catalog_by_name()
        for name in SLOT_PROJECTION_NAMES:
            assert by_name[name].keyed_by == dt.KEYED_BY_SLOT
        for name in SESSION_FOLD_NAMES:
            assert by_name[name].keyed_by == dt.KEYED_BY_SESSION

    def test_the_two_families_partition_the_catalog(self) -> None:
        """No fold is both and none is neither, or a reader could not tell what to
        read it against: a slot fold joins every unit a slot ran under, a session fold
        reads one log."""
        kinds = {entry.keyed_by for entry in dt.catalog()}
        assert kinds == {dt.KEYED_BY_SESSION, dt.KEYED_BY_SLOT}
        assert not set(SESSION_FOLD_NAMES) & set(SLOT_PROJECTION_NAMES)

    def test_only_the_owner_served_fold_is_marked_owner_served(self) -> None:
        marked = {entry.name for entry in dt.catalog() if entry.owner_served}
        assert marked == {OWNER_SERVED_SLOT_PROJECTION}

    def test_row_bytes_is_carried_for_the_slot_folds_and_absent_for_the_rest(self) -> None:
        for entry in dt.catalog():
            if entry.name in _SLOT_FOLD_ROW_BYTES:
                assert entry.row_bytes == _SLOT_FOLD_ROW_BYTES[entry.name]
            else:
                assert entry.row_bytes is None

    def test_state_version_comes_off_the_fold(self) -> None:
        for entry in dt.catalog():
            assert entry.state_version == _FOLDS[entry.name].state_version


class TestFoldedThrough:
    def test_an_omitted_fold_is_none_and_not_zero(self) -> None:
        """The distinction the whole live half rests on: ``None`` is 'not read',
        ``0`` is 'this fold has consumed nothing'. A reader that merged them would
        bind a block to a fold it cannot see and draw it as empty."""
        entries = dt.catalog_by_name({"status": 0})
        assert entries["status"].folded_through == 0
        assert entries["usage"].folded_through is None

    def test_a_supplied_seq_is_carried_through(self) -> None:
        entries = dt.catalog_by_name({"work": 4219, "agentic": 7})
        assert entries["work"].folded_through == 4219
        assert entries["agentic"].folded_through == 7
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
        by_name = dt.catalog_by_name()
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
        """The other direction: a stale entry naming a path a fold no longer renders
        would sit here forever claiming to describe something."""
        by_name = dt.catalog_by_name()
        for key in dt._OPAQUE:
            fold, _, path = key.partition(".")
            assert fold in by_name, key
            assert dt.shape_at(by_name[fold].shape, path) is not None, key

    def test_no_leaf_is_typed_unknown_today(self) -> None:
        """The third state is REACHABLE and currently EMPTY, which is the claim worth
        pinning: every nullable leaf the 14 folds render is declared, so an undeclared
        one is a fold added later and this fails rather than shipping a guess."""
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
        node = dt.shape_at(dt.catalog_by_name()["status"].shape, "closed_at")
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

    def test_every_null_leaf_the_folds_render_is_declared(self) -> None:
        undeclared = [
            f"{name}.{path}" for name in FOLD_NAMES for path in _nulls_in(_probe_render(name))
        ]
        assert [key for key in undeclared if key not in dt._NULLABLE] == []

    def test_a_declared_nullable_is_marked_nullable_in_the_shape(self) -> None:
        node = dt.shape_at(dt.catalog_by_name()["status"].shape, "closed_at")
        assert node == {"type": "string", "nullable": True}

    def test_a_declared_nullable_object_brings_its_own_keys(self) -> None:
        """``status.turn`` renders null on an empty fold, so the probe alone could
        never reach ``status.turn.actor`` -- which is a path a dashboard wants."""
        node = dt.shape_at(dt.catalog_by_name()["status"].shape, "turn.actor")
        assert node == {"type": "string"}


class TestShapeAt:
    def test_an_empty_path_is_the_whole_shape(self) -> None:
        shape = dt.catalog_by_name()["agentic"].shape
        assert dt.shape_at(shape, "") == dict(shape)

    def test_a_real_path_answers_its_leaf(self) -> None:
        shape = dt.catalog_by_name()["usage"].shape
        assert dt.shape_at(shape, "tokens.total") == {"type": "number"}
        assert dt.shape_at(shape, "context.window") == {"type": "number"}

    def test_a_path_the_fold_does_not_render_is_none(self) -> None:
        shape = dt.catalog_by_name()["usage"].shape
        assert dt.shape_at(shape, "tokens.nope") is None
        assert dt.shape_at(shape, "not_a_field") is None

    def test_a_walk_into_an_opaque_node_answers_the_opaque_node(self) -> None:
        """NOT ``None``. ``usage.by_model.claude-opus-4`` is a real reading, so
        reporting it absent would refuse a working dashboard; reporting its type would
        promise something this catalog cannot know. The caller is handed the opaque
        node and has to decide."""
        shape = dt.catalog_by_name()["usage"].shape
        node = dt.shape_at(shape, "by_model.some-model.credits")
        assert node is not None and node.get("opaque") is True

    def test_a_walk_stops_at_an_array(self) -> None:
        shape = dt.catalog_by_name()["timeline"].shape
        assert dt.shape_at(shape, "moments") == {"type": "array"}
        assert dt.shape_at(shape, "moments.kind") is None

    def test_the_returned_node_is_a_copy(self) -> None:
        """A caller mutating what it was handed must not edit the catalog, which is
        rebuilt per call but handed out node by node."""
        shape = dt.catalog_by_name()["usage"].shape
        node = dt.shape_at(shape, "tokens.total")
        assert node is not None
        node["type"] = "string"
        assert dt.shape_at(shape, "tokens.total") == {"type": "number"}


class TestDescribe:
    def test_the_payload_carries_the_types_and_the_vocabulary(self) -> None:
        payload = dt.describe({"agentic": 12})
        names = [row["name"] for row in payload["types"]]
        assert names == list(FOLD_NAMES)
        assert payload["unknown_type"] == dt.UNKNOWN_TYPE
        assert set(payload["field_types"]) == set(FIELD_TYPES)
        assert payload["owner_served"] == OWNER_SERVED_SLOT_PROJECTION
        assert payload["session_keyed"] == list(SESSION_FOLD_NAMES)
        assert payload["slot_keyed"] == list(SLOT_PROJECTION_NAMES)
        agentic = next(row for row in payload["types"] if row["name"] == "agentic")
        assert agentic["folded_through"] == 12

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
