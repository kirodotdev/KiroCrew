"""The pod-scenario matrix must track which platforms actually have a pod backend.

That tier's platform coverage is decided by exactly ONE fact -- whether
``runtime.require_backend()`` finds a service manager on the host -- because no
scenario body and no job carries a platform branch. The matrix is therefore a pure
consequence of which backends exist, which is also why a comment saying a platform
is "deferred until a backend lands" cannot be trusted: the day one lands, the
comment is false and nothing notices.

So the deferral is expressed HERE, as an assertion, instead of in prose. Every
platform whose backend exists must either be in the matrix or be named in
:data:`PENDING_VALIDATION` with the reason it is not, and that reason is what a
reviewer reads instead of trusting a comment to still be true.

Deliberately a plain unit test rather than part of the E2E tier: it reads YAML and
imports nothing that needs a host with pods, so it runs on every shard, including
the ones that cannot run a scenario at all.
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
NIGHTLY = ROOT / ".github" / "workflows" / "nightly.yml"
CI = ROOT / ".github" / "workflows" / "ci.yml"

#: What the nightly leg must report, and what the per-PR Windows canary must
#: report. Both greps are anchored in the workflows; these pins keep the two
#: lanes from drifting into each other (docs/ci/e2e-gate.md, "What has actually
#: run on hosted Windows").
NIGHTLY_SCENARIO_COUNT = 55
PR_BOOT_CANARY_COUNT = 3

#: The one PR label that opts `pod-boot-windows` into the full suite, and the exact
#: step condition every step it adds must carry. One env var evaluated once at the
#: job, one string on every gated step: a step gated on anything else is a step
#: that can run on an unlabelled PR by accident.
OPT_IN_LABEL = "ci:pod-scenarios"
OPT_IN_ENV = "POD_SCENARIOS"
OPT_IN_IF = f"env.{OPT_IN_ENV} == 'true'"
OPT_OUT_IF = f"env.{OPT_IN_ENV} != 'true'"

#: A step whose text contains one of these is paid for by the label, never by an
#: ordinary PR: the Node toolchain, the real SPA build, the wheel scenario's build
#: tool, and the suite itself.
EXPENSIVE_MARKERS = ("actions/setup-node@", "npm run build", "build==", "test/e2e/scenarios")

#: Runner label -> the backend module whose existence means that platform can run
#: pods. ``None`` means the backend lives in ``runtime`` itself (systemd).
BACKEND_FOR_RUNNER = {
    "ubuntu-latest": None,
    "macos-15": "kiro_crew.pod.launchd",
    "windows-latest": "kiro_crew.pod.windows",
}

#: Platforms whose backend EXISTS but which are deliberately not in the matrix yet.
#: Delete an entry and the assertion below demands the matrix entry -- which is the
#: point: this is the only place the deferral is recorded, so it cannot go stale
#: silently the way a comment does.
PENDING_VALIDATION: dict[str, str] = {}


def _matrix_runners() -> list[str]:
    doc = yaml.safe_load(NIGHTLY.read_text(encoding="utf-8"))
    return list(doc["jobs"]["pod-scenarios"]["strategy"]["matrix"]["os"])


def _backend_exists(module: str | None) -> bool:
    if module is None:
        return True
    return importlib.util.find_spec(module) is not None


@pytest.mark.parametrize("runner", sorted(BACKEND_FOR_RUNNER))
def test_a_platform_with_a_backend_is_either_covered_or_explicitly_pending(runner: str) -> None:
    """No third option. A backend with neither a matrix entry nor a stated reason is
    a platform everyone believes is covered and nothing runs."""
    if not _backend_exists(BACKEND_FOR_RUNNER[runner]):
        pytest.skip(f"{runner} has no pod backend yet, so the matrix owes it nothing")

    assert runner in _matrix_runners() or runner in PENDING_VALIDATION, (
        f"{runner} has a pod backend, so require_backend() succeeds there and the "
        "scenario fixture would select it -- add it to nightly.yml's pod-scenarios "
        "matrix, or record here why it is held back"
    )


def test_a_pending_platform_is_really_absent_from_the_matrix() -> None:
    """The list must describe reality, or it is worse than no list.

    A platform that is BOTH listed as pending and present in the matrix means the
    coverage happened and the note was never removed, so the next reader is told
    something false by the very mechanism added to stop that.
    """
    covered = set(_matrix_runners())
    stale = sorted(covered & set(PENDING_VALIDATION))

    assert not stale, (
        f"{', '.join(stale)} is in the pod-scenarios matrix AND listed as pending "
        "validation; delete the PENDING_VALIDATION entry now that it runs"
    )


def test_every_pending_entry_gives_a_reason_that_could_be_acted_on() -> None:
    """A one-word reason ('later', 'TODO') is how a deferral becomes permanent."""
    for runner, reason in PENDING_VALIDATION.items():
        assert len(reason.split()) >= 12, (
            f"{runner}'s pending reason is too thin to act on: state what is "
            "missing and what would have to be true to remove the entry"
        )


def _job_run_text(workflow: pathlib.Path, job: str) -> str:
    doc = yaml.safe_load(workflow.read_text(encoding="utf-8"))
    steps = doc["jobs"][job]["steps"]
    return "\n".join(str(step.get("run", "")) for step in steps)


def test_the_nightly_runs_the_full_suite_on_all_three_backends() -> None:
    """The 55-test suite is nightly coverage on every OS that has a backend.

    The Windows entry rests on one completed hosted run (docs/ci/e2e-gate.md
    records the permalink); this pins that the leg still exists and still
    demands the full count, so the coverage cannot quietly shrink to a subset.
    """
    assert set(_matrix_runners()) == set(BACKEND_FOR_RUNNER)
    run = _job_run_text(NIGHTLY, "pod-scenarios")
    assert "test/e2e/scenarios/" in run
    assert f"{NIGHTLY_SCENARIO_COUNT} passed" in run


def test_the_per_pr_windows_job_is_boot_only_by_default() -> None:
    """Unlabelled, `pod-boot-windows` boots a pod and nothing more.

    The steps that run without the opt-in are the original canary: install the
    control plane, build the payload venv, place the one-file SPA stand-in, run
    the 3-test Task Scheduler module and pin its count. None of them installs
    Node, builds the SPA or touches the scenario suite: that coverage is
    nightly.yml's pod-scenarios leg, or a PR that carried the label.
    """
    default_steps = [s for s in _pod_boot_steps() if str(s.get("if", "")) != OPT_IN_IF]
    run = _steps_run_text(default_steps)
    uses = _steps_uses(default_steps)

    assert "test/test_pod_windows_boot.py" in run
    assert f"{PR_BOOT_CANARY_COUNT} passed" in run
    assert (
        "printf '<!doctype html><title>pod boot canary</title>'" in run
    ), "the default run serves the one-file SPA stand-in, not a built bundle"
    assert not any(
        u.startswith("actions/setup-node@") for u in uses
    ), "an unlabelled pod-boot-windows run installs Node; only the scenario suite needs it"
    for marker in EXPENSIVE_MARKERS[1:]:
        assert marker not in run, (
            f"an unlabelled pod-boot-windows run reaches {marker!r}; that belongs "
            f"behind `if: {OPT_IN_IF}` (docs/ci/e2e-gate.md)"
        )


def test_every_expensive_step_shares_the_one_opt_in_condition() -> None:
    """Every step the label pays for is gated on the SAME expression.

    The job evaluates the label once into ``env.POD_SCENARIOS``; a step gated on
    a different spelling (a second `contains(...)`, a typo'd env name) is a step
    that can silently diverge from the others, running the suite without the
    build tool or building the SPA without running the suite.
    """
    job = _pod_boot_job()
    assert OPT_IN_ENV in job.get("env", {}), "the opt-in is evaluated once, at the job"
    expr = job["env"][OPT_IN_ENV]
    assert "github.event_name == 'pull_request'" in expr
    assert f"contains(github.event.pull_request.labels.*.name, '{OPT_IN_LABEL}')" in expr

    gated = [s for s in _pod_boot_steps() if str(s.get("if", "")) == OPT_IN_IF]
    assert len(gated) >= 4, f"expected Node, build tool, SPA build and suite steps, got {gated}"
    for step in _pod_boot_steps():
        text = _steps_run_text([step]) + " " + " ".join(_steps_uses([step]))
        if any(marker in text for marker in EXPENSIVE_MARKERS):
            assert str(step.get("if", "")) == OPT_IN_IF, (
                f"step {step.get('name') or step.get('uses')!r} is expensive but not "
                f"gated on exactly `if: {OPT_IN_IF}`"
            )
    # The stand-in yields to the real bundle on a labelled run, and only then.
    stand_in = [s for s in _pod_boot_steps() if "pod boot canary</title>" in str(s.get("run", ""))]
    assert len(stand_in) == 1 and str(stand_in[0].get("if", "")) == OPT_OUT_IF


def test_the_gated_full_suite_is_required_and_count_pinned() -> None:
    """When the label enables the suite, it must RUN all 55, not skip.

    Same locks as the nightly leg: KIROCREW_E2E_SCENARIOS opens the suite,
    KIROCREW_E2E_REQUIRE turns a precondition skip into a failure, and the
    anchored count grep refuses a collapsed collection. The 3-test canary still
    runs on the labelled path, unconditionally.
    """
    steps = _pod_boot_steps()
    suite = [s for s in steps if "test/e2e/scenarios" in str(s.get("run", ""))]
    assert len(suite) == 1, f"expected one scenario step, found {len(suite)}"
    step = suite[0]
    assert str(step.get("if", "")) == OPT_IN_IF
    assert step.get("env", {}).get("KIROCREW_E2E_SCENARIOS") == "1"
    assert step.get("env", {}).get("KIROCREW_E2E_REQUIRE") == "1"
    assert f"{NIGHTLY_SCENARIO_COUNT} passed" in str(step["run"])
    assert "PIPESTATUS[0]" in str(step["run"]), "pytest's own exit code must survive the tee"

    canary = [s for s in steps if "test/test_pod_windows_boot.py" in str(s.get("run", ""))]
    assert len(canary) == 1 and "if" not in canary[0], "the boot canary runs on both paths"


def _pod_boot_job() -> dict:
    doc = yaml.safe_load(CI.read_text(encoding="utf-8"))
    return doc["jobs"]["pod-boot-windows"]


def _pod_boot_steps() -> list[dict]:
    return list(_pod_boot_job()["steps"])


def _steps_run_text(steps: list[dict]) -> str:
    return "\n".join(str(step.get("run", "")) for step in steps)


def _steps_uses(steps: list[dict]) -> list[str]:
    return [str(step.get("uses", "")) for step in steps]
