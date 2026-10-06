#!/usr/bin/env python3
"""check_merge_queue_ruleset.py -- main's live rules must include the merge queue.

## The failure class

Two pull requests, each green against the main it was tested on, land minutes
apart and are red together: one changes a signature, a field or a census, the
other adds a test, a stub or a snapshot that pins the old shape. Neither PR's
CI ever saw the other's diff, so main goes red and every open pull request
inherits the red until a third PR repairs it. ``ci.yml``'s ``merge_group``
trigger and ``merge-queue-readiness.yml`` exist to close exactly this gap
(docs/ci/ci-and-reviews.md, "Merge queue"), but they only run when the
``protected-branches`` ruleset requires the queue. A ruleset change is not a
diff, so nothing in CI noticed when the queue stopped being required.

## What this checks

It reads ``GET /repos/{owner}/{repo}/rules/branches/main`` -- the rules that
apply to main right now, from every active ruleset -- and fails unless one of
them is a ``merge_queue`` rule backed by a ``required_status_checks`` rule (a
queue with no required check lands every group unexamined). A
``required_status_checks`` rule with
``strict_required_status_checks_policy: true`` is NOT accepted as a substitute:
"branch up to date" makes each author rebase, but at this repository's merge
rate the base moves again before the rerun finishes.

Usage::

    gh api repos/OWNER/REPO/rules/branches/main > rules.json
    MERGE_QUEUE_ENABLED=<repo variable> python3 scripts/check_merge_queue_ruleset.py rules.json

Exit 0 when the queue is required, 1 when it is not, 2 on unreadable input.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any


def merge_queue_rules(rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the ``merge_queue`` entries among the branch's active rules."""
    return [rule for rule in rules if isinstance(rule, dict) and rule.get("type") == "merge_queue"]


def verdict(rules: Any, queue_variable: str = "") -> tuple[bool, str]:
    """Return (ok, message) for one ``rules/branches/main`` response body.

    ``queue_variable`` is the ``MERGE_QUEUE_ENABLED`` repository variable. When
    it says ``true`` but the queue is not required, ci.yml skips main's push run
    as well, so the message names that worse state.
    """
    if not isinstance(rules, list):
        return False, "rules/branches/main did not return a JSON list"
    found = merge_queue_rules(rules)
    if found:
        ids = sorted({str(rule.get("ruleset_id")) for rule in found})
        # A queue with no required check merges every group unexamined: the
        # queue only waits on checks a rule requires (PR Readiness here).
        if not any(
            isinstance(rule, dict) and rule.get("type") == "required_status_checks"
            for rule in rules
        ):
            return False, (
                f"main requires the merge queue (ruleset {', '.join(ids)}) but no required_status_checks "
                "rule, so a merge group lands without waiting on PR Readiness."
            )
        return True, f"merge queue required on main (ruleset {', '.join(ids)})"
    kinds = sorted({str(rule.get("type")) for rule in rules if isinstance(rule, dict)})
    message = (
        "main has no merge_queue rule (active rule types: "
        + ", ".join(kinds)
        + "). ci.yml's merge_group run and merge-queue-readiness.yml never run, so two PRs "
        "green alone can land red together. Re-tick 'Require merge queue' on the "
        "protected-branches ruleset (docs/ci/ci-and-reviews.md, 'Merge queue')."
    )
    # Same comparison ci.yml and build.yml make on the variable.
    if queue_variable == "true":
        message += (
            " MERGE_QUEUE_ENABLED is 'true', so ci.yml also skips main's push run and "
            "nothing tests the merged tree: tick the queue or unset the variable."
        )
    return False, message


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: check_merge_queue_ruleset.py <rules.json>", file=sys.stderr)
        return 2
    try:
        rules = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"cannot read {argv[1]}: {exc}", file=sys.stderr)
        return 2
    ok, message = verdict(rules, os.environ.get("MERGE_QUEUE_ENABLED", ""))
    print(message if ok else f"::error::{message}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
