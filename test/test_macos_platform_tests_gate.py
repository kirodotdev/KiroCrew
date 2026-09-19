"""The macOS lane's placement, asserted from the workflow SOURCE.

WHY THIS FILE EXISTS. The macOS pytest lane belongs in ``platform-tests.yml``,
which ``nightly.yml`` calls, and NOT on the pull_request path. The reason is the
runner queue, not the runtime. MEASURED on three green PR runs (34866269260,
34864945056, 34863753125) that carried the lane: the macOS jobs waited 176, 190 and
213 minutes for a ``macos-15`` runner and then ran for 26-33. Everything non-macOS
in ``ci.yml`` finishes at 78-98 minutes while those runs took 248-268, so about 64
percent of a pull request's CI wall clock was macOS queue time -- and because
``pr-readiness.yml`` (this repository's only required check) is triggered by
``workflow_run`` on ``ci.yml``'s completion, that queue sits directly on the merge
button. Across the same window the lane produced 0 failures and 129 cancellations,
usually still queued when a newer push superseded the run. At nightly hours the
same runners arrive in 0-33 minutes.

Three properties have to hold for that placement to be a move and not a deletion,
and each one is quiet when it breaks:

1. Nothing on the pull_request path may instantiate a macOS runner. A
   ``continue-on-error`` macOS job still holds ``ci.yml``'s completion, so it still
   holds readiness -- an "advisory" macOS job in that file is not advisory.
2. A red macOS suite must hold PUBLICATION and never a BUILD. The artifacts are the
   evidence a fixer works from; this is the same trade
   ``dependency-vulnerability-gate`` makes, and that test file pins the other half.
3. The opt-in on-demand lane must stay OUT of readiness' lane list, or the queue
   it was extracted to avoid comes back through a different door.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


def _load(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def _triggers(document: dict) -> dict:
    """``on:`` is YAML 1.1's boolean ``True`` after ``safe_load``, not the string."""
    return document.get(True, document.get("on")) or {}


def _needs(job: dict) -> list[str]:
    needs = job.get("needs", [])
    return [needs] if isinstance(needs, str) else list(needs)


class TestThePullRequestPathInstantiatesNoMacRunner:
    """The property the whole move exists for, checked where it can regress."""

    def test_ci_declares_no_macos_runner_reachable_from_a_pull_request(self) -> None:
        # Checked on `runs-on` and `strategy.matrix.os` only -- the two places that
        # actually provision a runner. Step bodies legitimately mention macOS (an
        # `if: runner.os == 'macOS'` guard inside a cross-platform job costs
        # nothing), so scanning the whole job would fail on prose.
        ci = _load("ci.yml")
        offenders = []
        for name, job in ci["jobs"].items():
            runs_on = json.dumps(job.get("runs-on", ""))
            matrix_os = json.dumps((job.get("strategy") or {}).get("matrix", {}).get("os", ""))
            if "macos" not in f"{runs_on}{matrix_os}".lower():
                continue
            # The one permitted shape: a runner list that macOS enters only when
            # the event is NOT a pull request.
            if name == "e2e-boot-matrix" and "github.event_name == 'pull_request'" in matrix_os:
                continue
            offenders.append(name)

        assert not offenders, (
            f"{offenders} put a macOS runner on the pull_request path. A macos-15 job in ci.yml "
            "waits 176-213 minutes for a runner, and PR Readiness is triggered by this "
            "workflow's completion, so it holds the merge button even with "
            "continue-on-error. Opt-in macOS work belongs in macos-on-demand.yml, which "
            "calls platform-tests.yml."
        )

    def test_the_boot_matrix_keeps_macos_off_the_pull_request_leg_only(self) -> None:
        # Not just "the expression mentions pull_request": the two branches are
        # read, so an inverted condition (macOS on PRs, not on main) fails here
        # rather than in a three-hour queue.
        matrix_os = _load("ci.yml")["jobs"]["e2e-boot-matrix"]["strategy"]["matrix"]["os"]
        pull_request_side, _, push_side = matrix_os.partition("||")

        assert "macos" not in pull_request_side.lower()
        assert "macos-15" in push_side
        # Still a real gateway boot on the other two platforms in front of a PR: a
        # Windows-only delegation break that every unit test mocked past is the
        # escape this job exists for, and only a real boot can see it.
        assert "windows-latest" in pull_request_side
        assert "ubuntu-latest" in pull_request_side

    def test_ci_no_longer_owns_the_macos_pytest_job(self) -> None:
        assert "backend-test-macos" not in _load("ci.yml")["jobs"]
        assert "backend-test-macos" in _load("platform-tests.yml")["jobs"]


class TestTheMovedLaneIsTheSameLane:
    """A move that quietly drops shards or canaries is a deletion with a receipt."""

    def test_the_full_suite_still_runs_in_three_shards(self) -> None:
        job = _load("platform-tests.yml")["jobs"]["backend-test-macos"]
        assert job["runs-on"] == "macos-15", "macos-latest moves under us; pin the label"
        assert job["strategy"]["matrix"]["group"] == [1, 2, 3]
        assert job["env"]["SHARD_COUNT"] == 3
        # 40 was measured against the slowest observed shard; a runaway macOS shard
        # costs about ten times a Linux one.
        assert job["timeout-minutes"] == 40
        runs = "\n".join(str(step.get("run", "")) for step in job["steps"])
        assert "--splits" in runs and "--group" in runs

    def test_the_sharded_run_cannot_report_success_through_tee(self) -> None:
        # The shard pipes pytest to `tee` so the log survives as an artifact, and
        # a pipeline's status is its LAST command's. Without `set -o pipefail` every
        # failing macOS shard would exit 0 through tee, the job would go green, and
        # this entire lane would gate nothing while looking like it did -- the worst
        # available failure, because it is silent. Asserted in the same step as the
        # pipe, and before the pytest line, since a pipefail set afterwards protects
        # nothing.
        job = _load("platform-tests.yml")["jobs"]["backend-test-macos"]
        shard = next(step for step in job["steps"] if "--splits" in str(step.get("run", "")))
        run = str(shard["run"])
        assert "| tee" in run, "the shard no longer keeps a log"
        assert run.index("set -o pipefail") < run.index("pytest "), run

    def test_the_shard_log_is_uploaded_even_when_the_shard_is_cancelled(self) -> None:
        # A shard killed at the 40-minute cap is `cancelled`, and its ids are the
        # ones a human most needs. `if: failure()` would drop exactly that case.
        steps = _load("platform-tests.yml")["jobs"]["backend-test-macos"]["steps"]
        upload = next(
            s for s in steps if str(s.get("uses", "")).startswith("actions/upload-artifact@")
        )
        assert upload["if"] == "always()"
        assert "shard-macos-" in upload["with"]["name"]

    def test_native_peer_identity_and_terminal_contracts_are_asserted_by_name(self) -> None:
        # `pytest -q` does not name passing tests and a skip exits 0, so each of
        # these is asserted to have PASSED by node id. That is the same blindness
        # that once left four mutation-verified tests unrun inside conftest's
        # Windows collect_ignore.
        runs = "\n".join(
            str(step.get("run", ""))
            for step in _load("platform-tests.yml")["jobs"]["backend-test-macos"]["steps"]
        )
        for node_id in (
            "test/test_socketsec.py::test_macos_check_matches_a_socket_we_connected_to_ourselves",
            "test/test_terminal_handler.py",
        ):
            assert node_id in runs, f"{node_id} is no longer executed on real Darwin"

    def test_platform_tests_never_runs_on_a_pull_request(self) -> None:
        # A `pull_request` trigger here would put the whole suite back in front of
        # every merge, which is the exact regression this file guards. The two
        # allowed triggers are the nightly's call and a fixer's dispatch.
        triggers = _triggers(_load("platform-tests.yml"))
        assert set(triggers) == {"workflow_call", "workflow_dispatch"}


class TestARedMacSuiteHoldsPublicationAndNeverABuild:
    """The mirror image of test_dependency_vulnerability_gate.py's partition."""

    def test_every_shipper_is_behind_it_and_no_builder_is(self) -> None:
        jobs = _load("nightly.yml")["jobs"]
        assert jobs["platform-tests"]["uses"] == "./.github/workflows/platform-tests.yml"

        gated, ungated = [], []
        for name, job in jobs.items():
            if name == "platform-tests":
                continue
            (gated if "platform-tests" in _needs(job) else ungated).append(name)

        assert sorted(gated) == sorted(
            [
                "publish-cli",
                "publish-docker",
                "publish-linux-appimage-arm64",
                "publish-linux-appimage-x64",
                "publish-linux-deb-arm64",
                "publish-linux-deb-x64",
                "publish-linux-rpm-arm64",
                "publish-linux-rpm-x64",
                "publish-windows-x64",
                "sign-and-notarize",
            ]
        )
        # No build job may grow this `needs:`. Gating the builds is what blocked the
        # nightly for hours at a stretch when the dependency audit was wired that
        # way, and it would also destroy the artifacts a fixer needs: with the gate
        # on publication, a red macOS suite still leaves built, unsigned artifacts
        # in the run to download.
        assert sorted(ungated) == sorted(
            [
                "version",
                "build-wheel",
                "build-desktop",
                "build-windows",
                "dependency-vulnerability-gate",
                "pod-scenarios",
            ]
        )


class TestTheOnDemandLaneCannotBecomeAGate:
    """Advisory has to mean advisory, and that is a property of readiness' lists."""

    def test_readiness_does_not_evaluate_the_on_demand_lane(self) -> None:
        readiness = (WORKFLOWS / "pr-readiness.yml").read_text(encoding="utf-8")
        # Absent from both the workflow_run trigger allowlist and the evaluated
        # lane list. Adding it to either turns a 3-hour macOS queue back into a
        # merge blocker, one line at a time.
        assert "macos-on-demand" not in readiness
        assert "macOS Tests (on demand)" not in readiness
        assert "platform-tests" not in readiness
        assert "Platform Tests" not in readiness

    def test_the_on_demand_lane_listens_for_labels_and_keeps_paths_off_the_trigger(self) -> None:
        # A `paths:` filter applies to EVERY event type, so one on the trigger
        # would discard the `labeled` event on any PR that touches no darwin file
        # -- which is exactly the PR someone reaches for the label on. The path
        # test therefore lives in `decide`, as one of three switches.
        triggers = _triggers(_load("macos-on-demand.yml"))
        assert set(triggers) == {"pull_request"}
        pull_request = triggers["pull_request"]
        assert "paths" not in pull_request
        assert "labeled" in pull_request["types"]

    def test_the_decide_job_ors_paths_label_and_a_deterministic_sample(self) -> None:
        canary = _load("macos-on-demand.yml")
        decide = canary["jobs"]["decide"]
        assert "macos" not in str(decide["runs-on"]).lower(), "deciding must not cost a mac runner"
        run = "\n".join(str(step.get("run", "")) for step in decide["steps"])
        filters = "\n".join(
            str((step.get("with") or {}).get("filters", "")) for step in decide["steps"]
        )
        # The two darwin gap lists stay on the path switch: widening either one is
        # how darwin coverage shrinks without a single test changing.
        assert "test/macos-expected-failures.txt" in filters
        assert "test/macos-collect-ignore.txt" in filters
        # The label switch, spelled like ci.yml's `ci:pod-scenarios`.
        env = "\n".join(str(step.get("env", "")) for step in decide["steps"])
        assert "'ci:macos'" in env
        # The sample is a function of the PR HEAD SHA, never of a random source: a
        # re-run must give the same answer, or a red run vanishes on retry.
        assert "$RANDOM" not in run and "shuf" not in run
        assert "github.event.pull_request.head.sha" in env
        assert "16#${HEAD_SHA:0:8}" in run
        assert "'SAMPLE_ONE_IN': '20'" in env
        # And the mac job runs only on decide's say-so.
        mac = canary["jobs"]["platform-tests"]
        assert mac["needs"] == ["decide"]
        assert mac["if"] == "needs.decide.outputs.run == 'true'"

    def test_the_on_demand_lane_calls_the_nightly_workflow_not_a_copy(self) -> None:
        # One suite, two callers. A hand-maintained subset here would be a second
        # copy of the shard and contract steps that could drift from the nightly's
        # without a test noticing; calling the same reusable workflow removes the
        # drift axis instead of fencing it.
        mac = _load("macos-on-demand.yml")["jobs"]["platform-tests"]
        assert mac["uses"] == "./.github/workflows/platform-tests.yml"
        assert "runs-on" not in mac and "steps" not in mac
        # The called workflow declares only `contents: read`, and the caller job
        # must grant at least that and nothing this lane does not need.
        assert mac["permissions"] == {"contents": "read"}
        assert _load("platform-tests.yml")["permissions"] == {"contents": "read"}
