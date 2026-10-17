#!/usr/bin/env python3
"""check_merge_queue_ruleset.py -- an enabled merge queue on main must be configured.

## The failure class

Two pull requests, each green against the main it was tested on, land minutes
apart and are red together: one changes a signature, a field or a census, the
other adds a test, a stub or a snapshot that pins the old shape. Neither PR's
CI ever saw the other's diff, so main goes red and every open pull request
inherits the red until a third PR repairs it. ``ci.yml``'s ``merge_group``
trigger and ``merge-queue-readiness.yml`` exist to close exactly this gap
(docs/ci/ci-and-reviews.md, "Merge queue"), but they only run when the
``protected-branches`` ruleset requires the queue, and ci.yml trims main's push
run on the ``MERGE_QUEUE_ENABLED`` variable alone. A ruleset change is not a
diff, so no pull request check notices when the ruleset and the variable
disagree, or when an enabled queue is set up so it checks nothing.

## What this checks

The queue is optional: the repository owner decides whether main requires it.
This reads ``GET /repos/{owner}/{repo}/rules/branches/main`` -- the rules that
apply to main right now, from every active ruleset -- together with the
``MERGE_QUEUE_ENABLED`` repository variable, and judges three states:

* no ``merge_queue`` rule and the variable not ``true`` in any letter case:
  the queue is not enabled. That is a clean pass; the push-to-main path runs the full matrix.
* a ``merge_queue`` rule: it must be backed by a ``required_status_checks``
  rule, or the queue lands every group unexamined.
* the variable ``true`` but no ``merge_queue`` rule: ci.yml skips main's push
  run as if the queue had tested the tree, and nothing did. That fails.

Usage::

    gh api repos/OWNER/REPO/rules/branches/main > rules.json
    MERGE_QUEUE_ENABLED=<repo variable> python3 scripts/check_merge_queue_ruleset.py rules.json

Exit 0 when the queue is off or correctly required, 1 when it is misconfigured,
2 on unreadable input.
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

    ``queue_variable`` is the ``MERGE_QUEUE_ENABLED`` repository variable. With
    no ``merge_queue`` rule and the variable not ``true`` the queue is simply
    off, which passes; the variable ``true`` without the rule fails, because
    ci.yml then skips main's push run.
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
    # Same comparison ci.yml and build.yml make on the variable: an Actions
    # expression compares strings ignoring case, so `TRUE` counts as on there.
    if queue_variable.casefold() != "true":
        return (
            True,
            "merge queue not enabled on main (no merge_queue rule, MERGE_QUEUE_ENABLED not 'true')",
        )
    kinds = sorted({str(rule.get("type")) for rule in rules if isinstance(rule, dict)})
    return False, (
        "MERGE_QUEUE_ENABLED is 'true' but main has no merge_queue rule (active rule types: "
        + ", ".join(kinds)
        + "), so ci.yml skips main's push run and nothing tests the merged tree. Tick "
        "'Require merge queue' on the protected-branches ruleset (docs/ci/ci-and-reviews.md, "
        "'Merge queue') or unset the variable."
    )


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
