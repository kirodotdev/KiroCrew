"""Every matrix job in .github/workflows/ states an explicit max-parallel cap.

The runner pool is shared with other repositories, so each job with a
``strategy:`` block declares a cap of at most ``MAX_PARALLEL`` legs at once.  A
job may instead use a conditional expression for CodeBuild-routed runners; its
hosted fallback must still be at most ``MAX_PARALLEL``, its CodeBuild branch
must be at most the number of matrix legs (no artificial throttle), and the
output name it tests must match what the job's own ``runs-on`` references.

This test reads every workflow instead of a fixed list, so a new matrix job
that leaves the cap out, or sets it too high, fails here.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
MAX_PARALLEL = 4  # GitHub-hosted pool limit shared with other repositories

# Pattern for CodeBuild-routed conditional caps:
#   ${{ startsWith(needs.changes.outputs.<output>, 'codebuild-') && N || M }}
# group 1 = output name  (must appear in the job's own runs-on)
# group 2 = CodeBuild cap  (must be >= hosted cap, <= matrix leg count)
# group 3 = hosted fallback cap  (must be <= MAX_PARALLEL)
_CODEBUILD_EXPR_RE = re.compile(
    r"^\$\{\{\s*startsWith\(needs\.changes\.outputs\.(\w+),\s*'codebuild-'\)\s*"
    r"&&\s*(\d+)\s*\|\|\s*(\d+)\s*\}\}$"
)


def _matrix_leg_count(strategy: dict) -> int:
    """Return the number of legs the matrix produces, 0 when not determinable."""
    matrix = strategy.get("matrix")
    if not isinstance(matrix, dict):
        return 0
    axes = [v for k, v in matrix.items() if k not in ("include", "exclude")]
    if not axes or not all(isinstance(v, list) for v in axes):
        return 0  # expression-valued axes cannot be counted statically
    legs = 1
    for values in axes:
        legs *= len(values)
    legs -= len(matrix.get("exclude") or [])
    return max(0, legs)


def _runs_on_output(runs_on: object) -> str | None:
    """Extract the needs.changes.outputs.<name> referenced by a runs-on expression."""
    if not isinstance(runs_on, str):
        return None
    m = re.search(r"needs\.changes\.outputs\.(\w+)", runs_on)
    return m.group(1) if m else None


def _strategy_jobs() -> list[tuple[str, str, dict, dict]]:
    """Yield (workflow_name, job_id, strategy, job) for every strategy block."""
    found = []
    for path in sorted(WORKFLOWS.glob("*.y*ml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for job_id, job in (doc.get("jobs") or {}).items():
            strategy = (job or {}).get("strategy")
            if strategy is not None:
                found.append((path.name, job_id, strategy, job or {}))
    return found


def test_some_matrix_jobs_exist():
    # Guards the parametrized test below against passing vacuously.
    assert _strategy_jobs()


@pytest.mark.parametrize(
    ("workflow", "job_id", "strategy", "job"),
    _strategy_jobs(),
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_matrix_job_sets_max_parallel(workflow, job_id, strategy, job):
    cap = strategy.get("max-parallel")
    # bool is an int subclass; `max-parallel: true` is not a cap.
    is_int_cap = isinstance(cap, int) and not isinstance(cap, bool) and 1 <= cap <= MAX_PARALLEL
    is_expr_cap = False
    if isinstance(cap, str):
        m = _CODEBUILD_EXPR_RE.match(cap)
        assert m is not None, (
            f"{workflow}: job {job_id!r} has a string `max-parallel` that does not "
            f"match the required CodeBuild expression pattern, got {cap!r}"
        )
        output_name = m.group(1)
        codebuild_cap = int(m.group(2))
        hosted_cap = int(m.group(3))

        # 1. Hosted fallback must stay within the shared GitHub-runner pool limit.
        assert (
            hosted_cap <= MAX_PARALLEL
        ), f"{workflow}: {job_id!r} hosted fallback {hosted_cap} > MAX_PARALLEL {MAX_PARALLEL}"

        # 2. CodeBuild cap must be at least as generous as the hosted cap and
        #    must not exceed the number of legs the matrix actually produces
        #    (a cap larger than the leg count is a no-op and masks intent).
        legs = _matrix_leg_count(strategy)
        assert codebuild_cap >= hosted_cap, (
            f"{workflow}: {job_id!r} CodeBuild cap {codebuild_cap} < " f"hosted cap {hosted_cap}"
        )
        if legs > 0:
            assert codebuild_cap <= legs, (
                f"{workflow}: {job_id!r} CodeBuild cap {codebuild_cap} > "
                f"matrix leg count {legs}"
            )

        # 3. The output name in the predicate must match the output the job's
        #    own runs-on references, so the cap tracks the runner it guards.
        runs_on_output = _runs_on_output(job.get("runs-on"))
        assert runs_on_output == output_name, (
            f"{workflow}: {job_id!r} cap predicate tests output "
            f"{output_name!r} but runs-on "
            + (
                "has no needs.changes.outputs reference"
                if runs_on_output is None
                else f"uses {runs_on_output!r}"
            )
        )

        is_expr_cap = True

    assert is_int_cap or is_expr_cap, (
        f"{workflow}: job {job_id!r} needs `max-parallel: <1..{MAX_PARALLEL}>` "
        f"or a CodeBuild startsWith expression, got {cap!r}"
    )
