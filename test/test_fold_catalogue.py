"""The committed fold catalogue equals the one the code generates.

An agent authoring a dashboard template picks a fold, then writes a contract naming that
fold's fields with their types. It has no way to learn either except from the catalogue,
so a stale catalogue is worse than no catalogue: it sends the author to write a provider
reading a field no fold produces, and nothing downstream can tell that from a typo.

These tests are the gate that makes staleness impossible, plus the generator's own
self-test. Both are needed: the equality check proves the committed bytes are current,
and the self-test proves the check can observe a difference at all.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from skill_script_helpers import load_skill_script

from kiro_crew.crew_log.projection import (
    _FOLDS,
    FOLD_NAMES,
    INTERNAL_PROJECTION_NAMES,
)

ROOT = Path(__file__).resolve().parents[1]
GENERATOR = ROOT / "scripts" / "fold_catalogue.py"
SKILL_DIR = ROOT / "src" / "kiro_crew" / "builtin_skills" / "kirocrew-dev" / "dashboard-template"
JSON_PATH = SKILL_DIR / "folds.json"
MARKDOWN_PATH = SKILL_DIR / "FOLDS.md"


@pytest.fixture(scope="module")
def generator() -> Any:
    return load_skill_script("fold_catalogue_generator", GENERATOR)


@pytest.fixture(scope="module")
def scaffold() -> Any:
    return load_skill_script("fold_catalogue_scaffold_view", SKILL_DIR / "scripts" / "scaffold.py")


@pytest.fixture(scope="module")
def committed() -> dict[str, Any]:
    return json.loads(JSON_PATH.read_text(encoding="utf-8"))


class TestTheCommittedCatalogueIsCurrent:
    def test_it_matches_what_the_code_generates(self, generator: Any) -> None:
        """THE gate. Byte equality, both artifacts, no tolerance.

        The remedy is in the failure message rather than left to the reader, because the
        person who trips this is usually not the person who added the fold.
        """
        drifted = generator.check()
        assert not drifted, (
            f"the committed fold catalogue is stale: {drifted}. Regenerate it with "
            "`python3 scripts/fold_catalogue.py --write` and commit the result."
        )

    def test_the_generators_own_selftest_passes(self, generator: Any) -> None:
        """Proves the check above can SEE a drift.

        Without this, "nothing differs" reads the same way whether the comparison works
        or is broken -- and a broken comparison is the one failure mode that makes every
        other assertion here vacuous.
        """
        assert generator.selftest() == 0


class TestTheCatalogueCoversTheKernel:
    def test_it_names_exactly_the_folds_the_product_keeps(self, committed: dict[str, Any]) -> None:
        names = [fold["name"] for fold in committed["folds"]]
        assert names == list(FOLD_NAMES), (
            "the catalogue and the projection registry disagree about which folds exist: "
            f"{sorted(set(names) ^ set(FOLD_NAMES))}"
        )

    def test_an_internal_fold_is_marked_as_one(self, committed: dict[str, Any]) -> None:
        """A fold no panel draws is still listed, and still labelled.

        Hiding it would make the catalogue disagree with the registry; listing it
        unlabelled would invite a template to draw from a fold with no advertised reader.
        """
        by_name = {fold["name"]: fold for fold in committed["folds"]}
        for name in INTERNAL_PROJECTION_NAMES:
            assert by_name[name]["advertised"] is False, name
        advertised = [f["name"] for f in committed["folds"] if f["advertised"]]
        assert advertised, "no advertised fold -- the probe is broken, not the registry"

    def test_every_fold_says_what_it_answers(self, committed: dict[str, Any]) -> None:
        for fold in committed["folds"]:
            assert fold["answers"].strip(), f"{fold['name']} has no sentence"
            assert fold["fields"], f"{fold['name']} lists no fields"

    def test_a_fold_moved_by_every_entry_says_so_as_null(self, committed: dict[str, Any]) -> None:
        """``affects: null`` means every entry moves it.

        An empty list would read as "no entry moves this fold", which is the opposite
        fact and would tell an author the fold never updates.
        """
        status = next(fold for fold in committed["folds"] if fold["name"] == "status")
        assert status["affects"] is None
        work = next(fold for fold in committed["folds"] if fold["name"] == "work")
        assert work["affects"] == ["work/recorded"]

    def test_no_field_is_typed_as_the_absence_marker(self, committed: dict[str, Any]) -> None:
        """``NoneType`` is never a useful answer: it is what an empty fold shows, not
        what the field holds. Such a field is reported as optional with a real type, or
        as ``unknown``."""
        for fold in committed["folds"]:
            for row in fold["fields"]:
                assert row["type"] != "NoneType", f"{fold['name']}.{row['name']}"

    def test_an_unknown_type_is_always_optional(self, committed: dict[str, Any]) -> None:
        """The two are the same finding seen twice: the type is unknown BECAUSE the empty
        fold left the field null. An unknown on a non-optional row would mean the
        generator lost track of which branch it took."""
        for fold in committed["folds"]:
            for row in fold["fields"]:
                if row["type"] == "unknown":
                    assert row["optional"] is True, f"{fold['name']}.{row['name']}"


class TestANullFieldIsNeverGivenAGuessedType:
    """A null field's type is DECLARED or ``unknown``. It is never guessed.

    The guess -- look the field NAME up in the entry-type registry -- shipped first and
    was wrong: a rendered name is owned by no one entry type, so ``status`` reported
    ``previous: dict`` and ``turn: int``. A confident wrong row is worse than a missing
    one, because a reader cannot tell which rows to trust.

    What replaced the guess is not silence. ``dashboard_types._NULLABLE`` DECLARES the
    type of a null leaf where one is declarable, and a declaration is checked in both
    directions by ``test_dashboard_types``: the path must be one the fold renders null,
    and the type must be the one the fold writes there. So the rule here is the stricter
    one -- a null row carries exactly the declared type, or ``unknown``, and nothing in
    between.
    """

    def test_every_optional_row_is_declared_or_unknown(self, committed: dict[str, Any]) -> None:
        from kiro_crew import dashboard_types as dt

        guessed: list[str] = []
        for fold in committed["folds"]:
            for row in fold["fields"]:
                if row["optional"] is not True:
                    continue
                declared = dt._NULLABLE.get(f"{fold['name']}.{row['name']}")
                expected = dt.UNKNOWN_TYPE if declared is None else declared["type"]
                if row["type"] != expected:
                    guessed.append(
                        f"{fold['name']}.{row['name']} is {row['type']!r}, "
                        f"declared {expected!r}"
                    )
        assert guessed == [], f"a null field's type was neither declared nor unknown: {guessed}"

    def test_at_least_one_optional_row_exists_so_the_check_is_not_vacuous(
        self, committed: dict[str, Any]
    ) -> None:
        optional = [
            f"{fold['name']}.{row['name']}"
            for fold in committed["folds"]
            for row in fold["fields"]
            if row["optional"] is True
        ]
        assert optional, "no null fields at all: the rule above is asserting nothing"

    def test_the_row_builder_itself_carries_the_three_cases(self, generator: Any) -> None:
        """Held separately from the committed artifact, and it has to be: the committed
        one carries no undeclared null row (see the class below), so this is the only
        place the ``unknown`` branch is exercised.

        Three cases in one shape: a declared null, an undeclared null, and a plain value.
        """
        from kiro_crew import dashboard_types as dt

        entry = dt.FoldType(
            name="probe",
            shape={
                "type": "object",
                "properties": {
                    "stamp": {"type": "number", "nullable": True},
                    "n": {"type": dt.UNKNOWN_TYPE, "nullable": True, "why": "renders null"},
                    "said": {"type": "string"},
                },
            },
            source_fold="probe",
            keyed_by=dt.KEYED_BY_SESSION,
            owner_served=False,
            state_version=1,
            row_bytes=None,
        )
        assert generator._field_rows(entry) == [
            {"name": "n", "type": "unknown", "optional": True},
            {"name": "said", "type": "string", "optional": False},
            {"name": "stamp", "type": "number", "optional": True},
        ]


class TestOnlyFoldsDeclaringABinderAreBound:
    """The probe binds a slot to the folds that DECLARE a binder, not to every slot-keyed
    fold -- today one fold of the four declares one.

    Aimed at ``dashboard_types._probe``, which is where the probe lives now that the
    script renders the catalog instead of probing for itself. Same rule, one copy of it.

    Both directions matter. Binding a fold that declares no binder is impossible; NOT
    binding one that does would render a board-wide fold against no board, and it would
    still return a dict, so nothing else in the suite would notice.
    """

    def test_the_binder_set_is_derived_from_the_kernel_and_is_not_empty(self) -> None:
        binders = {name for name, fold in _FOLDS.items() if fold.bind_slot is not None}
        assert binders, "no fold declares a slot binder: the bind branch in _probe is dead"
        for name in binders:
            assert _FOLDS[name].bind_slot is not None

    def test_the_probe_binds_exactly_those_folds(self) -> None:
        from kiro_crew import dashboard_types as dt

        bound: list[str] = []
        for name, fold in _FOLDS.items():
            original = fold.bind_slot
            if original is None:
                continue

            def spy(state: Any, slot: str, _name: str = name, _inner: Any = original) -> Any:
                bound.append(_name)
                return _inner(state, slot)

            object.__setattr__(fold, "bind_slot", spy)
            try:
                dt._probe(name)
            finally:
                object.__setattr__(fold, "bind_slot", original)
        expected = sorted(name for name, fold in _FOLDS.items() if fold.bind_slot is not None)
        assert sorted(bound) == expected

    def test_a_fold_with_no_binder_still_renders(self) -> None:
        from kiro_crew import dashboard_types as dt

        unbound = [name for name, fold in _FOLDS.items() if fold.bind_slot is None]
        assert unbound, "every fold declares a binder: the docstring's other half is stale"
        for name in unbound:
            assert dt._probe(name)["type"] == "object"


class TestTheScaffoldReadsTheCatalogue:
    """The scaffold must not carry its own copy of the fold list.

    Two lists are two things to update, and the one that gets forgotten is the one that
    refuses a fold that exists.
    """

    def test_the_scaffolds_fold_choices_come_from_the_committed_json(
        self, committed: dict[str, Any]
    ) -> None:
        scaffold = load_skill_script(
            "dashboard_template_scaffold_catalogue", SKILL_DIR / "scripts" / "scaffold.py"
        )
        assert scaffold.folds() == tuple(fold["name"] for fold in committed["folds"])

    def test_an_unreadable_catalogue_is_a_named_error_not_a_fallback_list(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A fallback list would be reached exactly when the catalogue is missing, which
        is the one moment a guess is least likely to be right."""
        scaffold = load_skill_script(
            "dashboard_template_scaffold_missing", SKILL_DIR / "scripts" / "scaffold.py"
        )
        monkeypatch.setattr(scaffold, "CATALOGUE_PATH", tmp_path / "absent.json")
        with pytest.raises(scaffold.ScaffoldError, match="cannot read the fold catalogue"):
            scaffold.folds()

    def test_an_empty_catalogue_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        empty = tmp_path / "folds.json"
        empty.write_text(json.dumps({"catalogue_version": 1, "folds": []}), encoding="utf-8")
        scaffold = load_skill_script(
            "dashboard_template_scaffold_empty", SKILL_DIR / "scripts" / "scaffold.py"
        )
        monkeypatch.setattr(scaffold, "CATALOGUE_PATH", empty)
        with pytest.raises(scaffold.ScaffoldError, match="lists no folds"):
            scaffold.folds()


class TestTheSkillPointsAtTheCatalogue:
    def test_the_skill_body_names_the_generated_file(self) -> None:
        """The skill must send the reader to the generated catalogue rather than carrying
        its own table, which is the copy that goes stale."""
        body = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        assert "FOLDS.md" in body
        assert "fold_catalogue.py" in body

    def test_the_markdown_warns_against_editing_it(self) -> None:
        text = MARKDOWN_PATH.read_text(encoding="utf-8")
        assert "GENERATED by scripts/fold_catalogue.py" in text
        assert "--write" in text


class TestTheGeneratorWillNotWriteThroughALink:
    """`--write` targets two fixed paths derived from `__file__`, so redirecting it means
    committing a symlink at one of those names.

    An attacker who can do that can also commit Python, and the generator puts `src` on
    `sys.path` and imports the projection kernel before it writes -- so the redirect is
    strictly weaker than the code execution its own precondition already grants. The rule
    is here anyway because it is the rule the skill's own scaffold applies to the files it
    generates; a PR that hardens one generated write and not the other is harder to reason
    about than one that does both. The check itself is the host's
    ``platform_compat.is_link_or_junction`` -- this script already imports the projection
    kernel, so it has no ground to carry its own copy, unlike the scaffold that ships with
    the skill.
    """

    def test_it_refuses_a_symlinked_artifact_path(self, generator: Any, tmp_path: Path) -> None:
        target = tmp_path / "outside.json"
        target.write_text("do not touch\n", encoding="utf-8")
        planted = tmp_path / "folds.json"
        try:
            planted.symlink_to(target)
        except (OSError, NotImplementedError):
            pytest.skip("this platform will not create a symlink here")
        with pytest.raises(SystemExit, match="would land somewhere this path does not name"):
            generator._write_without_following(planted, "replaced\n")
        assert target.read_text(encoding="utf-8") == "do not touch\n"

    def test_it_writes_an_ordinary_path(self, generator: Any, tmp_path: Path) -> None:
        """Control: the refusal must be about the link, not about writing."""
        plain = tmp_path / "folds.json"
        generator._write_without_following(plain, "generated\n")
        assert plain.read_text(encoding="utf-8") == "generated\n"


class TestATextFieldNamesARealFoldKey:
    """The fraction halves are checked against the fold; the text half was not.

    `goal` mistyped as `goall` emitted `read_text(view, "goall")`, the card rendered the
    words "not said" at every render, and the emitted parity gates AGREED with it, because
    they build the card from an empty view where "not said" is the right answer. Nothing was
    red and the page was wrong -- the same typo class the fraction halves already refuse.
    """

    def test_a_text_source_absent_from_the_fold_is_refused(self, scaffold: Any) -> None:
        with pytest.raises(scaffold.ScaffoldError, match="not a text field of this fold"):
            scaffold.parse_fields(["goall:str|unsaid"], texts=scaffold.text_fields("ledger"))

    def test_a_renamed_card_field_may_name_the_key_it_reads(self, scaffold: Any) -> None:
        """The escape the fraction grammar already has, on the text kind."""
        fields = scaffold.parse_fields(
            ["headline:str|unsaid:goal"], texts=scaffold.text_fields("ledger")
        )
        body = scaffold.render_provider("x_board", "ledger", fields)
        assert '"headline": read_text(view, "goal"),' in body

    def test_a_renamed_key_is_checked_too(self, scaffold: Any) -> None:
        with pytest.raises(scaffold.ScaffoldError, match="not a text field of this fold"):
            scaffold.parse_fields(
                ["headline:str|unsaid:goall"], texts=scaffold.text_fields("ledger")
            )

    def test_the_menu_comes_from_the_catalogue(self, scaffold: Any) -> None:
        """Read, never restated -- the same rule the fraction menu follows. A fold with no
        `str` fields accepts no author-declared text field, which is the honest answer: the
        always-present text a page carries is written by the provider, not read from a fold.
        """
        assert "goal" in scaffold.text_fields("ledger")
        assert scaffold.text_fields("tools") == ()


class TestTheFoldMenusRespectAnUnknownType:
    """An `unknown` row is admitted to a menu, and that is not "no type".

    The menu is "names this fold has, minus the ones whose type is known and wrong", so a
    row the catalogue could not type must not be filtered out: an optional field is
    exactly the field a dashboard wants, because absence is what `Unsaid` renders.

    The committed catalogue currently has NO `unknown` row -- every null leaf is declared
    (see `TestEveryOptionalRowIsTypedToday`) -- so this behaviour is exercised against a
    planted row rather than asserted of a real one. Keeping it tested is the point: the
    next fold to render an undeclared null must still be declarable.
    """

    def test_an_unknown_typed_row_is_offered_to_both_menus(
        self, scaffold: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        planted = (("said", "string"), ("counted", "number"), ("mystery", "unknown"))
        monkeypatch.setattr(scaffold, "_catalogue_rows", lambda fold: planted)
        assert "mystery" in scaffold.text_fields("status")
        assert "mystery" in scaffold.counting_fields("status")
        # And the typed rows still land in their own menu only.
        assert "counted" not in scaffold.text_fields("status")
        assert "said" not in scaffold.counting_fields("status")

    def test_a_field_whose_type_is_known_and_wrong_is_still_refused(self, scaffold: Any) -> None:
        """The half that must not be widened away: `dropped` is an `object` and `resumed`
        a `boolean`, both declared, so neither is text and neither is a count."""
        for name in ("dropped", "resumed"):
            with pytest.raises(scaffold.ScaffoldError, match="not a text field of this fold"):
                scaffold.parse_fields([f"{name}:str|unsaid"], texts=scaffold.text_fields("status"))

    def test_a_name_the_fold_does_not_carry_is_still_refused(self, scaffold: Any) -> None:
        """The typo the check exists for survives the widening."""
        with pytest.raises(scaffold.ScaffoldError, match="not a text field of this fold"):
            scaffold.parse_fields(["goall:str|unsaid"], texts=scaffold.text_fields("status"))


class TestAnAlwaysPresentFieldIsNotCheckedAgainstTheFold:
    """An always-present field reads nothing out of the fold, so the fold menu cannot judge it.

    The provider writes `lede`, `you` and `notes` from the judgment and `captured_at` and
    `contract_version` from the clock and the constant. No fold is ever asked for those names,
    so checking them against a fold's text fields refuses a declaration on grounds that are
    not true of it -- and the parser already handles a re-declared always-present field by
    dropping the author's kind and saying so on stderr.
    """

    @pytest.mark.parametrize("name", ["lede", "you", "notes", "captured_at"])
    def test_declaring_one_explicitly_is_accepted(self, scaffold: Any, name: str) -> None:
        fields = scaffold.parse_fields([f"{name}:str|unsaid"], texts=scaffold.text_fields("ledger"))
        assert any(f.name == name for f in fields)

    def test_an_ordinary_text_field_is_still_checked(self, scaffold: Any) -> None:
        """The bypass must be the always-present names only, not a hole in the check."""
        with pytest.raises(scaffold.ScaffoldError, match="not a text field of this fold"):
            scaffold.parse_fields(["goall:str|unsaid"], texts=scaffold.text_fields("ledger"))


class TestAnUntypeableSourceIsSaidOutLoud:
    """The limit of the fold-source check, stated where a reader will meet it.

    A row the catalogue could not type is admitted to a menu, because refusing it would
    make the field undeclarable -- and NAMED, to the person making the choice, because no
    gate downstream can catch a wrong one: the emitted gates build the card from an EMPTY
    view, where "not said" is the expected answer, so they agree with a wrong render.

    `status.turn` is not an example of it: the catalogue renders `dashboard_types.catalog`,
    which declares `status.turn` as `object`, so `turn:str|unsaid` is REFUSED outright
    rather than admitted with a note. `TestEveryOptionalRowIsTypedToday` pins that.

    So the note's machinery is tested against a planted row, and it is worth keeping
    tested: a fold can render a null the declarations do not cover, and the first one to
    do so needs the note.
    """

    def test_an_untypeable_source_is_admitted_with_a_note(self, scaffold: Any, capsys: Any) -> None:
        planted = (("lifecycle", "string"), ("mystery", "unknown"))
        scaffold.parse_fields(
            ["mystery:str|unsaid"],
            texts=("lifecycle", "mystery"),
            unchecked=frozenset({"mystery"}),
        )
        note = capsys.readouterr().err
        assert "could not type it" in note
        assert "no gate will say so" in note, "the note must admit the gates agree with it"
        assert planted  # names the shape the note is about, for a reader of this case

    def test_a_typed_source_is_admitted_silently(self, scaffold: Any, capsys: Any) -> None:
        """The note must fire on the untypeable rows only, or it is noise an author learns
        to skip -- and the one that matters would be skipped with it."""
        scaffold.parse_fields(
            ["lifecycle:str|unsaid"],
            texts=scaffold.text_fields("status"),
            unchecked=scaffold.unchecked_sources("status"),
        )
        assert "could not type" not in capsys.readouterr().err

    def test_the_untypeable_set_is_exactly_the_unknown_rows(
        self, scaffold: Any, committed: Any
    ) -> None:
        """The set tracks the `unknown` TYPE, not optionality.

        The two are different sets: a null leaf carries its declared type, so an optional
        row is usually typed, and the one the note must follow is the type.
        """
        unknown = {
            f["name"]
            for fold in committed["folds"]
            if fold["name"] == "status"
            for f in fold["fields"]
            if f["type"] == "unknown"
        }
        assert set(scaffold.unchecked_sources("status")) == unknown


class TestEveryOptionalRowIsTypedToday:
    """No row in the committed catalogue is `unknown`.

    Every null leaf the folds render is declared in `dashboard_types._NULLABLE`, and the
    catalogue renders that catalog, so no row leaves an author to type it by reading the
    kernel. This case records the state rather than asserting it can never change: a new
    fold rendering an undeclared null makes it red, which is the moment to add the
    declaration or accept the `unknown` deliberately.
    """

    def test_no_committed_row_is_unknown(self, committed: Any) -> None:
        unknown = [
            f"{fold['name']}.{row['name']}"
            for fold in committed["folds"]
            for row in fold["fields"]
            if row["type"] == "unknown"
        ]
        assert unknown == [], (
            "these rows are typed 'unknown'; declare them in dashboard_types._NULLABLE "
            f"or change this case deliberately: {unknown}"
        )

    def test_status_turn_is_an_object_and_so_undeclarable_as_text(self, scaffold: Any) -> None:
        """The row the old limit was named after. Declared `object`, so the text menu
        refuses it instead of rendering 'not said' at every active turn."""
        with pytest.raises(scaffold.ScaffoldError, match="not a text field of this fold"):
            scaffold.parse_fields(["turn:str|unsaid"], texts=scaffold.text_fields("status"))


class TestOneDerivation:
    """The committed artifacts are the ``dashboard_types`` catalog, rendered.

    There were TWO registry-probing catalogs: this script's own
    ``start()``->``render()`` pass, and ``dashboard_types.catalog``. Two probes mean two
    answers, and they had already diverged -- the script typed a field ``unknown``
    wherever the empty fold rendered ``None``, while ``dashboard_types._NULLABLE``
    declares a type for the ones that are declarable, so ``status.opened_at`` read
    ``unknown`` in ``folds.json`` and ``number`` in the catalog.

    Neither gate could see it. ``--check`` compared the committed files against the
    script's OWN probe, and ``test_dashboard_types`` compared the catalog against the
    folds -- so each half agreed with itself. What was missing was a case that reads the
    two against each other, which is this one.
    """

    def _leaf(self, shape: dict[str, Any], field: str) -> dict[str, Any] | None:
        return ((shape.get("properties") or {}).get(field)) if isinstance(shape, dict) else None

    def test_every_committed_row_carries_the_catalog_s_type(self, committed: Any) -> None:
        """Row by row, against ``dashboard_types.catalog``. The one source of types."""
        from kiro_crew import dashboard_types as dt

        shapes = {entry.name: dict(entry.shape) for entry in dt.catalog()}
        disagree: list[str] = []
        for fold in committed["folds"]:
            shape = shapes.get(fold["name"])
            assert shape is not None, f"{fold['name']} is committed but not in the catalog"
            for row in fold["fields"]:
                node = self._leaf(shape, row["name"])
                assert node is not None, f"{fold['name']}.{row['name']} is in no catalog shape"
                if node.get("type") != row["type"]:
                    disagree.append(
                        f"{fold['name']}.{row['name']}: folds.json says {row['type']!r}, "
                        f"the catalog says {node.get('type')!r}"
                    )
        assert disagree == [], "\n".join(disagree)

    def test_the_five_declared_stamps_are_numbers_in_both(self, committed: Any) -> None:
        """The leaves the disagreement was found on, pinned by name.

        ``_NULLABLE`` declares these as ``number`` because the fold assigns
        ``entry.time``, an ``int`` of epoch milliseconds. A committed row that still says
        ``unknown`` sends a template author to type a stamp as a string.
        """
        rows = {
            (fold["name"], row["name"]): row["type"]
            for fold in committed["folds"]
            for row in fold["fields"]
        }
        for key in (("status", "opened_at"), ("status", "closed_at"), ("status", "last_time")):
            assert rows[key] == "number", f"{key[0]}.{key[1]} is {rows[key]!r}"

    def test_the_committed_types_are_the_manifest_vocabulary(self, committed: Any) -> None:
        """Not Python's. A row typed ``str`` is a row no Model field can declare, so the
        author has to translate it in their head and the catalog's own ``field_types``
        are the only list that matters."""
        from kiro_crew import dashboard_types as dt

        allowed = set(dt.describe()["field_types"]) | {dt.UNKNOWN_TYPE}
        seen = {row["type"] for fold in committed["folds"] for row in fold["fields"]}
        assert seen <= allowed, sorted(seen - allowed)

    def test_the_script_no_longer_probes_the_registry_itself(self, generator: Any) -> None:
        """The duplicate is GONE, not merely agreeing today. Two probes that match are
        still two probes, and the next edit to one of them is the next divergence."""
        assert not hasattr(generator, "_rendered"), "the script still has its own probe"
        assert generator.UNKNOWN_TYPE is not None
