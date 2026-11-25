"""verify-cli-publish.yml checks the CLI lane from a branch and publishes nothing.

publish-cli.yml's `verify_only` input skips its publish job. These tests pin the
three things that make dispatching the wrapper from a branch safe: the input
defaults off for every real caller, the publish job's condition is false
whenever it is on, and the wrapper hands over no secret.
"""

from __future__ import annotations

import ast
import itertools
import re
from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
CLI = WORKFLOWS / "publish-cli.yml"
WRAPPER = WORKFLOWS / "verify-cli-publish.yml"


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _eval(expr: str, ctx: dict[str, object]) -> bool:
    """Evaluate the publish job's GitHub expression with Python semantics.

    The expression uses only `!`, `&&`, `||`, `==`, parentheses, string
    literals and the names in *ctx*, so a token-level rewrite is exact."""
    body = " ".join(expr.split())
    body = re.sub(r"^\$\{\{|\}\}$", "", body).strip()
    body = body.replace("!cancelled()", "True").replace("&&", " and ").replace("||", " or ")
    body = re.sub(r"!(?!=)", " not ", body)
    for name in sorted(ctx, key=len, reverse=True):
        body = body.replace(name, repr(ctx[name]))
    return bool(_walk(ast.parse(body, mode="eval").body))


def _walk(node: ast.AST) -> object:
    """Evaluate only constants, `and`/`or`, `not` and `==`; refuse anything else."""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.BoolOp):
        values = [_walk(v) for v in node.values]
        return all(values) if isinstance(node.op, ast.And) else any(values)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return not _walk(node.operand)
    if isinstance(node, ast.Compare) and len(node.ops) == 1 and isinstance(node.ops[0], ast.Eq):
        return _walk(node.left) == _walk(node.comparators[0])
    raise AssertionError(f"unexpected expression node {ast.dump(node)}")


def test_verify_only_defaults_off() -> None:
    inputs = _load(CLI)[True]["workflow_call"]["inputs"]
    assert inputs["verify_only"]["type"] == "boolean"
    assert inputs["verify_only"]["default"] is False


@pytest.mark.parametrize(
    ("verify_only", "promote", "gate"),
    list(itertools.product([False, True], [False, True], ["success", "skipped", "failure"])),
)
def test_publish_job_never_runs_in_verify_only_and_is_unchanged_otherwise(
    verify_only, promote, gate
) -> None:
    job = _load(CLI)["jobs"]["publish-cli"]
    ctx = {
        "inputs.verify_only": verify_only,
        "inputs.promote": promote,
        "needs.verify-platform-wheels.result": gate,
    }
    runs = _eval(job["if"], ctx)
    old = gate == "success" or (promote and gate == "skipped")
    assert runs == (old and not verify_only)


def test_only_the_wrapper_turns_verify_only_on() -> None:
    callers = {}
    for path in WORKFLOWS.glob("*.yml"):
        for name, job in (_load(path).get("jobs") or {}).items():
            if str(job.get("uses", "")).endswith("/publish-cli.yml"):
                callers[f"{path.name}:{name}"] = job.get("with", {}).get("verify_only", False)
    assert callers.pop("verify-cli-publish.yml:verify") is True
    assert callers, "the real publish callers must still exist"
    assert not any(callers.values()), callers


def test_the_wrapper_is_dispatch_only_and_passes_no_secret() -> None:
    wrapper = _load(WRAPPER)
    assert set(wrapper[True]) == {"workflow_dispatch"}
    assert wrapper["permissions"] == {"contents": "read"}
    for name, job in wrapper["jobs"].items():
        assert "secrets" not in job, f"{name}: a verify run must hold no secret"
        assert "environment" not in job, name
    verify = wrapper["jobs"]["verify"]
    assert verify["with"]["verify_only"] is True
    assert {"build-wheel", "build-platform-wheels"} <= set(verify["needs"])
    assert wrapper["jobs"]["build-wheel"]["with"]["build_channel"] == "nightly"
