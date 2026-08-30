"""Repo-wide ratchet for checkout credential persistence.

`actions/checkout` writes the job's `GITHUB_TOKEN` into `.git/config` unless
`persist-credentials: false` is set, leaving a live push-capable credential in
the worktree for every later step to read (zizmor `artipacked`).

PR #6492 swept this class: 51 checkouts were pure omissions and got the opt-out,
and 11 were kept deliberately because the job authenticates a real git
operation -- `git fetch origin` through the shared diff-base resolver, or
`git push --force-with-lease` to open a bot PR. Each of those 11 got an inline
`# persist-credentials retained:` comment naming the job and the operation.

That convention then held only by habit, and a 12th instance leaked in with the
`testpaths-coverage` gate (#6577's follow-on): a whole-tree scan that runs no
git operation at all, so it never needed the credential and carried no comment
explaining why it had one. Nothing failed -- the omission was invisible until
the next manual zizmor triage.

These tests make the convention machine-checked, so the decision has to be
made rather than defaulted:

  * a checkout either opts out, or states why it cannot;
  * a checkout that opts out must not also claim retention, so a later fix
    cannot leave a stale rationale behind that reads as an approved residual.

Deliberately text-based, not YAML-parsed: the rationale lives in a COMMENT, and
PyYAML discards comments, so a parsed representation cannot see the half of the
invariant that carries the reasoning.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"

# The list-item form is the only one this repo uses for checkout steps.
CHECKOUT_RE = re.compile(r"^(\s*)-\s+uses:\s+actions/checkout@")
OPT_OUT = "persist-credentials: false"
RETAINED_RE = re.compile(r"#\s*persist-credentials retained:")


def _workflow_files() -> list[Path]:
    return sorted(p for p in WORKFLOWS.glob("*.yml") if p.is_file())


def _checkout_steps(path: Path) -> list[tuple[int, bool, bool]]:
    """Every checkout step in one workflow.

    Returns (line number, opts out, carries a retention rationale). The step's
    own block is the run of lines indented deeper than its `- ` marker; the
    rationale is searched in the unbroken run of comment lines directly above
    it, which is where #6492 put all eleven.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    found: list[tuple[int, bool, bool]] = []

    for i, line in enumerate(lines):
        match = CHECKOUT_RE.match(line)
        if match is None:
            continue
        indent = len(match.group(1))

        body: list[str] = []
        for following in lines[i + 1 :]:
            if not following.strip():
                continue
            if len(following) - len(following.lstrip()) <= indent:
                break
            body.append(following)
        opts_out = any(OPT_OUT in entry for entry in body)

        rationale = False
        for previous in reversed(lines[:i]):
            stripped = previous.strip()
            if not stripped:
                break
            if not stripped.startswith("#"):
                break
            if RETAINED_RE.search(previous):
                rationale = True
        found.append((i + 1, opts_out, rationale))

    return found


def test_the_scan_actually_finds_the_checkouts() -> None:
    """Guard the guard: a regex that silently stops matching would make every
    assertion below vacuously true, which is how a ratchet rots into decoration."""
    total = sum(len(_checkout_steps(path)) for path in _workflow_files())
    assert total > 40, (
        f"only {total} checkout steps found across {len(_workflow_files())} workflows; "
        "CHECKOUT_RE has stopped matching the form this repo uses"
    )


def test_every_checkout_either_opts_out_or_documents_why() -> None:
    """The invariant. A checkout that keeps the credential must say why."""
    offenders: list[str] = []
    for path in _workflow_files():
        for line_no, opts_out, rationale in _checkout_steps(path):
            if not opts_out and not rationale:
                offenders.append(f"{path.name}:{line_no}")

    assert offenders == [], (
        "these actions/checkout steps keep the GITHUB_TOKEN in .git/config with no "
        f"stated reason: {', '.join(offenders)}. Either add\n"
        "    with:\n"
        f"      {OPT_OUT}\n"
        "or, if the job genuinely authenticates a git operation, add a comment "
        "directly above the step naming the job and the operation:\n"
        "    # persist-credentials retained: <job>/<step> runs <git operation>"
    )


def test_no_checkout_both_opts_out_and_claims_retention() -> None:
    """The other direction. A step that was later fixed must not keep a stale
    rationale, which would read to the next auditor as an approved residual."""
    contradictions: list[str] = []
    for path in _workflow_files():
        for line_no, opts_out, rationale in _checkout_steps(path):
            if opts_out and rationale:
                contradictions.append(f"{path.name}:{line_no}")

    assert contradictions == [], (
        f"these checkout steps set `{OPT_OUT}` yet still carry a "
        f"`# persist-credentials retained:` comment: {', '.join(contradictions)}. "
        "The credential is not retained; delete the stale comment."
    )


@pytest.mark.parametrize(
    "name",
    [
        "add-contributor.yml",
        "ci.yml",
        "cleanup-temp-screenshots.yml",
        "memory-benchmark.yml",
        "test-durations.yml",
    ],
)
def test_the_known_residual_carriers_still_state_their_reason(name: str) -> None:
    """The five files that legitimately retain the credential somewhere. Pinned
    by name so that stripping every rationale comment from one of them fails
    here loudly, instead of quietly passing the invariant above by making the
    step look like a plain opt-out."""
    steps = _checkout_steps(WORKFLOWS / name)
    assert any(rationale for _, _, rationale in steps), (
        f"{name} carries no `# persist-credentials retained:` rationale on any "
        "checkout. If its git operations were removed, the checkout should now "
        f"set `{OPT_OUT}` and this file should come off this list."
    )
