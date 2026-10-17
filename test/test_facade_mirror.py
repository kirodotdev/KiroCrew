"""``kiro_crew._facade_mirror``: a write through a mirrored facade reaches every reader.

Each case builds the synthetic family in ``facade_mirror_helpers`` and pins one part of the
mechanism: the index, the read path, and every undo protocol a test harness uses on the
facade -- ``monkeypatch`` set and undo, ``mock.patch`` enter and exit (nested, mixed with
``monkeypatch``, by object and by string), a name a write creates (it lands on every
owner, as a module global shadowing a builtin did) and its undo, a purged owner, the
refusals of ``install``, and a facade that adopts the mirror over a shape of its own
(``forwards``, ``surface``, its own module-level ``__getattr__``). The guards that hold
every real facade to the same contract are in ``test_facade_mirror_guards.py``.
"""

from __future__ import annotations

import ast
import builtins
import dataclasses
import re
import sys
from pathlib import Path
from types import ModuleType
from unittest import mock

import pytest
from facade_mirror_helpers import (
    Family,
    _fake,
    _load,
    _owners_binding,
    build_family,
    second_facade,
)
from mirrored_owner_probe import SENTINEL, one_reexported_pair

from kiro_crew import _facade_mirror


@pytest.fixture
def family(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> Family:
    return build_family(tmp_path, monkeypatch, request)


# --------------------------------------------------------------------------- #
# The index
# --------------------------------------------------------------------------- #


def test_the_index_records_every_holder_in_owner_order(family: Family) -> None:
    index = family.index
    low, high = family.low.__name__, family.high.__name__
    assert index.facade == family.facade.__name__
    assert index.owners == (low, high)
    assert index.holders["helper"] == (low, high)
    assert index.holders["LIMIT"] == (low, high)
    assert index.holders["json"] == (low, high)
    assert index.holders["low_reads_helper"] == (low,)
    assert index.holders["high_reads_helper"] == (high,)


def test_a_read_is_served_from_the_first_holder_unless_the_facade_binds_the_name(
    family: Family,
) -> None:
    exports = family.index.exports
    assert exports["LIMIT"] == family.low.__name__
    assert exports["high_reads_limit"] == family.high.__name__
    assert "helper" not in exports, "the facade binds helper for its own code"
    assert family.facade.LIMIT == 3
    assert family.facade.low_reads_helper is family.low.low_reads_helper
    assert family.facade.high_reads_helper is family.high.high_reads_helper
    assert family.facade.helper is family.low.helper
    assert dict(family.facade._EXPORTS) == dict(exports)


def test_dir_and_a_star_import_carry_the_forwarded_names(family: Family) -> None:
    listed = dir(family.facade)
    assert {"LIMIT", "high_reads_limit", "ONLY_HERE", "helper"} <= set(listed)
    assert listed == sorted(listed)
    assert {"LIMIT", "high_reads_limit", "low_opens"} <= set(family.facade.__all__)
    assert not [name for name in family.facade.__all__ if name.startswith("_")]


def test_dunders_and_future_flags_are_never_indexed(family: Family) -> None:
    holders = family.index.holders
    assert "annotations" not in holders
    assert not [name for name in holders if name.startswith("__") and name.endswith("__")]


def test_a_name_bound_to_different_objects_is_excluded_and_touches_the_facade_alone(
    family: Family,
) -> None:
    index = family.index
    assert index.excluded == ("CLASH",)
    assert "CLASH" not in index.holders and "CLASH" not in index.exports
    with pytest.raises(AttributeError):
        getattr(family.facade, "CLASH")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(family.facade, "CLASH", "patched", raising=False)
        assert family.facade.__dict__["CLASH"] == "patched"
        assert (family.low.CLASH, family.high.CLASH) == ("low", "high")
    assert "CLASH" not in family.facade.__dict__
    assert (family.low.CLASH, family.high.CLASH) == ("low", "high")


def test_the_index_is_frozen(family: Family) -> None:
    index = family.index
    with pytest.raises(TypeError):
        index.holders["helper"] = ()  # type: ignore[index]
    with pytest.raises(TypeError):
        index.exports["LIMIT"] = "elsewhere"  # type: ignore[index]
    with pytest.raises(dataclasses.FrozenInstanceError):
        index.owners = ()  # type: ignore[misc]


def test_one_object_per_indexed_name_while_no_patch_is_active(family: Family) -> None:
    assert family.index.divergent() == ()


def test_divergent_names_a_write_that_bypassed_the_facade(family: Family) -> None:
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(family.high, "helper", lambda: "only-high")
        assert family.index.divergent() == ("helper",)
    assert family.index.divergent() == ()


def test_divergent_names_a_facade_bound_name_whose_facade_value_drifted(family: Family) -> None:
    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(vars(family.facade), "helper", _fake)
        assert family.index.divergent() == ("helper",)
    assert family.index.divergent() == ()


def test_divergent_names_a_forwarded_name_the_facade_also_binds(family: Family) -> None:
    """A binding there shadows the owner for every read, so a patch would miss reads."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(vars(family.facade), "LIMIT", 3)
        assert family.index.divergent() == ("LIMIT",)
    assert family.index.divergent() == ()


# --------------------------------------------------------------------------- #
# Writes and their undo
# --------------------------------------------------------------------------- #


def test_monkeypatch_reaches_every_reader_and_its_undo_restores_every_holder(
    family: Family,
) -> None:
    original = family.low.helper
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(family.facade, "helper", _fake)
        assert family.readers() == ("fake", "fake", "fake")
        assert family.facade.helper is _fake
    assert family.readers() == ("real", "real", "real")
    assert family.low.helper is family.high.helper is family.facade.helper is original
    assert family.index.divergent() == ()


def test_monkeypatch_by_dotted_string_reaches_every_reader(family: Family) -> None:
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(f"{family.facade.__name__}.LIMIT", 9)
        assert (family.low.low_reads_limit(), family.high.high_reads_limit()) == (9, 9)
        assert "LIMIT" not in family.facade.__dict__, "a forwarded name is not bound here"
    assert (family.low.LIMIT, family.high.LIMIT) == (3, 3)


def test_a_module_valued_name_is_forwarded_like_any_other(family: Family) -> None:
    fake_json = mock.Mock(dumps=lambda value: "stub")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(family.facade, "json", fake_json)
        assert (family.low.low_dumps(), family.high.high_dumps()) == ("stub", "stub")
    assert family.high.high_dumps() == '{"high": 1}'


def test_mock_patch_object_of_a_forwarded_name_round_trips(family: Family) -> None:
    original = family.low.low_reads_helper
    with mock.patch.object(family.facade, "low_reads_helper", return_value="mocked") as patched:
        assert family.low.low_reads_helper is patched
        assert family.facade.low_reads_helper() == "mocked"
    assert family.low.low_reads_helper is original
    assert family.facade.low_reads_helper is original
    assert family.index.divergent() == ()


def test_mock_patch_by_string_of_a_name_the_facade_binds_round_trips(family: Family) -> None:
    original = family.low.helper
    with mock.patch(f"{family.facade.__name__}.helper", side_effect=_fake):
        assert family.readers() == ("fake", "fake", "fake")
    assert family.low.helper is family.high.helper is family.facade.helper is original


def test_nested_mock_patches_of_one_name_unwind_in_order(family: Family) -> None:
    original = family.low.helper
    with mock.patch.object(family.facade, "helper", side_effect=lambda: "outer"):
        with mock.patch.object(family.facade, "helper", side_effect=lambda: "inner"):
            assert family.readers() == ("inner", "inner", "inner")
        assert family.readers() == ("outer", "outer", "outer")
    assert family.readers() == ("real", "real", "real")
    assert family.low.helper is original
    assert family.index.divergent() == ()


def test_nested_mock_patches_of_a_forwarded_name_unwind_in_order(family: Family) -> None:
    with mock.patch.object(family.facade, "LIMIT", 10):
        with mock.patch.object(family.facade, "LIMIT", 20):
            assert family.high.high_reads_limit() == 20
        assert family.high.high_reads_limit() == 10
    assert (family.low.LIMIT, family.high.LIMIT) == (3, 3)
    assert family.index.divergent() == ()


@pytest.mark.parametrize("outer", ["monkeypatch", "mock"])
def test_monkeypatch_and_mock_patch_mixed_unwind_to_the_original(
    family: Family, outer: str
) -> None:
    original = family.low.REGISTRY
    replacement_a: dict[str, int] = {"a": 1}
    replacement_b: dict[str, int] = {"b": 2}
    if outer == "monkeypatch":
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(family.facade, "REGISTRY", replacement_a)
            with mock.patch.object(family.facade, "REGISTRY", replacement_b):
                assert family.high.REGISTRY is replacement_b
            assert family.high.REGISTRY is replacement_a
    else:
        with mock.patch.object(family.facade, "REGISTRY", replacement_a):
            with pytest.MonkeyPatch.context() as mp:
                mp.setattr(family.facade, "REGISTRY", replacement_b)
                assert family.high.REGISTRY is replacement_b
            assert family.high.REGISTRY is replacement_a
    assert family.low.REGISTRY is family.high.REGISTRY is original
    assert family.index.divergent() == ()


def test_the_premise_an_owner_patched_before_the_facade_leaks_into_its_siblings(
    family: Family,
) -> None:
    """Why the guard on direct owner patches exists: the facade's undo restores every
    holder to the one owner's patched value, which the other holder keeps."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(family.low, "LIMIT", 5)
        mp.setattr(family.facade, "LIMIT", 9)
    assert (family.low.LIMIT, family.high.LIMIT) == (3, 5)


def test_an_owner_patched_inside_a_facade_patch_unwinds_cleanly(family: Family) -> None:
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(family.facade, "LIMIT", 9)
        mp.setattr(family.low, "LIMIT", 5)
        assert (family.low.LIMIT, family.high.LIMIT) == (5, 9)
    assert (family.low.LIMIT, family.high.LIMIT) == (3, 3)
    assert family.index.divergent() == ()


def test_a_created_name_shadows_the_builtin_for_every_owner_and_undo_leaves_no_residue(
    family: Family,
) -> None:
    """``monkeypatch.setattr(facade, "open", fake, raising=False)`` reached every reader of a
    bare ``open`` in the unsplit module; those readers now live in the owners."""
    fake_open = mock.Mock(name="open")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(family.facade, "open", fake_open, raising=False)
        assert family.facade.__dict__["open"] is fake_open
        assert family.low.low_opens() is fake_open
        assert family.high.high_opens() is fake_open
    assert "open" not in family.facade.__dict__
    assert _owners_binding(family, "open") == []
    assert family.low.low_opens() is builtins.open


def test_nested_creations_of_one_name_unwind_in_order(family: Family) -> None:
    first, second = mock.Mock(name="first"), mock.Mock(name="second")
    with pytest.MonkeyPatch.context() as outer:
        outer.setattr(family.facade, "brand_new", first, raising=False)
        with pytest.MonkeyPatch.context() as inner:
            inner.setattr(family.facade, "brand_new", second)
            assert (family.low.brand_new, family.high.brand_new) == (second, second)
        assert (family.low.brand_new, family.high.brand_new) == (first, first)
    assert "brand_new" not in family.facade.__dict__
    assert _owners_binding(family, "brand_new") == []


def test_a_created_name_deleted_and_undone_leaves_no_residue(family: Family) -> None:
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(family.facade, "brand_new", 1, raising=False)
        assert _owners_binding(family, "brand_new") == [family.low.__name__, family.high.__name__]
        mp.delattr(family.facade, "brand_new")
        assert not hasattr(family.facade, "brand_new")
        assert _owners_binding(family, "brand_new") == []
    assert not hasattr(family.facade, "brand_new")
    assert _owners_binding(family, "brand_new") == []


def test_a_creation_leaves_an_owner_that_already_binds_the_name_alone(family: Family) -> None:
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(family.low, "brand_new", 1, raising=False)
        mp.setattr(family.facade, "brand_new", 2, raising=False)
        assert (family.low.brand_new, family.high.brand_new) == (1, 2)
    assert _owners_binding(family, "brand_new") == []
    assert "brand_new" not in family.facade.__dict__


def test_a_creation_never_deletes_an_owner_binding_of_the_same_object(family: Family) -> None:
    """The facade records the owners its creation wrote; equal objects decide nothing."""
    shared = mock.Mock(name="shared")
    with pytest.MonkeyPatch.context() as owner_patch:
        owner_patch.setattr(family.low, "brand_new", shared, raising=False)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(family.facade, "brand_new", shared, raising=False)
            assert family.facade.__dict__[_facade_mirror.CREATED_ATTRIBUTE]["brand_new"] == (
                family.high.__name__,
            )
            assert family.high.brand_new is shared
        assert family.low.brand_new is shared, "the creation never wrote low, so undo keeps it"
        assert "brand_new" not in vars(family.high)
    assert _owners_binding(family, "brand_new") == []


def test_deleting_a_created_name_skips_a_recorded_owner_that_no_longer_binds_it(
    family: Family,
) -> None:
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(family.facade, "brand_new", 1, raising=False)
        del vars(family.high)["brand_new"]
        mp.delattr(family.facade, "brand_new")
        assert _owners_binding(family, "brand_new") == []
    assert _owners_binding(family, "brand_new") == []


def test_a_later_write_of_a_created_name_reaches_the_owners_its_creation_wrote(
    family: Family,
) -> None:
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(family.low, "brand_new", 1, raising=False)
        mp.setattr(family.facade, "brand_new", 2, raising=False)
        mp.setattr(family.facade, "brand_new", 3)
        assert (family.low.brand_new, family.high.brand_new) == (1, 3)
        mp.delattr(family.facade, "brand_new")
        assert _owners_binding(family, "brand_new") == [family.low.__name__]
        assert "brand_new" not in family.facade.__dict__[_facade_mirror.CREATED_ATTRIBUTE]
    assert _owners_binding(family, "brand_new") == []


def test_a_dunder_and_an_own_submodule_stay_on_the_facade(family: Family) -> None:
    child = ModuleType(f"{family.facade.__name__}.child")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(family.facade, "child", child, raising=False)
        mp.setattr(family.facade, "__marker__", 1, raising=False)
        assert family.facade.__dict__["child"] is child
        assert _owners_binding(family, "child") == []
        assert _owners_binding(family, "__marker__") == []
    assert "child" not in family.facade.__dict__


def test_a_name_only_the_facade_binds_is_an_ordinary_attribute(family: Family) -> None:
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(family.facade, "ONLY_HERE", "patched")
        assert family.facade.ONLY_HERE == "patched"
        assert _owners_binding(family, "ONLY_HERE") == []
    assert family.facade.ONLY_HERE == "facade"
    assert "ONLY_HERE" not in family.index.holders
    assert {"ONLY_HERE", "_EXPORTS", "facade_reads_helper"} <= family.index.facade_only
    assert not {"helper", "LIMIT", "CLASH", "__name__"} & family.index.facade_only


def test_a_facade_only_name_deleted_and_restored_stays_on_the_facade(family: Family) -> None:
    """A restore after a delete is not a creation: the name was the facade's at install."""
    with pytest.MonkeyPatch.context() as mp:
        mp.delattr(family.facade, "ONLY_HERE")
        assert not hasattr(family.facade, "ONLY_HERE")
    assert family.facade.ONLY_HERE == "facade"
    with mock.patch(f"{family.facade.__name__}.ONLY_HERE", "mocked"):
        assert family.facade.ONLY_HERE == "mocked"
    assert _owners_binding(family, "ONLY_HERE") == []
    with pytest.MonkeyPatch.context() as mp:
        mp.delattr(family.facade, "_EXPORTS")
    assert _owners_binding(family, "_EXPORTS") == []


def test_a_facade_only_name_removed_behind_the_mirror_is_written_back_here(
    family: Family,
) -> None:
    """The install-time picture decides, not a record of what the mirror saw since."""
    with pytest.MonkeyPatch.context() as mp:
        mp.delitem(vars(family.facade), "ONLY_HERE")  # gone without the mirror seeing it
        mp.setattr(family.facade, "ONLY_HERE", "written", raising=False)
        assert _owners_binding(family, "ONLY_HERE") == []
    assert family.facade.ONLY_HERE == "facade"


def test_a_name_the_facade_binds_after_install_is_not_a_creation(family: Family) -> None:
    """Bound by the facade's own code later (a ``global`` write), it stays on the facade,
    through a delete and its undo too, ``create=True`` inside the delete included."""
    vars(family.facade)["LATER"] = 1
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(family.facade, "LATER", 2)
        assert _owners_binding(family, "LATER") == []
    assert family.facade.LATER == 1
    with pytest.MonkeyPatch.context() as mp:
        mp.delattr(family.facade, "LATER")
        assert not hasattr(family.facade, "LATER")
        with mock.patch.object(family.facade, "LATER", 5, create=True):
            assert family.facade.LATER == 5 and _owners_binding(family, "LATER") == []
        assert not hasattr(family.facade, "LATER")
    assert family.facade.LATER == 1 and _owners_binding(family, "LATER") == []
    with pytest.MonkeyPatch.context() as mp:
        mp.delattr(family.facade, "LATER")
    assert family.facade.LATER == 1 and _owners_binding(family, "LATER") == []
    del vars(family.facade)["LATER"]


def test_deleting_a_forwarded_name_empties_every_holder_until_it_is_set_back(
    family: Family,
) -> None:
    """The exact sequence ``mock.patch`` runs on exit for a name the facade does not hold."""
    original = family.low.LIMIT
    with pytest.MonkeyPatch.context() as mp:
        mp.delattr(family.facade, "LIMIT")
        assert not hasattr(family.facade, "LIMIT")
        assert "LIMIT" not in vars(family.low) and "LIMIT" not in vars(family.high)
        assert family.index.divergent() == ("LIMIT",)
    assert family.low.LIMIT == family.high.LIMIT == original
    assert family.index.divergent() == ()


def test_deleting_a_name_the_facade_binds_removes_it_everywhere_until_undone(
    family: Family,
) -> None:
    original = family.low.helper
    with pytest.MonkeyPatch.context() as mp:
        mp.delattr(family.facade, "helper")
        assert "helper" not in family.facade.__dict__
        assert "helper" not in vars(family.low) and "helper" not in vars(family.high)
        with pytest.raises(NameError):
            family.high.high_reads_helper()
    assert family.low.helper is family.high.helper is family.facade.helper is original


def test_deleting_a_name_nobody_holds_raises_the_plain_module_error(family: Family) -> None:
    with pytest.MonkeyPatch.context() as mp:
        mp.delattr(family.facade, "LIMIT")
        with pytest.raises(AttributeError, match=re.escape(family.facade.__name__)):
            delattr(family.facade, "LIMIT")
    with pytest.raises(AttributeError, match="no attribute 'never_bound'"):
        getattr(family.facade, "never_bound")
    with pytest.raises(AttributeError, match="never_bound"):
        delattr(family.facade, "never_bound")


# --------------------------------------------------------------------------- #
# Resolution of owners
# --------------------------------------------------------------------------- #


def _warm(family: Family) -> None:
    """Read and write through the facade first, so a memo of the owner would be set."""
    assert family.facade.LIMIT == 3
    assert family.facade.low_reads_helper is family.low.low_reads_helper
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(family.facade, "LIMIT", 4)


def test_a_purged_owner_imported_again_is_the_one_read_and_patched(family: Family) -> None:
    _warm(family)
    fresh = family.reload_low()
    assert fresh is not family.low
    assert family.facade.low_reads_helper is fresh.low_reads_helper
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(family.facade, "LIMIT", 7)
        assert fresh.low_reads_limit() == 7
        assert family.high.high_reads_limit() == 7


def test_the_mirrored_owner_probe_sees_a_fresh_owner_through_the_facade(family: Family) -> None:
    """The purge probe ``test_mirrored_owner_storage`` runs, run on the synthetic facade."""
    _warm(family)
    pair = one_reexported_pair(family.facade)
    assert pair is not None, "the probe found no re-exported pair in _EXPORTS"
    attribute, owner_name = pair
    assert family.facade._EXPORTS[attribute] == owner_name
    assert owner_name == family.low.__name__, "the probe picks the first sorted name"
    fresh = family.reload_low()
    family.monkeypatch.setattr(fresh, attribute, SENTINEL)
    assert getattr(family.facade, attribute) == SENTINEL


def test_a_read_imports_an_owner_missing_from_sys_modules(family: Family) -> None:
    _warm(family)
    family.monkeypatch.delitem(sys.modules, family.low.__name__)
    assert family.facade.LIMIT == 3
    reimported = sys.modules[family.low.__name__]
    assert reimported is not family.low
    assert family.facade.low_reads_helper is reimported.low_reads_helper


def test_a_write_never_imports_an_owner_missing_from_sys_modules(family: Family) -> None:
    """A teardown must not run an owner's body; a missing owner holds nothing to patch."""
    low = family.low.__name__
    family.monkeypatch.delitem(sys.modules, low)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(family.facade, "helper", _fake)
        assert family.high.high_reads_helper() == "fake"
        with mock.patch.object(family.facade, "high_reads_limit", return_value=0):
            assert family.high.high_reads_limit() == 0
        mp.setattr(family.facade, "brand_new", 1, raising=False)
        assert family.high.brand_new == 1
        assert low not in sys.modules
    assert low not in sys.modules
    assert family.high.high_reads_helper() == "real"


def test_a_loaded_owner_is_read_and_written_without_an_import_call(family: Family) -> None:
    with mock.patch.object(
        _facade_mirror.importlib, "import_module", side_effect=AssertionError("import")
    ):
        assert family.facade.LIMIT == 3
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(family.facade, "LIMIT", 4)
            assert family.high.high_reads_limit() == 4


def test_an_owner_still_running_its_body_is_waited_for_through_the_import_system(
    family: Family,
) -> None:
    """Another thread importing an owner holds its import lock; ``import_module`` waits
    on it, so neither a read nor a write sees half the owner."""
    asked: list[str] = []
    real = _facade_mirror.importlib.import_module

    def recording(name: str, package: str | None = None) -> ModuleType:
        asked.append(name)
        return real(name, package)

    family.monkeypatch.setattr(_facade_mirror.importlib, "import_module", recording)
    assert family.facade.LIMIT == 3
    assert asked == [], "a finished owner is read straight from sys.modules"
    spec = family.low.__spec__
    assert spec is not None
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(spec, "_initializing", True, raising=False)
        assert family.facade.LIMIT == 3
        assert asked == [family.low.__name__], "the read"
        asked.clear()
        family.facade.LIMIT = 4
        assert asked == [family.low.__name__], "the write"
        assert family.high.high_reads_limit() == 4
        family.facade.LIMIT = 3
    asked.clear()
    assert family.facade.LIMIT == 3 and asked == []


# --------------------------------------------------------------------------- #
# install()
# --------------------------------------------------------------------------- #


def test_install_refuses_an_owner_that_is_not_imported_yet(family: Family) -> None:
    with pytest.raises(RuntimeError, match="not yet imported"):
        _facade_mirror.install(family.facade, (family.low.__name__, "no_such_owner_module"))


@pytest.mark.parametrize(
    ("owners", "message"),
    [((), "at least one owner"), (("{low}", "{low}"), "listed twice"), (("{facade}",), "its own")],
)
def test_install_refuses_a_malformed_owner_list(
    family: Family, owners: tuple[str, ...], message: str
) -> None:
    names = {"low": family.low.__name__, "facade": family.facade.__name__}
    with pytest.raises(ValueError, match=message):
        _facade_mirror.install(family.facade, tuple(owner.format(**names) for owner in owners))


def test_install_refuses_exclusions_the_facade_did_not_declare(family: Family) -> None:
    with pytest.raises(RuntimeError, match=r"\['CLASH'\].*declares \[\]"):
        _facade_mirror.install(family.facade, family.index.owners)


def test_install_under_a_direct_owner_patch_fails_loudly(family: Family) -> None:
    """A reload of the facade while an owner is patched directly would otherwise exclude
    the name, so every later patch through the facade would miss its readers."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(family.high, "helper", _fake)
        with pytest.raises(RuntimeError, match="helper"):
            _facade_mirror.install(family.facade, family.index.owners, excluded=("CLASH",))
    assert family.index.excluded == ("CLASH",)


def test_a_facade_binding_a_different_object_excludes_the_name(family: Family) -> None:
    other = ModuleType("facade_mirror_other_facade")
    other.LIMIT = 99  # type: ignore[attr-defined]
    index = _facade_mirror.install(other, family.index.owners, excluded=("CLASH", "LIMIT"))
    assert "LIMIT" in index.excluded and "LIMIT" not in index.holders
    assert index.exports["helper"] == family.low.__name__, "this facade does not bind helper"
    assert other.__dict__[_facade_mirror.INDEX_ATTRIBUTE] is index
    again = _facade_mirror.install(other, family.index.owners, excluded=("CLASH", "LIMIT"))
    assert again.excluded == index.excluded, "a facade with no source file re-installs too"
    with pytest.raises(RuntimeError, match="LIMIT"):
        _facade_mirror.install(other, family.index.owners, excluded=("CLASH",))


def test_install_again_replaces_the_index(family: Family) -> None:
    before = family.index
    after = _facade_mirror.install(family.facade, before.owners, excluded=before.excluded)
    assert family.index is after and after is not before
    assert after == before


def test_the_facade_class_adds_no_name_a_facade_would_serve() -> None:
    """Every attribute of the class is readable through every facade, so it has none."""
    assert [name for name in vars(_facade_mirror.MirroredModule) if not name.startswith("__")] == []


def test_a_mirrored_module_without_an_index_is_a_plain_module() -> None:
    module = _facade_mirror.MirroredModule("facade_mirror_without_index")
    module.value = 1
    assert module.value == 1
    assert dir(module) == sorted(vars(module))
    del module.value
    assert not hasattr(module, "value")
    with pytest.raises(AttributeError, match="facade_mirror_without_index"):
        getattr(module, "value")


# --------------------------------------------------------------------------- #
# Adopting the mirror in a facade that already has a shape
# --------------------------------------------------------------------------- #


def test_a_forwarded_name_keeps_single_owner_forwarding(family: Family) -> None:
    second, legacy = second_facade(family)
    index = vars(second)[_facade_mirror.INDEX_ATTRIBUTE]
    assert dict(index.forwards) == {"LEGACY": legacy.__name__, "legacy_reads": legacy.__name__}
    assert "LEGACY" not in index.holders and "LEGACY" in second._EXPORTS
    assert second.LEGACY == 1 and "LEGACY" in dir(second)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(second, "LEGACY", 5)
        assert legacy.legacy_reads() == 5
        assert "LEGACY" not in vars(second)
    assert legacy.LEGACY == 1
    with mock.patch.object(second, "LEGACY", 7):
        assert legacy.legacy_reads() == 7
    assert legacy.LEGACY == 1


@pytest.mark.parametrize(
    ("mode", "asks"), [("sys_modules_first", False), ("import_module", True), (None, False)]
)
def test_a_forwarded_read_fetches_its_owner_the_way_the_facade_says(
    family: Family, mode: str | None, asks: bool
) -> None:
    """Each legacy facade's own contract: one read ``sys.modules`` first, one imported on
    every read. The default is the first, and an export is read from ``sys.modules``."""
    _second, legacy = second_facade(family)
    other = ModuleType(f"{family.facade.__name__}_forward_reads")
    options = {} if mode is None else {"forward_reads": mode}
    forwards = {"LEGACY": legacy.__name__}
    _facade_mirror.install(
        other, family.index.owners, excluded=("CLASH",), forwards=forwards, **options
    )
    other.__class__ = _facade_mirror.MirroredModule
    asked: list[str] = []
    real = _facade_mirror.importlib.import_module

    def recording(name: str, package: str | None = None) -> ModuleType:
        asked.append(name)
        return real(name, package)

    family.monkeypatch.setattr(_facade_mirror.importlib, "import_module", recording)
    assert other.LEGACY == 1
    assert asked == ([legacy.__name__] if asks else [])
    assert other.LIMIT == 3 and asked == ([legacy.__name__] if asks else []), "an export never asks"


def test_install_refuses_an_unknown_forward_read_mode(family: Family) -> None:
    other = ModuleType(f"{family.facade.__name__}_bad_mode")
    with pytest.raises(ValueError, match="unknown forward_reads 'sometimes'"):
        _facade_mirror.install(
            other, family.index.owners, excluded=("CLASH",), forward_reads="sometimes"  # type: ignore[arg-type]
        )


def test_install_refuses_a_forward_that_an_indexed_owner_also_binds(family: Family) -> None:
    """``from legacy import LEGACY`` in an owner: a patch of the forward would miss it."""
    _second, legacy = second_facade(family)
    package = family.facade.__name__.rpartition(".")[0]
    path = family.package_dir / "imports_legacy.py"
    path.write_text(f"from {legacy.__name__} import LEGACY\n", encoding="utf-8")
    importer = _load(f"{package}.imports_legacy", path, family.monkeypatch)
    other = ModuleType(f"{family.facade.__name__}_forwarding")
    forwards = {"LEGACY": legacy.__name__}
    with pytest.raises(RuntimeError, match=rf"owner {re.escape(importer.__name__)} binds it too"):
        _facade_mirror.install(other, (family.low.__name__, importer.__name__), forwards=forwards)
    index = _facade_mirror.install(other, (family.low.__name__, legacy.__name__), forwards=forwards)
    assert dict(index.forwards) == forwards and "LEGACY" not in index.holders


def test_divergent_names_a_forward_shadowed_here_or_missing_from_its_owner(
    family: Family,
) -> None:
    second, legacy = second_facade(family)
    index = vars(second)[_facade_mirror.INDEX_ATTRIBUTE]
    assert index.divergent() == ()
    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(vars(second), "LEGACY", 5)
        assert index.divergent() == ("LEGACY",)
    with pytest.MonkeyPatch.context() as mp:
        mp.delitem(vars(legacy), "LEGACY")
        assert index.divergent() == ("LEGACY",)
    assert index.divergent() == ()


def test_install_refuses_a_forward_the_facade_binds_or_its_owner_lacks(family: Family) -> None:
    _second, legacy = second_facade(family)
    owners = family.index.owners
    with pytest.raises(ValueError, match="bound here"):
        _facade_mirror.install(
            family.facade, owners, excluded=("CLASH",), forwards={"helper": legacy.__name__}
        )
    with pytest.raises(RuntimeError, match="does not hold"):
        _facade_mirror.install(
            family.facade, owners, excluded=("CLASH",), forwards={"nope": legacy.__name__}
        )


def test_a_name_outside_the_surface_is_no_part_of_the_facade(family: Family) -> None:
    """An owner helper the split introduced: never served or listed, and a write through
    the facade follows the creation rule, so the owner keeps its own binding."""
    surface = {"LIMIT", "helper", "json", "REGISTRY", "high_reads_limit", "CLASH"}
    second, _legacy = second_facade(family, surface=surface)
    index = vars(second)[_facade_mirror.INDEX_ATTRIBUTE]
    assert "low_dumps" not in index.holders and "low_dumps" not in second._EXPORTS
    assert "low_dumps" not in dir(second)
    with pytest.raises(AttributeError):
        getattr(second, "low_dumps")
    original = family.low.low_dumps
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(second, "low_dumps", _fake, raising=False)
        assert family.low.low_dumps is original
        assert family.high.low_dumps is _fake  # created where no owner bound it
    assert family.low.low_dumps is original
    assert "low_dumps" not in vars(family.high)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(second, "LIMIT", 8)
        assert family.high.high_reads_limit() == 8


_OWN_RESOLVER = """
import importlib as _importlib
import sys as _sys

from kiro_crew import _facade_mirror


def __getattr__(name):
    if name == "OWN_RESOLVED":
        return "from-own-resolver"
    raise AttributeError(name)


def __dir__():
    return ["OWN_RESOLVED", *globals()]


_OWNERS = ("{low}", "{high}")
for _owner in _OWNERS:
    _importlib.import_module(_owner)
_EXPORTS: dict[str, str] = {{}}
_EXPORTS.update(
    _facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=("CLASH",)).served
)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
"""


def test_a_facade_keeps_its_own_module_getattr_and_dir(family: Family) -> None:
    """A facade's own PEP 562 resolver answers first; the mirror serves what it declines."""
    package = family.facade.__name__.rpartition(".")[0]
    path = family.package_dir / "own_resolver.py"
    path.write_text(
        _OWN_RESOLVER.format(low=family.low.__name__, high=family.high.__name__), encoding="utf-8"
    )
    facade = _load(f"{package}.own_resolver", path, family.monkeypatch)
    assert facade.OWN_RESOLVED == "from-own-resolver"
    assert facade.LIMIT == 3, "the resolver declines LIMIT, so the mirror answers it"
    assert facade.high_reads_limit is family.high.high_reads_limit, "the mirror serves the rest"
    with pytest.raises(AttributeError):
        getattr(facade, "never_bound")
    listed = dir(facade)
    assert "OWN_RESOLVED" in listed and "high_reads_limit" in listed
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(facade, "LIMIT", 8)
        assert facade.LIMIT == 8 and family.high.high_reads_limit() == 8
    with mock.patch.object(facade, "LIMIT", 9):
        assert facade.LIMIT == 9 and family.low.low_reads_limit() == 9
    assert facade.LIMIT == family.low.LIMIT == family.high.LIMIT == 3


@pytest.mark.parametrize("answered", ["LIMIT", "helper", "LEGACY"])
def test_install_refuses_a_module_getattr_answering_a_name_the_mirror_holds(
    family: Family, answered: str
) -> None:
    """Reads would come from the resolver while writes reach the owners, so an undo
    would restore a value no read sees."""
    _second, legacy = second_facade(family)
    other = ModuleType(f"{family.facade.__name__}_resolving")
    answers = {answered, "OWN"}

    def resolver(name: str) -> object:
        if name in answers:
            return "resolved"
        raise AttributeError(name)

    vars(other)["__getattr__"] = resolver
    owners = family.index.owners
    forwards = {"LEGACY": legacy.__name__}
    with pytest.raises(RuntimeError, match=rf"__getattr__ answers \['{answered}'\]"):
        _facade_mirror.install(other, owners, excluded=("CLASH",), forwards=forwards)

    def broken(name: str) -> object:
        raise KeyError(name)

    vars(other)["__getattr__"] = broken
    with pytest.raises(RuntimeError, match="raised KeyError") as raised:
        _facade_mirror.install(other, owners, excluded=("CLASH",), forwards=forwards)
    assert isinstance(raised.value.__cause__, KeyError)
    vars(other)["__getattr__"] = resolver
    assert _facade_mirror.INDEX_ATTRIBUTE not in vars(other), "a refused install sets nothing"
    answers.discard(answered)
    index = _facade_mirror.install(other, owners, excluded=("CLASH",), forwards=forwards)
    assert answered in {*index.holders, *index.forwards}, "a resolver of its own names is kept"


_EXT = """
def tool() -> str:
    return "ext"
"""

_EXT_OWNER = """
from {ext} import tool


def {reader}() -> str:
    return tool()
"""

_EXT_FACADE = """
import importlib as _importlib
import sys as _sys

from kiro_crew import _facade_mirror
from {ext} import tool

_OWNERS = ("{a}", "{b}")
for _owner in _OWNERS:
    _importlib.import_module(_owner)
_EXPORTS: dict[str, str] = {{}}
_EXPORTS.update(_facade_mirror.install(_sys.modules[__name__], _OWNERS).served)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
"""


def _external_family(family: Family) -> tuple[ModuleType, ModuleType, ModuleType, ModuleType]:
    """A facade and two owners that all import ``tool`` from a module outside the family."""
    package = family.facade.__name__.rpartition(".")[0]
    names = {key: f"{package}.{key}" for key in ("ext", "a", "b", "ext_facade")}
    sources = {
        "ext": _EXT,
        "a": _EXT_OWNER.format(ext=names["ext"], reader="a_reads"),
        "b": _EXT_OWNER.format(ext=names["ext"], reader="b_reads"),
        "ext_facade": _EXT_FACADE.format(**names),
    }
    loaded = {}
    for key, text in sources.items():
        path = family.package_dir / f"{key}.py"
        path.write_text(text, encoding="utf-8")
        loaded[key] = _load(names[key], path, family.monkeypatch)
    return loaded["ext_facade"], loaded["a"], loaded["b"], loaded["ext"]


def _execute_again(module: ModuleType) -> None:
    """Run *module*'s code again in its own namespace, as ``importlib.reload`` does."""
    assert module.__spec__ is not None and module.__spec__.loader is not None
    module.__spec__.loader.exec_module(module)


def test_a_reinstall_resyncs_a_name_imported_from_a_module_imported_again(
    family: Family,
) -> None:
    """The dependency is purged and imported again, the facade runs again and binds the
    fresh object, and the owners that kept the old one are brought into step."""
    facade, a, b, ext = _external_family(family)
    old_tool = ext.tool
    family.monkeypatch.delitem(sys.modules, ext.__name__)
    fresh_ext = _load(ext.__name__, Path(ext.__file__ or ""), family.monkeypatch)
    assert fresh_ext.tool is not old_tool
    assert a.tool is old_tool and b.tool is old_tool
    _execute_again(facade)
    index = vars(facade)[_facade_mirror.INDEX_ATTRIBUTE]
    assert a.tool is b.tool is facade.tool is fresh_ext.tool
    assert index.divergent() == ()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(facade, "tool", lambda: "patched")
        assert (a.a_reads(), b.b_reads()) == ("patched", "patched")
    assert (a.a_reads(), b.b_reads()) == ("ext", "ext")


def test_a_first_install_never_resyncs(family: Family) -> None:
    """Outside a re-install a divergence is a direct owner patch, and it fails loudly."""
    facade, a, _b, _ext = _external_family(family)
    other = ModuleType(f"{facade.__name__}_twin")
    other.tool = facade.tool  # type: ignore[attr-defined]
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(a, "tool", _fake)
        with pytest.raises(RuntimeError, match="tool"):
            _facade_mirror.install(other, vars(facade)[_facade_mirror.INDEX_ATTRIBUTE].owners)


def test_executing_a_facade_again_keeps_every_patch_seam(family: Family) -> None:
    before = family.index
    _execute_again(family.facade)
    assert family.index is not before and family.index == before
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(family.facade, "LIMIT", 6)
        assert (family.low.low_reads_limit(), family.high.high_reads_limit()) == (6, 6)


def test_a_reinstall_never_resyncs_a_declared_exclusion_and_writes_nothing_when_refused(
    family: Family,
) -> None:
    facade, a, b, ext = _external_family(family)
    old_tool = ext.tool
    family.monkeypatch.delitem(sys.modules, ext.__name__)
    fresh = _load(ext.__name__, Path(ext.__file__ or ""), family.monkeypatch)
    family.monkeypatch.setitem(vars(facade), "tool", fresh.tool)  # what a re-run binds
    owners = vars(facade)[_facade_mirror.INDEX_ATTRIBUTE].owners
    index = _facade_mirror.install(facade, owners, excluded=("tool",))
    assert index.excluded == ("tool",) and a.tool is b.tool is old_tool
    with pytest.raises(RuntimeError, match="declares \\['bogus'\\]"):
        _facade_mirror.install(facade, owners, excluded=("bogus",))
    assert a.tool is b.tool is old_tool, "a refused install re-syncs nothing"
    _facade_mirror.install(facade, owners)
    assert a.tool is b.tool is fresh.tool


def test_definitions_are_read_inside_module_level_blocks_and_unpacked_targets() -> None:
    type_alias = "type Alias = int\n" if sys.version_info >= (3, 12) else ""
    source = type_alias + (
        "if (walrus := 1):\n    pass\n[(in_comprehension := i) for i in ()]\n"
        "match 1:\n    case [first, *rest]:\n        pass\n"
        "    case {'k': value, **others}:\n        pass\n    case int() as whole:\n        pass\n"
        "def scoped():\n    (in_function := 1)\nhidden = lambda: (in_lambda := 1)\n"
        "import os\nfrom x import y\n"
        "try:\n    import fast\nexcept ImportError:\n    fast = None\n"
        "if True:\n    def in_if(): pass\nelse:\n    class InElse: pass\n"
        "with open('f') as handle:\n    WITHIN = 1\n"
        "for loop_var in ():\n    FROM_FOR = 1\n"
        "while False:\n    IN_WHILE = 1\n"
        "match 1:\n    case 1:\n        IN_CASE = 1\n"
        "a, (b, *c) = 1, (2, 3)\ncount: int = 0\ncount += 1\n"
        "def outer():\n    inner = 1\nclass K:\n    attr = 1\n"
    )
    expected_more = {"walrus", "in_comprehension", "first", "rest", "value", "others", "whole"}
    expected_more |= {"scoped", "hidden"} | ({"Alias"} if type_alias else set())
    names = _facade_mirror._defined_names(ast.parse(source))
    assert expected_more <= names, "walrus, match captures and type aliases are definitions"
    assert names - expected_more == {
        "fast",
        "in_if",
        "InElse",
        "handle",
        "WITHIN",
        "loop_var",
        "FROM_FOR",
        "IN_WHILE",
        "IN_CASE",
        "a",
        "b",
        "c",
        "count",
        "outer",
        "K",
    }


_GUARDED_OWNER = """
if True:
    def tool() -> str:
        return "defined"
"""


def test_a_reinstall_refuses_a_divergence_of_a_name_defined_in_a_module_level_block(
    family: Family,
) -> None:
    """``tool`` is defined inside ``if True:``: its divergence is a patch, never re-synced."""
    package = family.facade.__name__.rpartition(".")[0]
    names = {key: f"{package}.{key}" for key in ("guarded", "user", "guarded_facade")}
    sources = {
        "guarded": _GUARDED_OWNER,
        "user": _EXT_OWNER.format(ext=names["guarded"], reader="user_reads"),
        "guarded_facade": _EXT_FACADE.format(
            ext=names["guarded"], a=names["guarded"], b=names["user"]
        ),
    }
    loaded = {}
    for key, text in sources.items():
        path = family.package_dir / f"{key}.py"
        path.write_text(text, encoding="utf-8")
        loaded[key] = _load(names[key], path, family.monkeypatch)
    owners = (names["guarded"], names["user"])
    family.monkeypatch.setattr(loaded["user"], "tool", _fake)
    with pytest.raises(RuntimeError, match="tool"):
        _facade_mirror.install(loaded["guarded_facade"], owners)
    assert loaded["user"].tool is _fake, "nothing was re-synced"


def test_a_reinstall_under_an_active_creation_fails_loudly(family: Family) -> None:
    """The index would take the created name for a name of the family, and hold it as
    divergent once the patch is undone."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(family.facade, "brand_new", 1, raising=False)
        with pytest.raises(RuntimeError, match=r"\['brand_new'\] were created"):
            _facade_mirror.install(family.facade, family.index.owners, excluded=("CLASH",))
    assert "brand_new" not in family.index.holders
    _execute_again(family.facade)
    assert family.index.divergent() == () and "brand_new" not in family.index.holders
