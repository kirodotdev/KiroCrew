"""Auto-improvement agent discovery: focus ordering, dependents, surface normalization.

``test_agent_discovery_focus.py`` pins the edit-allowlist focus. These pin the pure
helpers that decide what the discovery agent reads each cycle and which of its
findings are trusted:

* ``prioritize_focus`` puts logic modules first and wiring last, rotates within each
  tier per cycle, and honours ``cap`` only when asked;
* ``changed_py_files`` and ``_is_test_path`` keep tests out of the focus;
* ``dependents_of`` maps a changed file to its importers, with caps;
* ``_normalize_surface`` only trusts a finding whose file really exists (and is in
  the diff scope when scoped), and fills the symbol and rule fallbacks;
* ``_render_focus_list`` bounds the per-cycle slice but keeps the rest visible;
* the prompt's skip list, and ``_diag_log``.

``_git`` is stubbed, so no git process runs. Files live under ``tmp_path``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew.apps.builtins.auto_improvement.spine import agent_discovery as ad


def _recording_git(calls: list[list[str]]):
    """A ``_git`` stand-in that records each argv and returns no output."""

    def _git(args: list[str], cwd: Path, timeout: float = 60.0) -> str:
        calls.append(args)
        return ""

    return _git


class TestPrioritizeFocus:
    FILES = [
        "src/pkg/__init__.py",  # wiring
        "src/pkg/util.py",  # middle
        "src/pkg/spine/gate.py",  # logic
        "src/pkg/routes.py",  # wiring
        "src/pkg/spine/ledger.py",  # logic
        "src/pkg/helpers.py",  # middle
    ]

    def test_logic_first_wiring_last_and_nothing_dropped(self) -> None:
        assert ad.prioritize_focus(self.FILES) == [
            "src/pkg/spine/gate.py",
            "src/pkg/spine/ledger.py",
            "src/pkg/helpers.py",
            "src/pkg/util.py",
            "src/pkg/__init__.py",
            "src/pkg/routes.py",
        ]

    def test_rotation_moves_each_tier_without_mixing_tiers(self) -> None:
        assert ad.prioritize_focus(self.FILES, rotate=1) == [
            "src/pkg/spine/ledger.py",
            "src/pkg/spine/gate.py",
            "src/pkg/util.py",
            "src/pkg/helpers.py",
            "src/pkg/routes.py",
            "src/pkg/__init__.py",
        ]

    def test_rotation_wraps_by_tier_size(self) -> None:
        assert ad.prioritize_focus(self.FILES, rotate=2) == ad.prioritize_focus(self.FILES)

    def test_windows_paths_are_tiered_like_posix_ones(self) -> None:
        # `/spine/` only matches once the separators are normalized; otherwise both
        # files would be middle tier and `aaa.py` would sort first.
        assert ad.prioritize_focus(["src\\pkg\\aaa.py", "src\\pkg\\spine\\x.py"]) == [
            "src\\pkg\\spine\\x.py",
            "src\\pkg\\aaa.py",
        ]

    def test_cap_applies_only_when_asked(self) -> None:
        assert ad.prioritize_focus(self.FILES, cap=2) == [
            "src/pkg/spine/gate.py",
            "src/pkg/spine/ledger.py",
        ]
        assert len(ad.prioritize_focus(self.FILES, cap=0)) == len(self.FILES)


@pytest.mark.parametrize(
    ("rel", "is_test"),
    [
        ("test/test_x.py", True),
        ("tests/x.py", True),
        ("src/pkg/test/helpers.py", True),
        ("src/pkg/tests/helpers.py", True),
        ("src/pkg/test_x.py", True),
        ("src\\pkg\\tests\\x.py", True),
        ("src/pkg/contest.py", False),
        ("src/pkg/latest.py", False),
    ],
)
def test_is_test_path(rel: str, is_test: bool) -> None:
    assert ad._is_test_path(rel) is is_test


class TestChangedFiles:
    def test_no_base_ref_asks_git_nothing(self, monkeypatch, tmp_path) -> None:
        calls: list[list[str]] = []
        monkeypatch.setattr(ad, "_git", _recording_git(calls))
        assert ad.changed_py_files(tmp_path, "") == []
        assert calls == []

    def test_only_product_python_files_are_kept(self, monkeypatch, tmp_path) -> None:
        seen: list[list[str]] = []

        def _git(args: list[str], cwd: Path, timeout: float = 60.0) -> str:
            seen.append(args)
            return "src/a.py\nREADME.md\n\n test/test_a.py \nsrc/pkg/b.py\n"

        monkeypatch.setattr(ad, "_git", _git)
        assert ad.changed_py_files(tmp_path, "main") == ["src/a.py", "src/pkg/b.py"]
        assert seen == [["diff", "--name-only", "main...HEAD"]]


class TestDependents:
    @pytest.fixture()
    def clone(self, tmp_path: Path) -> Path:
        (tmp_path / "src").mkdir()
        return tmp_path

    def test_no_src_dir_maps_nothing(self, tmp_path) -> None:
        assert ad.dependents_of(tmp_path, ["src/a.py"]) == {}

    def test_importers_exclude_the_file_itself_and_tests(self, monkeypatch, clone) -> None:
        patterns: list[str] = []

        def _git(args: list[str], cwd: Path, timeout: float = 60.0) -> str:
            patterns.append(args[2])
            return "src/pkg/a.py\nsrc/pkg/user.py\nsrc/pkg/tests/t.py\nsrc/pkg/notes.txt\n"

        monkeypatch.setattr(ad, "_git", _git)
        assert ad.dependents_of(clone, ["src/pkg/a.py"]) == {"src/pkg/a.py": ["src/pkg/user.py"]}
        assert patterns == [r"(import|from).*\ba\b"]

    def test_package_entry_points_are_not_probed(self, monkeypatch, clone) -> None:
        calls: list[list[str]] = []
        monkeypatch.setattr(ad, "_git", _recording_git(calls))
        assert ad.dependents_of(clone, ["src/pkg/__init__.py", "src/pkg/__main__.py"]) == {}
        assert calls == []

    def test_callers_and_files_are_capped(self, monkeypatch, clone) -> None:
        callers = "\n".join(f"src/c{i}.py" for i in range(10))
        monkeypatch.setattr(ad, "_git", lambda args, cwd, timeout=60.0: callers)
        out = ad.dependents_of(clone, [f"src/m{i}.py" for i in range(5)], limit=2)
        assert list(out) == ["src/m0.py", "src/m1.py"]
        assert all(len(v) == 6 for v in out.values())


class TestNormalizeSurface:
    @pytest.fixture()
    def clone(self, tmp_path: Path) -> Path:
        (tmp_path / "src" / "pkg").mkdir(parents=True)
        (tmp_path / "src" / "pkg" / "mod.py").write_text("x = 1\n")
        return tmp_path

    def _norm(self, raw: dict, clone: Path, scope: set[str] | None = None) -> dict | None:
        return ad._normalize_surface(raw, clone=clone, scope=scope, default_rule="logic")

    def test_a_full_record_is_kept_as_given(self, clone) -> None:
        raw = {
            "file": "src/pkg/mod.py",
            "line": "7",
            "symbol": "fn",
            "message": " off by one ",
            "hypothesis": "range end",
            "rule": "boundary",
        }
        assert self._norm(raw, clone) == {
            "target": "src/pkg/mod.py::fn",
            "rule": "boundary",
            "message": "off by one",
            "file": "src/pkg/mod.py",
            "line": 7,
            "symbol": "fn",
            "hypothesis": "range end",
        }

    def test_alternate_keys_and_fallbacks(self, clone) -> None:
        out = self._norm(
            {"path": "./src/pkg/mod.py", "line": 12, "title": "t", "repro": "r"}, clone
        )
        assert out is not None
        assert (out["symbol"], out["rule"], out["message"], out["hypothesis"]) == (
            "L12",
            "logic",
            "t",
            "r",
        )

    def test_no_line_and_no_symbol_falls_back_to_the_rule(self, clone) -> None:
        out = self._norm({"file": "src/pkg/mod.py", "line": "n/a", "rule": " "}, clone)
        assert out is not None
        assert (out["line"], out["symbol"], out["rule"]) == (0, "logic", "logic")

    def test_an_absolute_path_inside_the_clone_is_relativized(self, clone) -> None:
        out = self._norm({"file": str(clone / "src" / "pkg" / "mod.py")}, clone)
        # Relativized with the native separator, so compare as paths.
        assert out is not None and Path(out["file"]) == Path("src/pkg/mod.py")

    def test_an_absolute_path_outside_the_clone_is_not_trusted(self, clone, tmp_path) -> None:
        # Reduced to its basename, which is not a file at the clone root.
        assert self._norm({"file": "/elsewhere/src/pkg/mod.py"}, clone) is None

    @pytest.mark.parametrize("raw", [{}, {"file": "  "}, {"file": "src/pkg/missing.py"}])
    def test_no_file_or_a_missing_file_is_dropped(self, clone, raw) -> None:
        assert self._norm(raw, clone) is None

    def test_a_file_outside_the_diff_scope_is_dropped(self, clone) -> None:
        raw = {"file": "src/pkg/mod.py"}
        assert self._norm(raw, clone, scope={"src/other.py"}) is None
        assert self._norm(raw, clone, scope={"src/pkg/mod.py"}) is not None


class TestRenderFocusList:
    def test_a_short_list_is_rendered_whole_with_its_callers(self) -> None:
        out = ad._render_focus_list(["a.py", "b.py"], {"a.py": ["x.py", "y.py"]}, slice_n=5)
        assert out == "  - a.py   (used by: x.py, y.py)\n  - b.py"

    def test_a_long_list_bounds_the_slice_and_keeps_the_rest_visible(self) -> None:
        files = [f"f{i}.py" for i in range(5)]
        out = ad._render_focus_list(files, {}, slice_n=2).splitlines()
        assert out[0].startswith("PRIORITY SLICE")
        assert out[1:3] == ["  - f0.py", "  - f1.py"]
        assert out[3] == ""
        assert out[4].startswith("ALSO CHANGED (3 more")
        assert out[5:] == ["  - f2.py", "  - f3.py", "  - f4.py"]

    def test_a_zero_slice_renders_everything(self) -> None:
        files = [f"f{i}.py" for i in range(3)]
        assert "PRIORITY SLICE" not in ad._render_focus_list(files, {}, slice_n=0)


class TestSkipList:
    def _prompt(self, skip: list[str] | None) -> str:
        return ad._build_prompt(
            src_label="src/",
            changed=[],
            dependents={},
            scoped=False,
            limit=5,
            skip_targets=skip,
        )

    def test_no_skip_list_adds_no_block(self) -> None:
        assert "ALREADY HANDLED" not in self._prompt(None)
        assert "ALREADY HANDLED" not in self._prompt([])

    def test_the_skip_list_is_capped_with_a_count_of_the_rest(self) -> None:
        cap = ad.DEFAULT_SKIP_LIST_CAP
        prompt = self._prompt([f"src/m.py::f{i}" for i in range(cap + 3)])
        assert f"  - src/m.py::f{cap - 1}\n" in prompt
        assert f"src/m.py::f{cap}\n" not in prompt
        assert "…and 3 more already-handled loci." in prompt

    def test_a_short_skip_list_has_no_overflow_line(self) -> None:
        prompt = self._prompt(["src/m.py::f"])
        assert "  - src/m.py::f\n" in prompt
        assert "more already-handled loci" not in prompt


class TestDiagLog:
    def test_records_are_appended_one_json_object_per_line(self, tmp_path) -> None:
        log_dir = tmp_path / "logs" / "nested"
        ad._diag_log(log_dir, {"stage": "prompt", "path": Path("/x")})
        ad._diag_log(log_dir, {"stage": "done", "count": 0})
        lines = (log_dir / "agent_discovery.log").read_text().splitlines()
        assert [json.loads(line) for line in lines] == [
            {"stage": "prompt", "path": str(Path("/x"))},  # serialized with the native separator
            {"stage": "done", "count": 0},
        ]

    def test_no_log_dir_writes_nothing(self, tmp_path) -> None:
        ad._diag_log(None, {"stage": "prompt"})
        assert list(tmp_path.iterdir()) == []

    def test_a_write_failure_never_raises(self, tmp_path) -> None:
        blocker = tmp_path / "file"
        blocker.write_text("x")
        ad._diag_log(blocker, {"stage": "prompt"})  # a file where the dir should be
