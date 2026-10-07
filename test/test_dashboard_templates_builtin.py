"""The built-in dashboard templates, checked against a fold value a pod really served.

A template declares ``{"fold": ..., "path": ...}`` per field and the host resolves it
at run time. Nothing in :mod:`kiro_crew.dashboard_templates.manifest` can tell whether
such a path EXISTS -- it validates the path's spelling, not its target -- so a built-in
can ship a plausible path that resolves to nothing on every crewmate's dashboard, and
the page then renders a blank cell with every gate green.

That is the gap this module closes, and it closes it against
``test/fixtures/dashboard_templates/pod_folds.json``: the eight folds a real pod
session served over
``GET /api/sessions/{slot}/crew-log/projection/{name}`` after two chat turns with tool
calls and an approval, a session ledger written through ``session_ledger_record``, and a
work board opened through ``POST /api/work-ledger/record``. A hand-written fixture would
prove only that the test agrees with itself about the fold shape, which is the mistake
the first contract's own example paths made: it showed ``usage.credits.total`` and
``usage.turns.count``, and the real fold has ``usage.credits`` as a float and no
``count`` under ``usage.turns`` at all.

The fixture is a SNAPSHOT, so it ages. What ages safely is the thing being asserted: a
path missing from it fails here rather than on somebody's dashboard, and a fold that
gains a key leaves these assertions passing. A fold that RENAMES a key is exactly the
break this is for.
"""

from __future__ import annotations

import json
from configparser import ConfigParser
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Iterator

import pytest

from kiro_crew.crew_log.projection import PROJECTION_NAMES, SLOT_PROJECTION_NAMES
from kiro_crew.dashboard_templates import instance
from kiro_crew.dashboard_templates.manifest import (
    FIELD_TYPES,
    MAX_FIELDS,
    FieldSpec,
    ManifestError,
    TemplateManifest,
    load_template,
)

BUILTIN = Path(__file__).resolve().parents[1] / "src/kiro_crew/dashboard_templates/builtin"
FIXTURES = Path(__file__).resolve().parent / "fixtures/dashboard_templates"
FIXTURE = FIXTURES / "pod_folds.json"

#: Folds a built-in reads that the pod capture PREDATES, resolved against
#: ``sample_<fold>.json`` instead. Closed and short on purpose: every entry here is a
#: fold whose value nobody captured from a session, so it is the one place in this module
#: where the checked value was written rather than recorded. An entry earns its place
#: only until the capture is refreshed, and removing one is the cheapest change in this
#: file -- delete the line and the fold is demanded from the capture again.
#:
#: ``workstreams`` is newer than the capture, so no session in it served the fold.
SAMPLED_FOLDS: tuple[str, ...] = ("workstreams",)

#: Built-ins the repo ships. Named rather than discovered, so DELETING one fails here
#: instead of shrinking the matrix to nothing: a parametrisation over a directory listing
#: reports green on an empty directory, which is the one answer that must not look like
#: a pass.
EXPECTED_IDS = frozenset(
    {
        "flow",
        "goal-board",
        "office",
        "org-chart",
        "pr-watch",
        "project-report",
        "roadmap",
        "session-ledger",
        "standup",
        "swimlane",
        "timeline",
        "work-kanban",
    }
)

#: How many fields of one template may be agentic -- a fact no fold records, which the
#: agent writes and the page marks.
#:
#: A CAP rather than a prohibition, because the thing worth protecting is not the
#: count: it is that every NUMBER on the page stays the record's own. The manifest
#: already enforces that structurally, since a field names either a fold path or
#: `agentic` and never both, so an agentic field cannot overwrite a folded value. What
#: the cap buys on top of that is a decision point -- a template reaching for one more
#: judgment field has to come here and say which question no fold can answer.
#:
#: `project-report` holds the three that earned it: the lead's `for_you` lines, its
#: `verdict` (which of six reds is the one that matters is a judgment, and no fold
#: ranks them), and `ci` (the lane board a babysitting worker already reads every cycle
#: and currently throws away; a GitHub-polling fold is the correct source and is not a
#: this-week source).
MAX_AGENTIC_PER_TEMPLATE = 3


def _dirs() -> list[Path]:
    return sorted(p for p in BUILTIN.iterdir() if p.is_dir())


def _segment_match(path: str, pattern: str) -> bool:
    """Match *path* against *pattern* the way setuptools does: per path segment.

    :func:`fnmatch.fnmatch` lets ``*`` swallow a ``/``, so ``dashboard_templates/*.html``
    answers True for a file two directories down -- which is how a package_data entry
    can look like it covers a nested template while the build copies nothing. Comparing
    segment by segment is the question actually being asked.
    """
    parts, globs = path.split("/"), pattern.split("/")
    if len(parts) != len(globs):
        return False
    return all(fnmatch(part, glob) for part, glob in zip(parts, globs))


@pytest.fixture(scope="module")
def pod_folds() -> dict[str, Any]:
    """The captured fold values, keyed by fold name."""
    doc = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return {name: entry["value"] for name, entry in doc["folds"].items()}


@pytest.fixture(scope="module")
def checked_folds(pod_folds: dict[str, Any]) -> dict[str, Any]:
    """Every fold value the cases below resolve paths against: the pod capture, plus a
    named companion sample for any fold the capture PREDATES.

    The capture is a snapshot of one real session, so a fold added after it was taken is
    absent from it through no fault of the template that reads it -- and it cannot be
    re-captured until that fold ships, which is a different change than the page. The
    answer is not to hand-add the fold to the capture: a value typed into
    ``pod_folds.json`` would claim a session served it, and the whole worth of that file
    is that nobody typed it.

    So a newer fold is resolved against ``sample_<fold>.json`` instead, which says in
    its name that it is a sample. That keeps every assertion's teeth -- the paths still
    have to resolve and the types still have to match -- while the two sources stay
    told apart. :data:`SAMPLED_FOLDS` is the closed list, so this is not a hole a future
    template can fall into quietly: a fold reaching here without being listed fails.
    """
    out = dict(pod_folds)
    for fold in SAMPLED_FOLDS:
        path = FIXTURES / f"sample_{fold}.json"
        assert path.exists(), (
            f"{fold!r} is listed as sampled but {path.name} is not there, so the "
            "built-in reading it has no value to check its paths against"
        )
        doc = json.loads(path.read_text(encoding="utf-8"))
        entry = doc.get("folds", {}).get(fold)
        assert isinstance(entry, dict) and "value" in entry, (
            f"{path.name} carries no folds.{fold}.value, which is the shape "
            "pod_folds.json uses and the shape this fixture reads"
        )
        out[fold] = entry["value"]
    return out


@pytest.fixture(scope="module")
def loaded() -> dict[str, tuple[TemplateManifest, str]]:
    """Every built-in, through the shared loader. Fails the module if one is refused."""
    out: dict[str, tuple[TemplateManifest, str]] = {}
    for directory in _dirs():
        manifest, html = load_template(directory)
        out[manifest.id] = (manifest, html)
    return out


def _walk(value: Any, path: str) -> tuple[bool, Any]:
    """Resolve a dotted *path* against *value* the way the host's resolver must.

    Returns ``(found, value)``. ``found`` is False for a key that is absent and for a
    path that tries to walk THROUGH a non-mapping -- the two cases a resolver must
    report rather than paper over, since both end in an empty cell.
    """
    current = value
    for key in path.split("."):
        if not isinstance(current, dict) or key not in current:
            return False, None
        current = current[key]
    return True, current


def _type_of(value: Any) -> str:
    # ``bool`` first: it passes an ``int`` check, and a flag reported as a number is how
    # a toggle ends up rendered as ``1``.
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _fold_fields(manifest: TemplateManifest) -> Iterator[FieldSpec]:
    for spec in manifest.fields.values():
        if spec.fold is not None:
            yield spec


class TestTheFixtureItself:
    """The fixture is the authority every case below leans on, so it is checked first.

    Without these, a fixture truncated to ``{}`` would make every resolution case below
    fail with a confusing message, or -- worse, if a case were written to skip an absent
    fold -- pass over a template whose paths were never checked at all.
    """

    def test_the_fixture_carries_every_fold_a_builtin_reads(
        self, pod_folds: dict[str, Any], loaded: dict[str, tuple[TemplateManifest, str]]
    ) -> None:
        needed: set[str] = set()
        for manifest, _ in loaded.values():
            needed |= set(manifest.folds)
        missing = sorted(needed - set(pod_folds) - set(SAMPLED_FOLDS))
        assert not missing, (
            f"the built-ins read fold(s) the fixture does not carry: {missing}. "
            "Re-capture it from a pod session rather than hand-adding the fold: a "
            "hand-written value proves only that this test agrees with itself. If the "
            "fold is NEWER than the capture and cannot be in it yet, add a "
            "sample_<fold>.json and list it in SAMPLED_FOLDS, which says so in the name."
        )

    def test_every_sampled_fold_is_absent_from_the_capture(self, pod_folds: dict[str, Any]) -> None:
        """The planted failure for the escape hatch above.

        A fold listed in :data:`SAMPLED_FOLDS` that the capture DOES carry would mean a
        recorded value is being shadowed by a written one -- the exact substitution the
        list exists to avoid -- and it would look like nothing at all, because both
        resolve.
        """
        shadowed = sorted(set(SAMPLED_FOLDS) & set(pod_folds))
        assert not shadowed, (
            f"{shadowed} are in the pod capture AND listed as sampled, so a written "
            "value is standing in for a recorded one. Drop them from SAMPLED_FOLDS."
        )

    def test_every_sampled_fold_is_actually_read_by_a_builtin(
        self, loaded: dict[str, tuple[TemplateManifest, str]]
    ) -> None:
        """A stale entry would quietly keep a fold exempt after its reader was deleted."""
        read: set[str] = set()
        for manifest, _ in loaded.values():
            read |= set(manifest.folds)
        unused = sorted(set(SAMPLED_FOLDS) - read)
        assert not unused, f"{unused} are listed as sampled but no built-in reads them"

    def test_every_captured_fold_is_one_the_product_registers(
        self, pod_folds: dict[str, Any]
    ) -> None:
        known = set(PROJECTION_NAMES) | set(SLOT_PROJECTION_NAMES)
        unknown = sorted(set(pod_folds) - known)
        assert not unknown, f"fixture carries fold(s) the product does not register: {unknown}"

    def test_the_fixture_is_not_an_empty_session(self, pod_folds: dict[str, Any]) -> None:
        """A pod whose turns never ran would capture every fold at its empty value, and
        every resolution below would still pass -- an empty dict resolves no path, so the
        cases would fail, but a fold of all-zero SCALARS resolves fine and proves nothing
        about an array path. So the capture is required to hold real work."""
        assert pod_folds["status"]["turns_completed"] >= 1
        assert pod_folds["tools"]["calls"] >= 1
        assert len(pod_folds["timeline"]["moments"]) >= 1
        assert pod_folds["ledger"]["goal"]
        assert pod_folds["work"]["items"], "the work board captured no item"


@pytest.mark.parametrize("directory", _dirs(), ids=lambda p: p.name)
class TestEachBuiltinLoads:
    def test_the_shared_loader_accepts_it(self, directory: Path) -> None:
        """``load_template`` is the one gate a user template also passes, so a built-in
        that needs an exception to it is a built-in in the wrong format."""
        try:
            manifest, html = load_template(directory)
        except ManifestError as exc:  # pragma: no cover - the failure path is the message
            pytest.fail(f"{directory.name}: {exc}")
        assert manifest.id == directory.name, (
            f"{directory.name}: the manifest calls itself {manifest.id!r}; the registry "
            "discovers these by directory name, so the two must agree"
        )
        assert manifest.source == "builtin"
        assert html.strip()

    def test_it_declares_no_more_agentic_fields_than_the_cap(self, directory: Path) -> None:
        manifest, _ = load_template(directory)
        agentic = sorted(n for n, f in manifest.fields.items() if f.agentic)
        assert len(agentic) <= MAX_AGENTIC_PER_TEMPLATE, (
            f"{manifest.id}: {agentic} are agentic; at most "
            f"{MAX_AGENTIC_PER_TEMPLATE} may be, and only for a question no fold "
            "answers -- raising this cap is a decision, not a formality"
        )

    def test_its_script_reaches_no_network(self, directory: Path) -> None:
        """The frame's CSP blocks network, so a page that fetches renders a hole.

        Checked on the SOURCE rather than left to the CSP: a template whose chart never
        appears is indistinguishable from one whose data was empty, and the author of the
        next template copies whichever one is in the tree.
        """
        _, html = load_template(directory)
        lowered = html.lower()
        for forbidden in ("<script src", "fetch(", "xmlhttprequest", "importscripts", "//cdn."):
            assert (
                forbidden not in lowered
            ), f"{directory.name}: reaches the network via {forbidden!r}"

    def test_its_script_reads_the_hosts_own_field_bag(self, directory: Path) -> None:
        """A page drawing a chart must read ``window.kirocrew.fields``.

        The bound elements are filled by the host whether or not any script runs, so a
        page could pass parity while its charts drew from nothing. Every built-in here
        draws, so every one of them reads the bag.
        """
        _, html = load_template(directory)
        assert "window.kirocrew" in html, (
            f"{directory.name}: no script reads window.kirocrew, so whatever it draws "
            "is not drawn from the host's values"
        )


class TestEveryFoldPathResolves:
    """The case this module exists for."""

    def test_each_path_is_present_in_the_captured_fold(
        self, checked_folds: dict[str, Any], loaded: dict[str, tuple[TemplateManifest, str]]
    ) -> None:
        unresolved: list[str] = []
        for tid, (manifest, _) in sorted(loaded.items()):
            for spec in _fold_fields(manifest):
                assert spec.fold is not None and spec.path is not None
                fold = checked_folds.get(spec.fold)
                found, _value = _walk(fold, spec.path)
                if not found:
                    unresolved.append(f"{tid}.{spec.name} -> {spec.fold}.{spec.path}")
        assert not unresolved, (
            "fold path(s) that resolve to nothing on a real session, so the cell is "
            f"blank at every render: {unresolved}"
        )

    def test_each_resolved_value_has_the_declared_type(
        self, checked_folds: dict[str, Any], loaded: dict[str, tuple[TemplateManifest, str]]
    ) -> None:
        """A path that resolves is not enough: the host type-checks the value, and a
        mismatch is a refusal rather than a wrong-looking cell. A ``number`` field
        pointed at an object fails here instead of at publish time."""
        wrong: list[str] = []
        for tid, (manifest, _) in sorted(loaded.items()):
            for spec in _fold_fields(manifest):
                assert spec.fold is not None and spec.path is not None
                found, value = _walk(checked_folds.get(spec.fold), spec.path)
                if not found:
                    continue  # the case above owns this failure
                # ``None`` is what a fold renders for a measurement nobody reported --
                # ``status.last_stop_reason`` before a turn ends, ``work`` item fields a
                # conductor has not written. It is the ABSENCE of the declared type, not
                # another type, so it cannot be judged against one here. The page's own
                # job is to draw that as a dash, which is why every built-in reads its
                # values through a helper that answers for null.
                if value is None:
                    continue
                got = _type_of(value)
                if got != spec.type:
                    wrong.append(
                        f"{tid}.{spec.name} declares {spec.type!r} but "
                        f"{spec.fold}.{spec.path} served {got!r}"
                    )
        assert not wrong, "; ".join(wrong)

    def test_a_wrong_path_is_actually_caught(self, pod_folds: dict[str, Any]) -> None:
        """The planted failure. Without it, a resolver bug that answered "found" for
        everything would make the two cases above pass over every built-in."""
        found, _ = _walk(pod_folds["usage"], "credits.total")
        assert not found, (
            "usage.credits.total resolved, so the resolver walks through a float; the "
            "cases above would then pass on any path at all"
        )
        found, _ = _walk(pod_folds["usage"], "turns.count")
        assert not found, "usage.turns.count resolved; the real key is turns.completed"
        found, value = _walk(pod_folds["usage"], "credits")
        assert found and isinstance(value, float), "the control path did not resolve"


class TestTheyActuallyShip:
    """A built-in absent from the wheel is the quietest failure in this feature.

    Its page and its manifest are DATA files. ``pip`` copies a data file only when a
    ``[options.package_data]`` glob names it, and every test here reads the
    package-relative source tree -- which exists in a checkout whether or not the build
    would have copied anything. So the whole suite stays green while the registry
    discovers no built-in at all on any pip, PyPI or DMG install.

    Both file kinds are checked, because the two fail differently and only one of them
    is obvious: a directory with no page is nothing, while a directory whose manifest
    did not ship is refused by ``load_template`` with a message about a missing file
    that is present in every developer's checkout.
    """

    @staticmethod
    def _package_data_globs() -> list[str]:
        config = ConfigParser()
        config.read(Path(__file__).resolve().parents[1] / "setup.cfg")
        raw = config.get("options.package_data", "kiro_crew", fallback="")
        return [
            line.strip()
            for line in raw.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]

    @pytest.mark.parametrize("filename", ["template.html", "manifest.json"])
    def test_a_glob_covers_every_builtins_files(self, filename: str) -> None:
        globs = self._package_data_globs()
        probe = f"dashboard_templates/builtin/example/{filename}"
        # fnmatch's ``*`` crosses a path separator and setuptools' does not, so the
        # probe is matched against a SEGMENT-WISE test: a glob that only passes because
        # ``*`` swallowed a slash would ship nothing.
        assert any(_segment_match(probe, glob) for glob in globs), (
            f"no [options.package_data] glob covers a built-in's {filename}, so pip "
            f"installs the loader without the templates. Globs: {globs}"
        )

    def test_the_control_glob_does_not_match_everything(self) -> None:
        """Without this, a ``*`` entry would satisfy the case above for the wrong reason."""
        globs = self._package_data_globs()
        assert not any(_segment_match("elsewhere/example/manifest.json", glob) for glob in globs)

    def test_the_sdist_includes_the_manifests(self) -> None:
        """The wheel is built from the sdist, so a file missing from MANIFEST.in is
        missing everywhere even with the package_data glob above in place."""
        text = (Path(__file__).resolve().parents[1] / "MANIFEST.in").read_text(encoding="utf-8")
        assert "recursive-include src/kiro_crew/dashboard_templates/builtin *.json" in text, (
            "MANIFEST.in does not include the built-in manifests, so the sdist ships "
            "pages without them and load_template refuses every built-in"
        )


class TestTheSetOfBuiltins:
    def test_the_shipped_ids_are_the_expected_ones(
        self, loaded: dict[str, tuple[TemplateManifest, str]]
    ) -> None:
        assert set(loaded) == EXPECTED_IDS

    def test_no_two_builtins_claim_one_id(self) -> None:
        """``load_template`` checks one directory, so nothing it does can see a
        collision. The registry keys by id, so a duplicate silently wins or loses."""
        ids = [json.loads((d / "manifest.json").read_text(encoding="utf-8"))["id"] for d in _dirs()]
        assert len(ids) == len(set(ids)), f"duplicate built-in id(s) in {ids}"

    def test_every_builtin_stays_under_the_field_cap(
        self, loaded: dict[str, tuple[TemplateManifest, str]]
    ) -> None:
        for tid, (manifest, _) in sorted(loaded.items()):
            assert len(manifest.fields) <= MAX_FIELDS, f"{tid}: {len(manifest.fields)} fields"

    def test_every_declared_type_is_one_the_host_knows(
        self, loaded: dict[str, tuple[TemplateManifest, str]]
    ) -> None:
        for tid, (manifest, _) in sorted(loaded.items()):
            for spec in manifest.fields.values():
                assert spec.type in FIELD_TYPES, f"{tid}.{spec.name}: {spec.type!r}"

    def test_the_set_covers_the_slot_folds(
        self, loaded: dict[str, tuple[TemplateManifest, str]]
    ) -> None:
        """The slot-keyed folds are where a long-running crewmate's work actually lives,
        and the next author copies what is in the tree -- so the shipped set has to
        demonstrate reading them rather than leaving them to a first-time author.

        Asked about the SLOT half only, which is deliberate rather than an omission. A
        crewmate's boards, its spend and its ledger belong to the CREWMATE and are
        spread over every session it ran under, so a session-keyed fold answers for a
        slice and a reader cannot tell a slice from the whole. A built-in reading a
        session fold is allowed and simply not required.
        """
        folds: set[str] = set()
        for manifest, _ in loaded.values():
            folds |= set(manifest.folds)
        assert folds, "no built-in reads any fold, so none of them shows the record"
        assert folds & set(SLOT_PROJECTION_NAMES), "no built-in reads a slot fold"
        unknown = sorted(folds - (set(PROJECTION_NAMES) | set(SLOT_PROJECTION_NAMES)))
        assert not unknown, (
            f"built-in(s) read fold(s) the product does not register: {unknown}. "
            "The host resolves a fold by name through the projection registry, so a "
            "name absent from it is a cell that is blank at every render."
        )


# ------------------------------------ every shipped page can actually be adopted


class TestEveryBuiltinSurvivesPreviewAndApply:
    """A page that ships and cannot be adopted is a page nobody can reach.

    The suite above proves each built-in LOADS. Loading is not adopting: a crewmate
    gets a page by ``stage_preview`` then ``apply_preview``, and both run
    ``instance._check`` -- which applies a size ceiling the loader never asked about.

    THE BUG this closes. ``MAX_INSTANCE_HTML_BYTES`` was its own number, 64 KiB, while
    ``dashboard_frame.MAX_PAGE_BYTES`` -- the ceiling the document that renders the page
    is actually assembled under -- is 1,000,000. ``project-report`` is over 147,000
    bytes and is ``DEFAULT_TEMPLATE_ID``, so preview refused it with "over the
    65536-byte ceiling": a crewmate that adopted any other page could never come back
    to the one every crewmate starts on, and the only way to see the default was never
    to have left it.

    Asked over the WHOLE shipped set and not about the one page that was too big. The
    next template somebody writes is the one that trips a ceiling nothing here states,
    and a case naming ``project-report`` would pass while it did.
    """

    @pytest.fixture(autouse=True)
    def _home(self, tmp_path, _floor_monkeypatch):
        """An isolated data home. The real built-in directory, deliberately.

        A fixture template would test the machinery against a page this test wrote,
        and the claim is about the pages that SHIP.
        """
        _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
        yield

    def test_the_instance_ceiling_is_the_frames_own_constant(self) -> None:
        """One number, read from the module that pays for it.

        Two independent ceilings is what let the store refuse a page the frame would
        draw. Asserted as identity against the frame's constant rather than against a
        literal: a test carrying its own copy of the number would pass while the two
        drifted apart again.
        """
        from kiro_crew import dashboard_frame

        assert instance.MAX_INSTANCE_HTML_BYTES == dashboard_frame.MAX_PAGE_BYTES

    def test_every_builtin_previews_and_applies(self) -> None:
        """The full round trip, per shipped page, through the real store."""
        ids = sorted(load_template(d)[0].id for d in _dirs())
        assert ids, "no built-in templates to adopt"
        refused: list[str] = []
        for index, template_id in enumerate(ids):
            slug = f"adopt-probe-{index}"
            try:
                instance.stage_preview(slug, template_id=template_id)
                record = instance.apply_preview(slug, session_id="")
            except instance.InstanceError as exc:
                refused.append(f"{template_id}: {exc}")
                continue
            if record.template_id != template_id or record.instance_version != 1:
                refused.append(
                    f"{template_id}: applied as {record.template_id!r} "
                    f"v{record.instance_version}"
                )
        assert not refused, "shipped template(s) that cannot be adopted:\n  " + "\n  ".join(refused)

    def test_the_default_is_among_them(self) -> None:
        """The case the bug was actually about, named so the failure reads plainly.

        ``DEFAULT_TEMPLATE_ID`` is what a crewmate that adopted nothing renders, so it
        is the page a reader is most likely to want back.
        """
        ids = {load_template(d)[0].id for d in _dirs()}
        assert instance.DEFAULT_TEMPLATE_ID in ids, (
            f"{instance.DEFAULT_TEMPLATE_ID!r} does not ship, so a crewmate that "
            "adopted nothing renders a template the registry cannot serve"
        )
        instance.stage_preview("default-probe", template_id=instance.DEFAULT_TEMPLATE_ID)
        record = instance.apply_preview("default-probe", session_id="")
        assert record.template_id == instance.DEFAULT_TEMPLATE_ID
        assert record.instance_version == 1

    def test_a_page_over_the_shared_ceiling_is_still_refused(self) -> None:
        """The ceiling is RAISED to the frame's, not removed.

        Without this, the fix reads as "stop checking the size" -- and a store that
        accepts any page keeps ``MAX_RETAINED_VERSIONS`` copies of it per crewmate.
        Driven through ``_check`` because nothing that ships is this big.
        """
        directory = _dirs()[0]
        raw = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        html = (directory / "template.html").read_text(encoding="utf-8")
        over = html + "<!-- " + "x" * instance.MAX_INSTANCE_HTML_BYTES + " -->"
        with pytest.raises(instance.InstanceRefused) as refused:
            instance._check(raw, over)
        assert str(instance.MAX_INSTANCE_HTML_BYTES) in str(refused.value)

    def test_rollback_reaches_the_default_again_after_switching_away(self) -> None:
        """THE READER'S OWN JOURNEY, which is where the bug was felt.

        Adopt the default, switch to another page, then go back. The rollback restores
        the default's payload FROM DISK and re-checks it on the way in, so it runs the
        same ceiling the preview does -- which is why this is a second case and not a
        restatement of the one above: a rollback is the path a reader takes when they
        have already left, and it was equally shut.

        Forward, like every rollback: version 1 restored over version 2 becomes
        version 3, so going back is itself undoable.
        """
        other = next(
            load_template(d)[0].id
            for d in _dirs()
            if load_template(d)[0].id != instance.DEFAULT_TEMPLATE_ID
        )
        slug = "rollback-probe"
        default_html = next(
            html
            for d in _dirs()
            for manifest, html in [load_template(d)]
            if manifest.id == instance.DEFAULT_TEMPLATE_ID
        )

        instance.stage_preview(slug, template_id=instance.DEFAULT_TEMPLATE_ID)
        first = instance.apply_preview(slug, session_id="")
        assert first.instance_version == 1

        instance.stage_preview(slug, template_id=other)
        second = instance.apply_preview(slug, session_id="")
        assert second.instance_version == 2 and second.template_id == other

        back = instance.rollback(slug, 1, session_id="")
        assert back.instance_version == 3, "a rollback writes a NEW version"
        assert back.template_id == instance.DEFAULT_TEMPLATE_ID
        assert back.html == default_html, "the restored page is not the default's own"
