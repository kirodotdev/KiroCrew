"""The subprocess-encoding gate must be real, wired into CI, and ratchet-only.

Follow-up to #3219/#3669 (#5249): text-mode subprocess calls without an
explicit ``encoding=`` decode with the Windows ANSI code page. The lint gate in
``scripts/check_subprocess_encoding.py`` keeps that class from growing back.
These tests pin the halves that must stay true together: CI actually runs the
gate (a gate that exists only on disk is not a gate), the AST rules flag what
they claim to flag, and the baseline can only shrink -- no operation may add a
path or raise a count.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_subprocess_encoding.py"
BASELINE = ROOT / ".github" / "subprocess-encoding-baseline.txt"
CI = ROOT / ".github" / "workflows" / "ci.yml"

SPEC = importlib.util.spec_from_file_location("check_subprocess_encoding", SCRIPT)
assert SPEC and SPEC.loader
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


def _lint_steps() -> list[dict]:
    workflow = yaml.safe_load(CI.read_text(encoding="utf-8"))
    for job in workflow["jobs"].values():
        steps = job.get("steps") or []
        if any("isort --check-only" in str(step.get("run", "")) for step in steps):
            return steps
    raise AssertionError("ci.yml has no job running isort --check-only")


class TestCiWiring:
    def test_ci_actually_runs_the_gate(self) -> None:
        runs = [str(step.get("run", "")) for step in _lint_steps()]
        assert any(
            "scripts/check_subprocess_encoding.py" in run for run in runs
        ), "ci.yml's lint job no longer runs the subprocess-encoding gate"

    def test_ci_runs_the_self_test_first(self) -> None:
        # The self-test plants one probe per rule family, so a typo that
        # silently disables a rule fails in CI instead of shipping green.
        for run in (str(step.get("run", "")) for step in _lint_steps()):
            if "check_subprocess_encoding.py" not in run:
                continue
            assert "--test" in run, "the gate step must run the --test self-test"
            return
        raise AssertionError("gate step not found")

    def test_scope_resolver_coupling_is_alive(self) -> None:
        # The gate imports the black gate's private _changed_paths; a rename
        # there must fail HERE, not as an AttributeError inside a CI run.
        assert callable(gate._load_changed_paths())


class TestRuleFamilies:
    """One probe per rule family, through the real detector."""

    def _lines(self, source: str) -> list[int]:
        return gate._violations_in_source(source)

    def test_flags_text_true_without_encoding(self) -> None:
        assert self._lines("import subprocess\nsubprocess.run(['git'], text=True)\n")

    def test_flags_universal_newlines(self) -> None:
        source = "import subprocess\nsubprocess.check_output(['git'], universal_newlines=True)\n"
        assert self._lines(source)

    def test_flags_multiline_calls(self) -> None:
        # The reason the check is AST-based: a regex cannot pair a text= on
        # one line with the absence of encoding= three lines away.
        source = (
            "import subprocess\n"
            "subprocess.Popen(\n"
            "    ['git', 'show'],\n"
            "    stdout=subprocess.PIPE,\n"
            "    text=True,\n"
            ")\n"
        )
        assert self._lines(source) == [2]

    def test_flags_kwargs_forwarding_wrappers(self) -> None:
        source = "from kiro_crew.sandbox import run_limited\nrun_limited(['git'], text=True)\n"
        assert self._lines(source)

    def test_flags_encoding_none(self) -> None:
        # encoding=None IS the locale fallback; presence of the keyword must
        # not read as a pin.
        source = "import subprocess\nsubprocess.run(['git'], text=True, encoding=None)\n"
        assert self._lines(source)

    def test_flags_dict_literal_splat_carrying_text(self) -> None:
        source = "import subprocess\nsubprocess.run(['git'], **{'text': True})\n"
        assert self._lines(source)

    def test_flags_dict_literal_splat_with_encoding_none(self) -> None:
        source = (
            "import subprocess\n" "subprocess.run(['git'], **{'text': True, 'encoding': None})\n"
        )
        assert self._lines(source)

    def test_marker_inside_a_string_literal_does_not_exempt(self) -> None:
        # The marker must be a COMMENT token; the phrase as call data is not
        # an author's opt-out decision.
        source = "import subprocess\n" f"subprocess.run(['echo', '{gate.MARKER}'], text=True)\n"
        assert self._lines(source) == [2]

    def test_explicit_encoding_is_compliant(self) -> None:
        source = "import subprocess\nsubprocess.run(['git'], text=True, encoding='utf-8')\n"
        assert self._lines(source) == []

    def test_utf8_text_splat_is_compliant(self) -> None:
        source = (
            "import subprocess\n"
            "from kiro_crew.subprocess_utf8 import UTF8_TEXT\n"
            "subprocess.run(['git'], **UTF8_TEXT)\n"
        )
        assert self._lines(source) == []

    def test_marker_opts_out_on_any_call_line(self) -> None:
        source = (
            "import subprocess\n"
            "subprocess.run(\n"
            "    ['ps'],\n"
            f"    text=True,  # {gate.MARKER}\n"
            ")\n"
        )
        assert self._lines(source) == []

    def test_marker_on_an_unrelated_line_does_not_leak(self) -> None:
        source = (
            "import subprocess\n"
            f"subprocess.run(['ps'], text=True)  # {gate.MARKER}\n"
            "subprocess.run(['git'], text=True)\n"
        )
        assert self._lines(source) == [3]

    def test_unparseable_source_raises_instead_of_reading_clean(self) -> None:
        # A parse failure reading as "zero violations" would invite a baseline
        # prune that deletes the file's real entry.
        with pytest.raises(SyntaxError):
            self._lines("def broken(:\n")

    def test_self_test_passes(self) -> None:
        assert gate._self_test() == 0


class TestVerdicts:
    """The ratchet's verdict logic, on synthetic inputs."""

    def test_unbaselined_file_in_scope_is_a_new_offender(self) -> None:
        new, grown, on_added, shrunk = gate._verdicts({"src/x.py": [10]}, {}, {"src/x.py"}, None)
        assert new == ["src/x.py"]
        assert not grown and not on_added and not shrunk

    def test_out_of_scope_files_are_not_judged(self) -> None:
        # CI evaluates a merge ref: someone else's file must not colour this PR.
        new, grown, on_added, shrunk = gate._verdicts(
            {"src/x.py": [10], "src/y.py": [5, 6]},
            {"src/y.py": 1},
            {"src/other.py"},
            None,
        )
        assert not new and not grown and not on_added and not shrunk

    def test_grown_count_fails(self) -> None:
        new, grown, on_added, shrunk = gate._verdicts(
            {"src/x.py": [1, 2, 3]}, {"src/x.py": 2}, {"src/x.py"}, None
        )
        assert grown == ["src/x.py"]

    def test_swapping_one_violation_for_another_is_caught_by_added_lines(
        self,
    ) -> None:
        # Fix one old call, add one new one: the count is level, but the new
        # call sits on an added line and must still fail.
        new, grown, on_added, shrunk = gate._verdicts(
            {"src/x.py": [10, 30]},
            {"src/x.py": 2},
            {"src/x.py"},
            {"src/x.py": {30}},
        )
        assert on_added == {"src/x.py": [30]}
        assert not new and not grown

    def test_shrunk_count_demands_a_prune(self) -> None:
        new, grown, on_added, shrunk = gate._verdicts(
            {"src/x.py": [10]}, {"src/x.py": 3}, {"src/x.py"}, None
        )
        assert shrunk == ["src/x.py"]

    def test_undeterminable_scope_judges_the_whole_tree(self) -> None:
        new, _, _, _ = gate._verdicts({"src/x.py": [10]}, {}, None, None)
        assert new == ["src/x.py"]


class TestBaselineRatchet:
    def test_committed_baseline_parses_and_files_exist(self) -> None:
        entries = gate._read_baseline(BASELINE)
        assert entries, "committed baseline is empty -- was it regenerated?"
        for rel, count in entries.items():
            assert count > 0, f"baseline lists {rel} with a zero count"
            assert not Path(rel).is_absolute(), f"absolute path in baseline: {rel}"
            assert (ROOT / rel).is_file(), f"baseline lists a deleted file: {rel}"

    def test_missing_baseline_refuses_rather_than_absorbs(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit, match="restore it from git"):
            gate._read_baseline(tmp_path / "absent.txt")

    def test_malformed_baseline_line_is_rejected(self, tmp_path: Path) -> None:
        bad = tmp_path / "baseline.txt"
        bad.write_text("notanumber src/x.py\n", encoding="utf-8")
        with pytest.raises(SystemExit, match="malformed"):
            gate._read_baseline(bad)

    def test_duplicate_baseline_entry_is_rejected(self, tmp_path: Path) -> None:
        # A later duplicate would silently override the recorded ceiling.
        bad = tmp_path / "baseline.txt"
        bad.write_text("1 src/x.py\n9 src/x.py\n", encoding="utf-8")
        with pytest.raises(SystemExit, match="duplicate"):
            gate._read_baseline(bad)

    def test_refresh_never_adds_a_path(self) -> None:
        survivors = gate._shrunken_baseline({"src/a.py": 2}, {"src/new.py": 5})
        assert "src/new.py" not in survivors

    def test_refresh_never_raises_a_count(self) -> None:
        survivors = gate._shrunken_baseline({"src/a.py": 2}, {"src/a.py": 7})
        assert survivors == {"src/a.py": 2}

    def test_refresh_lowers_and_prunes(self) -> None:
        survivors = gate._shrunken_baseline(
            {"src/a.py": 5, "src/clean.py": 3, "gone.py": 1},
            {"src/a.py": 2},
        )
        assert survivors == {"src/a.py": 2}

    def test_cli_server_stays_clean(self) -> None:
        """cli_server.py once held an unpinned text-mode call the baseline
        under-counted, so any PR touching the file failed the gate even when
        the PR added nothing (#5580). Both calls are pinned now and the
        baseline entry is pruned; this pins the file at zero so it can never
        silently re-enter the list."""
        rel = "src/kiro_crew/cli_server.py"
        violations = gate._violations_in_source((ROOT / rel).read_text(encoding="utf-8"))
        assert violations == []
        assert rel not in gate._read_baseline(BASELINE)

    def test_write_read_roundtrip(self, tmp_path: Path) -> None:
        path = tmp_path / "baseline.txt"
        entries = {"src/b.py": 2, "src/a.py": 7}
        gate._write_baseline(path, entries)
        assert gate._read_baseline(path) == entries
        # Header survives as comments and the body is sorted.
        body = [
            line
            for line in path.read_text(encoding="utf-8").splitlines()
            if line and not line.startswith("#")
        ]
        assert body == ["7 src/a.py", "2 src/b.py"]
