"""Print a ready ``dashboard_write`` payload for project-report's ``ci`` field.

Copied beside the crewmate's upkeep skill by ``dashboard_upkeep``. Reads one PR's
checks with ``gh`` and prints ``{"field": "ci", "value": {...}}`` on stdout:

    python3 project_report_ci.py --repo OWNER/NAME --pr 123 [--item ITEM_ID]

``--item`` is the board task this PR belongs to; its counts land under
``checks[ITEM_ID]``. Every red or never-ran lane is listed with owner ``yours``:
change it to ``maintainer`` for a lane only a maintainer can clear, then write it.
Standard library only, so it runs wherever ``gh`` does.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

# gh's `bucket` for a check -> how the page tells it apart.
_FAILED = {"fail", "cancel"}
_NEVER_RAN = {"skipping"}
_RUNNING = {"pending"}


def _gh(*args: str) -> object:
    out = subprocess.run(
        ["gh", *args], capture_output=True, text=True, encoding="utf-8", timeout=60, check=False
    )
    # `gh pr checks` exits 8 while checks are pending and 1 when some failed; the JSON
    # is still printed, so only an empty stdout is a failure.
    if not out.stdout.strip():
        sys.exit(f"gh {' '.join(args)} failed: {out.stderr.strip() or out.returncode}")
    return json.loads(out.stdout)


def build(repo: str, pr: str, item: str | None) -> dict[str, object]:
    head = _gh("pr", "view", pr, "--repo", repo, "--json", "headRefOid")
    checks = _gh("pr", "checks", pr, "--repo", repo, "--json", "name,bucket")
    counts = {"pass": 0, "fail": 0, "running": 0}
    lanes: list[dict[str, str]] = []
    for check in checks if isinstance(checks, list) else []:
        name, bucket = str(check.get("name") or ""), str(check.get("bucket") or "")
        if bucket in _FAILED:
            counts["fail"] += 1
            lanes.append({"name": name, "state": "fail", "owner": "yours"})
        elif bucket in _NEVER_RAN:
            lanes.append({"name": name, "state": "did_not_run", "owner": "yours"})
        elif bucket in _RUNNING:
            counts["running"] += 1
        elif bucket == "pass":
            counts["pass"] += 1
    sha = str(head.get("headRefOid") or "") if isinstance(head, dict) else ""
    value: dict[str, object] = {"pr": pr, "head": sha[:10], "lanes": lanes}
    if item:
        value["checks"] = {item: counts}
    return {"field": "ci", "value": value}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr", required=True)
    parser.add_argument("--item")
    args = parser.parse_args()
    print(json.dumps(build(args.repo, args.pr, args.item)))


if __name__ == "__main__":
    main()
