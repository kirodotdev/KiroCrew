"""Unit tests for the fact checks in scripts/docs_lint.py.

Each fact check earns its keep from where it draws the line, so each one is pinned
from both sides: the defect it must report, and the legitimate shape it must stay
silent about. The immunity halves are not padding -- every one of them was a
measured false positive before its exemption existed, and a check whose findings
are mostly false trains a maintainer to skim past the gate.

The baseline gets its own class. It is the only part that can silently disable
everything else: a refresh that ADDED a triple, or a filter that matched too
loosely, would turn the whole family into a formality.

Most tests build their own throwaway tree under ``tmp_path``. Three classes read the
real checkout on purpose, anchored through ``_REPO_ROOT``: ``TestShippedBaseline``
(the committed backlog is the artifact under test), ``TestEntryPointDocs`` (the
router files have to actually be there), and ``TestNoHostSideEffects``'s self-test
case (the gate's own probes build their own temporary trees). Nothing writes outside
``tmp_path``.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from conftest import make_dir_link

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "docs_lint.py"


def _load():
    spec = importlib.util.spec_from_file_location("docs_lint", _SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["docs_lint"] = module
    spec.loader.exec_module(module)
    return module


gate = _load()


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A minimal healthy doc tree: one index that links its one doc."""
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "README.md").write_text("# Docs\n\n- [Ok](ok.md)\n", encoding="utf-8")
    (docs / "ok.md").write_text("# Ok\n\nBody.\n", encoding="utf-8")
    return tmp_path


def _write(root: Path, rel: str, body: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _tokens(root: Path, check: str, *, strict: bool = False) -> set[str]:
    """Tokens reported for ``check``, failing and report-only halves together."""
    findings = gate.run(root, strict_identifiers=strict)
    return {f.token for f in findings.facts + findings.advisories if f.check == check}


class TestPathExists:
    """A repo-anchored path resolves, or the doc is wrong."""

    def test_reports_a_path_that_names_no_file(self, tree: Path) -> None:
        _write(tree, "docs/ok.md", "# Ok\n\nSee `src/kiro_crew/ghost.py`.\n")
        assert "src/kiro_crew/ghost.py" in _tokens(tree, gate.CHECK_PATH_EXISTS)

    @pytest.mark.parametrize(
        "shape",
        [
            "src/kiro_crew/ghost.py",
            "scripts/ghost.py",
            "scripts/ghost.sh",
            "website/src/pages/Ghost.tsx",
            "website/src/utils/ghost.ts",
            ".github/workflows/ghost.yml",
            "docs/ci/ghost.md",
        ],
    )
    def test_covers_every_declared_shape(self, tree: Path, shape: str) -> None:
        _write(tree, "docs/ok.md", f"# Ok\n\nSee `{shape}`.\n")
        assert shape in _tokens(tree, gate.CHECK_PATH_EXISTS)

    def test_silent_when_the_path_exists(self, tree: Path) -> None:
        _write(tree, "src/kiro_crew/real.py", "a = 1\n")
        _write(tree, "docs/ok.md", "# Ok\n\nSee `src/kiro_crew/real.py`.\n")
        assert not _tokens(tree, gate.CHECK_PATH_EXISTS)

    def test_silent_for_a_path_that_resolves_one_root_down(self, tree: Path) -> None:
        # A skill's own `scripts/reaper.sh` reads as repo-anchored and is not.
        _write(tree, "src/kiro_crew/builtin_skills/demo/scripts/reaper.sh", "#!/bin/sh\n")
        _write(tree, "docs/ok.md", "# Ok\n\nRun `scripts/reaper.sh`.\n")
        assert not _tokens(tree, gate.CHECK_PATH_EXISTS)

    def test_silent_for_a_path_that_is_only_a_longer_path_s_tail(self, tree: Path) -> None:
        # `src/kiro_crew/docs/tips.md` must not also be read as `docs/tips.md`.
        _write(tree, "src/kiro_crew/docs/tips.md", "# Tips\n")
        _write(tree, "docs/ok.md", "# Ok\n\nSee `src/kiro_crew/docs/tips.md`.\n")
        assert not _tokens(tree, gate.CHECK_PATH_EXISTS)

    @pytest.mark.parametrize(
        "real,truncated",
        [
            ("scripts/vendor_manifest.sha256", "scripts/vendor_manifest.sh"),
            ("src/kiro_crew/types.pyi", "src/kiro_crew/types.py"),
            ("docs/ci/page.mdx", "docs/ci/page.md"),
            ("website/src/a.tsx.snap", "website/src/a.tsx"),
            ("scripts/docs_lint.python-wrapper", "scripts/docs_lint.py"),
        ],
    )
    def test_a_longer_extension_is_not_truncated_to_a_shorter_one(
        self, tree: Path, real: str, truncated: str
    ) -> None:
        # Without a trailing boundary the pattern reports the PREFIX of a real
        # filename, so a correct citation reads as rot.
        _write(tree, real, "x\n")
        _write(tree, "docs/ok.md", f"# Ok\n\nSee `{real}`.\n")
        assert truncated not in _tokens(tree, gate.CHECK_PATH_EXISTS)

    def test_a_local_coordinate_is_still_validated(self, tree: Path) -> None:
        # This repo addresses its OWN code as `path::Symbol`, so skipping on the
        # syntax alone would let a rename leave such a citation stale, gate green.
        _write(tree, "docs/ok.md", "# Ok\n\nSee `src/kiro_crew/gone.py::missing_symbol`.\n")
        assert "src/kiro_crew/gone.py" in _tokens(tree, gate.CHECK_PATH_EXISTS)

    def test_a_resolvable_local_coordinate_is_silent(self, tree: Path) -> None:
        _write(tree, "src/kiro_crew/mcp_tools/workflows.py", "def workflow_run():\n    pass\n")
        _write(
            tree,
            "docs/ok.md",
            "# Ok\n\nSee `src/kiro_crew/mcp_tools/workflows.py::workflow_run`.\n",
        )
        assert not _tokens(tree, gate.CHECK_PATH_EXISTS)

    @pytest.mark.parametrize("doc", sorted(gate._EXTERNAL_TARGET_COORDINATE_DOCS))
    def test_silent_for_a_coordinate_in_an_external_target_doc(self, tree: Path, doc: str) -> None:
        # `src/board.py::is_repetition` addresses a module in the target repo the
        # auto-improvement spine is pointed at, and the DOC is what says so.
        _write(tree, doc, "# Plan\n\nA target like `src/board.py::is_repetition`.\n")
        assert not _tokens(tree, gate.CHECK_PATH_EXISTS)

    def test_a_line_citation_is_still_seen_after_the_boundary(self, tree: Path) -> None:
        # The trailing boundary admits `:`, so the line check keeps its input.
        _write(tree, "src/kiro_crew/small.py", "a = 1\n")
        _write(tree, "docs/ok.md", "# Ok\n\nSee `small.py:1`.\n")
        assert "small.py:1" in _tokens(tree, gate.CHECK_LINE_REF)

    @pytest.mark.parametrize("genre", sorted(gate._FORWARD_LOOKING_DOC_PREFIXES))
    def test_silent_in_a_forward_looking_genre(self, tree: Path, genre: str) -> None:
        # A proposal names files BECAUSE they do not exist yet.
        rel = f"{genre}proposal.md"
        _write(tree, rel, "# Proposal\n\nThis adds `src/kiro_crew/planned.py`.\n")
        assert not _tokens(tree, gate.CHECK_PATH_EXISTS)

    def test_silent_for_a_bare_path_outside_backticks(self, tree: Path) -> None:
        # Backticks are what make a path a citation; prose about a tree is not one.
        _write(tree, "docs/ok.md", "# Ok\n\nThe handler is in src/kiro_crew/ghost.py.\n")
        assert not _tokens(tree, gate.CHECK_PATH_EXISTS)

    def test_silent_inside_a_fenced_block(self, tree: Path) -> None:
        _write(tree, "docs/ok.md", "# Ok\n\n```\ncat src/kiro_crew/ghost.py\n```\n")
        assert not _tokens(tree, gate.CHECK_PATH_EXISTS)


class TestLineRef:
    """The shape itself is the finding, not just the citation that already rotted."""

    def test_reports_a_line_inside_the_file(self, tree: Path) -> None:
        # In range, so the beyond-EOF check stays silent: this is the citation that
        # rots on the next refactor with nothing going red.
        _write(tree, "src/kiro_crew/small.py", "a = 1\nb = 2\nc = 3\n")
        _write(tree, "docs/ok.md", "# Ok\n\nSee `small.py:2`.\n")
        assert not gate.run(tree).stale_line_cites
        assert "small.py:2" in _tokens(tree, gate.CHECK_LINE_REF)

    def test_reports_a_range_and_a_list(self, tree: Path) -> None:
        _write(tree, "src/kiro_crew/small.py", "\n".join(f"x = {i}" for i in range(50)) + "\n")
        _write(tree, "docs/ok.md", "# Ok\n\nSee `small.py:1-3` and `small.py:4,9`.\n")
        assert _tokens(tree, gate.CHECK_LINE_REF) == {"small.py:1-3", "small.py:4,9"}

    def test_silent_for_a_citation_without_a_line(self, tree: Path) -> None:
        _write(tree, "src/kiro_crew/small.py", "a = 1\n")
        _write(tree, "docs/ok.md", "# Ok\n\nSee `resolve_it` in `small.py`.\n")
        assert not _tokens(tree, gate.CHECK_LINE_REF)

    def test_silent_inside_a_fenced_block(self, tree: Path) -> None:
        _write(tree, "src/kiro_crew/small.py", "a = 1\n")
        _write(tree, "docs/ok.md", "# Ok\n\n```\nsee `small.py:2`\n```\n")
        assert not _tokens(tree, gate.CHECK_LINE_REF)


class TestFencedPath:
    """A fenced block is a sample, except for the tokens a reader pastes."""

    def test_reports_a_dead_task_spec_path(self, tree: Path) -> None:
        _write(
            tree,
            "docs/ok.md",
            "# Ok\n\n```bash\nkirocrew run docs/task-specs/2026/01/gone/spec.md\n```\n",
        )
        assert "docs/task-specs/2026/01/gone/spec.md" in _tokens(tree, gate.CHECK_FENCED_PATH)

    def test_reports_a_dead_kirocrew_run_argument(self, tree: Path) -> None:
        _write(tree, "docs/ok.md", "# Ok\n\n```bash\nkirocrew run specs/gone.md\n```\n")
        assert "specs/gone.md" in _tokens(tree, gate.CHECK_FENCED_PATH)

    def test_silent_when_the_pasted_path_exists(self, tree: Path) -> None:
        _write(tree, "docs/task-specs/2026/01/live/spec.md", "# Spec\n")
        _write(
            tree,
            "docs/ok.md",
            "# Ok\n\n```bash\nkirocrew run docs/task-specs/2026/01/live/spec.md\n```\n",
        )
        assert not _tokens(tree, gate.CHECK_FENCED_PATH)

    def test_silent_for_a_bare_filename(self, tree: Path) -> None:
        # `TASK.md` is a file the READER creates; only a path with a directory
        # component is a claim about this tree.
        _write(tree, "docs/ok.md", "# Ok\n\n```bash\nkirocrew run TASK.md\n```\n")
        assert not _tokens(tree, gate.CHECK_FENCED_PATH)

    def test_silent_outside_a_fence(self, tree: Path) -> None:
        # Outside a fence the same token belongs to path-exists, so exactly one
        # check owns it and it is never reported twice.
        _write(tree, "docs/ok.md", "# Ok\n\nRun `kirocrew run specs/gone.md`.\n")
        assert not _tokens(tree, gate.CHECK_FENCED_PATH)

    def test_silent_in_a_forward_looking_genre(self, tree: Path) -> None:
        # An RFC's `kirocrew run docs/task-specs/future/spec.md` names the RFC's own
        # planned output, the same judgement path-exists already runs on.
        _write(
            tree,
            "docs/request-for-change/rfc-x.md",
            "# Rfc\n\n```bash\nkirocrew run docs/task-specs/future/spec.md\n```\n",
        )
        assert not _tokens(tree, gate.CHECK_FENCED_PATH)

    def test_silent_in_the_packaged_user_docs(self, tree: Path) -> None:
        # A packaged doc teaches a reader to write their OWN task spec, so the path
        # is a template for their project; "correct the path" is not a fix for it.
        _write(
            tree,
            "src/kiro_crew/docs/task-runner.md",
            "# Runner\n\n```bash\nkirocrew run docs/task-specs/2026/03/my-task/spec.md\n```\n",
        )
        assert not _tokens(tree, gate.CHECK_FENCED_PATH)

    def test_one_pasted_command_is_one_finding(self, tree: Path) -> None:
        # Both patterns match a `kirocrew run docs/task-specs/...` line.
        _write(
            tree,
            "docs/ok.md",
            "# Ok\n\n```bash\nkirocrew run docs/task-specs/gone/spec.md\n```\n",
        )
        hits = [f for f in gate.run(tree).facts if f.check == gate.CHECK_FENCED_PATH]
        assert len(hits) == 1


class TestTableRowMerge:
    """Two index rows on one physical line: every link resolves, a row vanishes."""

    def test_reports_a_glued_row(self, tree: Path) -> None:
        _write(
            tree,
            "docs/ok.md",
            "# Ok\n\n| Doc | What |\n|---|---|\n"
            "| [a.md](a.md) | First. || [b.md](b.md) | Second. |\n",
        )
        assert "b.md" in _tokens(tree, gate.CHECK_TABLE_ROW_MERGE)

    def test_silent_for_a_row_that_legitimately_holds_two_links(self, tree: Path) -> None:
        # A stable and a nightly download: two link cells, declared width.
        _write(
            tree,
            "docs/ok.md",
            "# Ok\n\n| Kind | Stable | Nightly |\n|---|---|---|\n"
            "| deb | [Stable](https://x/a.deb) | [Nightly](https://x/b.deb) |\n",
        )
        assert not _tokens(tree, gate.CHECK_TABLE_ROW_MERGE)

    def test_silent_for_a_wide_row_without_a_second_link(self, tree: Path) -> None:
        # Both signals are required: a stray pipe alone is not a glued index row.
        _write(tree, "docs/ok.md", "# Ok\n\n| A | B |\n|---|---|\n| one | two | three |\n")
        assert not _tokens(tree, gate.CHECK_TABLE_ROW_MERGE)

    def test_silent_when_a_pipe_is_escaped_or_in_code(self, tree: Path) -> None:
        _write(
            tree,
            "docs/ok.md",
            "# Ok\n\n| Doc | Shape |\n|---|---|\n" "| [a.md](a.md) | `x \\| y` and a \\| b |\n",
        )
        assert not _tokens(tree, gate.CHECK_TABLE_ROW_MERGE)


class TestCouplingCompleteness:
    """The inverse of CODE_COUPLED_DOCS: a consumer nobody recorded."""

    def test_reports_an_unrecorded_consumer(self, tree: Path) -> None:
        _write(tree, "src/kiro_crew/docs/ghost-integration.md", "# Ghost\n")
        _write(
            tree,
            "website/src/pages/settings/GhostPanel.tsx",
            "const SETUP_GUIDE = 'https://example.invalid/"
            "src/kiro_crew/docs/ghost-integration.md'\n",
        )
        assert "ghost-integration.md" in _tokens(tree, gate.CHECK_COUPLING_COMPLETENESS)

    def test_silent_when_the_coupling_is_already_recorded(self, tree: Path) -> None:
        recorded = sorted(
            doc for doc in gate.CODE_COUPLED_DOCS if doc.startswith(f"{gate._PACKAGED_DOCS_DIR}/")
        )[0]
        name = Path(recorded).name
        _write(tree, recorded, "# Recorded\n")
        _write(tree, "website/src/pages/settings/Panel.tsx", f"const G = 'x/{name}'\n")
        assert not _tokens(tree, gate.CHECK_COUPLING_COMPLETENESS)

    def test_silent_when_named_only_in_a_comment(self, tree: Path) -> None:
        # A comment is a citation, which check_code_citations already owns.
        _write(tree, "src/kiro_crew/docs/iframe-hosts.md", "# Hosts\n")
        _write(
            tree,
            "website/src/pages/ChatPage.tsx",
            "// See `src/kiro_crew/docs/iframe-hosts.md`.\nexport const x = 1\n",
        )
        assert not _tokens(tree, gate.CHECK_COUPLING_COMPLETENESS)

    def test_silent_when_named_only_in_a_test(self, tree: Path) -> None:
        _write(tree, "src/kiro_crew/docs/fixture-doc.md", "# Fixture\n")
        _write(tree, "website/src/test/Panel.test.tsx", "const f = 'fixture-doc.md'\n")
        assert not _tokens(tree, gate.CHECK_COUPLING_COMPLETENESS)

    def test_silent_for_an_ambiguous_filename(self, tree: Path) -> None:
        # Every directory has a README.md, so a match cannot be attributed to the
        # packaged copy: the real hits are a user's project README.
        _write(tree, "src/kiro_crew/docs/README.md", "# Index\n")
        _write(tree, "website/src/utils/fileTokens.ts", "const f = 'README.md'\n")
        assert not _tokens(tree, gate.CHECK_COUPLING_COMPLETENESS)


class TestDeadIdentifier:
    """Report-only by default, because the class is measurably noisy."""

    def test_reports_a_name_absent_from_every_code_tree(self, tree: Path) -> None:
        _write(tree, "src/kiro_crew/real.py", "def live_handler():\n    return 1\n")
        _write(tree, "docs/ok.md", "# Ok\n\nRouted through `vanished_handler`.\n")
        assert "vanished_handler" in _tokens(tree, gate.CHECK_DEAD_IDENTIFIER)

    def test_is_advisory_unless_strict(self, tree: Path) -> None:
        _write(tree, "src/kiro_crew/real.py", "a = 1\n")
        _write(tree, "docs/ok.md", "# Ok\n\nRouted through `vanished_handler`.\n")
        lenient = gate.run(tree)
        assert not lenient.facts_for(gate.CHECK_DEAD_IDENTIFIER)
        assert lenient.advisories and lenient.total() == 0
        strict = gate.run(tree, strict_identifiers=True)
        assert strict.facts_for(gate.CHECK_DEAD_IDENTIFIER)
        assert not strict.advisories and strict.total() == 1

    def test_silent_for_a_live_identifier(self, tree: Path) -> None:
        _write(tree, "src/kiro_crew/real.py", "def live_handler_name():\n    return 1\n")
        _write(tree, "docs/ok.md", "# Ok\n\nRouted through `live_handler_name`.\n")
        assert not _tokens(tree, gate.CHECK_DEAD_IDENTIFIER)

    def test_silent_for_a_name_that_lives_in_another_tree(self, tree: Path) -> None:
        # Restricting the corpus to src/ marks a test's own name dead; every
        # first-party tree is searched so "dead" means dead repo-wide.
        _write(tree, "test/test_thing.py", "def test_all_exports_exact():\n    pass\n")
        _write(tree, "docs/ok.md", "# Ok\n\nPinned by `test_all_exports_exact`.\n")
        assert not _tokens(tree, gate.CHECK_DEAD_IDENTIFIER)

    @pytest.mark.parametrize("prefix", sorted(gate._IDENT_PLACEHOLDER_PREFIXES))
    def test_silent_for_a_documentation_placeholder(self, tree: Path, prefix: str) -> None:
        _write(tree, "src/kiro_crew/real.py", "a = 1\n")
        _write(tree, "docs/ok.md", f"# Ok\n\nName it `{prefix}OwnWidgetThing`.\n")
        assert not _tokens(tree, gate.CHECK_DEAD_IDENTIFIER)

    def test_silent_for_a_short_token(self, tree: Path) -> None:
        _write(tree, "src/kiro_crew/real.py", "a = 1\n")
        _write(tree, "docs/ok.md", "# Ok\n\nSet `a_b_c`.\n")
        assert not _tokens(tree, gate.CHECK_DEAD_IDENTIFIER)

    def test_silent_in_a_forward_looking_genre(self, tree: Path) -> None:
        _write(tree, "src/kiro_crew/real.py", "a = 1\n")
        _write(
            tree,
            "docs/request-for-change/rfc-x.md",
            "# Rfc\n\nThis adds `planned_coordinator`.\n",
        )
        assert not _tokens(tree, gate.CHECK_DEAD_IDENTIFIER)


class TestBaseline:
    """The one part that could silently disable the whole family."""

    def _one_triple(self, tree: Path) -> tuple[str, str, str]:
        _write(tree, "docs/ok.md", "# Ok\n\nSee `src/kiro_crew/ghost.py`.\n")
        facts = gate.run(tree).facts_for(gate.CHECK_PATH_EXISTS)
        assert facts
        return facts[0].key

    def test_a_recorded_triple_passes(self, tree: Path) -> None:
        triple = self._one_triple(tree)
        findings = gate.run(tree)
        stale = gate.apply_baseline(findings, {triple})
        assert not findings.facts_for(gate.CHECK_PATH_EXISTS)
        assert not stale
        assert findings.total() == 0

    def test_an_unrecorded_triple_fails(self, tree: Path) -> None:
        self._one_triple(tree)
        findings = gate.run(tree)
        gate.apply_baseline(findings, set())
        assert findings.facts_for(gate.CHECK_PATH_EXISTS)
        assert findings.total() == 1

    def test_a_triple_that_no_longer_fires_is_reported_not_fatal(self, tree: Path) -> None:
        gone = (gate.CHECK_PATH_EXISTS, "docs/ok.md", "src/kiro_crew/already-fixed.py")
        findings = gate.run(tree)
        stale = gate.apply_baseline(findings, {gone})
        assert stale == [gone]
        assert findings.total() == 0

    def test_the_baseline_filters_advisories_too(self, tree: Path) -> None:
        _write(tree, "src/kiro_crew/real.py", "a = 1\n")
        _write(tree, "docs/ok.md", "# Ok\n\nRouted through `vanished_handler`.\n")
        triple = (gate.CHECK_DEAD_IDENTIFIER, "docs/ok.md", "vanished_handler")
        findings = gate.run(tree)
        assert gate.apply_baseline(findings, {triple}) == []
        assert not findings.advisories

    def test_a_baselined_triple_matches_across_a_reflow(self, tree: Path) -> None:
        # The line number is deliberately outside the identity: a citation that
        # merely moved down the page is the same unfixed finding.
        triple = self._one_triple(tree)
        _write(
            tree, "docs/ok.md", "# Ok\n\nExtra prose.\n\nMore.\n\nSee `src/kiro_crew/ghost.py`.\n"
        )
        findings = gate.run(tree)
        gate.apply_baseline(findings, {triple})
        assert not findings.facts_for(gate.CHECK_PATH_EXISTS)

    def test_round_trips_through_the_file(self, tree: Path) -> None:
        triple = self._one_triple(tree)
        path = tree / gate.DEFAULT_BASELINE
        gate._write_baseline(path, {triple})
        assert gate._read_baseline(path) == {triple}

    def test_an_absent_file_is_an_error_not_an_empty_set(self, tmp_path: Path) -> None:
        # Read as empty, one `rm` plus one refresh would accept every current
        # violation forever, so this is the load-bearing half of "shrink-only".
        with pytest.raises(SystemExit) as excinfo:
            gate._read_baseline(tmp_path / "nope.txt")
        assert "missing" in str(excinfo.value)

    def test_a_deleted_baseline_fails_the_gate_rather_than_passing_it(self, tree: Path) -> None:
        self._one_triple(tree)
        with pytest.raises(SystemExit):
            gate.main(["--root", str(tree), "--baseline", str(tree / "gone.txt")])

    def test_refreshing_a_deleted_baseline_is_refused(self, tree: Path) -> None:
        # The exact sequence a reviewer demonstrated: rm, refresh, gate green.
        self._one_triple(tree)
        baseline = tree / "gone.txt"
        with pytest.raises(SystemExit):
            gate.main(["--root", str(tree), "--baseline", str(baseline), "--update-baseline"])
        assert not baseline.exists()

    def test_a_symlinked_baseline_is_refused_on_read(self, tree: Path) -> None:
        sentinel = tree / "sentinel.txt"
        sentinel.write_text("do not touch\n", encoding="utf-8")
        link = tree / "linked-baseline.txt"
        try:
            link.symlink_to(sentinel)
        except (OSError, NotImplementedError):  # pragma: no cover - unprivileged Windows
            pytest.skip("file symlinks unavailable")
        with pytest.raises(SystemExit):
            gate._read_baseline(link)

    @pytest.mark.parametrize("verb", ["--update-baseline", "--accept-new"])
    def test_a_symlinked_baseline_leaves_its_target_byte_identical(
        self, tree: Path, verb: str
    ) -> None:
        # A fork can COMMIT the baseline path as a symlink; the documented refresh
        # then runs on a maintainer's own machine. Both writing verbs must refuse
        # rather than overwrite whatever the link points at.
        self._one_triple(tree)
        sentinel = tree / "sentinel.txt"
        original = "keep me exactly\n"
        sentinel.write_text(original, encoding="utf-8")
        link = tree / "linked-baseline.txt"
        try:
            link.symlink_to(sentinel)
        except (OSError, NotImplementedError):  # pragma: no cover - unprivileged Windows
            pytest.skip("file symlinks unavailable")
        with pytest.raises(SystemExit):
            gate.main(["--root", str(tree), "--baseline", str(link), verb])
        assert sentinel.read_text(encoding="utf-8") == original

    def test_writing_through_a_symlink_is_refused_directly(self, tree: Path) -> None:
        sentinel = tree / "sentinel.txt"
        sentinel.write_text("keep me\n", encoding="utf-8")
        link = tree / "linked-baseline.txt"
        try:
            link.symlink_to(sentinel)
        except (OSError, NotImplementedError):  # pragma: no cover - unprivileged Windows
            pytest.skip("file symlinks unavailable")
        with pytest.raises(SystemExit):
            gate._write_baseline(link, {("line-ref", "docs/a.md", "x.py:1")})
        assert sentinel.read_text(encoding="utf-8") == "keep me\n"

    def test_a_write_leaves_no_temp_file_behind(self, tree: Path) -> None:
        baseline = tree / "baseline.txt"
        gate._write_baseline(baseline, {("line-ref", "docs/a.md", "x.py:1")})
        assert not [p for p in tree.iterdir() if p.name.endswith(".tmp")]

    def test_comments_and_malformed_lines_are_skipped(self, tmp_path: Path) -> None:
        path = tmp_path / "baseline.txt"
        path.write_text(
            "# a comment\n\nline-ref\tdocs/a.md\tx.py:1\nnot-three-fields\n",
            encoding="utf-8",
        )
        assert gate._read_baseline(path) == {("line-ref", "docs/a.md", "x.py:1")}

    def test_update_never_adds_a_triple(self, tree: Path) -> None:
        # The one rule that keeps the gate from being a formality: a refresh must
        # not be able to clear a new finding.
        self._one_triple(tree)
        baseline = tree / "baseline.txt"
        gate._write_baseline(baseline, {("line-ref", "docs/other.md", "x.py:1")})
        assert (
            gate.main(["--root", str(tree), "--baseline", str(baseline), "--update-baseline"]) == 0
        )
        assert gate._read_baseline(baseline) == set()
        assert gate.main(["--root", str(tree), "--baseline", str(baseline)]) == 1

    def test_accept_new_is_the_only_verb_that_adds(self, tree: Path) -> None:
        triple = self._one_triple(tree)
        baseline = tree / "baseline.txt"
        gate._write_baseline(baseline, set())
        assert gate.main(["--root", str(tree), "--baseline", str(baseline)]) == 1
        assert gate.main(["--root", str(tree), "--baseline", str(baseline), "--accept-new"]) == 0
        assert triple in gate._read_baseline(baseline)
        assert gate.main(["--root", str(tree), "--baseline", str(baseline)]) == 0

    def test_accept_new_prints_every_triple_it_records(
        self, tree: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # An exemption nobody sees is an exemption nobody reviewed.
        triple = self._one_triple(tree)
        baseline = tree / "baseline.txt"
        gate._write_baseline(baseline, set())
        gate.main(["--root", str(tree), "--baseline", str(baseline), "--accept-new"])
        out = capsys.readouterr().out
        assert "\t".join(triple) in out
        assert "NEW triple(s)" in out

    def test_accept_new_also_refuses_a_missing_file(self, tree: Path) -> None:
        self._one_triple(tree)
        with pytest.raises(SystemExit):
            gate.main(["--root", str(tree), "--baseline", str(tree / "gone.txt"), "--accept-new"])

    def test_the_two_verbs_cannot_be_combined(self, tree: Path) -> None:
        baseline = tree / "baseline.txt"
        gate._write_baseline(baseline, set())
        assert (
            gate.main(
                [
                    "--root",
                    str(tree),
                    "--baseline",
                    str(baseline),
                    "--update-baseline",
                    "--accept-new",
                ]
            )
            == 2
        )


class TestNoHostSideEffects:
    """The gate reads a tree it is handed and writes only where it is told."""

    def test_a_clean_tree_reports_clean(self, tree: Path) -> None:
        assert gate.run(tree).total() == 0

    def test_a_symlinked_doc_is_never_opened(self, tree: Path) -> None:
        # A symlink in a tree a fork PR controls is an arbitrary read primitive.
        _write(tree, "docs/real-extra.md", "# Extra\n\nSee `src/kiro_crew/ghost.py`.\n")
        _write(tree, "docs/README.md", "# Docs\n\n- [Ok](ok.md)\n- [Extra](real-extra.md)\n")
        try:
            (tree / "docs" / "linked.md").symlink_to(tree / "docs" / "real-extra.md")
        except (OSError, NotImplementedError):  # pragma: no cover - unprivileged Windows
            pytest.skip("file symlinks unavailable")
        reported = {f.path for f in gate.run(tree).facts}
        assert "docs/linked.md" not in reported

    def test_a_doc_reached_through_a_directory_link_is_never_opened(self, tree: Path) -> None:
        # The link KIND is the subject, so this is the Windows counterpart to the
        # case above: a junction needs no privilege and the walk traverses it the
        # same way, so the guard keeps an assertion on the platform where reparse
        # semantics differ most.
        outside = tree / "outside"
        outside.mkdir()
        (outside / "sneaky.md").write_text(
            "# Sneaky\n\nSee `src/kiro_crew/ghost.py`.\n", encoding="utf-8"
        )
        make_dir_link(tree / "docs" / "linked-dir", outside)
        reported = {f.path for f in gate.run(tree).facts}
        assert not any(p.startswith("docs/linked-dir/") for p in reported)

    def test_the_self_test_passes(self, capsys: pytest.CaptureFixture[str]) -> None:
        # The gate's own planted-defect suite, run against throwaway trees. A gate
        # nobody has proven can fail is a gate that silently passes forever.
        assert gate._self_test() == 0
        assert "Self-test passed" in capsys.readouterr().out


class TestEntryPointDocs:
    """The router files every session loads are link-checked too."""

    @pytest.mark.parametrize("name", ["CLAUDE.md", "CODE_OF_CONDUCT.md"])
    def test_the_repository_router_files_are_entry_points(self, name: str) -> None:
        assert name in gate.ENTRY_POINT_DOCS
        assert (_REPO_ROOT / name).is_file()

    def test_every_entry_point_that_exists_is_a_real_file(self) -> None:
        for name in gate.ENTRY_POINT_DOCS:
            path = _REPO_ROOT / name
            assert not path.is_dir(), name


class TestShippedBaseline:
    """The committed baseline is the repository's recorded backlog."""

    def test_it_exists_and_parses(self) -> None:
        recorded = gate._read_baseline(_REPO_ROOT / gate.DEFAULT_BASELINE)
        assert recorded, "the shipped baseline must not be empty"
        known = {
            gate.CHECK_PATH_EXISTS,
            gate.CHECK_LINE_REF,
            gate.CHECK_FENCED_PATH,
            gate.CHECK_TABLE_ROW_MERGE,
            gate.CHECK_COUPLING_COMPLETENESS,
            gate.CHECK_DEAD_IDENTIFIER,
        }
        assert {check for check, _path, _token in recorded} <= known

    def test_it_records_no_path_this_checkout_lacks_a_doc_for(self) -> None:
        # A triple naming a doc that is gone is prunable, not fatal -- but the
        # paths must be repo-relative POSIX, or nothing can match them.
        for _check, path, _token in gate._read_baseline(_REPO_ROOT / gate.DEFAULT_BASELINE):
            assert not path.startswith("/"), path
            assert "\\" not in path, path
