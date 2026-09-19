"""The non-root boundary the backend shards run behind on the CodeBuild runner.

GitHub-hosted runners execute a job as an unprivileged user; the CodeBuild-hosted
runner executes it as root, and AWS documents no non-root mode for its GitHub
Actions runner. This suite asserts permission semantics root does not have
(read-only refusals, root-owned-ancestor checks, ``PermissionError``) and the code
refuses to run providers as root at all -- so the first pilot attempt to route the
backend shards failed 37-42 tests per shard on exactly that.

``.github/actions/run-as-runner`` closes it INSIDE the job: the runner process and
the ``setup-*`` actions stay root, and one boundary drops to an unprivileged user
for the steps whose correctness depends on it. Everything that makes that boundary
real is asserted here, because none of it is enforced by GitHub and every way it
can regress is SILENT in the sense that matters -- a boundary that quietly stops
working reappears as dozens of permission failures nobody reads as a routing bug.

Four properties:

1. The boundary is actually declared where the tests run. A step that keeps the
   default shell runs as root again, whatever the action did earlier.
2. The action runs before it, and after the setup-* actions that need root.
3. The privilege transition only ever goes DOWN: no ``chmod -R 777``, no sudoers
   rule, no widening of what root already had. A world-writable workspace would
   itself defeat the read-only refusals the suite asserts.
4. The jobs that need a real namespace sandbox stay hosted. ``unshare --mount
   --map-root-user`` is EPERM on this CodeBuild project (measured), so dropping
   privilege buys them nothing and routing them would turn a real enforcement
   check into a skip.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CI = _REPO_ROOT / ".github" / "workflows" / "ci.yml"
_RELEASE = _REPO_ROOT / ".github" / "workflows" / "release.yml"
_GUI_USER_TEST = _REPO_ROOT / ".github" / "workflows" / "gui-user-test.yml"
_ACTION = _REPO_ROOT / ".github" / "actions" / "run-as-runner" / "action.yml"

_CI_SHELL = "/usr/local/bin/ci-shell {0}"
_ACTION_REF = "./.github/actions/run-as-runner"

# The steps of `backend-test` whose result depends on not being root: the pytest
# run itself, and the coverage staging that reads the files it wrote. Named
# explicitly so a step silently losing its shell fails here.
_NON_ROOT_STEPS = ("Run tests", "Stage shard coverage data")

# Jobs that verify a real user namespace and therefore must NOT be routed to the
# CodeBuild project this action targets. Value is the workflow file each lives in.
_NAMESPACE_JOBS = {
    "backend-test-sandbox": _CI,
    "e2e": _CI,
    "release-candidate-tests": _RELEASE,
    "gui-user-test": _GUI_USER_TEST,
}


def _workflow(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def backend_test() -> dict:
    return _workflow(_CI)["jobs"]["backend-test"]


def _step_named(job: dict, prefix: str) -> dict:
    return next(s for s in job["steps"] if str(s.get("name", "")).startswith(prefix))


class TestTheBoundaryIsDeclaredWhereItMatters:
    def test_the_action_is_invoked(self, backend_test: dict) -> None:
        assert any(s.get("uses") == _ACTION_REF for s in backend_test["steps"]), (
            "backend-test no longer invokes run-as-runner: on the CodeBuild runner "
            "every step is root again and the permission assertions fail wholesale."
        )

    @pytest.mark.parametrize("prefix", _NON_ROOT_STEPS)
    def test_each_non_root_step_declares_ci_shell(self, backend_test: dict, prefix: str) -> None:
        step = _step_named(backend_test, prefix)
        assert step.get("shell") == _CI_SHELL, (
            f"step {prefix!r} lost `shell: {_CI_SHELL}` and would run as root on the "
            "CodeBuild runner."
        )

    def test_the_action_comes_before_them(self, backend_test: dict) -> None:
        steps = backend_test["steps"]
        provision = next(i for i, s in enumerate(steps) if s.get("uses") == _ACTION_REF)
        for prefix in _NON_ROOT_STEPS:
            consumer = next(
                i for i, s in enumerate(steps) if str(s.get("name", "")).startswith(prefix)
            )
            assert provision < consumer, (
                f"run-as-runner must be provisioned before {prefix!r} -- ci-shell does "
                "not exist yet, so the step fails to start rather than running unprivileged."
            )

    def test_the_action_comes_after_the_setup_actions(self, backend_test: dict) -> None:
        steps = backend_test["steps"]
        provision = next(i for i, s in enumerate(steps) if s.get("uses") == _ACTION_REF)
        setups = [
            i
            for i, s in enumerate(steps)
            if str(s.get("uses", "")).startswith(("actions/setup-", "astral-sh/setup-"))
        ]
        assert setups, "the setup-* actions vanished; this ordering check is now meaningless"
        assert provision > max(setups), (
            "run-as-runner must come AFTER the setup-* actions: they install as root "
            "into the tool cache, and the chown it performs covers only the workspace "
            "and the job temp."
        )


class TestThePrivilegeTransitionOnlyGoesDown:
    @pytest.fixture(scope="class")
    def body(self) -> str:
        return _ACTION.read_text(encoding="utf-8")

    @pytest.fixture(scope="class")
    def code(self, body: str) -> str:
        """The action without its prose: a comment naming a rule is not a breach of it."""
        return "\n".join(line for line in body.splitlines() if not line.lstrip().startswith("#"))

    def test_it_never_makes_a_tree_world_writable(self, code: str) -> None:
        assert "chmod -R 777" not in code and "chmod 777" not in code, (
            "a world-writable workspace defeats the read-only refusals this suite "
            "asserts -- hand ownership over with chown instead."
        )
        for line in code.splitlines():
            if "chmod" in line and "777" in line:
                assert "1777" in line and "$RUNNER_TEMP" in line, (
                    "the only permitted 777 is the sticky 1777 on the job temp, which "
                    f"is /tmp semantics. Offending line: {line.strip()}"
                )

    def test_it_grants_no_sudo_rule(self, code: str) -> None:
        assert "/etc/sudoers" not in code and "NOPASSWD" not in code, (
            "the unprivileged user must not be able to climb back to root; the only "
            "privilege transition is the runuser call that goes down."
        )

    def test_every_download_is_checksum_pinned(self, code: str) -> None:
        downloads = [line for line in code.splitlines() if "curl " in line]
        assert downloads, "no download left; drop this check with it"
        assert code.count("sha256sum -c -") >= len(downloads), (
            "a downloaded binary lands on PATH ahead of the test run -- pin every one "
            "by sha256, not by version alone."
        )

    def test_it_is_a_passthrough_off_a_root_runner(self, code: str) -> None:
        assert 'if [ "$(id -u)" != "0" ]' in code, (
            "the action must detect a non-root runner and change nothing there: the "
            "hosted image is the reference being matched, not something to modify."
        )

    def test_the_job_temp_is_never_handed_over(self, code: str) -> None:
        """Why the job temp gets a sticky bit instead of a chown, in two findings.

        ``$RUNNER_TEMP`` holds the runner's file-command directory -- ``$GITHUB_ENV``
        and its siblings, which the runner process reads AS ROOT after each step and
        applies to the next one -- plus the checkout's transient git credentials
        config. ``chown -R runner:runner "$RUNNER_TEMP"`` handed both over. Handing
        back only the file-command directory was not enough either: rename and unlink
        of a directory ENTRY are governed by the PARENT's permissions, so a
        runner-owned parent still lets the protected directory be moved aside and
        recreated. Both were blocking security findings from GPT 5.6, the second on
        the fix for the first. Root-owned + sticky closes the class instead of one
        spelling of it.
        """
        assert 'chown -R runner:runner "$RUNNER_TEMP"' not in code, (
            "chowning the job temp hands over the file-command directory and the "
            "checkout's git credentials config; the tests only need to CREATE entries."
        )
        assert 'chmod 1777 "$RUNNER_TEMP"' in code, (
            "the job temp must be root-owned, world-writable and STICKY -- /tmp "
            "semantics. Sticky is what stops a non-owner renaming an entry it does "
            "not own, which is the route a plain chown leaves open."
        )

    def test_the_boundary_assertion_covers_that(self) -> None:
        job = _workflow(_CI)["jobs"]["backend-test"]
        assertions = _step_named(job, "Assert the non-root boundary")["run"]
        for expected in (
            'test "$(stat -c %U "$RUNNER_TEMP")" = root',
            'test -k "$RUNNER_TEMP"',
            'test ! -w "$(dirname "$GITHUB_ENV")"',
        ):
            assert expected in assertions, (
                f"the in-job assertion must still carry `{expected}`: a silent "
                "regression here is a root escalation, not a test failure."
            )


class TestNamespaceJobsStayHosted:
    @pytest.mark.parametrize("job_name", sorted(_NAMESPACE_JOBS))
    def test_it_is_not_routed_to_codebuild(self, job_name: str) -> None:
        job = _workflow(_NAMESPACE_JOBS[job_name])["jobs"][job_name]
        runs_on = str(job["runs-on"])
        assert "codebuild" not in runs_on and "linux_runner" not in runs_on, (
            f"{job_name} verifies a real user namespace, and `unshare --mount "
            "--map-root-user` is EPERM on the CodeBuild project the routed jobs use. "
            "Routing it turns a real enforcement check into a skip; that migration "
            "needs its own compute, not this boundary."
        )
