"""The 2,000-line cap must hold on the real checkout, and every rule of it must bite.

``scripts/check_file_line_cap.py`` is the guard; this file is how CI runs it. It
pins the halves that must stay true together:

* the checkout passes for the change under test: no unlisted code file over the
  cap and no stale row in a file the change touches, and no malformed row in
  ``.github/file-line-cap-baseline.txt``. A violation in an untouched file is main
  drift, a warning; and where the change's scope cannot be determined (the
  shallow CI checkout) every violation is reported without failing;
* each rule fires on a synthetic tree, so a passing checkout means the rules
  held rather than that they stopped looking;
* the backend test suite (Linux and Windows) runs this file on every change,
  including the reduced runs: the leaf-test reduction treats it as a corpus gate,
  and the frontend-only reduction keeps it because it names the website tree;
* the docs that state the rule name the guard and the number it enforces, and
  the six homes of the 1,600-line working target state one figure.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import warnings
from pathlib import Path

import pytest
from git_maintenance_helpers import git_maintenance_off_entries

from kiro_crew.platform.update_governance import _GIT_LOCATION_VARS, git_command_env

# One xdist worker for the whole module: the checkout scan reads every tracked code
# file, and the selector pins read the test tree. Grouping keeps each scan to one
# worker instead of one per worker the module's tests land on.
pytestmark = pytest.mark.xdist_group(name="tree_scan_test_file_line_cap")

THIS = Path(__file__).resolve()
REPO_ROOT = THIS.parents[1]
SCRIPTS = REPO_ROOT / "scripts"
THIS_REL = THIS.relative_to(REPO_ROOT).as_posix()
# Spelled out once, literally, so a change to the baseline alone selects this test
# in the local related-test runner, which matches on the path text.
BASELINE = REPO_ROOT / ".github/file-line-cap-baseline.txt"

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import check_file_line_cap as cap  # noqa: E402  (path set immediately above)
import leaf_test_scope  # noqa: E402  (path set immediately above)

CAP = cap.CAP


def _write_lines(root: Path, rel: str, lines: int, *, terminated: bool = True) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    body = b"line\n" * lines
    target.write_bytes(body if terminated else body[:-1])


def _verdict(root: Path, paths: list[str], baseline_rows: str = "") -> cap.Verdict:
    return cap.evaluate(cap.scan(root, paths), cap.parse_baseline(cap.HEADER + baseline_rows))


def _git(repo: Path, *args: str) -> None:
    """Run a fixture git command: no inherited location, templates, hooks or config."""
    env = {
        **git_command_env(),
        "GIT_TEMPLATE_DIR": "",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
    }
    env.pop("GIT_CONFIG_PARAMETERS", None)
    # Last, so the entries land after the GIT_CONFIG_COUNT git_command_env set.
    env.update(git_maintenance_off_entries(env))
    subprocess.run(
        ["git", "-c", "init.templateDir=", *args],
        cwd=repo,
        env=env,
        capture_output=True,
        check=True,
        timeout=60,
    )


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty git checkout the guard can be pointed at with ``--root``."""
    # The guard runs git with the ambient environment; an exported GIT_DIR would
    # answer for another repository.
    for var in _GIT_LOCATION_VARS:
        monkeypatch.delenv(var, raising=False)
    # ...but keep git from climbing out of tmp_path into an enclosing checkout.
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", os.path.realpath(tmp_path))
    # A job-wide whole-tree override (the main audit sets one) would hide the
    # scope these fixtures exist to exercise.
    monkeypatch.delenv("RATCHET_SCOPE_WHOLE_TREE", raising=False)
    root = tmp_path / "checkout"
    root.mkdir()
    _git(root, "init", "-q")
    return root


@pytest.fixture(scope="module")
def checkout_scan() -> cap.Scan:
    """One scan of the real checkout, shared by every test that reads it."""
    return cap.scan(REPO_ROOT, cap.tracked_files(REPO_ROOT))


def _judge_checkout(verdict: cap.Verdict, changed: set[str] | None, label: str) -> list[str]:
    """The warnings the checkout test emits; raises when the verdict is enforced.

    Growth reports and main-drift warnings never fail. With a scope, a failure
    fails the test. Without one there is no telling which files the change
    touched, so every failure is reported instead: failing on another change's
    file would turn every open change red.
    """
    notes = [f"file line cap: {note}" for note in verdict.reports + verdict.warnings]
    if changed is None:
        return notes + [
            f"file line cap, advisory (scope {label}: this checkout cannot tell which "
            "files the change touched, so the verdict is reported, not enforced, here; "
            "`python3 scripts/check_file_line_cap.py` with the full history enforces "
            f"it): {failure}"
            for failure in verdict.failures
        ]
    assert not verdict.failures, "\n\n".join(
        [f"{len(verdict.failures)} line-cap failure(s) (scope {label}):", *verdict.failures]
    )
    return notes


class TestTheCheckout:
    def test_the_change_keeps_the_cap_and_its_baseline_rows(self, checkout_scan: cap.Scan) -> None:
        scope = cap.change_scope(REPO_ROOT)
        verdict = cap.judge(checkout_scan, cap.read_baseline(REPO_ROOT), scope)
        for note in _judge_checkout(verdict, scope.changed, scope.label):
            warnings.warn(note, UserWarning, stacklevel=1)

    def test_the_scan_reaches_backend_and_website_code(self, checkout_scan: cap.Scan) -> None:
        # A scan that silently measured nothing would pass the check above.
        measured = checkout_scan.lines
        assert len(measured) > 5000, len(measured)
        assert any(p.startswith("src/kiro_crew/") and p.endswith(".py") for p in measured)
        assert any(p.startswith("test/") and p.endswith(".py") for p in measured)
        assert any(p.startswith("website/src/") and p.endswith(".tsx") for p in measured)

    def test_the_baseline_is_in_canonical_form(self) -> None:
        text = BASELINE.read_text(encoding="utf-8")
        parsed = cap.parse_baseline(text)
        assert not parsed.problems, parsed.problems
        assert text == cap.render_baseline(parsed.rows), (
            f"{BASELINE.relative_to(REPO_ROOT).as_posix()} is not the generated header "
            "followed by sorted `<lines> <path>` rows; `python3 "
            "scripts/check_file_line_cap.py --update-baseline` rewrites it in that form"
        )


class TestLineCounting:
    @pytest.mark.parametrize(
        ("data", "lines"),
        [
            (b"", 0),
            (b"x", 1),
            (b"x\n", 1),
            (b"\n", 1),
            (b"x\ny", 2),
            (b"x\ny\n", 2),
            (b"x\r\ny\r\n", 2),
            (b"x\r\ny", 2),
            (b"\n\n\n", 3),
        ],
    )
    def test_lines_are_newlines_plus_an_unterminated_last_line(
        self, data: bytes, lines: int
    ) -> None:
        assert cap.count_lines(data) == lines

    def test_a_nul_in_the_first_8_kib_marks_a_file_binary(self) -> None:
        window = 8 * 1024
        assert cap.BINARY_PROBE_BYTES == window
        assert cap.is_binary(b"x" * (window - 1) + b"\0")
        assert not cap.is_binary(b"x" * window + b"\0")
        assert not cap.is_binary(b"x\n" * CAP)

    def test_a_binary_file_is_not_measured(self, tmp_path: Path) -> None:
        (tmp_path / "blob.js").write_bytes(b"\0" + b"x\n" * (CAP * 2))
        scanned = cap.scan(tmp_path, ["blob.js"])
        assert scanned.lines == {}
        assert scanned.skipped == {"blob.js": "binary"}


class TestScope:
    def test_the_code_extensions_cover_both_surfaces(self) -> None:
        assert cap.CODE_EXTENSIONS == {
            ".py",
            ".pyi",
            ".ts",
            ".tsx",
            ".js",
            ".jsx",
            ".mjs",
            ".cjs",
            ".css",
            ".scss",
            ".sh",
            ".bash",
            ".ps1",
            ".swift",
        }

    def test_an_extension_matches_in_any_case(self) -> None:
        assert cap.is_code_path("tools/Build.PS1")
        assert not cap.is_code_path("docs/notes.md")

    @pytest.mark.parametrize(
        "path",
        [
            "website/src/uiLocations/guidePlans.gen.ts",
            "src/kiro_crew/docs/ui-index.generated.json",
            "config-baseline.json",
            "test/link_screen_sites.py",
            "website/package-lock.json",
            "uv.lock",
            "website/src/i18n/locales/de.json",
            "src/kiro_crew/_vendor/llama_cpp/llama.py",
            "website/public/vendor/mermaid.min.js",
            "docs/assets/diagram.png",
            "website/public/fonts/inter.woff2",
            "THIRD-PARTY-NOTICES",
            "CHANGELOG.md",
            "docs/system-specs/common/code-style.md",
            ".github/workflows/ci.yml",
        ],
    )
    def test_each_documented_exemption_applies(self, path: str) -> None:
        exemption = cap.exemption_for(path)
        assert exemption is not None, path
        assert exemption.reason

    @pytest.mark.parametrize(
        "path",
        [
            "src/kiro_crew/_vendor_shim.py",
            "website/src/pages/Panel.gen.tsx",
            "test/test_link_screen_sites.py",
            "scripts/docs_lint.py",
            "website/src/i18n/format.ts",
            "website/src/utils/minify.js",
            ".github/scripts/take_away_check.py",
            "src/kiro_crew/docs/CHANGELOG.md.py",
        ],
    )
    def test_a_near_miss_is_not_exempt(self, path: str) -> None:
        assert cap.exemption_for(path) is None, path

    def test_the_exemption_patterns_are_exactly_the_documented_ones(self) -> None:
        # A widened pattern would silently exempt every file it newly covers that is
        # still under the cap, so the set is pinned, not sampled.
        assert {p for e in cap.EXEMPTIONS for p in e.patterns} == {
            "*.gen.ts",
            "*.generated.*",
            "config-baseline.json",
            "test/link_screen_sites.py",
            "package-lock.json",
            "*.lock",
            "website/src/i18n/locales/*.json",
            "src/kiro_crew/_vendor/*",
            "*.min.js",
            "*.png",
            "*.jpg",
            "*.jpeg",
            "*.gif",
            "*.webp",
            "*.ico",
            "*.icns",
            "*.pdf",
            "*.mp3",
            "*.mp4",
            "*.mov",
            "*.webm",
            "*.wav",
            "*.woff",
            "*.woff2",
            "*.ttf",
            "*.otf",
            "THIRD-PARTY-NOTICES*",
            "CHANGELOG.md",
            "docs/*.md",
            ".github/workflows/*.yml",
            ".github/workflows/*.yaml",
        }
        assert all(e.reason for e in cap.EXEMPTIONS)

    def test_an_exempt_file_is_not_measured(self, tmp_path: Path) -> None:
        _write_lines(tmp_path, "src/kiro_crew/_vendor/big.py", CAP * 2)
        scanned = cap.scan(tmp_path, ["src/kiro_crew/_vendor/big.py"])
        assert scanned.lines == {}
        assert scanned.skipped["src/kiro_crew/_vendor/big.py"].startswith("exempt (vendored")


class TestVerdicts:
    def test_a_file_at_the_cap_passes_and_one_line_more_fails(self, tmp_path: Path) -> None:
        _write_lines(tmp_path, "at_cap.py", CAP)
        _write_lines(tmp_path, "over.py", CAP + 1)
        verdict = _verdict(tmp_path, ["at_cap.py", "over.py"])
        assert [f.split()[0] for f in verdict.failures] == ["over.py"]

    def test_an_unterminated_last_line_counts(self, tmp_path: Path) -> None:
        _write_lines(tmp_path, "edge.ts", CAP + 1, terminated=False)
        assert cap.scan(tmp_path, ["edge.ts"]).lines == {"edge.ts": CAP + 1}
        assert len(_verdict(tmp_path, ["edge.ts"]).failures) == 1

    def test_an_unlisted_oversized_file_fails_with_the_remedy(self, tmp_path: Path) -> None:
        _write_lines(tmp_path, "website/src/Big.tsx", CAP + 5)
        (failure,) = _verdict(tmp_path, ["website/src/Big.tsx"]).failures
        assert failure.startswith(f"website/src/Big.tsx has {CAP + 5:,} lines")
        assert f"over the {CAP:,}-line cap" in failure
        assert "Split it; see AGENTS.md" in failure
        assert f"Never add a row to {cap.BASELINE_RELPATH}" in failure

    @pytest.mark.parametrize(
        ("rel", "lines", "state"),
        [
            ("gone.py", None, "does not exist"),
            ("small.py", 10, f"has 10 lines, within the {CAP:,}-line cap"),
            ("exact.py", CAP, f"has {CAP:,} lines, within the {CAP:,}-line cap"),
            ("gen/registry.gen.ts", CAP * 2, "is not checked: exempt (generated output"),
            ("data/notes.txt", CAP * 2, "is not checked: not a code file"),
        ],
    )
    def test_a_stale_row_fails_and_names_the_row_to_delete(
        self, tmp_path: Path, rel: str, lines: int | None, state: str
    ) -> None:
        paths = []
        if lines is not None:
            _write_lines(tmp_path, rel, lines)
            paths.append(rel)
        verdict = _verdict(tmp_path, paths, f"{CAP + 7} {rel}\n")
        (failure,) = verdict.failures
        assert failure.startswith(f"stale baseline row `{CAP + 7} {rel}`: {rel} {state}")
        assert f"Delete exactly that row from {cap.BASELINE_RELPATH}" in failure
        assert verdict.stale == {rel}

    def test_a_stale_row_for_a_binary_file_fails(self, tmp_path: Path) -> None:
        (tmp_path / "blob.js").write_bytes(b"\0" + b"x\n" * (CAP * 2))
        (failure,) = _verdict(tmp_path, ["blob.js"], f"{CAP + 1} blob.js\n").failures
        assert "blob.js is not checked: binary" in failure

    def test_growth_of_a_baselined_file_is_reported_not_failed(self, tmp_path: Path) -> None:
        _write_lines(tmp_path, "grew.py", CAP + 300)
        verdict = _verdict(tmp_path, ["grew.py"], f"{CAP + 100} grew.py\n")
        assert verdict.failures == []
        (report,) = verdict.reports
        assert report.startswith(f"grew.py grew past its baseline row ({CAP + 100:,} -> ")
        assert "must not grow" in report and "split it" in report

    def test_a_baselined_file_that_shrank_is_lowerable_not_failed(self, tmp_path: Path) -> None:
        _write_lines(tmp_path, "shrank.py", CAP + 1)
        verdict = _verdict(tmp_path, ["shrank.py"], f"{CAP + 900} shrank.py\n")
        assert verdict.failures == [] and verdict.reports == []
        assert verdict.lowerable == {"shrank.py": CAP + 1}

    def test_a_baselined_file_at_its_row_is_neither_reported_nor_lowerable(
        self, tmp_path: Path
    ) -> None:
        _write_lines(tmp_path, "kept.py", CAP + 40)
        verdict = _verdict(tmp_path, ["kept.py"], f"{CAP + 40} kept.py\n")
        assert verdict.failures == [] and verdict.reports == [] and verdict.lowerable == {}

    @pytest.mark.parametrize(
        ("rows", "problem"),
        [
            (f"{CAP + 1} b.py\n{CAP + 1} a.py\n", "a.py is out of order"),
            (f"{CAP + 1} a.py\n{CAP + 1} a.py\n", "a.py is listed twice"),
            (f"{CAP} a.py\n", f"a.py records {CAP} lines"),
            (f"0{CAP + 1} a.py\n", "is not a `<lines> <path>` row"),
            ("a.py\n", "is not a `<lines> <path>` row"),
            (f"{CAP + 1}  a.py\n", "is not a `<lines> <path>` row"),
            (f"{CAP + 1}\ta.py\n", "is not a `<lines> <path>` row"),
            (f"{CAP + 1} a.py \n", "is not a `<lines> <path>` row"),
            (f"{CAP + 1} a.py\n# a late comment\n", "a comment or blank line after"),
            (f"{CAP + 1} a.py\n\n{CAP + 1} b.py\n", "a comment or blank line after"),
            (f"{CAP + 1} src\\a.py\n", "not a plain repo-relative path"),
            (f"{CAP + 1} /src/a.py\n", "not a plain repo-relative path"),
            (f"{CAP + 1} ./src/a.py\n", "not a plain repo-relative path"),
            (f"{CAP + 1} src/../a.py\n", "not a plain repo-relative path"),
        ],
    )
    def test_a_malformed_baseline_fails(self, tmp_path: Path, rows: str, problem: str) -> None:
        baseline = cap.parse_baseline(cap.HEADER + rows)
        assert any(problem in p for p in baseline.problems), baseline.problems
        failures = cap.evaluate(cap.Scan(), baseline).failures
        assert any(f.startswith("malformed baseline: ") and problem in f for f in failures)

    def test_a_crlf_checkout_of_the_baseline_parses_the_same(self) -> None:
        text = cap.render_baseline({"a.py": CAP + 1, "b/c.ts": CAP + 2})
        crlf = cap.parse_baseline(text.replace("\n", "\r\n"))
        assert crlf.problems == []
        assert crlf.rows == {"a.py": CAP + 1, "b/c.ts": CAP + 2}

    def test_a_missing_baseline_is_the_strictest_one(self, tmp_path: Path) -> None:
        _write_lines(tmp_path, "big.py", CAP + 1)
        verdict = cap.evaluate(cap.scan(tmp_path, ["big.py"]), cap.read_baseline(tmp_path))
        assert [f.split()[0] for f in verdict.failures] == ["big.py"]


class TestScopedVerdict:
    def test_an_untouched_oversized_file_is_main_drift_not_a_failure(self, tmp_path: Path) -> None:
        _write_lines(tmp_path, "website/src/Drifted.tsx", CAP + 60)
        measured = cap.scan(tmp_path, ["website/src/Drifted.tsx"])
        verdict = cap.evaluate(measured, cap.parse_baseline(cap.HEADER), {"src/other.py"})
        assert verdict.failures == []
        (warning,) = verdict.warnings
        assert warning.startswith(f"main drift: website/src/Drifted.tsx {CAP + 60:,} lines")
        assert "Remedy: split it" in warning

    def test_a_touched_oversized_file_fails(self, tmp_path: Path) -> None:
        _write_lines(tmp_path, "website/src/Drifted.tsx", CAP + 60)
        measured = cap.scan(tmp_path, ["website/src/Drifted.tsx"])
        verdict = cap.evaluate(
            measured, cap.parse_baseline(cap.HEADER), {"website/src/Drifted.tsx"}
        )
        assert [f.split()[0] for f in verdict.failures] == ["website/src/Drifted.tsx"]
        assert verdict.warnings == []

    def test_a_stale_row_fails_only_for_a_touched_file(self, tmp_path: Path) -> None:
        _write_lines(tmp_path, "small.py", 10)
        measured = cap.scan(tmp_path, ["small.py"])
        baseline = cap.parse_baseline(cap.HEADER + f"{CAP + 3} small.py\n")
        untouched = cap.evaluate(measured, baseline, {"src/other.py"})
        assert untouched.failures == []
        assert [w.split("`")[1] for w in untouched.warnings] == [f"{CAP + 3} small.py"]
        touched = cap.evaluate(measured, baseline, {"small.py"})
        assert len(touched.failures) == 1 and touched.warnings == []
        assert "Delete only your own rows" in touched.failures[0]
        # Editing the baseline without touching small.py's row leaves it main drift.
        edit = cap.Scope({cap.BASELINE_RELPATH}, "test", dict(baseline.rows))
        edits_baseline = cap.judge(measured, baseline, edit)
        assert edits_baseline.failures == [] and len(edits_baseline.warnings) == 1

    def test_a_malformed_baseline_fails_whatever_the_scope(self) -> None:
        baseline = cap.parse_baseline(cap.HEADER + f"{CAP + 1} b.py\n{CAP + 1} a.py\n")
        failures = cap.evaluate(cap.Scan(), baseline, {"src/other.py"}).failures
        assert [f.split(":")[0] for f in failures] == ["malformed baseline"]

    def test_growth_is_reported_in_every_scope(self, tmp_path: Path) -> None:
        _write_lines(tmp_path, "grew.py", CAP + 9)
        measured = cap.scan(tmp_path, ["grew.py"])
        baseline = cap.parse_baseline(cap.HEADER + f"{CAP + 1} grew.py\n")
        for changed in (None, set(), {"grew.py"}):
            verdict = cap.evaluate(measured, baseline, changed)
            assert verdict.failures == [] and len(verdict.reports) == 1

    def test_a_scoped_refresh_edits_only_the_touched_files_rows(self, tmp_path: Path) -> None:
        for rel, lines in {"mine.py": 50, "theirs.py": 60, "shrunk_mine.py": CAP + 2}.items():
            _write_lines(tmp_path, rel, lines)
        _write_lines(tmp_path, "shrunk_theirs.py", CAP + 2)
        paths = ["mine.py", "theirs.py", "shrunk_mine.py", "shrunk_theirs.py"]
        baseline = cap.parse_baseline(
            cap.HEADER + f"{CAP + 9} mine.py\n{CAP + 9} shrunk_mine.py\n"
            f"{CAP + 9} shrunk_theirs.py\n{CAP + 9} theirs.py\n"
        )
        mine = {"mine.py", "shrunk_mine.py"}
        verdict = cap.evaluate(cap.scan(tmp_path, paths), baseline, mine)
        assert cap.pruned_rows(baseline, verdict, mine) == {
            "shrunk_mine.py": CAP + 2,
            "shrunk_theirs.py": CAP + 9,
            "theirs.py": CAP + 9,
        }

    def test_an_unresolvable_scope_and_the_overrides_judge_the_whole_tree(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert cap.change_scope(repo).changed is None
        requested = cap.change_scope(REPO_ROOT, whole_tree=True)
        assert requested.changed is None and requested.whole_tree_requested
        monkeypatch.setenv("RATCHET_SCOPE_WHOLE_TREE", "1")
        scope = cap.change_scope(REPO_ROOT)
        assert scope.changed is None and "RATCHET_SCOPE_WHOLE_TREE" in scope.label

    def test_a_baseline_edit_adding_a_row_is_judged_on_both_sides(self, tmp_path: Path) -> None:
        _write_lines(tmp_path, "old.py", CAP + 5)
        rows = {"ghost.py": CAP + 9, "old.py": CAP + 5}
        scope = cap.Scope({cap.BASELINE_RELPATH}, "test", {"old.py": CAP + 5})
        verdict = cap.judge(
            cap.scan(tmp_path, ["old.py"]), cap.parse_baseline(cap.render_baseline(rows)), scope
        )
        assert verdict.warnings == []
        assert sorted(f.split()[0] for f in verdict.failures) == ["stale", "this"]

    @pytest.mark.parametrize(
        ("lines", "lowered_to", "problem"),
        [
            (40, CAP + 10, f"stale baseline row `{CAP + 10} old.py`"),
            (CAP + 300, CAP + 100, f"lower it to {CAP + 300:,}"),
        ],
    )
    def test_a_lowered_row_that_misstates_its_file_fails(
        self, tmp_path: Path, lines: int, lowered_to: int, problem: str
    ) -> None:
        _write_lines(tmp_path, "old.py", lines)
        scope = cap.Scope({cap.BASELINE_RELPATH}, "test", {"old.py": CAP + 500})
        verdict = cap.judge(
            cap.scan(tmp_path, ["old.py"]),
            cap.parse_baseline(cap.render_baseline({"old.py": lowered_to})),
            scope,
        )
        (failure,) = verdict.failures
        assert problem in failure
        assert verdict.reports == [] and verdict.warnings == []

    def test_the_cli_warns_on_main_drift_and_fails_on_the_change(
        self,
        repo: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _write_lines(repo, "drifted.py", CAP + 1)
        _git(repo, "add", "-A")
        monkeypatch.setattr(
            cap, "change_scope", lambda root, whole_tree=False: cap.Scope({"other.py"}, "test")
        )
        assert cap.main(["--root", str(repo)]) == 0
        assert "WARN main drift: drifted.py" in capsys.readouterr().out
        monkeypatch.setattr(
            cap, "change_scope", lambda root, whole_tree=False: cap.Scope({"drifted.py"}, "test")
        )
        assert cap.main(["--root", str(repo)]) == 1
        assert "FAIL drifted.py has" in capsys.readouterr().out

    def test_someone_elses_stale_row_beside_a_deletion_only_edit_is_main_drift(
        self, tmp_path: Path
    ) -> None:
        _write_lines(tmp_path, "mine.py", 100)
        _write_lines(tmp_path, "theirs.py", 100)
        measured = cap.scan(tmp_path, ["mine.py", "theirs.py"])
        base = {"mine.py": CAP + 50, "theirs.py": CAP + 70}
        baseline = cap.parse_baseline(cap.render_baseline({"theirs.py": CAP + 70}))
        verdict = cap.judge(
            measured, baseline, cap.Scope({cap.BASELINE_RELPATH, "mine.py"}, "test", base)
        )
        assert verdict.failures == []
        assert [w.split("`")[1] for w in verdict.warnings] == [f"{CAP + 70} theirs.py"]

    @pytest.mark.parametrize(
        ("rows", "problem"),
        [
            ({"old.py": CAP + 5, "new.py": CAP + 9}, f"adds the baseline row `{CAP + 9} new.py`"),
            ({"old.py": CAP + 6}, "raises the baseline row for old.py"),
        ],
    )
    def test_a_baseline_edit_that_adds_a_path_or_raises_a_count_fails(
        self, tmp_path: Path, rows: dict[str, int], problem: str
    ) -> None:
        for rel, lines in rows.items():
            _write_lines(tmp_path, rel, lines)
        baseline = cap.parse_baseline(cap.render_baseline(rows))
        scope = cap.Scope({cap.BASELINE_RELPATH}, "test", {"old.py": CAP + 5})
        verdict = cap.judge(cap.scan(tmp_path, sorted(rows)), baseline, scope)
        (failure,) = verdict.failures
        assert problem in failure and "the baseline only shrinks" in failure

    def test_a_deleted_row_whose_file_is_still_over_the_cap_fails(self, tmp_path: Path) -> None:
        _write_lines(tmp_path, "still_big.py", CAP + 30)
        scope = cap.Scope({cap.BASELINE_RELPATH}, "test", {"still_big.py": CAP + 30})
        verdict = cap.judge(
            cap.scan(tmp_path, ["still_big.py"]), cap.parse_baseline(cap.HEADER), scope
        )
        assert [f.split()[0] for f in verdict.failures] == ["still_big.py"]

    def test_an_unreadable_base_baseline_judges_the_whole_tree(self, tmp_path: Path) -> None:
        _write_lines(tmp_path, "elsewhere.py", CAP + 1)
        verdict = cap.judge(
            cap.scan(tmp_path, ["elsewhere.py"]),
            cap.parse_baseline(cap.HEADER),
            cap.Scope({cap.BASELINE_RELPATH}, "test"),
        )
        assert [f.split()[0] for f in verdict.failures] == ["elsewhere.py"]
        assert "cannot be read" in verdict.scope_note

    def test_lowering_the_row_of_a_file_that_grew_past_it_says_restore_the_base_row(
        self, tmp_path: Path
    ) -> None:
        # main grew theirs.py from its row's 2,070 to 2,300 lines; this change lowers
        # the row to 2,050. "Lower it to 2,300" would be a raise, so the remedy is the
        # base row, and the growth is still reported against it.
        _write_lines(tmp_path, "theirs.py", CAP + 300)
        scope = cap.Scope({cap.BASELINE_RELPATH}, "test", {"theirs.py": CAP + 70})
        verdict = cap.judge(
            cap.scan(tmp_path, ["theirs.py"]),
            cap.parse_baseline(cap.render_baseline({"theirs.py": CAP + 50})),
            scope,
        )
        (failure,) = verdict.failures
        assert f"restore the base row {CAP + 70:,}" in failure
        (report,) = verdict.reports
        assert report.startswith(f"theirs.py grew past its baseline row ({CAP + 70:,} -> ")

    def test_a_refresh_without_a_scope_refuses_unless_the_whole_tree_is_asked_for(
        self, repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _write_lines(repo, "a.py", 5)
        (repo / ".github").mkdir()
        (repo / cap.BASELINE_RELPATH).write_text(
            cap.render_baseline({"a.py": CAP + 1}), encoding="utf-8"
        )
        _git(repo, "add", "-A")
        assert cap.main(["--update-baseline", "--root", str(repo)]) == 2
        assert "pass --whole-tree" in capsys.readouterr().err
        assert cap.read_baseline(repo).rows == {"a.py": CAP + 1}
        assert cap.main(["--update-baseline", "--whole-tree", "--root", str(repo)]) == 0
        assert cap.read_baseline(repo).rows == {}

    def test_the_checkout_test_reports_without_failing_when_the_scope_is_unknown(self) -> None:
        verdict = cap.Verdict(failures=["drifted.py has 2,001 lines"], reports=["grew.py grew"])
        notes = _judge_checkout(verdict, None, "undeterminable (judging the whole tree)")
        assert len(notes) == 2
        assert "advisory (scope undeterminable" in notes[1]
        assert notes[1].endswith("drifted.py has 2,001 lines")

    def test_the_checkout_test_fails_when_the_scope_is_known(self) -> None:
        verdict = cap.Verdict(failures=["drifted.py has 2,001 lines"])
        with pytest.raises(AssertionError, match="drifted.py has 2,001 lines"):
            _judge_checkout(verdict, {"drifted.py"}, "origin/main...HEAD")


@pytest.fixture()
def branch_repo(repo: Path) -> Path:
    """A checkout with a ``main`` commit (two oversized files, both baselined) and a
    ``topic`` branch checked out on top of it: the local-branch scope shape."""
    _git(repo, "checkout", "-q", "-b", "main")
    _write_lines(repo, "mine.py", CAP + 50)
    _write_lines(repo, "theirs.py", CAP + 70)
    _write_lines(repo, "small.py", 10)
    (repo / ".github").mkdir()
    (repo / cap.BASELINE_RELPATH).write_text(
        cap.render_baseline({"mine.py": CAP + 50, "theirs.py": CAP + 70}), encoding="utf-8"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "checkout", "-q", "-b", "topic")
    return repo


class TestChangeScope:
    def test_uncommitted_edits_count_on_a_local_branch(self, branch_repo: Path) -> None:
        _write_lines(branch_repo, "small.py", 12)
        _git(branch_repo, "add", "small.py")
        _write_lines(branch_repo, "theirs.py", CAP + 71)
        scope = cap.change_scope(branch_repo)
        assert scope.changed == {"small.py", "theirs.py"}
        assert scope.label == "main...HEAD plus uncommitted edits"

    def test_a_rename_keeps_the_old_path_in_scope(self, branch_repo: Path) -> None:
        _git(branch_repo, "mv", "mine.py", "renamed.py")
        _git(branch_repo, "commit", "-q", "-m", "rename")
        scope = cap.change_scope(branch_repo)
        assert {"mine.py", "renamed.py"} <= (scope.changed or set())
        failures = cap.check(branch_repo, scope).failures
        assert sorted(f.split()[0] for f in failures) == ["renamed.py", "stale"]
        assert any(f"`{CAP + 50} mine.py`" in f for f in failures)

    def test_a_split_that_deletes_its_own_row_passes_beside_main_drift(
        self, branch_repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # theirs.py shrank on main without its row being pruned: main drift.
        _git(branch_repo, "checkout", "-q", "main")
        _write_lines(branch_repo, "theirs.py", 40)
        _git(branch_repo, "commit", "-q", "-am", "theirs shrank, row left behind")
        _git(branch_repo, "checkout", "-q", "-b", "split", "main")
        _write_lines(branch_repo, "mine.py", 100)
        (branch_repo / cap.BASELINE_RELPATH).write_text(
            cap.render_baseline({"theirs.py": CAP + 70}), encoding="utf-8"
        )
        _git(branch_repo, "commit", "-q", "-am", "split mine.py and delete its row")
        scope = cap.change_scope(branch_repo)
        assert scope.base_rows == {"mine.py": CAP + 50, "theirs.py": CAP + 70}
        assert cap.main(["--root", str(branch_repo)]) == 0
        out = capsys.readouterr().out
        assert f"WARN main drift: stale baseline row `{CAP + 70} theirs.py`" in out

    def test_a_branch_that_adds_a_row_fails(self, branch_repo: Path) -> None:
        _write_lines(branch_repo, "new_giant.py", CAP + 400)
        (branch_repo / cap.BASELINE_RELPATH).write_text(
            cap.render_baseline(
                {"mine.py": CAP + 50, "new_giant.py": CAP + 400, "theirs.py": CAP + 70}
            ),
            encoding="utf-8",
        )
        _git(branch_repo, "add", "-A")
        _git(branch_repo, "commit", "-q", "-m", "absorb a new giant")
        (failure,) = cap.check(branch_repo, cap.change_scope(branch_repo)).failures
        assert f"adds the baseline row `{CAP + 400} new_giant.py`" in failure

    @pytest.mark.parametrize("commit_the_split", [True, False])
    def test_a_scoped_refresh_deletes_only_this_changes_row(
        self, branch_repo: Path, commit_the_split: bool
    ) -> None:
        _git(branch_repo, "checkout", "-q", "main")
        _write_lines(branch_repo, "theirs.py", 40)
        _git(branch_repo, "commit", "-q", "-am", "theirs shrank, row left behind")
        _git(branch_repo, "checkout", "-q", "-b", "split", "main")
        _write_lines(branch_repo, "mine.py", 100)
        if commit_the_split:
            _git(branch_repo, "commit", "-q", "-am", "split mine.py")
        assert cap.main(["--update-baseline", "--root", str(branch_repo)]) == 0
        assert cap.read_baseline(branch_repo).rows == {"theirs.py": CAP + 70}
        # The refresh's own (uncommitted) baseline edit is then judged clean.
        assert cap.main(["--root", str(branch_repo)]) == 0

    def test_a_refresh_refuses_when_the_base_copy_of_the_edited_baseline_is_unreadable(
        self, repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _git(repo, "checkout", "-q", "-b", "main")
        _write_lines(repo, "a.py", 5)
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "base without a baseline")
        _git(repo, "checkout", "-q", "-b", "topic")
        (repo / ".github").mkdir()
        (repo / cap.BASELINE_RELPATH).write_text(cap.render_baseline({}), encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "introduce the baseline")
        assert cap.change_scope(repo).changed is not None
        before = (repo / cap.BASELINE_RELPATH).read_bytes()
        assert cap.main(["--update-baseline", "--root", str(repo)]) == 2
        assert "cannot be read" in capsys.readouterr().err
        assert (repo / cap.BASELINE_RELPATH).read_bytes() == before
        assert cap.main(["--update-baseline", "--whole-tree", "--root", str(repo)]) == 0

    def test_the_ci_merge_shape_keeps_a_renamed_files_old_path(self, branch_repo: Path) -> None:
        _git(branch_repo, "mv", "mine.py", "renamed.py")
        _write_lines(branch_repo, "renamed.py", 100)
        (branch_repo / cap.BASELINE_RELPATH).write_text(
            cap.render_baseline({"theirs.py": CAP + 70}), encoding="utf-8"
        )
        _git(branch_repo, "commit", "-q", "-am", "rename and split mine.py")
        # main moves after the fork: the base is HEAD^1, main's tip, not the merge base.
        _git(branch_repo, "checkout", "-q", "main")
        _write_lines(branch_repo, "zz.py", CAP + 5)
        (branch_repo / cap.BASELINE_RELPATH).write_text(
            cap.render_baseline({"mine.py": CAP + 50, "theirs.py": CAP + 70, "zz.py": CAP + 5}),
            encoding="utf-8",
        )
        _git(branch_repo, "add", "-A")
        _git(branch_repo, "commit", "-q", "-m", "main moves on")
        _git(branch_repo, "merge", "-q", "--no-ff", "-m", "merge topic", "topic")
        scope = cap.change_scope(branch_repo)
        assert scope.label == "merge HEAD^1..HEAD"
        assert {"mine.py", "renamed.py", cap.BASELINE_RELPATH} <= (scope.changed or set())
        assert scope.base_rows == {"mine.py": CAP + 50, "theirs.py": CAP + 70, "zz.py": CAP + 5}
        assert cap.check(branch_repo, scope).failures == []


class TestUpdateBaseline:
    def _commit_tree(self, repo: Path, files: dict[str, int], rows: dict[str, int]) -> None:
        for rel, lines in files.items():
            _write_lines(repo, rel, lines)
        baseline = repo / cap.BASELINE_RELPATH
        baseline.parent.mkdir(parents=True, exist_ok=True)
        baseline.write_text(cap.render_baseline(rows), encoding="utf-8")
        _git(repo, "add", "-A")

    def test_it_deletes_stale_rows_and_lowers_counts(self, repo: Path) -> None:
        self._commit_tree(
            repo,
            {"shrank.py": CAP + 5, "split.py": 900, "kept.py": CAP + 40},
            {"gone.py": CAP + 1, "kept.py": CAP + 40, "shrank.py": CAP + 50, "split.py": CAP + 9},
        )
        assert cap.main(["--update-baseline", "--whole-tree", "--root", str(repo)]) == 0
        text = (repo / cap.BASELINE_RELPATH).read_text(encoding="utf-8")
        assert text == cap.render_baseline({"kept.py": CAP + 40, "shrank.py": CAP + 5})
        assert cap.main(["--root", str(repo)]) == 0

    def test_it_never_adds_a_path_or_raises_a_count(self, repo: Path) -> None:
        self._commit_tree(
            repo, {"new_giant.py": CAP * 3, "grew.py": CAP + 500}, {"grew.py": CAP + 1}
        )
        assert cap.main(["--update-baseline", "--whole-tree", "--root", str(repo)]) == 0
        rows = cap.read_baseline(repo).rows
        assert rows == {"grew.py": CAP + 1}
        assert cap.main(["--root", str(repo)]) == 1

    def test_it_refuses_a_malformed_baseline_and_leaves_it_untouched(
        self, repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._commit_tree(repo, {"a.py": CAP + 1}, {})
        baseline = repo / cap.BASELINE_RELPATH
        malformed = f"{CAP + 1} b.py\n{CAP + 1} a.py\n"
        baseline.write_text(malformed, encoding="utf-8")
        assert cap.main(["--update-baseline", "--whole-tree", "--root", str(repo)]) == 2
        assert "fix it by hand" in capsys.readouterr().err
        assert baseline.read_text(encoding="utf-8") == malformed

    def test_it_does_not_create_a_baseline(self, repo: Path) -> None:
        _write_lines(repo, "big.py", CAP + 1)
        _git(repo, "add", "-A")
        assert cap.main(["--update-baseline", "--whole-tree", "--root", str(repo)]) == 0
        assert not (repo / cap.BASELINE_RELPATH).exists()


class TestGitEnumeration:
    def test_only_regular_files_are_listed_once_each(self) -> None:
        blob = "0" * 40
        output = b"\0".join(
            [
                f"100644 {blob} 0\tsrc/a.py".encode(),
                f"100755 {blob} 0\tscripts/run.sh".encode(),
                f"120000 {blob} 0\tsrc/link.py".encode(),
                f"160000 {blob} 0\tvendor/submodule".encode(),
                f"100644 {blob} 1\tsrc/conflict.py".encode(),
                f"100644 {blob} 2\tsrc/conflict.py".encode(),
                f"100644 {blob} 3\tsrc/conflict.py".encode(),
                f"100644 {blob} 0\t".encode() + "src/café.py".encode(),
                b"",
            ]
        )
        assert cap.parse_ls_files_stage(output) == [
            "src/a.py",
            "scripts/run.sh",
            "src/conflict.py",
            "src/café.py",
        ]

    def test_a_record_without_a_path_is_an_error(self) -> None:
        with pytest.raises(cap.CheckError):
            cap.parse_ls_files_stage(b"100644 deadbeef 0 no-tab-here\0")

    def test_only_tracked_files_count(self, repo: Path) -> None:
        _write_lines(repo, "tracked.py", CAP + 1)
        _write_lines(repo, "scratch/untracked.py", CAP * 4)
        _git(repo, "add", "tracked.py")
        assert cap.tracked_files(repo) == ["tracked.py"]
        verdict = cap.check(repo)
        assert [f.split()[0] for f in verdict.failures] == ["tracked.py"]

    def test_a_tracked_file_deleted_from_the_worktree_is_skipped(self, repo: Path) -> None:
        _write_lines(repo, "deleted.py", CAP + 1)
        _git(repo, "add", "deleted.py")
        (repo / "deleted.py").unlink()
        assert cap.check(repo).failures == []

    def test_the_exit_codes(self, repo: Path, tmp_path: Path) -> None:
        _write_lines(repo, "ok.py", 3)
        _git(repo, "add", "-A")
        assert cap.main(["--root", str(repo)]) == 0
        _write_lines(repo, "big.py", CAP + 1)
        _git(repo, "add", "-A")
        assert cap.main(["--root", str(repo)]) == 1
        not_a_checkout = tmp_path / "plain"
        not_a_checkout.mkdir()
        assert cap.main(["--root", str(not_a_checkout)]) == 2

    def test_an_unreadable_baseline_is_an_environment_error(
        self, repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _write_lines(repo, "ok.py", 3)
        (repo / ".github").mkdir()
        (repo / cap.BASELINE_RELPATH).write_bytes(b"\xff\xfe\x00")
        _git(repo, "add", "-A")
        assert cap.main(["--root", str(repo)]) == 2
        assert f"cannot read {cap.BASELINE_RELPATH}" in capsys.readouterr().err

    def test_a_relative_root_names_the_same_checkout(self, repo: Path) -> None:
        _write_lines(repo, "big.py", CAP + 1)
        (repo / ".github").mkdir()
        (repo / cap.BASELINE_RELPATH).write_text(
            cap.render_baseline({"big.py": CAP + 1}), encoding="utf-8"
        )
        _git(repo, "add", "-A")
        for args in ([], ["--update-baseline", "--whole-tree"]):
            # The script as a child, so `--root ..` resolves against a cwd inside the
            # fixture checkout rather than the test process's own.
            proc = subprocess.run(
                [sys.executable, str(SCRIPTS / "check_file_line_cap.py"), *args, "--root", ".."],
                cwd=repo / ".github",
                capture_output=True,
                timeout=60,
                check=False,
            )
            assert proc.returncode == 0, proc.stdout + proc.stderr
        assert cap.read_baseline(repo).rows == {"big.py": CAP + 1}

    def test_the_cli_prints_every_failure_and_report(
        self, repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        for rel, lines in {"big.py": CAP + 2, "grew.py": CAP + 3}.items():
            _write_lines(repo, rel, lines)
        (repo / ".github").mkdir()
        (repo / cap.BASELINE_RELPATH).write_text(
            cap.render_baseline({"gone.py": CAP + 1, "grew.py": CAP + 1}), encoding="utf-8"
        )
        _git(repo, "add", "-A")
        assert cap.main(["--root", str(repo)]) == 1
        scope_line, *lines, summary = capsys.readouterr().out.splitlines()
        assert scope_line == ("check_file_line_cap scope: undeterminable (judging the whole tree)")
        assert sorted(" ".join(line.split()[:2]) for line in lines) == [
            "FAIL big.py",
            "FAIL stale",
            "REPORT grew.py",
        ]
        assert summary == (
            "check_file_line_cap: 2 code files checked, 2 failure(s), 0 main-drift "
            "warning(s), 1 report(s)."
        )


class TestSelfTest:
    def test_the_self_test_passes_on_a_fresh_tree(self, tmp_path: Path) -> None:
        assert cap._self_test(tmp_path) == []

    def test_the_cli_self_test_passes(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert cap.main(["--test"]) == 0
        assert "all checks passed" in capsys.readouterr().out


def _seed_selector_root(root: Path) -> None:
    """This file's own text at its own name, beside a control that must not be selected."""
    (root / "test").mkdir(parents=True)
    (root / "test" / THIS.name).write_text(THIS.read_text(encoding="utf-8"), encoding="utf-8")
    (root / "test" / "test_plain_control.py").write_text(
        "def test_x():\n    assert True\n", encoding="utf-8"
    )
    assert sorted(p.name for p in (root / "test").iterdir()) == [
        "test_file_line_cap.py",
        "test_plain_control.py",
    ]


def _load_surface_selector():
    """Load the hyphenated ``scripts/ci-surface-tests.py`` as a fresh module."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "ci_surface_tests_for_line_cap", SCRIPTS / "ci-surface-tests.py"
    )
    assert spec and spec.loader, "could not build an import spec for ci-surface-tests.py"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestCiReach:
    def test_the_leaf_test_reduction_runs_this_file_as_a_corpus_gate(self, tmp_path: Path) -> None:
        gates = leaf_test_scope.corpus_gates(REPO_ROOT)
        assert THIS_REL in gates
        # Not vacuous: the derivation still filters, and on a two-file tree it
        # selects exactly this file and not the control.
        assert len(gates) < len(list((REPO_ROOT / "test").glob("test_*.py")))
        _seed_selector_root(tmp_path)
        assert leaf_test_scope.corpus_gates(tmp_path) == [f"test/{THIS.name}"]

    def test_a_frontend_only_diff_still_runs_this_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        selector = _load_surface_selector()
        # The facts a synthetic root cannot carry, read from the real tree.
        assert any(THIS_REL.startswith(f"{root}/") for root in selector._BACKEND_ROOTS)
        assert THIS.name not in selector._windows_collect_ignore(REPO_ROOT)
        # The real selection on a tree holding this file and a control, on both
        # platform branches. Reading the whole real test tree here would repeat
        # the scan test_ci_surface_tests already pays.
        _seed_selector_root(tmp_path)
        monkeypatch.setattr(selector, "_repo_root", lambda: tmp_path)
        for windows in (False, True):
            monkeypatch.setattr(selector, "_is_windows", lambda windows=windows: windows)
            assert selector.collect("backend") == [f"test/{THIS.name}"]


class TestCiWiring:
    """The jobs that enforce the scoped verdict run the guard with full history."""

    @staticmethod
    def _job(workflow: str, job: str) -> dict:
        import yaml

        text = (REPO_ROOT / ".github" / "workflows" / workflow).read_text(encoding="utf-8")
        return yaml.safe_load(text)["jobs"][job]

    @staticmethod
    def _runs(job: dict) -> list[str]:
        return [str(step.get("run") or "") for step in job.get("steps") or []]

    @pytest.mark.parametrize("job", ["backend-lint", "frontend-lint"])
    def test_a_lint_job_runs_the_guard_on_full_history(self, job: str) -> None:
        spec = self._job("ci.yml", job)
        checkouts = [
            step.get("with") or {}
            for step in spec["steps"]
            if str(step.get("uses", "")).startswith("actions/checkout@")
        ]
        # A shallow clone leaves the scope undeterminable, and the guard then
        # judges the whole tree, failing this change for every other change's file.
        assert [c.get("fetch-depth") for c in checkouts] == [0], checkouts
        runs = "\n".join(self._runs(spec))
        assert "python3 scripts/check_file_line_cap.py --test" in runs
        assert "python3 scripts/check_file_line_cap.py\n" in runs + "\n"

    def test_the_main_audit_judges_the_whole_tree(self) -> None:
        spec = self._job("main-ratchet-audit.yml", "ratchet-gates")
        assert spec["env"]["RATCHET_SCOPE_WHOLE_TREE"] == "1"
        assert any("python3 scripts/check_file_line_cap.py" in run for run in self._runs(spec))


class TestDocs:
    _DOCS = ("AGENTS.md", "docs/system-specs/common/code-style.md", "website/AGENTS.md")

    @pytest.mark.parametrize("doc", _DOCS)
    def test_the_rule_is_documented_where_authors_look(self, doc: str) -> None:
        assert "scripts/check_file_line_cap.py" in (REPO_ROOT / doc).read_text(encoding="utf-8")

    @pytest.mark.parametrize("doc", _DOCS)
    def test_the_documented_cap_is_the_enforced_one(self, doc: str) -> None:
        assert f"{CAP:,} lines" in (REPO_ROOT / doc).read_text(encoding="utf-8")


class TestWorkingTarget:
    """The 1,600-line working target says one figure in every place that states it.

    The cap is enforced; the target is advice, so nothing but this test notices
    when one of its homes moves and the others do not. The source is
    ``code-style.md`` § Splitting a file, which every other home points to. Each
    home is read down to the passage about the target (a section, a paragraph, a
    table row, one review rule), never the whole file, so an unrelated
    ``past 1,500 ms`` elsewhere cannot answer for it.
    """

    # Spelled out literally, so an edit to any home alone selects this test in the
    # local related-test runner, which matches on the path text.
    _SOURCE = "docs/system-specs/common/code-style.md"
    _HOMES = (
        "docs/system-specs/common/code-style.md",
        "AGENTS.md",
        "website/AGENTS.md",
        "AUTOSDE.yaml",
        "website/AUTOSDE.yaml",
        "src/kiro_crew/builtin_skills/kirocrew-dev/kirocrew-prepare-pr/SKILL.md",
    )
    # Every phrasing the homes use for the target. A figure is \d,\d{3}: the
    # homes write thousands with a comma, as they write the cap.
    _FIGURE = re.compile(r"\b(?:past|already over|holds|working target is) (\d,\d{3})\b")

    @staticmethod
    def _flat(text: str) -> str:
        return " ".join(text.split())

    @classmethod
    def _passage(cls, home: str) -> str:
        text = (REPO_ROOT / home).read_text(encoding="utf-8")
        if home.endswith("AUTOSDE.yaml"):
            import yaml

            rules = yaml.safe_load(text)["custom-rules"]
            (rule,) = [r for r in rules if r["id"] == "keep-modules-small"]
            return cls._flat(rule["rule"])
        if home == "docs/system-specs/common/code-style.md":
            start = text.index("### Splitting a file")
            end = text.find("\n#", start + 1)
            return cls._flat(text[start : end if end != -1 else None])
        if home == "AGENTS.md":
            start = text.index("**No hand-written file over")
            return cls._flat(text[start : text.index("\n\n", start)])
        # website/AGENTS.md's table row and the skill's checklist line are one line.
        anchor = {
            "website/AGENTS.md": "| a file nearing 2,000 lines",
            "src/kiro_crew/builtin_skills/kirocrew-dev/kirocrew-prepare-pr/SKILL.md": (
                "**Split a code file the diff takes past"
            ),
        }[home]
        start = text.index(anchor)
        return cls._flat(text[start : text.index("\n", start)])

    @classmethod
    def _figures(cls, passage: str) -> list[int]:
        return [int(m.replace(",", "")) for m in cls._FIGURE.findall(passage)]

    @classmethod
    def _target(cls) -> int:
        source = cls._passage(cls._SOURCE)
        match = re.search(r"takes a code file past (\d,\d{3}) lines", source)
        assert match, f"{cls._SOURCE} § Splitting a file no longer states the target"
        return int(match.group(1).replace(",", ""))

    def test_the_target_leaves_room_under_the_cap(self) -> None:
        assert 0 < self._target() < CAP

    @pytest.mark.parametrize("home", _HOMES)
    def test_each_home_states_the_target_and_no_other_figure(self, home: str) -> None:
        figures = self._figures(self._passage(home))
        target = self._target()
        assert figures, f"{home} no longer states the {target:,}-line working target"
        assert set(figures) == {target}, (
            f"{home} states {sorted(set(figures))}, but {self._SOURCE} says {target:,}: "
            "update every home in the same change"
        )

    def test_a_stale_home_is_caught(self) -> None:
        # The negative control: a home where one mention moved and one did not.
        stale = "A change past 1,500 lines splits; a file already over 1,600 too."
        assert set(self._figures(stale)) == {1500, 1600}
        # The cap's own sentence is not a statement of the target.
        assert self._figures("No hand-written file over 2,000 lines.") == []
