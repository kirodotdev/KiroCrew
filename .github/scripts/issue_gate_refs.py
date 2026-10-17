#!/usr/bin/env python3
"""Issue Gate: print the issue numbers a PR body DECLARES for one repository.

The gate (`.github/workflows/issue-gate.yml`) needs one answer from a PR body:
which issues of THIS repository does it declare it closes? That question
already has an in-tree answer -- the closing-reference grammar in
`kirocrew-prepare-pr/scripts/pr_status.py`, exported as `declared_issue_numbers()`,
which is what the local kirocrew-prepare-pr loop prints its `NOTICE:` from. This
script is the gate's adapter onto that one entry point, so the
same body gets the same answer from both surfaces; a grammar re-derived in
the workflow drifts from it (a different keyword set, a different mask).

What counts, by reference to pr_status.py's `declared_issue_numbers`: a line
that STARTS (three columns of indent at most, an optional bullet) with a
closing verb (`close|closes|closed|fix|fixes|fixed|resolve|resolves|resolved`,
the issue auto-closes on merge) or a non-closing `Refs` / `Part of` (the issue
stays open: partial work), plus `#N`, `owner/repo#N` or a github.com issue
URL, after HTML comments, fenced blocks (unclosed ones through end of body)
and inline code spans are masked; every reference on that line is read, and
what follows is free, so `Fixes #123 (the Windows half)` counts. A `>`-quoted
line, a four-column code line, a reference buried mid-sentence and a bare `#N`
are not declarations. The gate asks which issue the work is FOR, not what
closes, which is why the non-closing verbs count here while pr_status.py's
NOTICE path (a different question: why did the HOST resolve no closure)
ignores them.

A URL target counts only on the github.com host; that rule lives in the
grammar itself (`_DECLARING_REF_RE`), not here. This script adds nothing to
the grammar: it loads it, feeds it the body, prints the numbers.

Usage:
    issue_gate_refs.py OWNER/REPO < body.md

Prints one issue number per line, ascending, for references that name
OWNER/REPO (an unqualified `#N` can only mean the PR's own repository). A
reference naming another repository is not printed: the gate checks labels on
this repository's issues only.

Exit 0 = grammar applied (an empty stdout is "no declaration", a verdict, not
an error); 3 = a reference carries a number the host could never have issued
(reported, never silently dropped: the author meant SOMETHING there); 4 = more
than MAX_DECLARED distinct issues declared -- each one costs the gate an API
read against the shared hourly token pool, so the set is capped and the excess
is a finding, not a read; 2 = the reference grammar could not be loaded.

The body is untrusted author input. It arrives on stdin and is only ever
regex-matched by the reference module; nothing here evaluates or executes it.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PR_STATUS = (
    _REPO_ROOT
    / "src"
    / "kiro_crew"
    / "builtin_skills"
    / "kirocrew-dev"
    / "kirocrew-prepare-pr"
    / "scripts"
    / "pr_status.py"
)

# A PR that genuinely closes more issues than this is a PR to split. Above it
# the gate reports the count instead of reading every issue: one `pull_request`
# event must not become hundreds of API reads from a body an author controls.
MAX_DECLARED = 20


def load_grammar():
    """Import pr_status.py by path so its `_review_contract` sibling resolves."""
    scripts_dir = str(_PR_STATUS.parent)
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    spec = importlib.util.spec_from_file_location("pr_status", _PR_STATUS)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {_PR_STATUS}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def declared_numbers(body: str, repo: str, grammar=None) -> tuple[list[str], bool]:
    """Return (numbers declared for ``repo``, well_formed) via the reference grammar.

    One call into pr_status.py's public entry point; nothing is re-derived here.
    """
    grammar = grammar or load_grammar()
    return grammar.declared_issue_numbers(body, repo)


def main(argv: list[str]) -> int:
    if len(argv) != 2 or "/" not in argv[1]:
        print("usage: issue_gate_refs.py OWNER/REPO < body.md", file=sys.stderr)
        return 2
    try:
        grammar = load_grammar()
    except Exception as exc:  # the gate fails closed on this, naming the cause
        print(f"issue_gate_refs: cannot load the reference grammar: {exc}", file=sys.stderr)
        return 2
    body = sys.stdin.read()
    numbers, well_formed = declared_numbers(body, argv[1], grammar)
    if len(numbers) > MAX_DECLARED:
        print(
            f"issue_gate_refs: {len(numbers)} distinct issues declared, above the "
            f"ceiling of {MAX_DECLARED}; a PR closing that many is a PR to split",
            file=sys.stderr,
        )
        return 4
    for number in numbers:
        print(number)
    if not well_formed:
        print(
            "issue_gate_refs: a closing reference names a number GitHub could never have issued",
            file=sys.stderr,
        )
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
