"""Tests for the prepare-pr project-profile mechanism.

Covers:
  * resolve_profile.py resolution order (config / kirocrew markers /
    auto-detect / generic) and the bundled KiroCrew profile contents.
  * pr_status.py readiness-context override (flag / env / default) and the
    positional-argument stripping that makes it work.

The scripts live under the packaged builtin skill and are NOT importable as a
package, so we load them by path with importlib. Everything here is stdlib.
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from skill_script_helpers import load_skill_script

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = REPO_ROOT / "src" / "kiro_crew" / "builtin_skills" / "kirocrew-dev" / "prepare-pr"
SCRIPTS_DIR = SKILL_DIR / "scripts"
PROFILES_DIR = SKILL_DIR / "profiles"


def _load(module_name, filename):
    return load_skill_script(module_name, SCRIPTS_DIR / filename)


resolve_profile = _load("_pp_resolve_profile", "resolve_profile.py")
pr_status = _load("_pp_pr_status", "pr_status.py")


# --------------------------------------------------------------------------
# resolve_profile.py
# --------------------------------------------------------------------------
def test_generic_fallback_on_empty_repo(tmp_path):
    prof = resolve_profile.resolve(str(tmp_path))
    assert prof["source"] == "generic"
    assert prof["setup"] == []
    assert prof["gates"] == []
    assert prof["reviewers"] == []
    assert prof["readiness"] == {"status_context": None, "defer_label": None}
    assert prof["single_commit"] is False


def test_profile_is_loaded_from_base_ref_not_worktree(tmp_path):
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    profile = tmp_path / ".prepare-pr.toml"
    profile.write_text("[project]\nsingle_commit = true\n")
    subprocess.run(["git", "add", ".prepare-pr.toml"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=tmp_path, check=True)
    profile.write_text("[project]\nsingle_commit = false\n")

    resolved = resolve_profile.resolve(str(tmp_path), base_ref="HEAD")

    assert resolved["source"] == "config"
    assert resolved["single_commit"] is True


def test_branch_only_profile_is_ignored_when_base_has_none(tmp_path):
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("base\n")
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=tmp_path, check=True)
    (tmp_path / ".prepare-pr.toml").write_text("[project]\nsingle_commit = true\n")

    resolved = resolve_profile.resolve(str(tmp_path), base_ref="HEAD")

    assert resolved["source"] == "generic"
    assert resolved["single_commit"] is False


def test_branch_deleting_a_review_workflow_cannot_drop_the_lane(tmp_path):
    """Auto-detection is pinned to the base ref too: deleting a review workflow
    in the checkout must not remove that reviewer from the resolved profile."""
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "codex-review.yml").write_text("name: review\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=tmp_path, check=True)
    (wf / "codex-review.yml").unlink()

    resolved = resolve_profile.resolve(str(tmp_path), base_ref="HEAD")

    assert resolved["source"] == "auto-detect"
    assert [r["name"] for r in resolved["reviewers"]] == ["codex-review"]
    assert resolved["reviewers"][0]["contract"] == ".github/workflows/codex-review.yml"


def test_unresolvable_base_ref_is_a_hard_error(tmp_path):
    """A base ref that names nothing must fail loudly, never silently hand
    resolution back to the branch checkout."""
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("base\n")
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=tmp_path, check=True)

    with pytest.raises(RuntimeError, match="cannot resolve base ref"):
        resolve_profile.resolve(str(tmp_path), base_ref="no-such-ref")


def test_cli_without_base_ref_pins_to_the_remote_default_branch(tmp_path):
    """The documented no-argument invocation must not read reviewer authority
    from the branch checkout when a remote base exists to pin to."""
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=upstream, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=upstream, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=upstream, check=True)
    (upstream / ".prepare-pr.toml").write_text("[project]\nsingle_commit = true\n")
    subprocess.run(["git", "add", ".prepare-pr.toml"], cwd=upstream, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=upstream, check=True)
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(upstream), str(clone)], check=True)
    (clone / ".prepare-pr.toml").write_text("[project]\nsingle_commit = false\n")

    proc = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "resolve_profile.py"), str(clone)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )

    assert json.loads(proc.stdout)["single_commit"] is True


def test_autodetect_python_stack(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    prof = resolve_profile.resolve(str(tmp_path))
    assert prof["source"] == "auto-detect"
    assert prof["setup"] == []
    assert "python -m pytest -q" in prof["gates"]


def test_autodetect_package_json_only_declared_scripts(tmp_path):
    (tmp_path / "package.json").write_text('{"scripts": {"build": "vite build"}}')
    prof = resolve_profile.resolve(str(tmp_path))
    assert prof["source"] == "auto-detect"
    assert "npm run build" in prof["gates"]
    assert "npm test" not in prof["gates"]  # no test script -> no test gate


def test_autodetect_package_json_no_scripts_emits_no_npm_gate(tmp_path):
    (tmp_path / "package.json").write_text('{"name": "x"}')
    prof = resolve_profile.resolve(str(tmp_path))
    assert all(not g.startswith("npm") for g in prof["gates"])


def test_autodetect_reviewers_from_workflows(tmp_path):
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "codex-review.yml").write_text("name: codex\n")
    (tmp_path / "go.mod").write_text("module x\n")
    prof = resolve_profile.resolve(str(tmp_path))
    assert prof["source"] == "auto-detect"
    names = [r["name"] for r in prof["reviewers"]]
    assert "codex-review" in names
    assert prof["reviewers"][0]["contract"].endswith("codex-review.yml")


def test_kirocrew_markers_load_bundled_profile(tmp_path):
    (tmp_path / "AUTOSDE.yaml").write_text("rules: []\n")
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "codex-review.yml").write_text("name: codex\n")
    (wf / "claude-review.yml").write_text("name: claude\n")
    prof = resolve_profile.resolve(str(tmp_path))
    assert prof["source"] == "kirocrew"
    assert prof["single_commit"] is True
    assert prof["base_branch"] == "main"
    assert prof["setup"] == ["(cd website && npx playwright install chromium)"]
    assert all("playwright install" not in gate for gate in prof["gates"])
    assert prof["readiness"]["status_context"] == "PR Readiness"
    models = {r["name"]: r["model"] for r in prof["reviewers"]}
    assert models["gpt"] == "gpt-5.6-sol"
    assert models["opus"] == "claude-opus-4.8"


def test_opus_profile_model_matches_the_ci_workflow():
    """The local reviewer must mirror the model CI actually runs.

    prepare-pr's whole value is that local-green predicts server-green. When the
    profile pinned claude-opus-5 while claude-review.yml had moved to
    opus-4-8, the local gate was reviewing with a different model than the gate
    it claims to mirror. This test fails the next time they diverge.

    The ids differ by namespace on purpose -- CI uses the Bedrock regional
    inference profile (`us.anthropic.claude-opus-4-8`), the local harness uses
    the kiro-cli id (`claude-opus-4.8`) -- so compare the normalized version.
    """
    workflow = (REPO_ROOT / ".github" / "workflows" / "claude-review.yml").read_text(
        encoding="utf-8"
    )
    # Match the real claude_args entry -- a line whose content IS the flag --
    # not the prose mention of "--model below" in the comment above the job.
    ci_models = re.findall(r"(?m)^\s*--model\s+(\S+)\s*$", workflow)
    assert ci_models, "could not find the --model argument in claude-review.yml"
    # The lane runs two stages (discovery, then validation), so there is one
    # --model per stage. They must agree with each other -- a lane that
    # discovers with one model and validates with another has no single model
    # for the local gate to mirror -- and that one value must match the profile.
    assert len(set(ci_models)) == 1, (
        f"claude-review.yml's stages disagree on the model: {ci_models}"
    )
    ci_model = ci_models[0]

    data = json.loads((PROFILES_DIR / "kirocrew.json").read_text(encoding="utf-8"))
    local_model = next(r["model"] for r in data["reviewers"] if r["name"] == "opus")

    def _normalize(model_id: str) -> str:
        # us.anthropic.claude-opus-4-8 -> claude-opus-4.8
        tail = model_id.rsplit(".", 1)[-1] if "anthropic." in model_id else model_id
        return re.sub(r"-(\d)-(\d)$", r"-\1.\2", tail)

    assert _normalize(ci_model) == _normalize(local_model), (
        f"prepare-pr opus reviewer ({local_model}) no longer mirrors "
        f"claude-review.yml ({ci_model})"
    )


def test_charter_budgets_match_the_ci_workflows():
    """The budget numbers restated in SKILL.md must match the workflows.

    The charter hand-copies CI's budgets. That copy is exactly what drifted
    before -- the skill still claimed ≤2 BLOCKING long after CI moved to 5 --
    so pin the wording rather than trusting prose to be kept in sync. The Opus
    lane still carries a numeric cap; the GPT lane's budget is report-ALL (a
    numeric cap encouraged staging discoveries across review rounds), so its
    charter must NOT restate a numeric cap.
    """
    skill = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")

    # The Opus lane's budgets live with the contract that applies them -- the
    # validation prompt -- not in the workflow that merely invokes it.
    opus_contract = REPO_ROOT / ".github" / "review-prompts" / "opus-validate.md"
    claude = opus_contract.read_text(encoding="utf-8")
    opus_match = re.search(r"At most (\d+) BLOCKING", claude)
    assert opus_match, f"no BLOCKING budget in {opus_contract.name}"
    opus_blocking = opus_match.group(1)

    advisory_match = re.search(r"At most (\d+) advisory FINDING", claude)
    assert advisory_match, f"no advisory-FINDING budget in {opus_contract.name}"
    opus_advisory = advisory_match.group(1)

    assert (
        f"≤{opus_blocking} BLOCKING, ≤{opus_advisory} advisory FINDING" in skill
    ), (
        "the opus charter's budget no longer matches claude-review.yml "
        f"({opus_blocking} BLOCKING / {opus_advisory} advisory)"
    )

    # The GPT lane's budget lives with the contract that applies it -- the
    # shared review-core prompt (#5852) -- not in the workflow that splices it.
    gpt_contract = (
        REPO_ROOT / ".github" / "review-prompts" / "gpt-review-core.md"
    ).read_text(encoding="utf-8")
    assert "BUDGET: report ALL findings that genuinely meet WHAT BLOCKS" in gpt_contract, (
        "gpt-review-core.md's BUDGET is expected to be report-ALL; if a numeric "
        "cap returned, restore the numeric charter assertions here"
    )
    assert re.search(r"BUDGET: at most \d+ BLOCKING", gpt_contract) is None
    assert "report-ALL" in skill, (
        "the gpt charter's budget no longer matches codex-review.yml "
        "(expected the report-ALL wording)"
    )
    assert re.search(r"≤\d+ BLOCKING\*\* budget", skill) is None, (
        "the gpt charter still restates a numeric BLOCKING cap that "
        "codex-review.yml no longer has"
    )


def _ci_workflow_run_text() -> str:
    """Every blocking CI workflow, with comment-only lines removed.

    Every scan here matches a COMMAND, never a comment. ci.yml explains in
    prose why the Type check step uses `tsc -b` and not `npm run typecheck`,
    so a naive grep for `npm run <script>` finds a script CI deliberately does
    NOT run -- the same trap as reading a ratchet number out of a comment.

    Both blocking workflows are read, not just ci.yml. The cheap lint gates now
    live in fast-gate.yml and ci.yml blocks on it through `await-fast-gate`, so a
    gate in either one is a gate CI enforces and the floor must mirror. Reading
    ci.yml alone silently dropped eleven jobs' worth of gates out of this scan's
    view, which is the exact rot this test exists to catch.
    """
    parts = []
    for name in ("ci.yml", "fast-gate.yml"):
        text = (REPO_ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")
        parts.append(
            "\n".join(
                line for line in text.splitlines() if not line.lstrip().startswith("#")
            )
        )
    return "\n".join(parts)


def test_every_floor_command_names_a_real_target():
    """A floor command naming a missing script fails for the wrong reason.

    The floor is data, so nothing type-checks it: a renamed script or npm
    script turns a gate into a command-not-found, which reads as a defect in
    the branch under review rather than as rot in the floor.
    """
    data = json.loads((PROFILES_DIR / "kirocrew.json").read_text(encoding="utf-8"))
    commands = "\n".join(data["setup"] + data["gates"])

    for rel in sorted(set(re.findall(r"\bscripts/[A-Za-z0-9_.-]+\.(?:py|sh)", commands))):
        assert (REPO_ROOT / rel).is_file(), f"floor references missing script {rel}"

    npm_scripts = set(re.findall(r"\bnpm(?: --prefix \S+)? run ([a-z0-9:-]+)", commands))
    declared = json.loads(
        (REPO_ROOT / "website" / "package.json").read_text(encoding="utf-8")
    )["scripts"]
    for name in sorted(npm_scripts):
        assert name in declared, f"floor references undeclared npm script {name!r}"


def test_ci_blocking_scans_are_covered_by_the_floor():
    """CI adding a blocking scan must fail this test, not a later PR's review.

    The floor mirrors ci.yml by hand, and the profile ships frozen into every
    install -- so a gate CI gains after release is one an installed copy can
    never learn about. Prose asking the loop to keep them in sync is the same
    unenforced copy this suite already replaced for reviewer budgets and the
    opus model id. Anything CI runs that is deliberately NOT a local gate has
    to be named here with its reason, so the exemption is a decision on the
    record rather than an omission.
    """
    run_text = _ci_workflow_run_text()
    data = json.loads((PROFILES_DIR / "kirocrew.json").read_text(encoding="utf-8"))
    # Only repeatable verdict-producing gates satisfy the CI floor. A command
    # filed under setup runs once per worktree, so counting it here would let a
    # future blocking CI check disappear from later prepare-pr passes.
    floor = "\n".join(data["gates"])

    exempt_scripts = {
        # Chooses WHICH tests to run for the changed surface; not itself a gate.
        "scripts/ci-surface-tests.py",
        # Generates the manifest. verify_vendor_manifest.py is the checker, and
        # that one is in the floor.
        "scripts/vendor_manifest.sh",
        # Resolves the diff base inside Actions (it lives under .github/scripts).
        # The floor resolves the same base with `git merge-base` inline.
        "scripts/resolve-i18n-base.sh",
        # Installs the built Linux packages in Ubuntu and Amazon Linux containers.
        # It needs docker AND a completed electron-builder run, so it cannot be a
        # pre-push gate: the floor would then demand a ~10-minute desktop build
        # from every contributor whose diff happens to touch packaging.
        "scripts/smoke-linux-packages.sh",
        # Invoked BY packaging/build-desktop.sh to write the beacon provenance
        # module, never standalone. Gating on it would gate on the build script.
        "scripts/stamp-distribution.sh",
    }

    invoked = set(re.findall(r"\bscripts/[A-Za-z0-9_.-]+\.(?:py|sh)", run_text))
    # The scan must actually see the gates that live in fast-gate.yml. If a
    # rename or a further workflow split drops them out of `run_text`, every
    # assertion below passes by measuring nothing -- green because it looked at
    # an empty set, which is indistinguishable from green because the floor is
    # complete. Name a few of the moved gates outright so that silence fails.
    moved_to_fast_gate = {
        "scripts/scrub-lint.sh",
        "scripts/verify_vendor_manifest.py",
        "scripts/check_brand_name.py",
        "scripts/docs_lint.py",
    }
    assert moved_to_fast_gate <= invoked, (
        "these gates are no longer visible to this scan: "
        f"{sorted(moved_to_fast_gate - invoked)}. They ran in ci.yml, then moved to "
        "fast-gate.yml; if they have moved again, add that workflow to "
        "_ci_workflow_run_text() -- otherwise this test silently stops checking the "
        "floor against them."
    )

    missing = sorted(s for s in invoked - exempt_scripts if s not in floor)
    assert not missing, (
        "ci.yml/fast-gate.yml run these scripts but the prepare-pr floor does not: "
        f"{missing}. Add them to profiles/kirocrew.json gates[] in their "
        "CI-exact form, or exempt them here with a reason."
    )

    npm_invoked = set(re.findall(r"\bnpm run ([a-z0-9:-]+)", run_text))
    npm_missing = sorted(n for n in npm_invoked if f"run {n}" not in floor)
    assert not npm_missing, (
        f"ci.yml runs these npm scripts but the floor does not: {npm_missing}"
    )

    # A blocking step can also be a bare binary -- `cfn-lint`, `mypy`, `flake8`
    # -- which neither scan above can see. Enumerating the TOOL NAMES keeps that
    # class visible: a tool CI starts using is either a gate or an exemption,
    # and this fails until someone decides which.
    exempt_tools = {
        # Environment setup, not gates.
        "pip": "installs the pinned lint tool",
        "uv": "resolves/installs dependencies",
        "sudo": "privileged provisioning -- belongs in manual host setup",
        # Wrappers whose payload is already covered by another assertion.
        "bash": "an interpreter prefix -- the payload is the .sh path, covered by "
                "the script scan above",
        "npm": "covered by the npm-script scan above",
        "npx": "covered by the npm-script scan and the tsc/eslint assertions",
        "python": "covered by the scripts/ scan and the scoped-test gate, which "
                  "runs pytest (or falls back to the full suite)",
        "python3": "covered by the scripts/ scan and the scoped-test gate, which "
                   "runs pytest (or falls back to the full suite)",
        "unshare": "namespace wrapper around the backend test gate",
        # node runs two kinds of step: the diagnostic blob-reconcile step in
        # frontend-coverage-merge (always exits 0, never a gate) and the
        # bundle-size gate. The latter IS a gate and is carried in gates[]
        # (analyze-mode build + scripts/check-bundle-size.mjs); this exemption
        # covers only the diagnostic step. A future gating node step must be
        # added to gates[] by hand -- the tool scan cannot see through this
        # exemption, so keep the reason accurate.
        "node": "diagnostic blob-reconcile step; the gating bundle-size step is in gates[]",
    }
    tools = set(re.findall(r"(?m)^\s*run: ([a-z][a-z0-9_-]+) ", run_text))
    tool_missing = sorted(
        t for t in tools - set(exempt_tools) if not re.search(rf"\b{re.escape(t)}\b", floor)
    )
    assert not tool_missing, (
        f"ci.yml runs these tools but the floor does not: {tool_missing}. "
        "Add each to profiles/kirocrew.json gates[] in its CI-exact form, or "
        "add it to exempt_tools here with the reason it is not a local gate."
    )


def test_test_gates_are_diff_scoped_and_carry_a_base_ref():
    """The test gates must be diff-aware, and that means a base ref is mandatory.

    A diff-aware gate whose base ref is missing cannot know which surface the
    change touches, so it would reduce the wrong suite -- a sibling of the
    "no-op that always passes" failure `references/gate-floor.md` calls worse
    than a missing gate. The runner fails closed on an empty base, so the floor's
    job is to always supply one.
    """
    data = json.loads((PROFILES_DIR / "kirocrew.json").read_text(encoding="utf-8"))
    gates = data["gates"]

    for surface in ("backend", "frontend"):
        entries = [g for g in gates if f"run_scoped_tests.py --surface {surface}" in g]
        assert len(entries) == 1, f"expected exactly one {surface} test gate, got {entries}"
        entry = entries[0]
        assert "SCOPED_TESTS_BASE_REF=" in entry, (
            f"the {surface} test gate must pass SCOPED_TESTS_BASE_REF; without it "
            "the runner cannot know which surface the diff touches"
        )
        # Resolve-then-use, so an unresolvable base returns nonzero instead of
        # inlining an empty substitution and reducing on nothing.
        assert entry.startswith('BASE="$(git merge-base HEAD origin/main)" &&'), (
            f"the {surface} test gate must resolve the base first and short-circuit"
        )

    assert "python3 scripts/run_scoped_tests.py --test" in gates, (
        "the runner's self-test must sit ahead of its scans -- a PR that changes "
        "the reducer has to fail the self-test, not silently reduce"
    )


def test_scoped_frontend_gate_keeps_the_lanes_npm_test_used_to_carry():
    """A reduced frontend run must not silently drop jscpd or the Electron specs.

    `npm --prefix website test` ran three things: `pretest` (jscpd), `test:website`
    (vitest) and `test:electron`. The cross-surface path runs only vitest, so the
    other two have to appear as gates in their own right or they vanish from the
    floor without anyone deciding that they should.
    """
    data = json.loads((PROFILES_DIR / "kirocrew.json").read_text(encoding="utf-8"))
    gates = "\n".join(data["gates"])
    for script in ("jscpd", "test:electron"):
        assert f"run {script}" in gates, (
            f"{script!r} was covered transitively by `npm test`; the reduced "
            "frontend gate does not run it, so it needs its own floor entry"
        )


def test_scoped_runner_self_test_passes():
    """The runner's escalations are its whole contract, so CI runs its self-test.

    Its `--test` mode asserts that an unresolvable base fails closed, that every
    hardcoded broad-impact path resolves on disk, that surface ownership treats
    documentation as backend-owned rather than inert, that the cross-surface list
    arrives in each runner's own path space, and that a hostile target cannot
    reach argv as an option. A green suite that never exercises those is not
    evidence.
    """
    script = REPO_ROOT / "scripts" / "run_scoped_tests.py"
    assert script.is_file(), "scripts/run_scoped_tests.py is missing"
    proc = subprocess.run(
        [sys.executable, str(script), "--test"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, (
        f"run_scoped_tests.py --test failed (rc={proc.returncode}):\n"
        f"{proc.stdout}\n{proc.stderr}"
    )


def test_floor_typechecks_the_way_ci_does():
    """The floor spells out `tsc -b` rather than going through an npm script.

    Build mode is what makes the check real: the root tsconfig is `files: []`
    plus project references, and references are followed only by `-b`, so any
    single-project invocation there compiles an EMPTY program and passes
    unconditionally. Naming the command directly means the floor cannot be
    changed out from under itself by an edit to `package.json` -- the same reason
    ci.yml's Type check step spells it out too.
    """
    gates = "\n".join(
        json.loads((PROFILES_DIR / "kirocrew.json").read_text(encoding="utf-8"))["gates"]
    )
    assert "tsc -b" in gates, "the gate floor no longer type-checks with `tsc -b`"
    assert "run typecheck" not in gates, (
        "the floor reaches type-checking through an npm script, so a package.json "
        "edit can silently change what this gate runs"
    )


def test_typecheck_script_actually_type_checks():
    """`npm run typecheck` must run in BUILD mode, or it checks nothing at all.

    `website/tsconfig.json` is a solution-style config -- `{"files": [], "references":
    [...]}`. TypeScript follows `references` only in build mode, so `tsc --noEmit`
    there compiles an empty program: measured 0 files listed, exit 0 with a genuine
    type error present in `src/App.tsx`. `npm run check` chains this script, so the
    one command that looks like a pre-push gate would pass over the whole tree.

    Nothing else pins the spelling, so without this a revert to `tsc --noEmit`
    restores a gate that is enforced in appearance only.
    """
    scripts = json.loads(
        (REPO_ROOT / "website" / "package.json").read_text(encoding="utf-8")
    )["scripts"]
    typecheck = scripts["typecheck"]

    assert "tsc -b" in typecheck, (
        f"website `typecheck` script is {typecheck!r}; it must use build mode "
        "(`tsc -b`) because the root tsconfig has `files: []` and a "
        "non-build invocation there type-checks zero files"
    )
    assert "--noEmit" not in typecheck, (
        f"website `typecheck` script is {typecheck!r}; `--noEmit` selects "
        "single-project mode, which compiles an empty program against the "
        "solution-style root tsconfig"
    )


def _decide(**kw):
    base = dict(
        state="OPEN",
        mergeable="MERGEABLE",
        merge_state="CLEAN",
        decision="APPROVED",
        draft=False,
        readiness_kind="pass",
        n_running=0,
        n_fail=0,
        n_checks=50,
        readiness_context="PR Readiness",
    )
    base.update(kw)
    return pr_status.decide(**base)


def test_conflict_outranks_in_flight_checks():
    """A conflicted PR must report 20 even with checks still running.

    This is the indefinite-stall bug: a conflicted PR dispatches no
    pull_request workflows, so ranking "still running" first answers "wait" on
    every poll while nothing can ever complete. Distrusting the exit code in
    prose is not a fix -- the precedence belongs here.
    """
    for state_field in ({"mergeable": "CONFLICTING"}, {"merge_state": "DIRTY"}):
        code, status = _decide(readiness_kind="running", n_running=20, **state_field)
        assert code == 20, f"{state_field} with checks running returned {code}"
        assert "conflict" in status


def test_behind_draft_and_changes_requested_also_outrank_running():
    """Each survives any wait, so each must surface on the first poll."""
    code, status = _decide(merge_state="BEHIND", readiness_kind="running", n_running=9)
    assert (code, "BEHIND" in status) == (20, True)
    code, status = _decide(draft=True, readiness_kind="running", n_running=9)
    assert (code, "draft" in status) == (20, True)
    code, status = _decide(decision="CHANGES_REQUESTED", readiness_kind="running", n_running=9)
    assert (code, "CHANGES_REQUESTED" in status) == (20, True)


def test_running_is_still_a_wait_when_nothing_structural_blocks():
    assert _decide(readiness_kind="running", n_running=16)[0] == 10
    assert _decide(readiness_kind=None, n_running=3)[0] == 10


def test_non_open_is_terminal_before_any_wait():
    # mergeable stays UNKNOWN forever on a closed PR, so this must not wait.
    code, status = _decide(state="MERGED", mergeable="UNKNOWN", readiness_kind="running")
    assert code == 20 and "not OPEN" in status


def test_uncomputed_mergeability_waits_and_empty_rollup_fails_closed():
    assert _decide(mergeable="UNKNOWN")[0] == 10
    code, status = _decide(n_checks=0)
    assert code == 20 and "fail-closed" in status


def test_clean_only_when_everything_holds():
    assert _decide() == (
        0,
        "STATUS: CLEAN (readiness passed, mergeable, no blocking review decision)",
    )
    assert _decide(merge_state="BLOCKED")[0] == 0  # pending required review
    assert _decide(readiness_kind="fail")[0] == 20


def test_gate_rationale_reference_exists_and_is_pointed_at():
    """The rationale lives beside the profile, and SKILL.md must point at it.

    Phase 2 carries only the rules the loop executes; the reasons each gate is
    shaped the way it is moved to a reference file so they do not dilute the
    operational instructions on every skill load. A pointer to a file that does
    not ship is worse than no pointer, so pin both directions.
    """
    ref = SKILL_DIR / "references" / "gate-floor.md"
    assert ref.is_file(), "references/gate-floor.md is missing from the skill"
    skill = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert "references/gate-floor.md" in skill, (
        "SKILL.md no longer points at the gate-floor rationale"
    )
    body = ref.read_text(encoding="utf-8")
    # The constraints a gate must satisfy are the load-bearing part; if they are
    # gone the reference has stopped carrying what SKILL.md delegates to it.
    for needle in ("privilege", "provisions", "base ref"):
        assert needle in body, f"gate-floor.md no longer covers {needle!r}"


def test_bundled_kirocrew_profile_is_valid_json():
    data = json.loads((PROFILES_DIR / "kirocrew.json").read_text())
    assert data["name"] == "kirocrew"
    assert isinstance(data["setup"], list)
    # Every reviewer must carry a served model id (no bare gpt-5.6).
    for r in data["reviewers"]:
        assert r["model"] and r["model"] != "gpt-5.6"


def test_toml_config_path(tmp_path):
    toml = tmp_path / ".prepare-pr.toml"
    toml.write_text(
        "[project]\n"
        'base_branch = "trunk"\n'
        "single_commit = true\n\n"
        "[setup]\n"
        'commands = ["make bootstrap"]\n\n'
        "[gates]\n"
        'commands = ["make check"]\n\n'
        "[review]\n"
        'rule_files = ["AGENTS.md"]\n\n'
        "[[review.reviewers]]\n"
        'name = "gpt"\n'
        'model = "gpt-5.6-sol"\n'
        "[readiness]\n"
        'status_context = "My Readiness"\n'
    )
    prof = resolve_profile.resolve(str(tmp_path))
    assert prof["source"] == "config"
    assert prof["base_branch"] == "trunk"
    assert prof["setup"] == ["make bootstrap"]
    assert prof["gates"] == ["make check"]
    assert prof["rule_files"] == ["AGENTS.md"]
    assert prof["reviewers"][0]["model"] == "gpt-5.6-sol"
    assert prof["readiness"]["status_context"] == "My Readiness"


def test_partial_toml_config_fills_gates_from_autodetect(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    (tmp_path / ".prepare-pr.toml").write_text('[project]\nbase_branch = "trunk"\n')
    prof = resolve_profile.resolve(str(tmp_path))
    assert prof["source"] == "config"
    assert prof["base_branch"] == "trunk"
    assert prof["setup"] == []
    assert "python -m pytest -q" in prof["gates"]  # filled from auto-detect


def test_normalize_defaults_fill_missing_keys():
    prof = resolve_profile.normalize({}, "generic")
    for key in ("source", "base_branch", "single_commit", "setup", "gates",
                "rule_files", "reviewers", "readiness"):
        assert key in prof


def test_legacy_profile_without_setup_stays_compatible():
    prof = resolve_profile.normalize({"gates": ["make check"]}, "config")
    assert prof["setup"] == []
    assert prof["gates"] == ["make check"]


def test_single_commit_string_false_is_not_truthy():
    n = resolve_profile.normalize
    assert n({"single_commit": "false"}, "config")["single_commit"] is False
    assert n({"single_commit": True}, "config")["single_commit"] is True
    assert n({"single_commit": "true"}, "config")["single_commit"] is True


def test_symlinked_config_is_refused(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("token=abc\n")
    os.symlink(secret, tmp_path / ".prepare-pr.toml")
    prof = resolve_profile.resolve(str(tmp_path))
    # A symlinked config is refused -> resolution does not take the "config" path.
    assert prof["source"] != "config"


# --------------------------------------------------------------------------
# TreeReader interface parity
# --------------------------------------------------------------------------
def test_tree_reader_worktree_and_pinned_parity(tmp_path):
    """#6236: WorktreeReader and PinnedTreeReader share the TreeReader contract."""
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)

    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'parity'\n")
    (tmp_path / "package.json").write_text('{"scripts": {"build": "npm run build"}}')
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "test-review.yml").write_text("name: test\n")
    (wf / "other.yaml").write_text("name: other\n")

    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=tmp_path, check=True)

    wt_reader = resolve_profile.WorktreeReader(str(tmp_path))
    pinned_reader = resolve_profile.PinnedTreeReader(str(tmp_path), "HEAD")

    # has()
    assert wt_reader.has("pyproject.toml") is True
    assert pinned_reader.has("pyproject.toml") is True
    assert wt_reader.has("missing.txt") is False
    assert pinned_reader.has("missing.txt") is False

    # read()
    assert wt_reader.read("package.json") == '{"scripts": {"build": "npm run build"}}'
    assert pinned_reader.read("package.json") == '{"scripts": {"build": "npm run build"}}'
    assert wt_reader.read("missing.txt") is None
    assert pinned_reader.read("missing.txt") is None

    # ls()
    assert wt_reader.ls(".github/workflows") == [
        ".github/workflows/other.yaml",
        ".github/workflows/test-review.yml",
    ]
    assert pinned_reader.ls(".github/workflows") == [
        ".github/workflows/other.yaml",
        ".github/workflows/test-review.yml",
    ]
    assert wt_reader.ls(".nonexistent") == []
    assert pinned_reader.ls(".nonexistent") == []

    # Detection functions take reader directly
    assert resolve_profile.detect_gates(wt_reader) == ["python -m pytest -q", "npm run build"]
    assert resolve_profile.detect_gates(pinned_reader) == ["python -m pytest -q", "npm run build"]
    assert resolve_profile.detect_kirocrew(wt_reader) is False
    assert resolve_profile.detect_kirocrew(pinned_reader) is False

    reviewers = resolve_profile.detect_reviewers(wt_reader)
    assert len(reviewers) == 1
    assert reviewers[0]["name"] == "test-review"


def test_unreadable_prepare_pr_toml_raises(tmp_path):
    """A present but unreadable/malformed .prepare-pr.toml raises, never silently ignored."""
    (tmp_path / ".prepare-pr.toml").write_bytes(b"\xff\xfe\x00\x00malformed")
    with pytest.raises(Exception):
        resolve_profile.resolve(str(tmp_path))


# --------------------------------------------------------------------------
# pr_status.py readiness-context override
# --------------------------------------------------------------------------
def test_readiness_context_default():
    ctx = pr_status.resolve_readiness_context(["pr_status.py", "662"], {})
    assert ctx == "PR Readiness"


def test_readiness_context_env_override():
    ctx = pr_status.resolve_readiness_context(
        ["pr_status.py"], {"PREPARE_PR_READINESS_CONTEXT": "Custom Gate"}
    )
    assert ctx == "Custom Gate"


def test_readiness_context_flag_beats_env():
    argv = ["pr_status.py", "662", "--readiness-context", "Flag Gate"]
    ctx = pr_status.resolve_readiness_context(
        argv, {"PREPARE_PR_READINESS_CONTEXT": "Env Gate"}
    )
    assert ctx == "Flag Gate"


def test_readiness_context_flag_equals_form():
    argv = ["pr_status.py", "--readiness-context=Eq Gate", "662"]
    assert pr_status.resolve_readiness_context(argv, {}) == "Eq Gate"


def test_positional_args_strip_flag():
    argv = ["662", "--readiness-context", "X"]
    assert pr_status.positional_args(argv) == ["662"]
    argv2 = ["--readiness-context=X", "700"]
    assert pr_status.positional_args(argv2) == ["700"]


if __name__ == "__main__":  # pragma: no cover - manual convenience
    sys.exit(os.system("pytest -q " + __file__))
