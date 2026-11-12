"""The built-in dashboard templates, checked against a fold value a pod really served.

The manifest validates a ``{"fold": ..., "path": ...}`` path's spelling, not its target,
so a built-in could ship a path that renders a blank cell with every gate green.

Paths resolve against ``test/fixtures/dashboard_templates/pod_folds.json``, the folds a
real pod session served over ``GET /api/sessions/{slot}/crew-log/projection/{name}``.
A hand-written fixture would only agree with itself about the fold shape. The fixture is
a snapshot: an added key still passes, a RENAMED key fails here.
"""

from __future__ import annotations

import json
import shutil
import subprocess
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
from kiro_crew.subprocess_utf8 import UTF8_TEXT

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
#: ``panel`` was never in it: the capture's session called no ``panel_publish``.
SAMPLED_FOLDS: tuple[str, ...] = ("workstreams", "panel")

#: Built-ins the repo ships. Named rather than discovered, so DELETING one fails here
#: instead of shrinking the matrix to nothing: a parametrisation over a directory listing
#: reports green on an empty directory, which is the one answer that must not look like
#: a pass.
EXPECTED_IDS = frozenset(
    {
        "flow",
        "goal-board",
        "kirocrew-conductor",
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

#: How many fields of one template may be agentic (written by the agent, no fold).
#:
#: A cap, not a ban: the manifest already stops an agentic field overwriting a folded
#: value, so the cap is a decision point for each new judgment field. `project-report`
#: holds three: `for_you`, `verdict` (which red matters is a judgment) and `ci`.
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
    """Every fold value the cases resolve against: the pod capture plus named samples.

    A fold newer than the capture resolves against ``sample_<fold>.json``; typing it into
    ``pod_folds.json`` would claim a real session served it. :data:`SAMPLED_FOLDS` is the
    closed list, so an unlisted fold reaching here fails.
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
    """Every built-in's page and manifest are named by a ``[options.package_data]`` glob.

    Tests read the source tree, so they stay green while an install ships no built-in.
    Both kinds are checked: a missing manifest fails ``load_template`` confusingly.
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
        """The shipped set reads the slot-keyed folds, where a crewmate's work lives.

        Slot only: a session-keyed fold answers for one slice of a crewmate's sessions.
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
    """Every built-in passes ``stage_preview`` and ``apply_preview``, not only the loader.

    Both run ``instance._check``, whose size ceiling must fit ``MAX_PAGE_BYTES``:
    ``project-report`` (the default) is over 147,000 bytes. Asked over the whole set.
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

    def test_a_page_exactly_at_the_shared_ceiling_is_kept_and_one_byte_more_is_not(
        self,
    ) -> None:
        """The ceiling's own boundary, on the check preview, apply and rollback all run.

        A shipped page padded to exactly the frame's ceiling is a page an operator can
        put in a template folder: it previews and applies. One byte past it is refused
        with the ceiling named.
        """
        directory = _dirs()[0]
        raw = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        html = (directory / "template.html").read_text(encoding="utf-8")
        frame = "<!--  -->"
        room = instance.MAX_INSTANCE_HTML_BYTES - len((html + frame).encode("utf-8"))
        at = html + "<!-- " + "x" * room + " -->"
        assert len(at.encode("utf-8")) == instance.MAX_INSTANCE_HTML_BYTES
        assert instance._check(raw, at) == raw
        with pytest.raises(instance.InstanceRefused) as refused:
            instance._check(raw, html + "<!-- " + "x" * (room + 1) + " -->")
        said = str(refused.value)
        assert f"is {instance.MAX_INSTANCE_HTML_BYTES + 1} bytes" in said
        assert f"over the {instance.MAX_INSTANCE_HTML_BYTES}-byte ceiling" in said

    def test_rollback_reaches_the_default_again_after_switching_away(self) -> None:
        """Adopt the default, switch away, then roll back to it.

        A rollback re-checks the payload from disk under the same ceiling as preview.
        Version 1 restored over version 2 becomes version 3.
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


# ------------------------------------ the flow page counts the items the fold forgot

#: A DOM just wide enough to run the ``flow`` page's script as far as its tiles. Each
#: element records what is written to it and nothing more, with ``textContent`` behaving
#: the way a browser's does (a write replaces the children, a read joins them).
_FLOW_DOM = r"""
class El {
  constructor(tag) { this.tag = tag; this.children = []; this.className = ''; this.style = {}; this._t = ''; }
  get textContent() { return this._t + this.children.map((c) => c.textContent).join(''); }
  set textContent(v) { this._t = String(v); this.children = []; }
  appendChild(c) { this.children.push(c); return c; }
  setAttribute() {}
  getAttribute() { return null; }
}
const byId = {};
for (const id of ['kpis', 'burn', 'cfd', 'burnax', 'cfdax', 'cfdlegend']) byId[id] = new El('div');
globalThis.window = globalThis;
globalThis.addEventListener = () => {};
globalThis.document = {
  body: { querySelectorAll: () => [] },
  querySelectorAll: () => [],
  querySelector: () => null,
  getElementById: (id) => byId[id] || null,
  createElement: (tag) => new El(tag),
  createElementNS: (ns, tag) => new El(tag),
  createTextNode: (s) => { const n = new El('#text'); n.textContent = s; return n; },
};
"""

#: What the page drew into its tile row, one record per tile.
_FLOW_PROBE = r"""
console.log(JSON.stringify(byId.kpis.children.map((card) => ({
  value: card.children[1]._t,
  unit: card.children[1].children.map((c) => c.textContent).join(''),
  fill: card.children[2] ? card.children[2].children[0].style.width : null,
}))));
"""

_ABSENT = object()


def _flow_item(state: str) -> dict[str, Any]:
    """One ``work`` item with only what the tiles read. It carries no stamps, so the
    page reports that its charts cannot be drawn and stops after the tiles."""
    return {"item_id": f"it-{state}", "title": f"a {state} task", "state": state}


def _flow_tiles(html: str, fields: dict[str, Any]) -> list[dict[str, Any]]:
    """Run the page's own script over *fields* and return the tiles it drew."""
    node = shutil.which("node")
    if node is None:  # pragma: no cover - CI builds the website, so node is present
        pytest.fail(
            "node is required to run the flow page's script; the tile assertions cannot be skipped"
        )
    script = html.split("<script>")[1].split("</script>")[0]
    program = "\n".join(
        (
            f"globalThis.kirocrew = {{ fields: {json.dumps(fields)} }};",
            _FLOW_DOM,
            script,
            _FLOW_PROBE,
        )
    )
    done = subprocess.run([node, "-"], input=program, capture_output=True, timeout=60, **UTF8_TEXT)
    assert done.returncode == 0, done.stderr
    return list(json.loads(done.stdout.strip().splitlines()[-1]))


class TestFlowCountsTheItemsTheFoldForgot:
    """The ``flow`` page's tiles read the whole board, not only the part the fold lists.

    The ``work`` fold keeps the open items and the newest closed ones. A closed item past
    that window leaves ``items`` and is counted in ``forgotten``, so a tile that counts
    ``items`` alone shrinks as a goal runs long. The page's script is RUN here over a
    field bag: "the tile reads 5 of 7" is a claim about what executes, and a source
    pattern holding the word ``forgotten`` is satisfied by a page that never adds it.
    """

    def test_it_binds_forgotten_the_way_goal_board_binds_it(
        self, loaded: dict[str, tuple[TemplateManifest, str]]
    ) -> None:
        flow, html = loaded["flow"]
        assert "forgotten" in flow.fields, "flow does not bind the work fold's forgotten counts"
        mine = flow.fields["forgotten"]
        theirs = loaded["goal-board"][0].fields["forgotten"]
        assert (mine.type, mine.fold, mine.path, mine.agentic) == (
            theirs.type,
            theirs.fold,
            theirs.path,
            theirs.agentic,
        )
        assert 'data-dashboard-field="forgotten"' in html

    def test_the_accepted_tile_adds_the_forgotten_items(
        self, loaded: dict[str, tuple[TemplateManifest, str]]
    ) -> None:
        accepted, still_open, rejected = _flow_tiles(
            loaded["flow"][1],
            {
                "items": [_flow_item("accepted"), _flow_item("accepted"), _flow_item("open")],
                "forgotten": {
                    "created": 7,
                    "items": 4,
                    "accepted": 3,
                    "rejected": 1,
                    "abandoned": 0,
                    "in_round": 0,
                },
            },
        )
        assert (accepted["value"], accepted["unit"]) == ("5", "of 7")
        assert float(accepted["fill"].rstrip("%")) == pytest.approx(5 / 7 * 100)
        assert still_open["value"] == "1"
        assert rejected["value"] == "1"

    def test_the_rejected_tile_adds_the_forgotten_rejected_and_abandoned(
        self, loaded: dict[str, tuple[TemplateManifest, str]]
    ) -> None:
        accepted, _, rejected = _flow_tiles(
            loaded["flow"][1],
            {
                "items": [_flow_item("rejected")],
                "forgotten": {"items": 3, "accepted": 0, "rejected": 2, "abandoned": 1},
            },
        )
        assert rejected["value"] == "4"
        assert (accepted["value"], accepted["unit"]) == ("0", "of 4")

    @pytest.mark.parametrize(
        "forgotten",
        [
            pytest.param(_ABSENT, id="absent"),
            pytest.param(None, id="null"),
            pytest.param("many", id="string"),
            pytest.param([3], id="array"),
            pytest.param({"items": -2, "accepted": "5", "rejected": None}, id="not-counts"),
        ],
    )
    def test_a_missing_or_odd_forgotten_reads_as_zeros(
        self, loaded: dict[str, tuple[TemplateManifest, str]], forgotten: Any
    ) -> None:
        fields: dict[str, Any] = {
            "items": [_flow_item("accepted"), _flow_item("accepted"), _flow_item("open")]
        }
        if forgotten is not _ABSENT:
            fields["forgotten"] = forgotten
        accepted, still_open, rejected = _flow_tiles(loaded["flow"][1], fields)
        assert (accepted["value"], accepted["unit"]) == ("2", "of 3")
        assert (still_open["value"], rejected["value"]) == ("1", "0")
