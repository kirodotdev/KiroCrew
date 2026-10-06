#!/usr/bin/env python3
"""Report a pull request merged while its required check was not green.

``PR Readiness`` is the one required status on main, but the protected-branches
ruleset lets its bypass actors merge past it. Each time that happened in
2026-09 an unrelated pull request paid for it: #11947 merged 14 s after its
last push with readiness ``pending`` and every shard then went red (reverted by
#12091); #15150 merged 39 min into a run whose Windows shard 2 then failed
(repaired by #15633); #11594 merged over a failing readiness and main's
Coverage Gate stayed red for every open PR until a follow-up added the tests.
Nothing recorded any of the three, so each author debugged a red their diff
never touched.

The audit reads the PR's head, its merge time, and that head's PR Readiness
status history, and reports the newest readiness state set at or before the
merge. Anything but ``success`` is a bypass. A status that turns green after
the merge does not excuse it: the merge did not wait for it.

    audit_bypass_merge.py --pr 15150             # exit 1: merged past pending
    audit_bypass_merge.py --commit <sha> --annotate

``--commit`` resolves the PR whose merge commit is ``<sha>`` (a direct push has
none and is silent). ``--annotate`` is the push-to-main mode
(merged-readiness-audit.yml): it prints a ``::warning::`` and always exits 0,
because the merge already happened and a red push run would only add a second
unexplained red.

Exit codes: 0 merged on green, not merged, or no PR; 1 merged past a non-green
readiness; 2 GitHub could not be read.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from typing import Any

READINESS = "PR Readiness"


def readiness_at_merge(statuses: list[dict[str, Any]], merged_at: str) -> str | None:
    """Return the newest PR Readiness state created at or before ``merged_at``.

    ISO-8601 UTC timestamps of the one shape GitHub returns compare correctly
    as text.
    """
    best: tuple[str, str] | None = None
    for item in statuses:
        if item.get("context") != READINESS:
            continue
        created = str(item.get("created_at") or "")
        if not created or created > merged_at:
            continue
        if best is None or created > best[0]:
            best = (created, str(item.get("state")))
    return None if best is None else best[1]


def verdict(pull: dict[str, Any], statuses: list[dict[str, Any]]) -> tuple[int, str]:
    merged_at = pull.get("merged_at")
    if not merged_at:
        return 0, "not merged"
    state = readiness_at_merge(statuses, merged_at)
    if state == "success":
        return 0, f"merged on green readiness at {merged_at}"
    merged_by = (pull.get("merged_by") or {}).get("login", "?")
    return 1, (
        f"merged by {merged_by} at {merged_at} while {READINESS} was "
        f"{state or 'never reported'} on the head"
    )


def pull_for_commit(pulls: list[dict[str, Any]], sha: str) -> dict[str, Any] | None:
    """Return the merged PR whose merge commit is ``sha``, or None.

    ``commits/{sha}/pulls`` also lists PRs that merely contain the commit, so
    only an exact merge-commit match is attributed.
    """
    for pull in pulls:
        if pull.get("merge_commit_sha") == sha and pull.get("merged_at"):
            return pull
    return None


def _gh(path: str) -> Any:
    out = subprocess.run(
        ["gh", "api", path],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout
    return json.loads(out)


def _gh_all(path: str) -> list[Any]:
    out = subprocess.run(
        ["gh", "api", "--paginate", "--slurp", path],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout
    return [item for page in json.loads(out) for item in page]


def audit(repo: str, pr: int | None, commit: str | None) -> tuple[int, str]:
    if commit is not None:
        pull = pull_for_commit(_gh_all(f"repos/{repo}/commits/{commit}/pulls?per_page=100"), commit)
        if pull is None:
            return 0, f"{commit[:12]}: no merged pull request has this merge commit"
        pr = pull["number"]  # the listing omits merged_by; read the PR itself
    pull = _gh(f"repos/{repo}/pulls/{pr}")
    head = pull["head"]["sha"]
    statuses = _gh_all(f"repos/{repo}/commits/{head}/statuses?per_page=100")
    code, text = verdict(pull, statuses)
    return code, f"#{pull['number']} {head[:12]}: {text}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", default="kirodotdev/KiroCrew")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--pr", type=int)
    target.add_argument("--commit")
    parser.add_argument(
        "--annotate",
        action="store_true",
        help="print a ::warning:: for a bypass or an unreadable PR and exit 0",
    )
    args = parser.parse_args(argv)
    try:
        code, text = audit(args.repo, args.pr, args.commit)
    except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError) as exc:
        target_name = f"#{args.pr}" if args.pr is not None else args.commit
        message = f"could not read {target_name}: {exc}"
        if args.annotate:
            print(f"::warning::{message}")
            return 0
        print(message, file=sys.stderr)
        return 2
    if not args.annotate:
        print(text)
        return code
    if code:
        print(
            f"::warning::{text}. Open PRs inherit whatever was red: record why on "
            "the PR, and title a revert or fix-forward of a red main as such."
        )
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
