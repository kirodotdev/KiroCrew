"""The find_ui index freshness check is strict only where staleness is the PR's, and at release.

Pins `.github/scripts/ui_index_freshness.py` (CI's PR freshness step) and the
workflow wiring that keeps a release strict and nightly warn-only. The
generator's own warn/strict modes are pinned in
`website/src/uiLocations/uiIndex.test.ts`. Spec: "The find_ui index is strict
only where the PR caused it, and at release" in docs/ci/ci-and-reviews.md.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github" / "scripts" / "ui_index_freshness.py"
WORKFLOWS = ROOT / ".github" / "workflows"
BASE = "b" * 40


def _load():
    spec = importlib.util.spec_from_file_location("ui_index_freshness", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["ui_index_freshness"] = module
    spec.loader.exec_module(module)
    return module


mod = _load()

OLD_STALE = (
    "gen-ui-index: src/kiro_crew/docs/ui-index.generated.json is stale. "
    "Run `npm run gen:ui` in website/ and commit the result.\n"
)
NEW_STALE = (
    "gen-ui-index: src/kiro_crew/docs/ui-index.generated.json is stale; "
    "website/src/uiLocations/guidePlans.gen.ts is stale. Run `npm run gen:ui` in website/ and commit the result.\n"
)
PROBLEM = "gen-ui-index: 2 problem(s); the index was not written:\n  a\n  b\n"


def _workflow(name: str) -> dict:
    # PyYAML reads the bare `on:` key as the boolean True.
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


class TestClassify:
    def test_exit_zero_is_fresh(self):
        assert mod.classify(mod.Run(0, "")) == mod.FRESH

    def test_the_stale_exit_code_is_stale(self):
        assert mod.classify(mod.Run(mod.STALE_EXIT, NEW_STALE)) == mod.STALE

    def test_a_base_from_before_exit_3_reads_stale_by_its_message(self):
        assert mod.classify(mod.Run(1, OLD_STALE)) == mod.STALE

    def test_a_generator_problem_is_broken_not_stale(self):
        assert mod.classify(mod.Run(1, PROBLEM)) == mod.BROKEN
        assert mod.classify(mod.Run(1, PROBLEM + OLD_STALE)) == mod.BROKEN
        assert mod.classify(mod.Run(2, "gen-ui-index: unknown argument --x")) == mod.BROKEN


def _fake(results: dict[str, Any]):
    """A runner that answers per tree ('head' / 'base') and records the order it was asked."""
    calls: list[str] = []

    def runner(website: Path) -> Any:
        which = "base" if "ui-index-base" in str(website) else "head"
        calls.append(which)
        return results[which]

    def checkout(workspace: Path, sha: str, dest: Path) -> Optional[Path]:
        assert sha == BASE
        return dest / "website"

    return runner, checkout, calls


def _main(
    event: str,
    head: Any,
    base: Any = None,
    base_sha: str = BASE,
    checkout_ok: bool = True,
):
    runner, checkout, calls = _fake({"head": head, "base": base or mod.Run(0, "")})
    env = {
        "EVENT_NAME": event,
        "BASE_SHA": base_sha,
        "GITHUB_WORKSPACE": "/ws",
        "RUNNER_TEMP": "/rt",
    }
    code = mod.main(
        env, runner=runner, base_checkout=checkout if checkout_ok else (lambda *a: None)
    )
    return code, calls


class TestMain:
    def test_a_fresh_merge_ref_passes_without_checking_the_base(self, capsys):
        code, calls = _main("pull_request", mod.Run(0, ""))
        assert (code, calls) == (0, ["head"])

    def test_a_pr_that_makes_a_fresh_base_stale_fails(self, capsys):
        code, calls = _main("pull_request", mod.Run(3, NEW_STALE), base=mod.Run(0, ""))
        assert (code, calls) == (1, ["head", "base"])
        out = capsys.readouterr().out
        assert "::error::" in out and "This PR changed index inputs" in out

    def test_a_merge_group_that_makes_a_fresh_base_stale_fails(self):
        assert _main("merge_group", mod.Run(3, NEW_STALE), base=mod.Run(0, ""))[0] == 1

    def test_a_pr_on_an_already_stale_base_only_warns(self, capsys):
        code, calls = _main("pull_request", mod.Run(3, NEW_STALE), base=mod.Run(1, OLD_STALE))
        assert (code, calls) == (0, ["head", "base"])
        out = capsys.readouterr().out
        assert "::warning::" in out and "already stale" in out

    def test_a_push_to_main_only_warns_and_checks_no_base(self, capsys):
        code, calls = _main("push", mod.Run(3, NEW_STALE), base_sha="")
        assert (code, calls) == (0, ["head"])
        assert "::warning::" in capsys.readouterr().out

    def test_a_base_that_cannot_be_checked_out_fails_closed(self):
        assert _main("pull_request", mod.Run(3, NEW_STALE), checkout_ok=False)[0] == 1

    def test_a_broken_base_fails_closed(self):
        assert _main("pull_request", mod.Run(3, NEW_STALE), base=mod.Run(1, PROBLEM))[0] == 1

    def test_a_pr_with_no_base_sha_fails_closed(self):
        assert _main("pull_request", mod.Run(3, NEW_STALE), base_sha="")[0] == 1

    @pytest.mark.parametrize("event", ["pull_request", "push"])
    def test_a_generator_problem_fails_everywhere(self, event):
        code, calls = _main(event, mod.Run(1, PROBLEM))
        assert (code, calls) == (1, ["head"])


def _git(cwd: Path, *args: str) -> str:
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@e",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@e",
    }
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, **env},
    ).stdout.strip()


class TestCheckoutBase:
    def test_it_checks_out_the_base_beside_the_head_sharing_node_modules(self, tmp_path):
        upstream = tmp_path / "upstream"
        (upstream / "website").mkdir(parents=True)
        _git(upstream, "init", "-q")
        (upstream / "website" / "marker.txt").write_text("base\n")
        _git(upstream, "add", ".")
        _git(upstream, "commit", "-qm", "base")
        base_sha = _git(upstream, "rev-parse", "HEAD")
        (upstream / "website" / "marker.txt").write_text("head\n")
        _git(upstream, "commit", "-qam", "head")

        head = tmp_path / "head"
        _git(tmp_path, "clone", "-q", "--depth=1", f"file://{upstream}", str(head))
        (head / "website" / "node_modules").mkdir()

        site = mod.checkout_base(head, base_sha, tmp_path / "rt" / "ui-index-base")
        assert site is not None
        assert (site / "marker.txt").read_text() == "base\n"
        assert (site / "node_modules").resolve() == (head / "website" / "node_modules").resolve()

    def test_an_unknown_base_returns_none(self, tmp_path):
        head = tmp_path / "head"
        (head / "website").mkdir(parents=True)
        _git(head, "init", "-q")
        _git(head, "remote", "add", "origin", f"file://{tmp_path / 'missing'}")
        assert mod.checkout_base(head, BASE, tmp_path / "rt" / "ui-index-base") is None


class TestWorkflowWiring:
    def test_ci_runs_the_script_with_the_merge_refs_base(self):
        jobs = _workflow("ci.yml")["jobs"]
        step = next(
            s
            for s in jobs["frontend-lint"]["steps"]
            if s.get("name") == "Check the find_ui location index is fresh"
        )
        assert ".github/scripts/ui_index_freshness.py" in step["run"]
        assert "pull_request.base.sha" in step["env"]["BASE_SHA"]
        assert "merge_group.base_sha" in step["env"]["BASE_SHA"]
        assert step["env"]["EVENT_NAME"] == "${{ github.event_name }}"

    @pytest.mark.parametrize("job", ["build-wheel", "build-desktop", "build-windows"])
    def test_release_builds_are_strict(self, job):
        assert _workflow("release.yml")["jobs"][job]["with"]["ui_index_strict"] is True

    def test_nightly_never_asks_for_strict(self):
        for name, job in _workflow("nightly.yml")["jobs"].items():
            assert "ui_index_strict" not in (job.get("with") or {}), name

    @pytest.mark.parametrize("name", ["build-wheel.yml", "build-desktop.yml", "build-windows.yml"])
    def test_each_reusable_build_maps_the_input_to_the_env_the_generator_reads(self, name):
        wf = _workflow(name)
        assert wf["env"]["KC_UI_INDEX_STRICT"] == "${{ inputs.ui_index_strict && '1' || '' }}"
        for trigger, spec in wf[True].items():
            inputs = (spec or {}).get("inputs") or {}
            if inputs:
                assert inputs["ui_index_strict"]["default"] is False, trigger
                assert inputs["ui_index_strict"]["type"] == "boolean", trigger
