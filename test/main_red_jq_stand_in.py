"""A `jq` for the main-red step tests, so they run where the host has none.

The two workflow steps and the `gh` stub the tests put in front of them pipe JSON
through four `jq` programs and one `jq 'length' FILE`. A host without `jq` gets
this stand-in first on the harness PATH instead of a skip, and a host with one
runs every step test once per instrument, so the stand-in is held to the answers
of the real `jq`. It knows those programs and nothing else: any other invocation
exits 2, so a changed program in a workflow fails here until it is taught.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

JQ_MODES = ["stand-in"] + (["host"] if shutil.which("jq") else [])

_PROGRAM = r"""
import json
import os
import sys

args = sys.argv[1:]


def fail() -> None:
    sys.stderr.write("jq stand-in: unsupported invocation: %r\n" % (args,))
    sys.exit(2)


def red(job):
    return job.get("conclusion") in ("failure", "timed_out")


def text(value):
    return "null" if value is None else str(value)


if len(args) == 2 and args[0] == "length":
    with open(args[1], encoding="utf-8") as handle:
        print(len(json.load(handle)))
    sys.exit(0)
if len(args) != 2 or args[0] not in ("-r", "-c"):
    fail()
program = args[1]
try:
    data = json.load(sys.stdin)
except ValueError:
    sys.exit(2)
if "env.PROOF_STEP_NAME" in program:
    for job in data["jobs"]:
        if not job["name"].startswith(os.environ["PROOF_JOB_PREFIX"]):
            continue
        for step in job.get("steps") or []:
            if step["name"] == os.environ["PROOF_STEP_NAME"]:
                print(text(step.get("conclusion")))
elif program.endswith("| [.id, .name] | @tsv"):
    for job in data["jobs"]:
        if red(job):
            print("%s\t%s" % (job["id"], job["name"]))
elif program.endswith('"timed_out") | .id'):
    for job in data["jobs"]:
        if red(job):
            print(job["id"])
elif '.event == "cross-referenced"' in program:
    for event in data:
        issue = ((event.get("source") or {}).get("issue")) or {}
        if event.get("event") != "cross-referenced" or issue.get("pull_request") is None:
            continue
        row = {
            "url": issue.get("html_url"),
            "state": issue.get("state"),
            "author": (issue.get("user") or {}).get("login"),
            "created": issue.get("created_at"),
        }
        print(json.dumps(row, separators=(",", ":")))
else:
    fail()
"""


def install_jq(bin_dir: Path, mode: str) -> None:
    """Put the stand-in on ``bin_dir`` as ``jq``; ``host`` leaves the real one to PATH."""
    if mode != "stand-in":
        return
    program = bin_dir / "jq_stand_in.py"
    program.write_text(_PROGRAM, encoding="utf-8")
    jq = bin_dir / "jq"
    jq.write_text(
        f'#!/usr/bin/env bash\nexec "{sys.executable}" -I "{program}" "$@"\n',
        encoding="utf-8",
        newline="\n",
    )
    jq.chmod(0o755)
