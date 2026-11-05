"""Unit tests for .github/scripts/adopted_wheel_copy_check.py.

The first property pinned is the one the gate is for: two real wheel-copy-only
fixes to a pinned app (one on papyrus's backend, one on file-explorer's server)
are flagged, so each must name its app-repo commit.
Owning spec: docs/system-specs/modules/app-kit-platform.md § Former built-in
adoption.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github" / "scripts" / "adopted_wheel_copy_check.py"
WORKFLOW = ROOT / ".github" / "workflows" / "pr-hygiene.yml"
SHIPPED_PINS = ROOT / "src" / "kiro_crew" / "apps" / "adoption-pins.json"

PAPYRUS = "KiroCrewCaptain/kirocrew-app-papyrus"
FILE_EXPLORER = "KiroCrewCaptain/kirocrew-app-file-explorer"
SHA = "54b741668eab5735106e6c594051559946bf1564"


def _load():
    spec = importlib.util.spec_from_file_location("adopted_wheel_copy_check", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mod = _load()


def _pins(*names: str) -> str:
    rows = [
        {"name": n, "repository": f"https://github.com/KiroCrewCaptain/kirocrew-app-{n}"}
        for n in names
    ]
    return json.dumps({"schemaVersion": 1, "pins": rows})


def _ns(*rows: tuple[str, str]) -> str:
    return "".join(f"{status}\t{path}\n" for status, path in rows)


PR30438 = _ns(
    ("M", "src/kiro_crew/apps/builtins/papyrus/backend/latex.py"),
    ("M", "test/test_papyrus_latex.py"),
)
PR30505 = _ns(
    ("M", "src/kiro_crew/apps/builtins/file_explorer/server.py"),
    ("M", "src/kiro_crew/apps/builtins/file_explorer/tests/test_server.py"),
)


class TestRealRegressions:
    def test_30438_papyrus_backend_fix_is_flagged(self) -> None:
        errors = mod.check(PR30438, [SHIPPED_PINS.read_text(encoding="utf-8")], "")
        assert len(errors) == 1 and errors[0].startswith("papyrus:"), errors
        assert f"App repo: {PAPYRUS}@<commit>" in errors[0]

    def test_30505_file_explorer_fix_is_flagged(self) -> None:
        errors = mod.check(PR30505, [SHIPPED_PINS.read_text(encoding="utf-8")], "")
        assert len(errors) == 1 and errors[0].startswith("file-explorer:"), errors
        assert "tests/test_server.py" not in errors[0]

    def test_naming_the_app_repo_commit_passes(self) -> None:
        body = f"## Tests\n\nApp repo: {PAPYRUS}@{SHA}\n"
        assert mod.check(PR30438, [SHIPPED_PINS.read_text(encoding="utf-8")], body) == []


class TestDetection:
    def test_frontend_wheel_copy_counts(self) -> None:
        ns = _ns(("M", "website/src/apps/papyrus/CoAuthorPanel.tsx"))
        assert list(mod.changed_apps(ns, mod.load_pins([_pins("papyrus")]))) == ["papyrus"]

    def test_added_file_counts(self) -> None:
        ns = _ns(("A", "src/kiro_crew/apps/builtins/personal_shopper/new.py"))
        assert list(mod.changed_apps(ns, mod.load_pins([_pins("personal-shopper")]))) == [
            "personal-shopper"
        ]

    def test_deleting_the_wheel_copy_is_allowed(self) -> None:
        ns = _ns(
            ("D", "src/kiro_crew/apps/builtins/papyrus/app.json"),
            ("D", "website/src/apps/papyrus/lib.ts"),
        )
        assert mod.check(ns, [_pins("papyrus")], "") == []

    def test_test_files_are_exempt(self) -> None:
        ns = _ns(
            ("M", "website/src/apps/papyrus/FileTree.cov80.test.tsx"),
            ("M", "src/kiro_crew/apps/builtins/papyrus/tests/test_x.py"),
        )
        assert mod.check(ns, [_pins("papyrus")], "") == []

    def test_an_unpinned_builtin_is_not_checked(self) -> None:
        ns = _ns(
            ("M", "src/kiro_crew/apps/builtins/mochi/server.py"),
            ("M", "website/src/apps/mochi/x.tsx"),
        )
        assert mod.check(ns, [_pins("papyrus")], "") == []

    def test_a_sibling_prefix_is_not_the_app(self) -> None:
        ns = _ns(
            ("M", "website/src/apps/papyrus-extra/x.ts"), ("M", "website/src/apps/papyrusApi.ts")
        )
        assert mod.check(ns, [_pins("papyrus")], "") == []

    def test_a_pin_dropped_at_the_head_still_counts(self) -> None:
        ns = _ns(("M", "src/kiro_crew/apps/builtins/papyrus/server.py"))
        assert len(mod.check(ns, [_pins("papyrus"), _pins()], "")) == 1

    def test_a_pin_added_at_the_head_counts(self) -> None:
        ns = _ns(("M", "src/kiro_crew/apps/builtins/papyrus/server.py"))
        assert len(mod.check(ns, ["", _pins("papyrus")], "")) == 1

    def test_a_pin_only_at_the_base_tip_counts(self) -> None:
        ns = _ns(("M", "src/kiro_crew/apps/builtins/papyrus/server.py"))
        assert len(mod.check(ns, ["", _pins("papyrus"), ""], "")) == 1

    def test_no_table_at_any_revision_passes(self) -> None:
        ns = _ns(("M", "src/kiro_crew/apps/builtins/papyrus/server.py"))
        assert mod.check(ns, ["", "", ""], "") == []


class TestMarker:
    def _errors(self, body: str) -> list[str]:
        ns = PR30438 + PR30505
        return mod.check(ns, [_pins("papyrus", "file-explorer")], body)

    def test_every_changed_app_needs_its_own_line(self) -> None:
        errors = self._errors(f"App repo: {PAPYRUS}@{SHA}\n")
        assert len(errors) == 1 and errors[0].startswith("file-explorer:")
        both = f"- App repo: {PAPYRUS}@{SHA[:7]}\n* App repo: {FILE_EXPLORER}#12\n"
        assert self._errors(both) == []

    def test_the_slug_compares_case_insensitively(self) -> None:
        body = f"App repo: {PAPYRUS.lower()}@{SHA}\r\nApp repo: {FILE_EXPLORER.upper()}#3\r\n"
        assert self._errors(body) == []

    def test_a_malformed_line_does_not_count(self) -> None:
        for bad in (
            f"App repo: {PAPYRUS}@<commit>",
            f"App repo: {PAPYRUS}@abc12",
            f"App repo: {PAPYRUS}",
            f"App repo: {PAPYRUS}#0",
            f"see App repo: {PAPYRUS}@{SHA}",
            f"App repo: KiroCrewCaptain/kirocrew-app-papyrus-fork@{SHA}",
            f"App repo: https://github.com/{PAPYRUS}@{SHA}",
        ):
            assert any(e.startswith("papyrus:") for e in self._errors(bad)), bad

    def test_an_unreadable_table_fails_closed(self) -> None:
        ns = _ns(("M", "src/kiro_crew/apps/builtins/papyrus/server.py"))
        for bad in ("{not json", json.dumps({"pins": [{"name": "papyrus"}]})):
            with pytest.raises(ValueError):
                mod.check(ns, [bad], "")


class TestShippedTable:
    def test_every_pinned_wheel_package_sits_where_the_check_looks(self) -> None:
        """A pinned app's backend package must be ``builtins/<name with _>``, or the gate misses it."""
        pins = mod.load_pins([SHIPPED_PINS.read_text(encoding="utf-8")])
        assert pins, "the shipped table pins nothing"
        builtins = ROOT / mod.BUILTINS_DIR
        for manifest in builtins.glob("*/app.json"):
            name = json.loads(manifest.read_text(encoding="utf-8")).get("name")
            if name in pins:
                assert f"{mod.BUILTINS_DIR}{manifest.parent.name}/" == mod.wheel_copy_dirs(name)[0]


class TestCli:
    def _run(
        self, tmp_path: Path, ns: str, pins: list[str], body: str
    ) -> subprocess.CompletedProcess[str]:
        ns_file = tmp_path / "ns.txt"
        ns_file.write_text(ns, encoding="utf-8")
        pin_files = []
        for i, text in enumerate(pins):
            path = tmp_path / f"pins{i}.json"
            path.write_text(text, encoding="utf-8")
            pin_files.append(str(path))
        env = {**os.environ, "PR_BODY": body}
        return subprocess.run(
            [sys.executable, str(SCRIPT), str(ns_file), *pin_files],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            cwd=tmp_path,
            check=False,
        )

    def test_fail_prints_the_line_to_add(self, tmp_path: Path) -> None:
        r = self._run(tmp_path, PR30438, [_pins("papyrus"), _pins("papyrus")], "")
        assert r.returncode == 1
        assert "::error::" in r.stdout and f"App repo: {PAPYRUS}@<commit>" in r.stdout

    def test_pass_with_the_marker(self, tmp_path: Path) -> None:
        r = self._run(tmp_path, PR30438, [_pins("papyrus")], f"App repo: {PAPYRUS}@{SHA}")
        assert r.returncode == 0, r.stdout

    def test_unreadable_table_fails(self, tmp_path: Path) -> None:
        r = self._run(tmp_path, PR30438, ["{not json"], "")
        assert r.returncode == 1 and "unreadable" in r.stdout

    def test_body_is_data_not_code(self, tmp_path: Path) -> None:
        body = '$(touch pwned) `touch pwned` "; import os; os.system("touch pwned")'
        r = self._run(tmp_path, PR30438, [_pins("papyrus")], body)
        assert r.returncode == 1
        assert not (tmp_path / "pwned").exists()


class TestWiring:
    def _step(self) -> dict:
        wf = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        steps = wf["jobs"]["pr-hygiene"]["steps"]
        names = [s.get("name", "") for s in steps]
        (index,) = [
            i for i, s in enumerate(steps) if "adopted_wheel_copy_check.py" in s.get("run", "")
        ]
        assert names.index("Resolve diff base (merge-base)") < index
        return steps[index]

    def test_pr_hygiene_runs_the_script_with_the_body_in_env(self) -> None:
        step = self._step()
        assert step["env"]["PR_BODY"] == "${{ github.event.pull_request.body }}"
        assert "pull_request.body" not in step["run"]

    def test_every_revisions_table_and_the_merge_base_diff_are_passed(self) -> None:
        step = self._step()
        assert step["env"]["MERGE_BASE"] == "${{ steps.diffbase.outputs.sha }}"
        run = step["run"]
        assert 'git diff --name-status --no-renames "$MERGE_BASE" "$HEAD_SHA"' in run
        assert 'for rev in "$MERGE_BASE" "$BASE_TIP" "$HEAD_SHA"' in run
        assert "src/kiro_crew/apps/adoption-pins.json" in run

    def test_the_script_is_read_from_the_base_ref(self) -> None:
        run = self._step()["run"]
        assert 'git show "$BASE_TIP:.github/scripts/adopted_wheel_copy_check.py"' in run
