"""docs-lint's planted-defect self-test, run by ``python3 scripts/docs_lint.py --test``.

``_self_test`` builds a throwaway tree per probe, plants one defect, runs the whole lint
over it and asserts that the check meant to catch it fires; each immunity probe plants a
legitimate shape and asserts that nothing does. ``scripts/docs_lint.py`` imports
``_self_test`` from here for ``--test``, and this module takes ``run``, the baseline
filter and the check ids from it. CI runs the self-test before the lint itself, so a
check that has stopped firing fails there instead of reading as a green gate.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from docs_lint import (
    _HARNESS_INVARIANT_DOC,
    CHECK_COUPLING_COMPLETENESS,
    CHECK_DEAD_IDENTIFIER,
    CHECK_FENCED_PATH,
    CHECK_LINE_REF,
    CHECK_PATH_EXISTS,
    CHECK_TABLE_ROW_MERGE,
    apply_baseline,
    run,
)


def _self_test() -> int:
    """Plant a defect per check and assert the check catches it.

    A gate nobody has proven can fail is a gate that silently passes forever.
    """
    failures = 0

    def probe(label: str, build) -> None:
        nonlocal failures
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "docs").mkdir(parents=True)
            # A minimal healthy tree: an index that links its one doc.
            (root / "docs" / "README.md").write_text("# Docs\n\n- [Ok](ok.md)\n", encoding="utf-8")
            (root / "docs" / "ok.md").write_text("# Ok\n\nBody.\n", encoding="utf-8")
            expected = build(root)
            if expected is None:
                # The planter could not create its defect on this host (an
                # unprivileged Windows shell cannot make a symlink). That is a
                # gap in what THIS run proved, said out loud -- not a check
                # that failed to fire, which is what a FAIL here would claim.
                print(f"  skip {label}: host cannot plant this defect")
                return
            findings = run(root)
            got = getattr(findings, expected)
            if got:
                print(f"  ok  {label} detected")
            else:
                print(f"  FAIL {label} NOT detected")
                failures += 1

    def clean_probe() -> None:
        nonlocal failures
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "docs").mkdir(parents=True)
            (root / "docs" / "README.md").write_text("# Docs\n\n- [Ok](ok.md)\n", encoding="utf-8")
            (root / "docs" / "ok.md").write_text("# Ok\n\nBody.\n", encoding="utf-8")
            findings = run(root)
            if findings.total() == 0:
                print("  ok  healthy tree reports clean")
            else:
                print(f"  FAIL healthy tree reported {findings.total()} finding(s)")
                failures += 1

    def plant_broken_link(root: Path) -> str:
        (root / "docs" / "ok.md").write_text("# Ok\n\nSee [gone](nope.md).\n", encoding="utf-8")
        return "broken_links"

    def plant_broken_html_link(root: Path) -> str:
        # GitHub renders inline HTML, and the repo README uses <a href> badges for
        # its most prominent links, so these must be checked too.
        (root / "docs" / "ok.md").write_text(
            '# Ok\n\n<a href="nope.md"><img src="x.svg" alt="badge"></a>\n', encoding="utf-8"
        )
        return "broken_links"

    def plant_unreachable(root: Path) -> str:
        (root / "docs" / "orphan.md").write_text("# Orphan\n\nBody.\n", encoding="utf-8")
        return "unreachable"

    def plant_missing_index(root: Path) -> str:
        sub = root / "docs" / "sub"
        sub.mkdir()
        (sub / "page.md").write_text("# Page\n\nBody.\n", encoding="utf-8")
        # Link it so the finding is specifically the absent index.
        (root / "docs" / "README.md").write_text(
            "# Docs\n\n- [Ok](ok.md)\n- [Page](sub/page.md)\n", encoding="utf-8"
        )
        return "missing_index"

    def plant_changelog_preamble(root: Path) -> str:
        (root / "docs" / "ok.md").write_text(
            "# Ok\n\nLast Updated: 2026-01-01\n\nBody.\n", encoding="utf-8"
        )
        return "changelog_preamble"

    def plant_conflict_marker(root: Path) -> str:
        # The separator alone, which is what a half-finished resolution leaves
        # behind once the labelled <<< and >>> lines have been deleted.
        (root / "docs" / "ok.md").write_text(
            "# Ok\n\n- kept bullet\n=======\n- other side\n", encoding="utf-8"
        )
        return "conflict_markers"

    def plant_conflict_marker_head(root: Path) -> str:
        (root / "docs" / "ok.md").write_text(
            "# Ok\n\n<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> branch\n", encoding="utf-8"
        )
        return "conflict_markers"

    def plant_conflict_marker_uncurated(root: Path) -> str:
        # Archival trees are exempt from the style checks but not from corruption.
        archive = root / "docs" / "archive"
        archive.mkdir()
        (archive / "old.md").write_text("# Old\n\nkept\n=======\nother\n", encoding="utf-8")
        return "conflict_markers"

    def plant_phantom_ref(root: Path) -> str:
        pkg = root / "src" / "kiro_crew"
        pkg.mkdir(parents=True)
        (pkg / "mod.py").write_text(
            '"""Spec: ``docs/system-specs/modules/ghost.md``."""\n', encoding="utf-8"
        )
        return "phantom_refs"

    def plant_unknown_harness_invariant(root: Path) -> str:
        spec = root / _HARNESS_INVARIANT_DOC
        spec.parent.mkdir(parents=True)
        spec.write_text("| Id | Rule |\n|---|---|\n| H1 | One |\n", encoding="utf-8")
        source = root / "src" / "kiro_crew"
        source.mkdir(parents=True)
        (source / "harness.py").write_text("# H99: missing invariant\n", encoding="utf-8")
        return "unknown_harness_invariants"

    def plant_phantom_source_path(root: Path) -> str:
        # A module spec naming a file that exists nowhere -- the case the class
        # reports. The doc has to sit in the scoped tree to be checked at all.
        pkg = root / "src" / "kiro_crew" / "acp"
        pkg.mkdir(parents=True)
        (pkg / "real.py").write_text("x = 1\n", encoding="utf-8")
        spec_dir = root / "docs" / "system-specs" / "modules"
        spec_dir.mkdir(parents=True)
        (root / "docs" / "README.md").write_text(
            "# Docs\n\n- [Ok](ok.md)\n- [Spec](system-specs/modules/thing.md)\n",
            encoding="utf-8",
        )
        (spec_dir / "thing.md").write_text(
            "# Thing\n\nThe handler lives in `acp/ghost.py`.\n", encoding="utf-8"
        )
        return "phantom_source_paths"

    def plant_citation_of_a_symlinked_file(root: Path) -> str | None:
        # A symlink is an arbitrary read primitive in a tree a fork PR controls, so
        # it is never indexed -- which makes a citation naming it UNRESOLVABLE, and
        # in a module spec that is a finding. Pointed at a real file in-tree, so
        # the probe proves the exclusion rather than the hazard.
        pkg = root / "src" / "kiro_crew"
        pkg.mkdir(parents=True)
        (pkg / "real.py").write_text("a = 1\nb = 2\n", encoding="utf-8")
        try:
            (pkg / "linked.py").symlink_to(pkg / "real.py")
        except (OSError, NotImplementedError):  # pragma: no cover - Windows
            # No link, no defect: returning the check name here would make
            # the probe report a check that "did not fire" on a tree that
            # never contained what it looks for.
            return None
        spec_dir = root / "docs" / "system-specs" / "modules"
        spec_dir.mkdir(parents=True)
        (root / "docs" / "README.md").write_text(
            "# Docs\n\n- [Ok](ok.md)\n- [Spec](system-specs/modules/thing.md)\n",
            encoding="utf-8",
        )
        (spec_dir / "thing.md").write_text(
            "# Thing\n\nSee `kiro_crew/linked.py:900`.\n", encoding="utf-8"
        )
        return "phantom_source_paths"

    def plant_stale_line_cite(root: Path) -> str:
        pkg = root / "src" / "kiro_crew"
        pkg.mkdir(parents=True)
        (pkg / "small.py").write_text("a = 1\nb = 2\n", encoding="utf-8")
        (root / "docs" / "ok.md").write_text("# Ok\n\nSee `small.py:900`.\n", encoding="utf-8")
        return "stale_line_cites"

    def plant_absurd_line_number(root: Path) -> str:
        # 4301 digits: past CPython's int(str) cap, so converting it raises and
        # would abort the whole gate on input a fork PR controls.
        pkg = root / "src" / "kiro_crew"
        pkg.mkdir(parents=True)
        (pkg / "small.py").write_text("a = 1\n", encoding="utf-8")
        (root / "docs" / "ok.md").write_text(
            "# Ok\n\nSee `small.py:" + "9" * 4301 + "`.\n", encoding="utf-8"
        )
        return "stale_line_cites"

    def plant_line_zero(root: Path) -> str:
        # Files are 1-indexed, so `:0` reads as precise and points at nothing.
        pkg = root / "src" / "kiro_crew"
        pkg.mkdir(parents=True)
        (pkg / "small.py").write_text("a = 1\n", encoding="utf-8")
        (root / "docs" / "ok.md").write_text("# Ok\n\nSee `small.py:0`.\n", encoding="utf-8")
        return "stale_line_cites"

    def plant_stale_line_cite_in_an_uncurated_tree(root: Path) -> str:
        # An archive is exempt from CURATION -- indexes, style -- but a reader who
        # opens it still follows its line numbers, so the line check applies.
        pkg = root / "src" / "kiro_crew"
        pkg.mkdir(parents=True)
        (pkg / "small.py").write_text("a = 1\nb = 2\n", encoding="utf-8")
        archive = root / "docs" / "archive"
        archive.mkdir()
        (archive / "old.md").write_text("# Old\n\nSee `small.py:900`.\n", encoding="utf-8")
        return "stale_line_cites"

    def plant_stale_line_cite_in_an_rfc(root: Path) -> str:
        # The ``_PATH_CITE_DOC_PREFIXES`` exemption must NOT carry the line check with
        # it: a line number is a claim about code that exists now, even inside a
        # proposal.
        pkg = root / "src" / "kiro_crew"
        pkg.mkdir(parents=True)
        (pkg / "small.py").write_text("a = 1\nb = 2\n", encoding="utf-8")
        rfc = root / "docs" / "request-for-change"
        rfc.mkdir()
        (root / "docs" / "README.md").write_text(
            "# Docs\n\n- [Ok](ok.md)\n- [Rfc](request-for-change/rfc-x.md)\n", encoding="utf-8"
        )
        (rfc / "rfc-x.md").write_text("# Rfc\n\nToday: `small.py:900`.\n", encoding="utf-8")
        return "stale_line_cites"

    def plant_stale_line_range(root: Path) -> str:
        # The END of a range is what must be inside the file: a range whose start
        # is valid and whose end is past EOF is still a citation into nothing.
        pkg = root / "src" / "kiro_crew"
        pkg.mkdir(parents=True)
        (pkg / "small.py").write_text("a = 1\nb = 2\n", encoding="utf-8")
        (root / "docs" / "ok.md").write_text("# Ok\n\nSee `small.py:1-40`.\n", encoding="utf-8")
        return "stale_line_cites"

    def plant_coupling(root: Path) -> str:
        pkg = root / "src" / "kiro_crew"
        pkg.mkdir(parents=True)
        (pkg / "tips_allowlist.py").write_text(
            'TIP_DOC_ALLOWLIST = frozenset({"vanished.md"})\n', encoding="utf-8"
        )
        return "coupling"

    def code_immunity_probe(label: str, body: str, field: str = "broken_links") -> None:
        """Assert markup written inside code is NOT reported by ``field``.

        The field is explicit because a probe that watches the wrong one cannot
        fail: it would pass while the check it claims to guard regressed.
        """
        nonlocal failures
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "docs").mkdir(parents=True)
            (root / "docs" / "README.md").write_text("# Docs\n\n- [Ok](ok.md)\n", encoding="utf-8")
            (root / "docs" / "ok.md").write_text(body, encoding="utf-8")
            if getattr(run(root), field):
                print(f"  FAIL {label} was flagged")
                failures += 1
            else:
                print(f"  ok  {label} ignored")

    def source_cite_immunity_probe(label: str, build) -> None:
        """Assert a legitimate citation shape is NOT reported.

        Each shape here was MEASURED as a false positive on the real tree before
        its exemption existed, so the probe records the evidence rather than a
        hunch -- and pins the exemption so a later widening of the rule fails here
        instead of burying the real findings again.
        """
        nonlocal failures
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "docs").mkdir(parents=True)
            (root / "docs" / "README.md").write_text("# Docs\n\n- [Ok](ok.md)\n", encoding="utf-8")
            (root / "docs" / "ok.md").write_text("# Ok\n\nBody.\n", encoding="utf-8")
            field = build(root)
            if getattr(run(root), field):
                print(f"  FAIL {label} was flagged")
                failures += 1
            else:
                print(f"  ok  {label} ignored")

    def allow_unresolvable_path_outside_a_spec(root: Path) -> str:
        # The scope IS the rule: an RFC or a guide names files it proposes or that
        # belong to the reader, so an unresolvable path there is correct writing.
        # Pinned, because widening the class past module specs re-buries the real
        # findings 27:11 (measured).
        (root / "docs" / "ok.md").write_text(
            "# Ok\n\nThis adds `acp/planned.py` and reads `cron/jobs.json`.\n",
            encoding="utf-8",
        )
        return "phantom_source_paths"

    def allow_allowlisted_ref_in_a_spec(root: Path) -> str:
        # Inside the scope, an allowlisted ref stays silent -- each entry was
        # traced to the code that builds the path before it was listed.
        spec_dir = root / "docs" / "system-specs" / "modules"
        spec_dir.mkdir(parents=True)
        (root / "docs" / "README.md").write_text(
            "# Docs\n\n- [Ok](ok.md)\n- [Spec](system-specs/modules/thing.md)\n",
            encoding="utf-8",
        )
        (spec_dir / "thing.md").write_text(
            "# Thing\n\nState lives in `cron/jobs.json`.\n", encoding="utf-8"
        )
        return "phantom_source_paths"

    def allow_bare_filename_in_a_spec(root: Path) -> str:
        # A citation with no directory names a runtime or generated file the repo
        # cannot adjudicate; only a path WITH a directory is a checkable claim.
        spec_dir = root / "docs" / "system-specs" / "modules"
        spec_dir.mkdir(parents=True)
        (root / "docs" / "README.md").write_text(
            "# Docs\n\n- [Ok](ok.md)\n- [Spec](system-specs/modules/thing.md)\n",
            encoding="utf-8",
        )
        (spec_dir / "thing.md").write_text(
            "# Thing\n\nThe gateway rewrites `mcp.json`.\n", encoding="utf-8"
        )
        return "phantom_source_paths"

    def allow_symlinked_doc(root: Path) -> str:
        # The markdown walk skips symlinks for the same reason, so a symlinked doc
        # contributes no findings of its own -- it is never opened.
        (root / "docs" / "real-extra.md").write_text(
            "# Extra\n\nSee `nope/ghost.py:900`.\n", encoding="utf-8"
        )
        try:
            (root / "docs" / "linked.md").symlink_to(root / "docs" / "real-extra.md")
        except (OSError, NotImplementedError):  # pragma: no cover - Windows
            pass
        (root / "docs" / "README.md").write_text(
            "# Docs\n\n- [Ok](ok.md)\n- [Extra](real-extra.md)\n", encoding="utf-8"
        )
        return "unreachable"

    def allow_fenced_sample(root: Path) -> str:
        pkg = root / "src" / "kiro_crew"
        pkg.mkdir(parents=True)
        (pkg / "small.py").write_text("a = 1\n", encoding="utf-8")
        (root / "docs" / "ok.md").write_text(
            "# Ok\n\n```\nsee `small.py:900`\n```\n", encoding="utf-8"
        )
        return "stale_line_cites"

    def allow_in_range_cite(root: Path) -> str:
        pkg = root / "src" / "kiro_crew"
        pkg.mkdir(parents=True)
        (pkg / "small.py").write_text("a = 1\nb = 2\nc = 3\n", encoding="utf-8")
        (root / "docs" / "ok.md").write_text(
            "# Ok\n\nSee `small.py:2` and `small.py:1-3`.\n", encoding="utf-8"
        )
        return "stale_line_cites"

    def fact_probe(label: str, check: str, build) -> None:
        """Assert a fact check fires, and that its triple names the right token."""
        nonlocal failures
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "docs").mkdir(parents=True)
            (root / "docs" / "README.md").write_text("# Docs\n\n- [Ok](ok.md)\n", encoding="utf-8")
            (root / "docs" / "ok.md").write_text("# Ok\n\nBody.\n", encoding="utf-8")
            expected_token = build(root)
            got = run(root).facts_for(check) + [f for f in run(root).advisories if f.check == check]
            tokens = {f.token for f in got}
            if expected_token in tokens:
                print(f"  ok  {label} detected")
            else:
                print(f"  FAIL {label} NOT detected (got {sorted(tokens)})")
                failures += 1

    def fact_immunity_probe(label: str, check: str, build) -> None:
        """Assert a legitimate shape produces NO triple for ``check``."""
        nonlocal failures
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "docs").mkdir(parents=True)
            (root / "docs" / "README.md").write_text("# Docs\n\n- [Ok](ok.md)\n", encoding="utf-8")
            (root / "docs" / "ok.md").write_text("# Ok\n\nBody.\n", encoding="utf-8")
            build(root)
            findings = run(root)
            hits = findings.facts_for(check) + [f for f in findings.advisories if f.check == check]
            if hits:
                print(f"  FAIL {label} was flagged ({sorted(f.token for f in hits)})")
                failures += 1
            else:
                print(f"  ok  {label} ignored")

    def plant_missing_repo_path(root: Path) -> str:
        (root / "docs" / "ok.md").write_text(
            "# Ok\n\nThe entry point is `src/kiro_crew/ghost.py`.\n", encoding="utf-8"
        )
        return "src/kiro_crew/ghost.py"

    def allow_repo_path_that_exists(root: Path) -> None:
        pkg = root / "src" / "kiro_crew"
        pkg.mkdir(parents=True)
        (pkg / "real.py").write_text("a = 1\n", encoding="utf-8")
        (root / "docs" / "ok.md").write_text(
            "# Ok\n\nThe entry point is `src/kiro_crew/real.py`.\n", encoding="utf-8"
        )

    def allow_repo_path_resolvable_one_root_down(root: Path) -> None:
        # A skill's own `scripts/reaper.sh` reads as repo-anchored and is not; the
        # suffix index is what tells the two apart, and dropping this exemption
        # re-reports 33 correct citations (measured).
        skill = root / "src" / "kiro_crew" / "builtin_skills" / "demo" / "scripts"
        skill.mkdir(parents=True)
        (skill / "reaper.sh").write_text("#!/bin/sh\n", encoding="utf-8")
        (root / "docs" / "ok.md").write_text(
            "# Ok\n\nRun `scripts/reaper.sh` from the skill.\n", encoding="utf-8"
        )

    def allow_repo_path_in_a_proposal(root: Path) -> None:
        # An RFC names files BECAUSE they do not exist; that is the genre's job.
        rfc = root / "docs" / "request-for-change"
        rfc.mkdir()
        (root / "docs" / "README.md").write_text(
            "# Docs\n\n- [Ok](ok.md)\n- [Rfc](request-for-change/rfc-x.md)\n", encoding="utf-8"
        )
        (rfc / "rfc-x.md").write_text(
            "# Rfc\n\nThis adds `src/kiro_crew/planned.py`.\n", encoding="utf-8"
        )

    def plant_line_ref_inside_the_file(root: Path) -> str:
        # Inside the file, so the beyond-EOF check stays silent and only the shape
        # check fires -- which is the citation that is about to rot unnoticed.
        pkg = root / "src" / "kiro_crew"
        pkg.mkdir(parents=True)
        (pkg / "small.py").write_text("a = 1\nb = 2\nc = 3\n", encoding="utf-8")
        (root / "docs" / "ok.md").write_text("# Ok\n\nSee `small.py:2`.\n", encoding="utf-8")
        return "small.py:2"

    def allow_symbol_cite_without_a_line(root: Path) -> None:
        pkg = root / "src" / "kiro_crew"
        pkg.mkdir(parents=True)
        (pkg / "small.py").write_text("a = 1\n", encoding="utf-8")
        (root / "docs" / "ok.md").write_text(
            "# Ok\n\nSee `resolve_usable_model` in `small.py`.\n", encoding="utf-8"
        )

    def plant_dead_fenced_task_spec(root: Path) -> str:
        (root / "docs" / "ok.md").write_text(
            "# Ok\n\n```bash\nkirocrew run docs/task-specs/2026/01/gone/spec.md\n```\n",
            encoding="utf-8",
        )
        return "docs/task-specs/2026/01/gone/spec.md"

    def allow_bare_filename_after_kirocrew_run(root: Path) -> None:
        # `TASK.md` is a file the READER creates; only a path with a directory
        # component is a claim about this tree.
        (root / "docs" / "ok.md").write_text(
            "# Ok\n\n```bash\nkirocrew run TASK.md\n```\n", encoding="utf-8"
        )

    def plant_glued_table_row(root: Path) -> str:
        (root / "docs" / "ok.md").write_text(
            "# Ok\n\n| Doc | What |\n|---|---|\n"
            "| [a.md](a.md) | First. || [b.md](b.md) | Second. |\n",
            encoding="utf-8",
        )
        return "b.md"

    def allow_two_link_cells_in_one_row(root: Path) -> None:
        # A stable and a nightly download in one row: two link cells, correct width.
        (root / "docs" / "ok.md").write_text(
            "# Ok\n\n| Kind | Stable | Nightly |\n|---|---|---|\n"
            "| deb | [Stable](https://x/a.deb) | [Nightly](https://x/b.deb) |\n",
            encoding="utf-8",
        )

    def plant_unrecorded_coupling(root: Path) -> str:
        packaged = root / "src" / "kiro_crew" / "docs"
        packaged.mkdir(parents=True)
        (packaged / "ghost-integration.md").write_text("# Ghost\n", encoding="utf-8")
        panel = root / "website" / "src" / "pages" / "settings"
        panel.mkdir(parents=True)
        (panel / "GhostPanel.tsx").write_text(
            "const SETUP_GUIDE = 'https://example.invalid/"
            "src/kiro_crew/docs/ghost-integration.md'\n",
            encoding="utf-8",
        )
        return "ghost-integration.md"

    def allow_coupling_named_only_in_a_comment(root: Path) -> None:
        # A comment is a CITATION, which check_code_citations already owns.
        packaged = root / "src" / "kiro_crew" / "docs"
        packaged.mkdir(parents=True)
        (packaged / "iframe-hosts.md").write_text("# Hosts\n", encoding="utf-8")
        page = root / "website" / "src" / "pages"
        page.mkdir(parents=True)
        (page / "ChatPage.tsx").write_text(
            "// See `src/kiro_crew/docs/iframe-hosts.md` for the host list.\n"
            "export const x = 1\n",
            encoding="utf-8",
        )

    def allow_coupling_named_only_inside_a_longer_token(root: Path) -> None:
        # ``apps.mdNotebook.sort.nameAZ`` is an i18n key, not a reference to the
        # packaged ``apps.md``. Without the regex's trailing guard every module
        # using that key namespace read as a consumer of the doc.
        packaged = root / "src" / "kiro_crew" / "docs"
        packaged.mkdir(parents=True)
        (packaged / "apps.md").write_text("# Apps\n", encoding="utf-8")
        page = root / "website" / "src" / "apps"
        page.mkdir(parents=True)
        (page / "labels.ts").write_text(
            "export const s = i18nT('apps.mdNotebook.sort.nameAZ')\n", encoding="utf-8"
        )

    def allow_coupling_named_only_in_a_test(root: Path) -> None:
        packaged = root / "src" / "kiro_crew" / "docs"
        packaged.mkdir(parents=True)
        (packaged / "fixture-doc.md").write_text("# Fixture\n", encoding="utf-8")
        tests = root / "website" / "src" / "test"
        tests.mkdir(parents=True)
        (tests / "Panel.test.tsx").write_text("const f = 'fixture-doc.md'\n", encoding="utf-8")

    def plant_dead_identifier(root: Path) -> str:
        pkg = root / "src" / "kiro_crew"
        pkg.mkdir(parents=True)
        (pkg / "real.py").write_text("def live_handler():\n    return 1\n", encoding="utf-8")
        (root / "docs" / "ok.md").write_text(
            "# Ok\n\nRouted through `vanished_handler`.\n", encoding="utf-8"
        )
        return "vanished_handler"

    def allow_live_identifier(root: Path) -> None:
        pkg = root / "src" / "kiro_crew"
        pkg.mkdir(parents=True)
        (pkg / "real.py").write_text("def live_handler_name():\n    return 1\n", encoding="utf-8")
        (root / "docs" / "ok.md").write_text(
            "# Ok\n\nRouted through `live_handler_name`.\n", encoding="utf-8"
        )

    def allow_placeholder_identifier(root: Path) -> None:
        (root / "src" / "kiro_crew").mkdir(parents=True)
        (root / "src" / "kiro_crew" / "real.py").write_text("a = 1\n", encoding="utf-8")
        (root / "docs" / "ok.md").write_text(
            "# Ok\n\nName it `MyOwnWidget` in your app.\n", encoding="utf-8"
        )

    def baseline_probe() -> None:
        """A recorded triple passes; the same triple unrecorded fails."""
        nonlocal failures
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "docs").mkdir(parents=True)
            (root / "docs" / "README.md").write_text("# Docs\n\n- [Ok](ok.md)\n", encoding="utf-8")
            (root / "docs" / "ok.md").write_text(
                "# Ok\n\nThe entry point is `src/kiro_crew/ghost.py`.\n", encoding="utf-8"
            )
            unfiltered = run(root)
            if not unfiltered.facts_for(CHECK_PATH_EXISTS):
                print("  FAIL baseline probe could not produce a finding")
                failures += 1
                return
            triple = unfiltered.facts_for(CHECK_PATH_EXISTS)[0].key
            filtered = run(root)
            stale = apply_baseline(filtered, {triple})
            if filtered.facts_for(CHECK_PATH_EXISTS) or stale:
                print("  FAIL a baselined triple still failed the run")
                failures += 1
                return
            other = run(root)
            stale = apply_baseline(other, {(CHECK_PATH_EXISTS, "docs/ok.md", "src/other.py")})
            if not other.facts_for(CHECK_PATH_EXISTS) or not stale:
                print("  FAIL an unrecorded triple was absorbed, or a stale entry went unreported")
                failures += 1
                return
            print("  ok  baseline admits recorded triples and reports stale ones")

    print("Running docs-lint self-test...")
    clean_probe()
    probe("broken link", plant_broken_link)
    probe("broken HTML anchor", plant_broken_html_link)
    probe("unreachable doc", plant_unreachable)
    probe("missing directory index", plant_missing_index)
    probe("changelog preamble", plant_changelog_preamble)
    probe("conflict marker (bare separator)", plant_conflict_marker)
    probe("conflict marker (full three-way)", plant_conflict_marker_head)
    probe("conflict marker (uncurated tree)", plant_conflict_marker_uncurated)
    probe("phantom source path in a module spec", plant_phantom_source_path)
    probe("citation of a symlinked file", plant_citation_of_a_symlinked_file)
    probe("line citation past EOF", plant_stale_line_cite)
    probe("line citation past EOF inside an RFC", plant_stale_line_cite_in_an_rfc)
    probe("line citation in an uncurated tree", plant_stale_line_cite_in_an_uncurated_tree)
    probe("absurd line number (past the int digit cap)", plant_absurd_line_number)
    probe("line zero", plant_line_zero)
    probe("line RANGE ending past EOF", plant_stale_line_range)
    probe("phantom spec citation", plant_phantom_ref)
    probe("unknown harness invariant citation", plant_unknown_harness_invariant)
    probe("code-coupled doc missing", plant_coupling)

    # Code-markup immunity is an inverse assertion (nothing should fire).
    source_cite_immunity_probe(
        "unresolvable path outside a module spec", allow_unresolvable_path_outside_a_spec
    )
    source_cite_immunity_probe("allowlisted ref inside a spec", allow_allowlisted_ref_in_a_spec)
    source_cite_immunity_probe("bare filename inside a spec", allow_bare_filename_in_a_spec)
    source_cite_immunity_probe("symlinked doc is not walked", allow_symlinked_doc)
    source_cite_immunity_probe("fenced sample citation", allow_fenced_sample)
    source_cite_immunity_probe("in-range line citation", allow_in_range_cite)
    code_immunity_probe("fenced example link", "# Ok\n\n```md\n[example](does-not-exist.md)\n```\n")
    code_immunity_probe("inline-code example link", "# Ok\n\nSpoken as `[label](url)` aloud.\n")
    code_immunity_probe(
        "prose discussing conflict markers",
        "# Ok\n\ngit emits `<<<<<<< HEAD` / `=======` /\n`>>>>>>>` around the region.\n",
        "conflict_markers",
    )
    code_immunity_probe(
        "fenced conflict demonstration",
        "# Ok\n\n```diff\n<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> branch\n```\n",
        "conflict_markers",
    )

    # Fact checks: each one fires on a planted defect, and each documented
    # exemption is pinned so widening the rule fails here instead of in review.
    fact_probe("missing repo-anchored path", CHECK_PATH_EXISTS, plant_missing_repo_path)
    fact_immunity_probe("repo path that exists", CHECK_PATH_EXISTS, allow_repo_path_that_exists)
    fact_immunity_probe(
        "repo path resolvable one root down",
        CHECK_PATH_EXISTS,
        allow_repo_path_resolvable_one_root_down,
    )
    fact_immunity_probe(
        "repo path inside a proposal", CHECK_PATH_EXISTS, allow_repo_path_in_a_proposal
    )
    fact_probe("line citation inside the file", CHECK_LINE_REF, plant_line_ref_inside_the_file)
    fact_immunity_probe(
        "symbol citation without a line", CHECK_LINE_REF, allow_symbol_cite_without_a_line
    )
    fact_probe("dead pasteable path in a fence", CHECK_FENCED_PATH, plant_dead_fenced_task_spec)
    fact_immunity_probe(
        "bare filename after `kirocrew run`",
        CHECK_FENCED_PATH,
        allow_bare_filename_after_kirocrew_run,
    )
    fact_probe("glued table row", CHECK_TABLE_ROW_MERGE, plant_glued_table_row)
    fact_immunity_probe(
        "two link cells in one row", CHECK_TABLE_ROW_MERGE, allow_two_link_cells_in_one_row
    )
    fact_probe(
        "unrecorded packaged-doc coupling",
        CHECK_COUPLING_COMPLETENESS,
        plant_unrecorded_coupling,
    )
    fact_immunity_probe(
        "packaged doc named only in a comment",
        CHECK_COUPLING_COMPLETENESS,
        allow_coupling_named_only_in_a_comment,
    )
    fact_immunity_probe(
        "packaged doc named only in a test",
        CHECK_COUPLING_COMPLETENESS,
        allow_coupling_named_only_in_a_test,
    )
    fact_immunity_probe(
        "packaged doc name inside a longer token",
        CHECK_COUPLING_COMPLETENESS,
        allow_coupling_named_only_inside_a_longer_token,
    )
    fact_probe("dead identifier", CHECK_DEAD_IDENTIFIER, plant_dead_identifier)
    fact_immunity_probe("live identifier", CHECK_DEAD_IDENTIFIER, allow_live_identifier)
    fact_immunity_probe(
        "documentation placeholder identifier",
        CHECK_DEAD_IDENTIFIER,
        allow_placeholder_identifier,
    )
    baseline_probe()

    if failures:
        print(f"\nSELF-TEST FAILED: {failures} check(s) do not fire")
        return 1
    print("\nSelf-test passed")
    return 0
