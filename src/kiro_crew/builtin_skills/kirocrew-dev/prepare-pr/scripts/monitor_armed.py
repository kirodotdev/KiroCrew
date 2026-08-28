#!/usr/bin/env python3
"""monitor_armed.py - verify a monitor_start loop actually armed.

``monitor_start`` is a STATELESS session directive: the MCP tool only validates
its arguments and returns "Monitor loop requested ...", and the loop is armed
later, when the turn's tool result is consumed by the session-aware consumer.
Every drop on that path is silent to the model - an ``_meta.kiro.mcpServerName``
identity mismatch strips the directive marker, an oversized payload is refused,
and a slot-less session (sub-agent, cron, webhook, task-runner) is denied - so
the reply text "requested" is compatible with NO loop existing.

This script is the evidence the reply cannot give: it reads the auto-nudge loop
store and reports whether an ACTIVE loop is present, optionally requiring the
loop's message to name a specific PR (so a stale loop from earlier work is not
mistaken for this round's driver).

Read-only. Portable: stdlib only, no third-party deps, no shell pipelines.

Usage:  python3 monitor_armed.py [--pr N] [--match TEXT] [--json]
          --pr N       require an active loop whose message mentions PR N
                       (matches "PR #N", "PR N", "#N" and "pull/N")
          --match TEXT require an active loop whose message contains TEXT
                       (case-insensitive); repeatable
          --json       print the matching loops as JSON instead of text
Exit:   0  an active loop is present (and matches, when a filter was given)
       20  nothing armed - call monitor_start once more, and if it is still 20
           fall back to an in-turn wait + re-poll loop this same turn
        2  the loop store could not be read (missing/corrupt/permission) -
           treat exactly like 20: assume NOT armed
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

STORE_NAME = "autonudge.json"
#: Fields echoed for a matching loop - enough for the caller to confirm on a
#: later cycle that ``cycle_count`` is advancing (a present-but-frozen loop is
#: also a fallback case). This tuple is also the OUTPUT ALLOWLIST: a persisted
#: loop additionally carries its full re-injected instruction (``message``,
#: LLM/user-authored) and ``stop_sentinel_path``, and this script's output lands
#: in a chat transcript, so every emitted shape - text and ``--json`` alike - is
#: a projection of these status fields, never the stored dict.
REPORT_FIELDS = (
    "id",
    "slot_key",
    "active",
    "cycle_count",
    "max_cycles",
    "idle_secs",
    "next_due_ts",
)


def project(loop):
    """Return only the allowlisted status fields of *loop* (see REPORT_FIELDS)."""
    return {k: loop.get(k) for k in REPORT_FIELDS if k in loop}


def err(msg):
    sys.stderr.write(msg + "\n")


def store_path():
    """Return the auto-nudge store path, honouring KIROCREW_HOME."""
    home = os.environ.get("KIROCREW_HOME", "").strip()
    root = Path(home).expanduser() if home else Path.home() / ".kiro" / "crew"
    return root / STORE_NAME


def load_loops(path):
    """Return the store's loop dicts, or raise OSError/ValueError."""
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, list):  # tolerate a bare-list store
        raw = data
    elif isinstance(data, dict):
        raw = data.get("loops") or []
    else:
        raise ValueError("unexpected store shape: {}".format(type(data).__name__))
    return [lp for lp in raw if isinstance(lp, dict)]


def pr_patterns(pr):
    n = re.escape(str(pr))
    return (
        re.compile(r"\bPR\s*#?\s*" + n + r"\b", re.IGNORECASE),
        re.compile(r"#" + n + r"\b"),
        re.compile(r"/pull/" + n + r"\b"),
    )


def matches(loop, prs, texts):
    message = str(loop.get("message") or "")
    for pr in prs:
        if not any(pat.search(message) for pat in pr_patterns(pr)):
            return False
    low = message.lower()
    for text in texts:
        if text.lower() not in low:
            return False
    return True


def main(argv):
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--pr", action="append", default=[])
    ap.add_argument("--match", action="append", default=[])
    ap.add_argument("--json", action="store_true", dest="as_json")
    args = ap.parse_args(argv)

    path = store_path()
    try:
        loops = load_loops(path)
    except FileNotFoundError:
        err(
            "ERROR: no auto-nudge store at {} - nothing has ever armed a loop "
            "on this host. Treat as NOT armed.".format(path)
        )
        return 2
    except (OSError, ValueError) as exc:
        err("ERROR: cannot read {}: {}. Treat as NOT armed.".format(path, exc))
        return 2

    active = [lp for lp in loops if lp.get("active")]
    hits = [lp for lp in active if matches(lp, args.pr, args.match)]

    if not hits:
        if args.as_json:
            print(json.dumps({"armed": False, "active_loops": len(active)}, indent=2))
        elif not active:
            err(
                "NOT ARMED: the loop store holds no active loop ({} total entr"
                "{}). monitor_start reported a request, but nothing was applied."
                "".format(len(loops), "y" if len(loops) == 1 else "ies")
            )
        else:
            err(
                "NOT ARMED for this work: {} active loop(s) exist, none matching "
                "{}. A loop from other work is not this round's driver.".format(
                    len(active),
                    " + ".join(["PR " + str(p) for p in args.pr] + [repr(t) for t in args.match]),
                )
            )
        return 20

    if args.as_json:
        print(
            json.dumps(
                {"armed": True, "loops": [project(lp) for lp in hits]}, indent=2, default=str
            )
        )
    else:
        for lp in hits:
            print(
                "ARMED: " + " ".join("{}={}".format(k, lp.get(k)) for k in REPORT_FIELDS if k in lp)
            )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
