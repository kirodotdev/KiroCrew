"""A `jq` for the readiness sweep's scan-read tests, so they run where the host has none.

The sweep's run block reads each scan row with single-path `jq -r` programs,
folds the issue comments into the newest disposition time, and builds the set
of active dispatches; the harness `gh` stub applies the block's own status
filter. A host without `jq` gets this stand-in first on the harness PATH
instead of a skip, and a host with one runs every test once per instrument, so
the stand-in is held to the answers of the real `jq`. It knows those programs
and nothing else: any other invocation exits 2, so a changed program in the
workflow fails here until it is taught.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

JQ_MODES = ["stand-in"] + (["host"] if shutil.which("jq") else [])

_PROGRAM = r"""
import json
import re
import sys

args = sys.argv[1:]


def fail() -> None:
    sys.stderr.write("jq stand-in: unsupported invocation: %r\n" % (args,))
    sys.exit(2)


def raw(value) -> str:
    # `jq -r`: a string as its text, everything else as JSON.
    return value if isinstance(value, str) else json.dumps(value)


def step(value, key):
    # `.key` on null is null; on anything but an object it is an error.
    if value is None:
        return None
    if not isinstance(value, dict):
        raise TypeError(key)
    return value.get(key)


if len(args) != 2 or args[0] != "-r":
    fail()
program = " ".join(args[1].split())
try:
    data = json.load(sys.stdin)
except ValueError:
    sys.exit(2)

PATH = re.compile(r"^\.([a-z_]+)(?:\.([a-z_]+)(\?)?)?(?: // (empty|0))?$")
STATUS = re.compile(
    r'^\[\.statuses\[\]\? \| select\(\.context == "([^"]+)"\)\]\[0\]\.description // empty$'
)

path = PATH.match(program)
status = STATUS.match(program)
if path:
    first, second, quiet, alternative = path.groups()
    try:
        value = step(data, first)
        if second:
            value = step(value, second)
    except TypeError:
        if not quiet:
            sys.exit(5)
        value = None
    if value is None or value is False:
        if alternative == "empty":
            sys.exit(0)
        if alternative == "0":
            value = 0
    print(raw(value))
elif program == '(.readiness.description? | type) == "string"':
    readiness = data.get("readiness")
    described = isinstance(readiness, dict) and isinstance(readiness.get("description"), str)
    print(raw(described))
elif program == ".comments_read == true":
    print(raw(data.get("comments_read") is True))
elif 'startswith("<!-- ai-review-disposition ")) | .updated_at]' in program:
    stamps = [
        comment.get("updated_at")
        for page in data
        for comment in (page if isinstance(page, list) else [])
        if (comment.get("body") or "").startswith("<!-- ai-review-disposition ")
    ]
    stamps = [stamp for stamp in stamps if stamp is not None]
    if stamps:
        print(raw(max(stamps)))
elif ".workflow_runs[]?" in program and 'capture("^PR-wd-(?<pr>[0-9]+)-(?<sha>[0-9a-f]+)$")' in program:
    for page in data:
        for run in page.get("workflow_runs") or []:
            if run.get("status") not in ("queued", "in_progress"):
                continue
            title = re.match(r"^PR-wd-([0-9]+)-([0-9a-f]{40})$", run.get("display_title") or "")
            if title:
                print("%s::%s" % title.groups())
elif status:
    found = [s for s in data.get("statuses") or [] if s.get("context") == status.group(1)]
    description = found[0].get("description") if found else None
    if description is not None and description is not False:
        print(raw(description))
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
