"""``scripts/ci/runner_watchdog.py`` watches every fleet-routed workflow, and only those.

The watched set is derived from the workflow files and split into heal-safe and
heal-exempt by each YAML. The per-tick heal cap is global, live job reads are bounded,
and unread runs that could still show a slow start hold the heal.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
import test_ci_fleet_routing_expression_parity as parity
import yaml
from ci_runner_watchdog_helpers import (
    REPO,
    ROOT,
    WORKFLOWS_DIR,
    FakeApi,
    _cancelled_orphan_job,
    _Clock,
    _job,
    _policy,
    _posts,
    _run,
    _sweep,
    _ts,
    _verdict_of,
    wd,
)

# ── the watched set covers every fleet-routed workflow, and only those ──────


def _fleet_routed_workflow_files() -> set[str]:
    """The fleet-routed workflow files, projected from the parity suite's pinned inventory.

    Reuses ``test_ci_fleet_routing_expression_parity._EXPECTED_ROUTED_JOBS``, which
    already pins the routed pairs, rather than scanning ``runs-on`` a second time. The
    watchdog's own workflow is absent: its job runs hosted.
    """
    return {workflow for workflow, _ in parity._EXPECTED_ROUTED_JOBS}


def _local_callers_of(callees: set[str]) -> set[str]:
    """Workflow files with a job that calls one of ``callees`` with a local ``uses:``.

    A called job belongs to the CALLER's run, so the run carries the caller's `path`,
    which is what `_is_watched` reads. A caller therefore has to be watched for its
    callee's fleet jobs to be seen at all, even when none of the caller's own jobs
    declares a fleet `runs-on`.
    """
    callers: set[str] = set()
    for path in sorted(WORKFLOWS_DIR.glob("*.yml")):
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        for spec in (workflow.get("jobs") or {}).values():
            match = re.match(r"^\./\.github/workflows/(.+?\.yml)", str(spec.get("uses") or ""))
            if match and match.group(1) in callees:
                callers.add(path.name)
                break
    return callers


def _fleet_reaching_workflow_files() -> set[str]:
    """Every workflow whose RUN can contain a fleet job: routed directly, or calling one.

    Transitive, because a caller's caller carries the same run path.
    """
    reaching = _fleet_routed_workflow_files()
    while True:
        more = _local_callers_of(reaching) - reaching
        if not more:
            return reaching
        reaching |= more


# `nightly.yml` reaches the fleet only by calling `build-wheel.yml` and is not watched,
# so a stuck nightly CodeBuild job is reported by nothing. Listed so the equality below
# stays exact and the gap stays on the record.
_UNWATCHED_INDIRECT_WORKFLOWS = {"nightly.yml"}


def test_the_watched_set_is_exactly_the_fleet_reaching_workflows() -> None:
    """A workflow that gains or loses a fleet route must join or leave WATCHED_WORKFLOWS.

    Reachability, not a `runs-on` scan: `release.yml` has no route of its own but its
    runs carry the CodeBuild jobs of the workflows it calls.
    """
    assert set(wd.WATCHED_WORKFLOWS) == (
        _fleet_reaching_workflow_files() - _UNWATCHED_INDIRECT_WORKFLOWS
    )


def test_heal_safe_and_exempt_workflows_form_the_exact_partition() -> None:
    heal_safe = frozenset(
        {
            "build.yml",
            "ci.yml",
            "fast-gate.yml",
            # Ref-keyed, publishes nothing, triggered only by `pull_request`; those
            # runs are healed, so the declaration is reachable.
            "macos-on-demand.yml",
        }
    )
    pull_request_only = frozenset(
        {
            "code-review.yml",
            "dependency-review.yml",
            "pr-hygiene.yml",
            # The four read-only PR policies as one job: PR-keyed,
            # pull_request-only, publishes nothing.
            "pr-policy.yml",
        }
    )
    exempt = frozenset(
        {
            "release.yml",
            "release-lts.yml",
            "pages.yml",
            "main-ratchet-audit.yml",
            "build-wheel.yml",
            "dependency-vulnerability.yml",
            # The OIDC content scan and its caller. The gate's group is keyed on
            # `pull_request.number || sha`, so it is neither ref-keyed nor
            # pull-request-only and cannot join either declared-safe set; a red
            # or orphaned scan is a human's to re-run.
            "internal-content-scan-gate.yml",
            "internal-content-scan.yml",
            "pr-merge-conflict-label.yml",
            # Posts a sticky PR comment; left to a human to re-run.
            "sensitive-change-review.yml",
            # Publishes the required commit status and the readiness label. The
            # sweep's hosted recompute republishes it, so no heal is needed.
            "pr-readiness.yml",
            # Neither declared set can hold it, so exemption is derived rather
            # than chosen. Its group, `codeql-${{ pull_request.number ||
            # run_id }}`, is PR-keyed on a pull request -- which the ref-keyed
            # set rejects -- and degenerates to a per-run key on its `push`,
            # `schedule` and `workflow_dispatch` runs, where no successor check
            # protects a re-run; those same triggers keep it out of the
            # pull-request-only set, which admits one trigger.
            "codeql.yml",
        }
    )
    assert wd.HEAL_SAFE_WORKFLOWS == heal_safe
    assert wd.HEAL_SAFE_PULL_REQUEST_WORKFLOWS == pull_request_only
    assert wd.heal_exempt_workflows() == exempt
    assert heal_safe | pull_request_only | exempt == frozenset(wd.WATCHED_WORKFLOWS)
    assert not heal_safe & pull_request_only
    assert not (heal_safe | pull_request_only) & exempt


def test_heal_safe_declared_is_per_event_for_pr_keyed_workflows() -> None:
    """A PR-keyed workflow is declared safe for pull-request runs only: on any other
    event its group is a constant, so nothing protects a re-run from a successor."""
    assert wd.heal_safe_declared("code-review.yml", "pull_request")
    assert not wd.heal_safe_declared("code-review.yml", "push")
    assert not wd.heal_safe_declared("code-review.yml", "workflow_dispatch")
    # A ref-keyed declaration covers every event, as it always did.
    assert wd.heal_safe_declared("ci.yml", "push")
    assert wd.heal_safe_declared("ci.yml", "pull_request")
    assert wd.heal_safe_declared("ci.yml", "workflow_dispatch")
    assert not wd.heal_safe_declared("release.yml", "pull_request")
    # And `_mark_heal_exempt` consults it, so a push-shaped orphan of a PR-keyed
    # workflow is exempt even though the workflow is not in the exempt SET.
    orphan = wd.RunVerdict(
        run_id=1,
        run_attempt=1,
        head_branch="main",
        head_repo=REPO,
        event="push",
        status="in_progress",
        url="https://example.invalid/runs/1",
        age=timedelta(minutes=60),
        verdict=wd.ORPHANED,
        workflow="code-review.yml",
        orphans=[
            wd.OrphanedJob(
                "j", 1, ("codebuild-x",), timedelta(minutes=20), datetime.now(timezone.utc)
            )
        ],
    )
    assert wd._mark_heal_exempt(orphan, wd.heal_exempt_workflows()).verdict == wd.HEAL_EXEMPT


def test_the_two_declared_sets_are_keyed_the_way_their_successor_check_needs() -> None:
    """HEAL_SAFE_WORKFLOWS is ref-keyed: a push run's successor is judged by the
    branch listing. HEAL_SAFE_PULL_REQUEST_WORKFLOWS is PR-keyed and pull-request-only:
    its runs are judged by head SHA, and no push run exists to judge by branch."""
    pr_keyed_in_ref_set = []
    for name in sorted(wd.HEAL_SAFE_WORKFLOWS):
        text = (WORKFLOWS_DIR / name).read_text(encoding="utf-8")
        head = text.split("jobs:", 1)[0]
        if "pull_request.number" in head:
            pr_keyed_in_ref_set.append(name)
    assert (
        pr_keyed_in_ref_set == []
    ), f"PR-keyed workflows belong in the PR-only set: {pr_keyed_in_ref_set}"
    for name in sorted(wd.HEAL_SAFE_PULL_REQUEST_WORKFLOWS):
        text = (WORKFLOWS_DIR / name).read_text(encoding="utf-8")
        head = text.split("jobs:", 1)[0]
        assert "pull_request.number" in head, f"{name} is declared PR-keyed but is not"
        triggers = {
            keys[1]
            for keys, _ in wd._yaml_mapping_entries(text)
            if len(keys) >= 2 and keys[0] == "on"
        }
        assert triggers == {"pull_request"}, (
            f"{name} is declared safe for pull-request runs only but also triggers on "
            f"{sorted(triggers - {'pull_request'})}; those runs would have no concurrency group"
        )
        assert not wd.workflow_text_has_publish_or_deploy_step(text), name
    # The exempt tier excludes the PR-keyed ones, so the set above is not passing
    # merely because nothing PR-keyed is declared anywhere.
    assert "code-review.yml" not in wd.heal_exempt_workflows()


def test_the_watchdog_workflow_grants_the_pulls_read_its_pull_request_judgement_needs() -> None:
    """`_open_pull_request_heads` reads `GET /repos/{repo}/pulls`, which a workflow
    token sees only with `pull-requests: read`. Without it every pull-request orphan
    would be cancelled, its pulls read would 403, and the re-run would never follow:
    a cancelled run and a red tick where a heal was meant."""
    text = (WORKFLOWS_DIR / wd.WATCHDOG_WORKFLOW).read_text(encoding="utf-8")
    permissions = {
        keys[1]: value.partition("#")[0].strip()
        for keys, value in wd._yaml_mapping_entries(text)
        if len(keys) == 2 and keys[0] == "permissions"
    }
    assert permissions.get("pull-requests") == "read", permissions
    assert permissions.get("actions") == "write", permissions


def test_every_declared_heal_safe_workflow_has_a_trigger_a_heal_can_reach() -> None:
    """A declaration that can never act reads as coverage that does not exist.

    A heal acts on `push` and `pull_request` runs, so each declared workflow needs one of
    those triggers. The pull-request-only `macos-on-demand.yml` is reachable for that reason.
    """
    unreachable = []
    for name in sorted(wd.HEAL_SAFE_WORKFLOWS | wd.HEAL_SAFE_PULL_REQUEST_WORKFLOWS):
        text = (WORKFLOWS_DIR / name).read_text(encoding="utf-8")
        triggers = {
            keys[1]
            for keys, _ in wd._yaml_mapping_entries(text)
            if len(keys) >= 2 and keys[0] == "on"
        }
        if triggers and not triggers & {"push", "pull_request"}:
            unreachable.append(name)
    assert unreachable == [], (
        f"these workflows are declared heal-safe but no push or pull-request run can "
        f"trigger them: {unreachable}"
    )
    macos = (WORKFLOWS_DIR / "macos-on-demand.yml").read_text(encoding="utf-8")
    macos_triggers = {
        keys[1] for keys, _ in wd._yaml_mapping_entries(macos) if len(keys) >= 2 and keys[0] == "on"
    }
    assert macos_triggers == {"pull_request"}
    assert "macos-on-demand.yml" in wd.HEAL_SAFE_WORKFLOWS


def test_every_declared_heal_safe_workflow_passes_the_derived_gate_on_its_real_yaml() -> None:
    """The symmetric pin: the derived gate must ADMIT the four we declared safe.

    `workflow_is_heal_safe_at_revision` is fail-closed, so a benign edit the text gate
    misreads would silently disable healing. This fails at the edit, not at the next incident.
    """
    refused = [
        name
        for name in sorted(wd.HEAL_SAFE_WORKFLOWS)
        if wd.workflow_text_is_heal_safe((WORKFLOWS_DIR / name).read_text(encoding="utf-8"))
        is not True
    ]
    assert refused == [], (
        "these workflows are declared heal-safe but the derived gate refuses their own "
        f"YAML, so healing is silently disabled for them: {refused}"
    )


def test_an_exempt_orphan_at_the_attempt_cap_still_asks_for_a_human() -> None:
    """The attempt cap must not turn a promised red into a green.

    The cap stops our own re-run loops, and an exempt workflow is never re-run by us, so
    it must not hide a stuck orphan behind a green tick. A run without orphan evidence
    still gets the plain attempt-cap report.
    """
    api = FakeApi(
        {"in_progress": [_run(1, workflow="main-ratchet-audit.yml", attempt=3)]},
        {1: [_job(11)]},
    )
    verdicts, outcomes = _sweep(api)
    verdict = _verdict_of(verdicts, 1)
    assert verdict.verdict == wd.HEAL_EXEMPT
    assert "attempt 3" in verdict.detail
    assert outcomes == {1: wd.OUTCOME_HUMAN_REQUIRED}
    assert api.posts == []

    healthy = FakeApi(
        {"in_progress": [_run(2, workflow="main-ratchet-audit.yml", attempt=3)]},
        {2: [_job(21, status="completed")]},
    )
    quiet_verdicts, quiet_outcomes = _sweep(healthy)
    assert _verdict_of(quiet_verdicts, 2).verdict != wd.HEAL_EXEMPT
    assert quiet_outcomes == {}


def test_a_rate_limited_recovery_keeps_the_heal_slots_it_already_spent() -> None:
    """Partial recovery results survive the abort, so the five-per-tick cap holds.

    A tuple return never lands when the pass raises, so the caller owns the containers:
    otherwise a re-run done before the abort is uncounted and five more live runs heal.
    """
    recovered_orphan = _run(
        1,
        minutes_ago=100,
        status="completed",
        conclusion="cancelled",
        updated_minutes_ago=30,
        branch="recovered",
    )
    limited = _run(
        2,
        minutes_ago=100,
        status="completed",
        conclusion="cancelled",
        updated_minutes_ago=31,
        branch="limited",
    )
    live = [_run(100 + i, branch=f"live-{i}") for i in range(5)]
    api = FakeApi(
        {"cancelled": [limited, recovered_orphan], "in_progress": live},
        {
            1: [_cancelled_orphan_job(11, run_id=1)],
            2: [_cancelled_orphan_job(21, run_id=2)],
            **{run["id"]: [_job(run["id"] * 10, run_id=run["id"])] for run in live},
        },
        newest_by_branch={"recovered": 1, "limited": 2, **{f"live-{i}": 100 + i for i in range(5)}},
    )
    original_get = api.get

    def get(path: str) -> Any:
        # Run 2 is the OLDER cancellation, so it is classified and re-run first;
        # the limit then lands on run 1's job read, mid-pass.
        if "/runs/1/jobs" in path:
            raise wd.ApiError(403, "API rate limit exceeded", remaining="0")
        return original_get(path)

    api.get = get  # type: ignore[method-assign]
    _, outcomes = _sweep(api, max_runs=5)
    assert outcomes.get(2) == wd.OUTCOME_RECOVERED
    assert outcomes[wd.RATE_LIMIT_MARKER_ID] == wd.OUTCOME_ABORTED_RATE_LIMITED
    reruns = _posts(api, "/rerun")
    assert len(reruns) <= 5, f"the five-per-tick cap was bypassed: {reruns}"


def test_a_newly_watched_workflow_is_heal_exempt_until_declared_safe() -> None:
    # Classification reads the declaration alone, so a workflow nobody has
    # classified is exempt whatever its YAML says.
    assert wd.heal_exempt_workflows(("newcomer.yml",), frozenset()) == frozenset({"newcomer.yml"})
    assert wd.heal_exempt_workflows(("newcomer.yml",), frozenset({"newcomer.yml"})) == frozenset()


@pytest.mark.parametrize(
    ("group", "safe"),
    [
        ("${{ github.workflow }}-${{ github.ref }}", True),
        ("${{ github.ref_name }}", True),
        ("${{ github.head_ref }}", True),
        # A PR-keyed group is NOT heal-safe for a PUSH run: the push successor check
        # filters the runs listing by branch NAME, so it cannot tell one pull request's
        # run from another's on a shared head branch, and on a push the PR number is
        # empty anyway. The pull-request event admits it -- the case below.
        ("code-review-${{ github.event.pull_request.number }}", False),
        ("a-constant-group", False),
        ("audit-${{ github.sha }}", False),
        ("run-${{ github.run_id }}", False),
    ],
)
def test_heal_safety_requires_a_ref_keyed_group(group: str, safe: bool) -> None:
    text = "on: push\nconcurrency:\n  group: " + group + "\n  cancel-in-progress: true\n"
    assert wd.workflow_text_is_heal_safe(text) is safe


@pytest.mark.parametrize(
    ("group", "safe"),
    [
        ("code-review-${{ github.event.pull_request.number }}", True),
        ("pr-${{ github.event.number }}", True),
        ("${{ github.workflow }}-${{ github.ref }}", True),
        # Still not a run-scoped key on any event.
        ("a-constant-group", False),
        ("audit-${{ github.sha }}", False),
    ],
)
def test_a_pull_request_run_admits_a_pr_keyed_group(group: str, safe: bool) -> None:
    """A pull-request run's successor is judged by head SHA, not by the listing, so
    the PR-number key is enough for it; the derived gate says so only for that event."""
    text = "on: pull_request\nconcurrency:\n  group: " + group + "\n  cancel-in-progress: true\n"
    assert wd.workflow_text_is_heal_safe(text, event="pull_request") is safe
    assert wd.workflow_text_is_heal_safe(text, event="push") is (safe and "github.ref" in group)


def test_a_workflow_with_no_concurrency_group_is_heal_exempt() -> None:
    assert (
        wd.workflow_text_is_heal_safe("on: push\njobs:\n  one:\n    runs-on: ubuntu-latest\n")
        is False
    )


@pytest.mark.parametrize(
    "step",
    [
        "run: npm publish",
        "run: gh release create v1",
        "uses: actions/deploy-pages@abc",
        "uses: actions/upload-pages-artifact@abc",
        "uses: ./.github/workflows/publish-cli.yml",
        "run: docker push example/image:tag",
        "run: docker buildx build . --push",
        "run: aws s3 cp artifact s3://bucket/key",
        "run: aws codeartifact publish-package-version --domain d",
        "run: twine upload --repository-url https://d.codeartifact.example wheel",
        "run: twine upload dist/*",
        "uses: ./.github/workflows/sign-and-notarize.yml",
    ],
)
def test_a_publish_or_deploy_step_makes_a_workflow_unsafe(step: str) -> None:
    safe = "concurrency:\n  group: ${{ github.ref }}\njobs:\n  safe:\n    runs-on: ubuntu-latest\n"
    assert wd.workflow_text_is_heal_safe(safe) is True
    assert wd.workflow_text_is_heal_safe(safe + f"    steps:\n      - {step}\n") is False


def test_a_job_environment_makes_a_workflow_unsafe() -> None:
    text = (
        "concurrency:\n  group: ${{ github.ref }}\njobs:\n  deploy:\n"
        "    runs-on: ubuntu-latest\n    environment: production\n"
    )
    assert wd.workflow_text_is_heal_safe(text) is False


def test_no_publish_pattern_chases_a_toolchain_this_repo_does_not_use() -> None:
    """Keeps the backstop from drifting into a spelling chase.

    A pattern earns its place by naming a publish route this repository could grow: it
    appears in `.github/workflows` or is one step from a route that does. Each unmatched
    pattern needs a stated reason below.
    """
    text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((ROOT / ".github" / "workflows").glob("*.yml"))
    ).lower()
    allowed_unmatched = {
        # An npm/Electron app: `npx electron-builder` already runs here with
        # `--publish never`, so dropping that flag is the whole distance to a publish.
        r"\bnpm\s+publish\b": "npm/Electron project",
        # `docker push` matches and `publish-docker.yml` exists; buildx with --push
        # is the same route spelled in one command.
        r"\bdocker\s+buildx\s+build\b[^\n]*\s--push\b": "docker publishing exists",
        # The repo authenticates to CodeArtifact already; publishing to it is the
        # same endpoint in the other direction.
        r"\baws\s+codeartifact\s+publish-package-version\b": "CodeArtifact is wired up",
        # `build-wheel.yml` builds a Python wheel; twine is how a wheel leaves.
        r"\btwine\s+upload\b": "a wheel is built here",
    }
    unmatched = {p.pattern for p in wd._PUBLISH_OR_DEPLOY_PATTERNS if not p.search(text)}
    unjustified = unmatched - set(allowed_unmatched)
    assert unjustified == set(), (
        "these publish patterns match nothing in .github/workflows and name no route "
        f"this repo could grow, so they are a spelling chase: {sorted(unjustified)}"
    )
    # Every allowance is doing work rather than passing vacuously, and a pattern that
    # starts matching must lose its allowance so the reasons stay true.
    assert set(allowed_unmatched) == unmatched


@pytest.mark.parametrize("permission", ["pages", "packages", "deployments"])
def test_a_durable_write_permission_makes_a_workflow_unsafe(permission: str) -> None:
    text = (
        f"concurrency:\n  group: ${{{{ github.ref }}}}\npermissions:\n  contents: read\n"
        f"  {permission}: write\njobs:\n  safe:\n    runs-on: ubuntu-latest\n"
    )
    assert wd.workflow_text_is_heal_safe(text) is False


def test_id_token_write_alone_leaves_a_workflow_safe() -> None:
    text = (
        "concurrency:\n  group: ${{ github.ref }}\npermissions:\n  contents: read\n"
        "  id-token: write\njobs:\n  safe:\n    runs-on: ubuntu-latest\n"
    )
    assert wd.workflow_text_is_heal_safe(text) is True


def test_a_publish_marker_only_in_a_comment_leaves_a_workflow_safe() -> None:
    text = (
        "# run: npm publish\nconcurrency:\n  group: ${{ github.ref }}\n"
        "jobs:\n  safe:\n    runs-on: ubuntu-latest\n"
    )
    assert wd.workflow_text_is_heal_safe(text) is True


def test_the_watchdog_never_watches_its_own_workflow() -> None:
    assert wd.WATCHDOG_WORKFLOW == "ci-runner-watchdog.yml"
    assert wd.WATCHDOG_WORKFLOW not in wd.WATCHED_WORKFLOWS
    text = (WORKFLOWS_DIR / wd.WATCHDOG_WORKFLOW).read_text(encoding="utf-8")
    # A naive whole-file grep WOULD pull it in; the runs-on rule keeps it out.
    assert "codebuild-kirocrew-gha" in text
    assert wd.WATCHDOG_WORKFLOW not in _fleet_routed_workflow_files()


def test_the_drift_guard_flags_a_new_unregistered_fleet_route() -> None:
    """A workflow that gains a fleet route without joining the watched set breaks the equality.

    The route goes into the borrowed inventory, not a synthetic directory: the scan is the
    parity suite's job, and this guards only the projection.
    """
    routed = _fleet_reaching_workflow_files() | {"surprise.yml"}
    assert set(wd.WATCHED_WORKFLOWS) != routed - _UNWATCHED_INDIRECT_WORKFLOWS


# ── the watchdog heals orphans in every watched workflow, capped globally ───


def test_an_orphan_in_a_non_ci_watched_workflow_is_healed() -> None:
    """A fast-gate.yml orphan is found via the repo-wide listing and filtered in by `path`."""
    api = FakeApi({"in_progress": [_run(1, workflow="fast-gate.yml")]}, {1: [_job(11)]})
    verdicts, outcomes = _sweep(api)
    assert _verdict_of(verdicts, 1).verdict == wd.ORPHANED
    assert _verdict_of(verdicts, 1).workflow == "fast-gate.yml"
    assert outcomes == {1: wd.OUTCOME_HEALED}
    assert any(p.startswith(f"repos/{REPO}/actions/runs?status=") for p in api.gets)


def test_an_unwatched_workflows_run_in_the_repo_wide_listing_is_filtered_out() -> None:
    """An unwatched run with the orphan shape is filtered out client-side, never healed.

    The repo-wide listing returns runs of every workflow, so the filter is the only guard.
    """
    api = FakeApi(
        {"in_progress": [_run(1, workflow="fast-gate.yml"), _run(2, workflow="issue-triage.yml")]},
        {1: [_job(11)], 2: [_job(21, run_id=2)]},
    )
    verdicts, outcomes = _sweep(api)
    # The unwatched run was returned by the repo-wide listing ...
    assert any(p.startswith(f"repos/{REPO}/actions/runs?status=") for p in api.gets)
    # ... but is filtered out: never classified, never acted on. Were the filter
    # dropped, run 2 would be ORPHANED and healed and both assertions below fail.
    assert [v.run_id for v in verdicts] == [1]
    assert outcomes == {1: wd.OUTCOME_HEALED}
    assert not any("/runs/2/" in p for p in api.posts)


def test_the_five_per_tick_cap_is_global_across_workflows() -> None:
    """Seven orphans over the four heal-safe workflows: five heal, oldest first.

    Two workflows carry two orphans each, which shows the cap counts runs globally.
    """
    heal_safe = sorted(wd.HEAL_SAFE_WORKFLOWS)
    wfs = [heal_safe[i % len(heal_safe)] for i in range(7)]
    runs = [
        _run(i, minutes_ago=100 - i, branch=f"pr-{i}", workflow=wfs[i - 1]) for i in range(1, 8)
    ]
    api = FakeApi(
        {"in_progress": runs},
        {i: [_job(i * 10, run_id=i)] for i in range(1, 8)},
    )
    _, outcomes = _sweep(api, max_runs=5)
    healed = sorted(run_id for run_id, outcome in outcomes.items() if outcome == wd.OUTCOME_HEALED)
    assert healed == [1, 2, 3, 4, 5]  # oldest first, across workflows
    assert outcomes[6] == outcomes[7] == wd.OUTCOME_NOT_ATTEMPTED
    revision_reads = [path for path in api.gets if "/contents/.github/workflows/" in path]
    assert len(revision_reads) == 5


def test_live_job_read_bound_keeps_the_oldest_and_the_newest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The oldest runs are the only actionable ones; the newest carry the dispatch
    # evidence a saturation hold is judged by. A bound that kept only the oldest
    # would leave the sweep unable to tell a dead fleet from a busy one.
    runs = [_run(i, minutes_ago=100 - i, status="queued") for i in range(1, 7)]
    api = FakeApi({"queued": list(reversed(runs))}, {i: [] for i in range(1, 7)}, evidence=False)
    monkeypatch.setattr(wd, "LIVE_CLASSIFY_READS", 3)
    monkeypatch.setattr(wd, "LIVE_EVIDENCE_RESERVE", 1)
    logged: list[str] = []
    clock = _Clock()
    wd.run_watchdog(api, _policy(), clock=clock.now, sleep=clock.sleep, log=logged.append)
    live_ids = set(range(1, 7))
    reads = [
        int(path.split("/runs/", 1)[1].split("/", 1)[0])
        for path in api.gets
        if "/jobs?" in path and int(path.split("/runs/", 1)[1].split("/", 1)[0]) in live_ids
    ]
    assert reads == [1, 2, 6]
    assert any("live job-read cap of 3" in line for line in logged)


def test_a_short_reserve_does_not_leave_the_read_bound_unspent() -> None:
    """The classify slice is sized off the reserve taken, not off the constant.

    `_evidence_reserve` takes from the capable runs only, so a burst of fresh pushes
    yields a short reserve. Subtracting the constant would leave reads unspent and more
    unread capable runs holding the heal back.
    """
    policy = _policy()
    # Over the bound so the whole-listing early return cannot apply, but with only
    # three runs old enough to be capable.
    capable = [_run(i, minutes_ago=30 + i) for i in range(1, 4)]
    young = [_run(100 + i, minutes_ago=1) for i in range(wd.LIVE_CLASSIFY_READS + 10)]
    runs = capable + young
    assert len(runs) > wd.LIVE_CLASSIFY_READS

    logged: list[str] = []
    bounded, _ = wd.live_runs_within_read_bound(runs, policy, logged.append)

    # Three reserved plus a classify slice of 47 spends the bound exactly. Sizing off
    # the constant would have given 3 + 40 = 43.
    assert len(bounded) == wd.LIVE_CLASSIFY_READS, (
        f"the bound allows {wd.LIVE_CLASSIFY_READS} reads and a short reserve must not "
        f"shrink that; got {len(bounded)}"
    )

    # The budget line must name the split this tick actually took -- 47/3, not the
    # constants' 40/10 -- and it is emitted once, after the reserve is computed.
    cap_lines = [line for line in logged if "live job-read cap" in line]
    assert len(cap_lines) == 1, "the read budget must be reported exactly once"
    assert f"the {wd.LIVE_CLASSIFY_READS - 3} classified" in cap_lines[0]
    assert "3 reads are reserved" in cap_lines[0]
    assert "(3 of them can)" in cap_lines[0]
    assert "shortfall" not in cap_lines[0]
    assert str(wd.LIVE_EVIDENCE_RESERVE) + " reads are reserved" not in cap_lines[0]


def test_a_run_listed_live_for_days_is_a_ghost_neither_read_nor_holding_the_heal() -> None:
    """A ghost run is dropped before the bound is drawn: not read, not unread-capable.

    The runs index keeps listing queued runs weeks old that answer a cancel with 409.
    Read, each spends a classify slot every tick; unread, each holds the heal for ever.
    """
    policy = _policy()
    ghosts = [
        _run(i, minutes_ago=5 * 24 * 60 + i, status="queued", event="pull_request")
        for i in range(1, 17)
    ]
    live = [
        _run(100 + i, minutes_ago=30 + i, status="queued") for i in range(wd.LIVE_CLASSIFY_READS)
    ]
    runs = ghosts + live
    logged: list[str] = []

    bounded, unread = wd.live_runs_within_read_bound(runs, policy, logged.append)

    bounded_ids = {run["id"] for run in bounded}
    assert not bounded_ids & {g["id"] for g in ghosts}, "a ghost must not be read"
    assert not set(unread) & {g["id"] for g in ghosts}, "a ghost must not hold the heal as unread"
    assert bounded_ids == {
        run["id"] for run in live
    }, "every live run fits once the ghosts are gone"
    ghost_lines = [line for line in logged if "dropped as ghosts" in line]
    assert len(ghost_lines) == 1
    assert "16 listed run(s) older than" in ghost_lines[0]

    # A ghost two days old to the second is dropped; one a second younger is live.
    at_line = _run(7, minutes_ago=2 * 24 * 60, status="queued")
    under_line = _run(8, minutes_ago=2 * 24 * 60 - 1 / 60, status="queued")
    kept = wd.drop_ghost_runs([at_line, under_line], policy, lambda _l: None)
    assert [run["id"] for run in kept] == [8]


def test_a_re_run_of_an_old_run_ages_from_its_attempt_not_its_creation() -> None:
    """A re-run keeps ``created_at`` and moves ``run_started_at`` to the attempt.

    Heals are re-runs, so a stuck attempt on an old run aged by creation would be dropped
    silently. A ghost never started, so its two stamps agree.
    """
    policy = _policy()
    rerun = _run(9, minutes_ago=5 * 24 * 60, status="queued", attempt=2)
    rerun["run_started_at"] = _ts(30)
    ghost = _run(10, minutes_ago=5 * 24 * 60, status="queued")
    ghost["run_started_at"] = ghost["created_at"]
    unstamped = _run(11, minutes_ago=5 * 24 * 60, status="queued")
    unstamped.pop("run_started_at", None)
    kept = wd.drop_ghost_runs([rerun, ghost, unstamped], policy, lambda _l: None)
    assert [run["id"] for run in kept] == [9]


def test_without_the_ghost_line_the_same_ghosts_displace_live_runs_from_the_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The control for the test above: the line is what protects the bound."""
    monkeypatch.setattr(wd, "GHOST_AFTER", timedelta(days=3650))
    policy = _policy()
    # Heal-eligible in shape, so that once they are not dropped they rank FIRST in the
    # bound, exactly as the oldest ci.yml ghosts measured here did.
    ghosts = [_run(i, minutes_ago=5 * 24 * 60 + i, status="queued") for i in range(1, 17)]
    live = [
        _run(100 + i, minutes_ago=30 + i, status="queued") for i in range(wd.LIVE_CLASSIFY_READS)
    ]
    bounded, unread = wd.live_runs_within_read_bound(ghosts + live, policy, lambda _l: None)
    bounded_ids = {run["id"] for run in bounded}
    assert bounded_ids & {g["id"] for g in ghosts}, "ghosts are read once the line is lifted"
    assert (set(unread) | bounded_ids) & {g["id"] for g in ghosts}
    assert {run["id"] for run in live} - bounded_ids, "and a live run is displaced"


def test_the_reserve_log_names_the_fallback_when_no_run_can_carry_a_slow_start() -> None:
    """The log must not claim an age band it did not get.

    With nothing old enough, `_evidence_reserve` falls back to the newest runs, which
    cannot show a slow start, and the log must say so.
    """
    policy = _policy()
    young = [_run(i, minutes_ago=1) for i in range(1, wd.LIVE_CLASSIFY_READS + 12)]
    logged: list[str] = []
    wd.live_runs_within_read_bound(young, policy, logged.append)

    cap_lines = [line for line in logged if "live job-read cap" in line]
    assert cap_lines
    assert "(0 of them can)" in cap_lines[0]
    assert "shortfall" in cap_lines[0]


def test_a_pending_run_is_not_saturation_capable_however_old_it_is() -> None:
    """A jobless run can hold no start, so it must not hold a heal back.

    A ``pending`` run is held by its concurrency group with no jobs created, and
    `pending` runs pile up during exactly the saturation this hold exists for.
    """
    policy = _policy()
    pending = _run(9, minutes_ago=90, status="pending")
    live = _run(8, minutes_ago=20, status="queued")
    assert wd._can_carry_a_slow_start(live, policy) is True
    assert wd._can_carry_a_slow_start(pending, policy) is False
    # Age alone -- the reverted rule -- would call the 90-minute pending run capable.
    assert policy.now - wd.parse_timestamp(pending["created_at"]) >= policy.saturation_wait

    # The reserve prefers the live run even though the pending one is older, and with
    # only the pending run present it falls back rather than reserving nothing.
    assert [int(run["id"]) for run in wd._evidence_reserve([pending, live], policy)] == [8]
    assert [int(run["id"]) for run in wd._evidence_reserve([pending], policy)] == [9]


def test_the_tick_logs_the_slowest_served_wait_against_the_saturation_line() -> None:
    """Drift toward the line has to be visible without re-measuring by hand.

    The reading is logged whether or not it crosses the line, because the margin is the
    signal. It is in seconds: typical waits are under a minute and would print as zero.
    """
    sibling = _job(
        21, status="in_progress", minutes_ago=4, started_minutes_ago=2, runner_name="r", run_id=2
    )
    api = FakeApi(
        {"in_progress": [_run(1), _run(2, minutes_ago=40, branch="other")]},
        {1: [_job(11)], 2: [sibling]},
    )
    logged: list[str] = []
    clock = _Clock()
    verdicts, _ = wd.run_watchdog(
        api, _policy(), clock=clock.now, sleep=clock.sleep, log=logged.append
    )
    assert _verdict_of(verdicts, 1).verdict == wd.ORPHANED  # 2 min is under the line
    line = next(line for line in logged if "slowest served CodeBuild wait" in line)
    assert "120s" in line and "300s" in line

    # A sub-minute wait -- the normal case here -- prints a real number, not zero.
    brief = _job(
        31, status="in_progress", minutes_ago=2.5, started_minutes_ago=2, runner_name="r", run_id=2
    )
    api2 = FakeApi(
        {"in_progress": [_run(1), _run(2, minutes_ago=40, branch="other")]},
        {1: [_job(11)], 2: [brief]},
    )
    brief_log: list[str] = []
    clock2 = _Clock()
    wd.run_watchdog(api2, _policy(), clock=clock2.now, sleep=clock2.sleep, log=brief_log.append)
    brief_line = next(line for line in brief_log if "slowest served CodeBuild wait" in line)
    assert "30s" in brief_line and "0 min" not in brief_line

    # Nothing served at all: no reading is invented.
    quiet = wd.DispatchEvidence()
    assert quiet.slowest_served_wait(_policy()) is None


def test_a_tick_that_saw_no_served_start_logs_no_calibration_reading() -> None:
    """Absent evidence is not a fast queue, so a quiet tick reports nothing.

    The one run here is young with a job that never got a runner, so nothing was
    served and no orphan exists to trigger the completed-run sample. A zero reading
    would read as an instantly-dispatching fleet, which is the opposite of what the
    tick knows.
    """
    api = FakeApi({"in_progress": [_run(1, minutes_ago=2)]}, {1: [_job(11, minutes_ago=2)]})
    logged: list[str] = []
    clock = _Clock()
    wd.run_watchdog(api, _policy(), clock=clock.now, sleep=clock.sleep, log=logged.append)
    assert not any("slowest served CodeBuild wait" in line for line in logged)


def test_an_unread_run_that_crosses_the_line_mid_sweep_turns_the_hold_on() -> None:
    """The unread premise is re-judged on each call's clock, not frozen at the listing.

    The cancel phase reads the listing once, so a run just under ``saturation_wait`` then
    is over it by a later cancel. Keeping its creation time lets the hold engage then.
    """
    policy = _policy()
    just_under = policy.now - policy.saturation_wait + timedelta(seconds=10)
    evidence = wd.DispatchEvidence(unread_candidates={900: just_under})

    # At the listing's clock the run cannot hold a qualifying wait, so nothing holds.
    assert evidence.unread_saturation_capable(policy) == 0

    # A minute later -- ordinary wall-clock advance across a multi-cancel phase -- the
    # same retained run is over the line and the hold engages.
    later = _policy(now=policy.now + timedelta(seconds=60))
    assert evidence.unread_saturation_capable(later) == 1

    prompt_start = policy.now - timedelta(minutes=2)
    evidence.starts.append(
        (prompt_start, timedelta(seconds=5), wd._orphan(_job(31), timedelta(seconds=5)))
    )
    since = policy.now - timedelta(minutes=20)
    assert wd.resolve_hold(FakeApi({}, {}), policy, evidence, since) is None
    held = wd.resolve_hold(FakeApi({}, {}), later, evidence, since)
    assert held is not None and held[0] == wd.SKIPPED_PARTIAL_EVIDENCE

    # The control: a count frozen at the listing's clock answers zero at both.
    frozen = sum(
        1
        for created in evidence.unread_candidates.values()
        if policy.now - created >= policy.saturation_wait
    )
    assert frozen == 0


def test_the_partial_hold_turns_on_unread_saturation_capable_runs_not_on_the_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hold's premise is what went unread, not that the bound was reached.

    Only a run at least ``saturation_wait`` old can hold such a served start, so unread
    young runs do not hold. The completed-run sample cannot close the gap: it reads the
    newest completions, where a prompt start can hide a queue past the threshold.
    """
    # On another fleet (`instance-size:large`): this test pins the label-blind path, and
    # a prompt start on the orphan's OWN queue would take the saturation question
    # (though not this hold) down the own-queue path instead.
    prompt = _job(
        21,
        status="in_progress",
        minutes_ago=20,
        started_minutes_ago=19,
        runner_name="r",
        run_id=2,
        large=True,
    )
    monkeypatch.setattr(wd, "LIVE_CLASSIFY_READS", 2)
    monkeypatch.setattr(wd, "LIVE_EVIDENCE_RESERVE", 1)
    # The unread run is affordable to read, and a tick with an orphan reads it
    # (`top_up_unread_evidence`); this pins the hold itself, so the top-up is priced out.
    monkeypatch.setattr(wd, "LIVE_EVIDENCE_TOPUP_READS", 0)

    # Unread: two runs of 3 and 2 minutes. Neither can hold a 15-minute wait.
    young = [_run(400 + i, minutes_ago=3 - i, status="queued", branch=f"y{i}") for i in range(2)]
    cleared = FakeApi(
        {"in_progress": [_run(1), _run(2, minutes_ago=20, branch="other")] + young},
        {1: [_job(11)], 2: [prompt], 400: [], 401: []},
    )
    verdicts, outcomes = _sweep(cleared)
    assert _verdict_of(verdicts, 1).verdict == wd.ORPHANED
    assert outcomes == {1: wd.OUTCOME_HEALED}

    # Unread: one 30-minute run, which could have held a served 15-minute wait.
    older = _run(500, minutes_ago=30, status="queued", branch="older")
    held = FakeApi(
        {"in_progress": [_run(1), older, _run(2, minutes_ago=20, branch="other")]},
        {1: [_job(11)], 2: [prompt], 500: []},
    )
    held_verdicts, held_outcomes = _sweep(held)
    assert _verdict_of(held_verdicts, 1).verdict == wd.SKIPPED_PARTIAL_EVIDENCE
    assert held_outcomes == {}
    assert held.posts == []


def test_a_tick_with_an_orphan_reads_the_unread_capable_runs_and_heals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An orphan makes the unspent read worth its quota: the top-up reads it and heals.

    With the top-up priced out, the same run stays unread and the tick is held, so the
    heal comes from the top-up.
    """
    monkeypatch.setattr(wd, "LIVE_CLASSIFY_READS", 2)
    monkeypatch.setattr(wd, "LIVE_EVIDENCE_RESERVE", 1)
    prompt = _job(
        21,
        status="in_progress",
        minutes_ago=20,
        started_minutes_ago=19,
        runner_name="r",
        run_id=2,
        large=True,
    )

    def listing() -> FakeApi:
        return FakeApi(
            {
                "in_progress": [
                    _run(1),
                    _run(500, minutes_ago=30, status="queued", branch="older"),
                    _run(2, minutes_ago=20, branch="other"),
                ]
            },
            {1: [_job(11)], 2: [prompt], 500: []},
        )

    api = listing()
    verdicts, outcomes = _sweep(api)
    assert _verdict_of(verdicts, 1).verdict == wd.ORPHANED
    assert outcomes == {1: wd.OUTCOME_HEALED}
    job_reads = [path for path in api.gets if "/jobs?" in path]
    assert any("/runs/500/jobs?" in path for path in job_reads), job_reads

    monkeypatch.setattr(wd, "LIVE_EVIDENCE_TOPUP_READS", 0)
    priced_out = listing()
    held_verdicts, held_outcomes = _sweep(priced_out)
    assert _verdict_of(held_verdicts, 1).verdict == wd.SKIPPED_PARTIAL_EVIDENCE
    assert held_outcomes == {} and priced_out.posts == []
    assert not any("/runs/500/jobs?" in path for path in priced_out.gets)


def test_a_slow_start_found_by_the_top_up_is_saturation_not_a_heal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A slow start found by the top-up gives SKIPPED_SATURATED and nothing is cancelled.

    The unread run's job waited 12 minutes, past the 5-minute line, after the orphan queued.
    """
    monkeypatch.setattr(wd, "LIVE_CLASSIFY_READS", 2)
    monkeypatch.setattr(wd, "LIVE_EVIDENCE_RESERVE", 1)
    prompt = _job(
        21,
        status="in_progress",
        minutes_ago=20,
        started_minutes_ago=19,
        runner_name="r",
        run_id=2,
        large=True,
    )
    slow = _job(
        51,
        status="in_progress",
        minutes_ago=30,
        started_minutes_ago=18,
        runner_name="r",
        run_id=500,
        large=True,
    )
    api = FakeApi(
        {
            "in_progress": [
                _run(1),
                _run(500, minutes_ago=30, status="queued", branch="older"),
                _run(2, minutes_ago=20, branch="other"),
            ]
        },
        {1: [_job(11)], 2: [prompt], 500: [slow]},
    )
    verdicts, outcomes = _sweep(api)
    assert _verdict_of(verdicts, 1).verdict == wd.SKIPPED_SATURATED
    assert outcomes == {} and api.posts == []


def test_a_tick_without_an_orphan_does_not_spend_the_top_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tick with nothing to heal reads nothing past the per-tick bound.

    The 30-minute run is unread and capable, so a tick with an orphan would read it.
    """
    monkeypatch.setattr(wd, "LIVE_CLASSIFY_READS", 2)
    monkeypatch.setattr(wd, "LIVE_EVIDENCE_RESERVE", 1)
    served = _job(
        21,
        status="in_progress",
        minutes_ago=20,
        started_minutes_ago=19,
        runner_name="r",
        run_id=2,
    )
    served_older = _job(
        41,
        status="in_progress",
        minutes_ago=40,
        started_minutes_ago=39,
        runner_name="r",
        run_id=4,
    )
    api = FakeApi(
        {
            "in_progress": [
                _run(4, minutes_ago=40, branch="oldest"),
                _run(3, minutes_ago=2),
                _run(500, minutes_ago=30, status="queued", branch="older"),
                _run(2, minutes_ago=20, branch="other"),
            ]
        },
        {4: [served_older], 3: [_job(31, minutes_ago=2)], 2: [served], 500: []},
    )
    verdicts, outcomes = _sweep(api)
    assert not any(v.verdict == wd.ORPHANED for v in verdicts)
    assert outcomes == {}
    job_reads = [path for path in api.gets if "/jobs?" in path]
    assert not any("/runs/500/jobs?" in path for path in job_reads), job_reads
    assert len(job_reads) <= wd.LIVE_CLASSIFY_READS


def test_a_capable_set_past_the_top_up_bound_is_not_read_at_all() -> None:
    """All or nothing: a partial read cannot lift the hold, so it is not spent.

    Three capable unread runs against a top-up bound of 2. Reading two of them leaves
    one unread, which holds the heal exactly as three did, so the top-up reads none,
    reports the set, and every candidate stays retained for the hold to count.
    """
    policy = _policy()
    now = policy.now
    evidence = wd.DispatchEvidence(
        unread_candidates={900 + i: now - timedelta(minutes=30 + i) for i in range(3)}
    )
    reads: list[int] = []

    def read_jobs(run_id: int) -> list[dict[str, Any]]:
        reads.append(run_id)
        return []

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(wd, "LIVE_EVIDENCE_TOPUP_READS", 2)
        read, unread = wd.top_up_unread_evidence(
            None, policy, evidence, lambda _line: None, read_jobs=read_jobs  # type: ignore[arg-type]
        )
    assert (read, unread) == (0, 3)
    assert reads == []
    assert evidence.unread_saturation_capable(policy) == 3

    # Within the bound: every capable run is read, oldest first, and leaves the set; a
    # run under the age line is neither read nor removed.
    evidence.unread_candidates[950] = now - timedelta(minutes=1)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(wd, "LIVE_EVIDENCE_TOPUP_READS", 3)
        read, unread = wd.top_up_unread_evidence(
            None, policy, evidence, lambda _line: None, read_jobs=read_jobs  # type: ignore[arg-type]
        )
    assert (read, unread) == (3, 0)
    assert reads == [902, 901, 900]
    assert set(evidence.unread_candidates) == {950}


def test_the_reserve_takes_the_age_band_not_this_minutes_pushes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reserved read goes to a run that could report saturation, not the newest.

    Fresh pushes cannot hold a wait past the orphan threshold and answer only
    "dispatching", so the 20-minute run takes the slot and the order stays chronological.
    The `reverted` control reserves the newest runs outright and misses it.
    """
    stuck = _run(7, minutes_ago=360, status="queued", event="push", workflow="fast-gate.yml")
    fresh = [
        _run(300 + i, minutes_ago=3 - i * 0.1, status="queued", event="push", workflow="ci.yml")
        for i in range(3)
    ]
    mid = _run(50, minutes_ago=20, status="queued", event="pull_request", branch="pr")
    candidates = sorted([stuck, mid] + fresh, key=lambda run: run["created_at"])
    monkeypatch.setattr(wd, "LIVE_CLASSIFY_READS", 3)
    monkeypatch.setattr(wd, "LIVE_EVIDENCE_RESERVE", 1)
    bounded, _ = wd.live_runs_within_read_bound(candidates, _policy(), lambda _line: None)
    ids = [int(run["id"]) for run in bounded]
    assert 7 in ids and 50 in ids
    assert [run["created_at"] for run in bounded] == sorted(run["created_at"] for run in bounded)

    reverted = [int(run["id"]) for run in candidates[-wd.LIVE_EVIDENCE_RESERVE :]]
    assert reverted == [302] and 50 not in reverted


def test_the_classify_bound_prefers_a_healable_run_over_older_unhealable_ones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Age alone hands the bound to runs no heal can ever act on.

    The oldest live runs here are fork pull-request runs no token can re-run, so pure
    oldest-first re-reads them every tick and misses the same-repository heal-safe run.
    """
    # Under the ghost line on purpose: a run past GHOST_AFTER is dropped before the
    # bound is drawn at all (its own test below), so the priority rule is exercised
    # here on unhealable runs that ARE still live -- old, but under two days.
    zombies = [
        _run(
            100 + i,
            minutes_ago=2_800 - i,
            status="queued",
            event="pull_request",
            branch="pr",
            fork=True,
        )
        for i in range(4)
    ]
    healable = _run(7, minutes_ago=360, status="queued", event="push", workflow="fast-gate.yml")
    # Old enough to be reserved for evidence, so the healable run must win a classify
    # slot on priority rather than riding in on the reserve.
    mid = _run(50, minutes_ago=20, status="queued", event="pull_request", branch="pr")
    young = [_run(200 + i, minutes_ago=2 - i * 0.1, status="queued") for i in range(2)]
    candidates = zombies + [healable, mid] + young  # oldest first, as the gather returns them
    monkeypatch.setattr(wd, "LIVE_CLASSIFY_READS", 4)
    monkeypatch.setattr(wd, "LIVE_EVIDENCE_RESERVE", 1)
    logged: list[str] = []
    bounded, unread_candidates = wd.live_runs_within_read_bound(
        candidates, _policy(), logged.append
    )
    # The unread zombies are retained: they are `queued` and far past the line, so the
    # listing cannot tell them from a run whose job has been queued that whole time.
    # The two young runs are retained too -- they bear jobs -- but the age half is
    # judged later, so they add nothing to the count until they cross the line.
    evidence = wd.DispatchEvidence(unread_candidates=unread_candidates)
    assert evidence.unread_saturation_capable(_policy()) == 2
    assert 7 in {int(run["id"]) for run in bounded}
    # Still oldest first, so the sweep's log stays chronological.
    assert [run["created_at"] for run in bounded] == sorted(run["created_at"] for run in bounded)
    # The reserved slot goes to the newest run old enough to hold a wait past the
    # threshold, not to the newest run outright: a 2-minute run could only report
    # the fleet dispatching.
    assert int(bounded[-1]["id"]) == 50
    assert not {int(run["id"]) for run in young} & {int(run["id"]) for run in bounded}
    assert any("actionable-shaped first" in line for line in logged)

    oldest = wd.LIVE_CLASSIFY_READS - wd.LIVE_EVIDENCE_RESERVE
    control = candidates[:oldest] + candidates[-wd.LIVE_EVIDENCE_RESERVE :]  # the reverted rule
    assert 7 not in {int(run["id"]) for run in control}


def test_heal_eligible_shape_reads_only_what_the_later_gates_cannot_reverse() -> None:
    """Only a same-repository `push` or `pull_request` run of a heal-safe workflow.

    It is a priority signal, so it must not admit what the real gates refuse: forks,
    push runs of PR-keyed workflows, undeclared workflows, other events.
    """
    assert wd._heal_eligible_shape(_run(1, event="push", workflow="fast-gate.yml"), REPO)
    assert wd._heal_eligible_shape(_run(2, event="pull_request", workflow="fast-gate.yml"), REPO)
    assert wd._heal_eligible_shape(_run(3, event="pull_request", workflow="code-review.yml"), REPO)
    assert not wd._heal_eligible_shape(_run(4, event="push", workflow="code-review.yml"), REPO)
    assert not wd._heal_eligible_shape(
        _run(5, event="pull_request", workflow="fast-gate.yml", fork=True), REPO
    )
    assert not wd._heal_eligible_shape(_run(6, event="push", workflow="release.yml"), REPO)
    assert not wd._heal_eligible_shape(_run(7, event="schedule", workflow="ci.yml"), REPO)
    # A run whose payload carries no event at all is not promoted on a guess.
    pathless = _run(8, event="push", workflow="ci.yml")
    del pathless["event"]
    assert not wd._heal_eligible_shape(pathless, REPO)
