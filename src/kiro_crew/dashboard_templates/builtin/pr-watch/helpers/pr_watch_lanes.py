"""Print a ready ``dashboard_write`` payload for pr-watch's ``lanes`` field.

Copied beside the crewmate's upkeep skill by ``dashboard_upkeep``. Reads each PR's
checks with ``gh`` and prints ``{"field": "lanes", "value": [...]}`` on stdout:

    python3 pr_watch_lanes.py --repo OWNER/NAME --pr 123 [--pr 124 ...]

Every lane gets owner ``""``: fill in who clears each red or skipped one before you
write it. Standard library only, so it runs wherever ``gh`` does.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

# gh's `bucket` for a check -> the four lane readings the page knows.
_STATE = {
    "fail": "fail",
    "cancel": "fail",
    "skipping": "skipped",
    "pending": "running",
    "pass": "pass",
}


def _checks(repo: str, pr: str) -> list[dict[str, object]]:
    args = ["pr", "checks", pr, "--repo", repo, "--json", "name,bucket"]
    out = subprocess.run(
        ["gh", *args], capture_output=True, text=True, encoding="utf-8", timeout=60, check=False
    )
    # `gh pr checks` exits non-zero while checks are pending or failed and still prints
    # the JSON, so only an empty stdout is a failure.
    if not out.stdout.strip():
        sys.exit(f"gh {' '.join(args)} failed: {out.stderr.strip() or out.returncode}")
    rows = json.loads(out.stdout)
    return rows if isinstance(rows, list) else []


def build(repo: str, prs: list[str]) -> dict[str, object]:
    lanes: list[dict[str, object]] = []
    for pr in prs:
        for check in _checks(repo, pr):
            state = _STATE.get(str(check.get("bucket") or ""))
            if state is None:
                continue
            lanes.append(
                {"name": str(check.get("name") or ""), "state": state, "owner": "", "pr": int(pr)}
            )
    return {"field": "lanes", "value": lanes}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr", required=True, action="append")
    args = parser.parse_args()
    print(json.dumps(build(args.repo, args.pr)))


if __name__ == "__main__":
    main()
