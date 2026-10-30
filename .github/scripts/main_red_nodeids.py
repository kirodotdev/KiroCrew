#!/usr/bin/env python3
"""Main-red watch: which pytest tests failed on a red push to main, and the issue that says so.

`main-red-watch.yml` runs this after a push run of `CI` on `main` ends. Every
function takes text or plain data and returns plain data, so the tests need no
network and a later workflow can import the same parsing.

    extract LOGDIR           print {node id: first 80 characters of the message}
                             read from every *.log file in LOGDIR
    pick-issue ISSUES.json   print the number of the open main-red issue
    render                   write the issue title and body for this red run
    should-close             exit 0 when a green run ends the open streak

A run that names no failing test (a Fast Gate barrier timeout, a cancelled run,
a Coverage Gate failure) gives `{}`, and the workflow then writes nothing.

Log text is untrusted. It is read from files, never placed in a shell command,
and everything that reaches an issue is cut to printable ASCII, has its
backticks removed and sits inside a code span, so it cannot @-mention anyone or
break out of the issue body.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

TITLE_PREFIX = "Main is red: "
STATE_MARKER = "main-red-state"
MESSAGE_CHARS = 80
MAX_NODEIDS = 100
MAX_LISTED = 50
MAX_FIX_PRS = 3
STALE = 10

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_STAMP = re.compile(r"^\ufeff?\d{4}-\d\d-\d\dT[\d:.]+Z ")
# The short test summary pytest ends with. `[gw0] [ 12%] FAILED ...` progress
# lines do not start with the word, and a `FAIL Required test coverage` line is a
# different word, so neither can match. The node id is split by code, not by one
# regex: a parameter id may hold spaces and ` - `, and a pattern with nested
# repeats over untrusted log text can backtrack without bound.
_PATH = re.compile(r"[A-Za-z0-9_./\\-]+\.py")
_SHA = re.compile(r"[0-9a-f]{1,12}")
_URL = re.compile(r"https://github\.com/[A-Za-z0-9_./-]{1,200}")
STATE_OPEN = "<!-- " + STATE_MARKER + " "
STATE_CLOSE = " -->"
_LOGIN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}(\[bot\])?$")


def clean(text: str, limit: int) -> str:
    """Printable ASCII, no backtick, runs of space collapsed, cut to ``limit``."""
    text = "".join(ch if " " <= ch <= "~" else " " if ch in "\t\r\n" else "?" for ch in text)
    return " ".join(text.replace("`", "'").split())[:limit]


def log_line(raw: str) -> str:
    """One log line without its timestamp, colour codes or line end."""
    return _STAMP.sub("", _ANSI.sub("", raw.rstrip("\r\n")))


def split_nodeid(rest: str) -> tuple[str, str]:
    """Split ``test_name[param] - message`` into the name and what follows it.

    A name runs to the first space outside a ``[...]`` group. One pass, no backtracking.
    """
    in_group = False
    for at, ch in enumerate(rest):
        if ch == "[":
            in_group = True
        elif ch == "]":
            in_group = False
        elif ch.isspace() and not in_group:
            return rest[:at], rest[at:]
    return rest, ""


def summary_entry(line: str) -> tuple[str, str] | None:
    """``(node id, message)`` for one `FAILED`/`ERROR` summary line, else ``None``."""
    word, _, body = line.partition(" ")
    if word not in ("FAILED", "ERROR"):
        return None
    path, sep, rest = body.partition("::")
    if not sep or _PATH.fullmatch(path) is None:
        return None
    name, tail = split_nodeid(rest)
    if not name or not (tail == "" or tail.startswith(" - ")):
        return None
    nodeid = (path + "::" + name).replace("\\", "/")
    return clean(nodeid, 200), clean(tail[3:], MESSAGE_CHARS)


def parse_failures(log_text: str) -> dict[str, str]:
    """``{node id: message prefix}`` from the short test summary of one job log."""
    found: dict[str, str] = {}
    for raw in log_text.splitlines():
        entry = summary_entry(log_line(raw))
        if entry is not None:
            found.setdefault(entry[0], entry[1])
    return found


def merge_failures(per_job: list[dict[str, str]]) -> dict[str, str]:
    """Union over jobs, first message wins, cut to ``MAX_NODEIDS``."""
    merged: dict[str, str] = {}
    for failures in per_job:
        for nodeid, message in failures.items():
            merged.setdefault(nodeid, message)
    return dict(list(merged.items())[:MAX_NODEIDS])


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _text(value: Any, pattern: re.Pattern[str]) -> str | None:
    return value if isinstance(value, str) and pattern.fullmatch(value) else None


def parse_state(body: str) -> dict[str, Any] | None:
    """The streak record an earlier run left in an issue body, or ``None``.

    The body is editable by anyone who can edit the issue, so every field is
    checked and rebuilt rather than trusted.
    """
    text = body or ""
    start = text.find(STATE_OPEN)
    end = text.find(STATE_CLOSE, start) if start >= 0 else -1
    if start < 0 or end < 0:
        return None
    try:
        raw = json.loads(text[start + len(STATE_OPEN) : end])
    except ValueError:
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("failures"), dict):
        return None
    state: dict[str, Any] = {}
    for key in ("first_run_id", "last_run_id", "streak"):
        state[key] = _count(raw.get(key))
    for key in ("first_sha", "last_sha"):
        state[key] = _text(raw.get(key), _SHA)
    for key in ("first_url", "last_url"):
        state[key] = _text(raw.get(key), _URL)
    if any(value is None for value in state.values()):
        return None
    pairs = [(clean(str(k), 200), clean(str(v), MESSAGE_CHARS)) for k, v in raw["failures"].items()]
    state["failures"] = dict(pairs[:MAX_NODEIDS])
    return state


def dump_state(state: dict[str, Any]) -> str:
    """JSON that cannot end the HTML comment it sits in."""
    text = json.dumps(state, ensure_ascii=True)
    return text.replace("<", "\\u003c").replace(">", "\\u003e").replace("--", "-\\u002d")


def next_state(
    previous: dict[str, Any] | None,
    failures: dict[str, str],
    sha: str,
    run_id: int,
    url: str,
) -> dict[str, Any] | None:
    """The streak after this red run, or ``None`` when an older run finished late."""
    latest = {"last_sha": sha[:12], "last_run_id": run_id, "last_url": url, "failures": failures}
    if previous is None:
        first = {"first_sha": sha[:12], "first_run_id": run_id, "first_url": url, "streak": 1}
        return {**first, **latest}
    last = int(previous["last_run_id"])
    if run_id < last:
        return None
    streak = int(previous["streak"]) + (0 if run_id == last else 1)
    return {**previous, **latest, "streak": streak}


def should_close(previous: dict[str, Any] | None, run_id: int) -> bool:
    """A green run ends a streak unless it is older than the last red run.

    The same run id is a successful re-run of that red run and does end it.
    """
    return previous is not None and run_id >= int(previous["last_run_id"])


WATCHER_LOGINS = ("app/github-actions", "github-actions[bot]")


def is_watcher_author(author: Any) -> bool:
    """True when the issue was written by the Actions token, which files ours.

    Anyone can open an issue with our title prefix and a well-formed record, so
    the record is trusted only from the workflow's own author. ``gh`` spells a
    bot login ``app/<slug>``; the REST API spells it ``<slug>[bot]``.
    """
    if not isinstance(author, dict):
        return False
    login = author.get("login")
    if login in WATCHER_LOGINS:
        return True
    return login == "github-actions" and author.get("is_bot") is True


def pick_issue(issues: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The oldest open issue this watcher filed: its author, title prefix and record.

    Needs `author` in the issue list. An issue without it, or by a person, is never ours.
    """
    ours = [
        issue
        for issue in issues
        if is_watcher_author(issue.get("author"))
        and str(issue.get("title", "")).startswith(TITLE_PREFIX)
        and parse_state(str(issue.get("body") or "")) is not None
    ]
    return min(ours, key=lambda item: int(item["number"])) if ours else None


def fix_prs(rows: list[dict[str, Any]], repo: str) -> list[dict[str, str]]:
    """Open pull requests of ``repo`` that cross-reference the issue, oldest first."""
    prefix = f"https://github.com/{repo}/pull/"
    out: list[dict[str, str]] = []
    for row in rows:
        url = str(row.get("url") or "")
        login = str(row.get("author") or "")
        if row.get("state") != "open" or not url.startswith(prefix) or not _LOGIN.match(login):
            continue
        number = url[len(prefix) :]
        if number.isdigit() and url not in {p["url"] for p in out}:
            out.append(
                {
                    "url": url,
                    "number": number,
                    "author": login,
                    "created": str(row.get("created") or ""),
                }
            )
    return sorted(out, key=lambda pr: pr["created"])[:MAX_FIX_PRS]


def _opened(stamp: str) -> str:
    try:
        return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").strftime("%Y-%m-%d %H:%MZ")
    except ValueError:
        return "at an unknown time"


def fix_line(prs: list[dict[str, str]], issue_number: int | None) -> str:
    """The one line the watch rewrites on every main run."""
    if not prs:
        ref = f"#{issue_number}" if issue_number else "#<this issue>"
        return (
            "Fix in progress: none yet. Anyone may open a fix PR "
            f"(put `Fixes {ref}` in its body)."
        )
    parts = []
    for pr in prs:
        who = pr["author"] if pr["author"].endswith("[bot]") else "@" + pr["author"]
        parts.append(f"#{pr['number']} ({who}, opened {_opened(pr['created'])})")
    return "Fix in progress: " + "; ".join(parts)


def title_for(failures: dict[str, str]) -> str:
    return TITLE_PREFIX + clean(next(iter(failures)), 150)


def render_body(
    state: dict[str, Any], prs: list[dict[str, str]], issue_number: int | None = None
) -> str:
    failures: dict[str, str] = state["failures"]
    lines = [
        f"Main's `CI` push run [{state['last_sha']}]({state['last_url']}) ended red with "
        f"{len(failures)} failing test(s). First red: [{state['first_sha']}]({state['first_url']}). "
        f"Streak: {state['streak']} red main run(s) that named a failing test.",
        "",
        fix_line(prs, issue_number),
        "",
        "Failing tests, with the first 80 characters of each message:",
        "",
    ]
    for nodeid, message in list(failures.items())[:MAX_LISTED]:
        lines.append(f"- `{nodeid}`" + (f": `{message}`" if message else ""))
    if len(failures) > MAX_LISTED:
        lines.append(f"- and {len(failures) - MAX_LISTED} more in the run's job logs")
    lines += [
        "",
        "A push to `main` that runs the tests green closes this issue. A run that names "
        "no failing test (a Fast Gate timeout, a cancelled run) changes nothing here.",
        "",
        "<sub>Filed by `.github/workflows/main-red-watch.yml`. It never assigns anyone.</sub>",
        f"<!-- {STATE_MARKER} {dump_state(state)} -->",
        "",
    ]
    return "\n".join(lines)


def read_json_values(text: str) -> list[dict[str, Any]]:
    """Every JSON object in ``text``, one per line or pretty-printed back to back."""
    decoder = json.JSONDecoder()
    rows: list[dict[str, Any]] = []
    at = 0
    while at < len(text):
        if text[at].isspace():
            at += 1
            continue
        try:
            row, at = decoder.raw_decode(text, at)
        except ValueError:
            break
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _read_text(path: str | None) -> str:
    if not path or not Path(path).is_file():
        return ""
    return Path(path).read_text(encoding="utf-8", errors="replace")


def cmd_extract(args: argparse.Namespace) -> int:
    logs = sorted(Path(args.logdir).glob("*.log"), key=lambda p: (len(p.stem), p.stem))
    per_job = [parse_failures(_read_text(str(path))) for path in logs]
    print(json.dumps(merge_failures(per_job)))
    return 0


def cmd_pick_issue(args: argparse.Namespace) -> int:
    issues = json.loads(_read_text(args.issues) or "[]")
    issue = pick_issue(issues)
    if issue is None:
        return 0
    if args.body_out:
        Path(args.body_out).write_text(str(issue.get("body") or ""), encoding="utf-8")
    print(int(issue["number"]))
    return 0


def cmd_render(args: argparse.Namespace) -> int:
    failures = json.loads(_read_text(args.failures) or "{}")
    if _text(args.sha[:12], _SHA) is None or _text(args.run_url, _URL) is None:
        print("The run's sha or url has an unexpected shape.", file=sys.stderr)
        return 2
    state = next_state(
        parse_state(_read_text(args.prev_body)),
        failures,
        args.sha,
        int(args.run_id),
        args.run_url,
    )
    if state is None:
        print("An older run finished after a newer red one; nothing to update.")
        return STALE
    prs = fix_prs(read_json_values(_read_text(args.timeline)), args.repo)
    Path(args.out_title).write_text(title_for(failures), encoding="utf-8")
    Path(args.out_body).write_text(render_body(state, prs, args.issue_number), encoding="utf-8")
    return 0


def cmd_should_close(args: argparse.Namespace) -> int:
    return 0 if should_close(parse_state(_read_text(args.prev_body)), int(args.run_id)) else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    extract = sub.add_parser("extract")
    extract.add_argument("logdir")
    extract.set_defaults(run=cmd_extract)
    pick = sub.add_parser("pick-issue")
    pick.add_argument("issues")
    pick.add_argument("--body-out")
    pick.set_defaults(run=cmd_pick_issue)
    render = sub.add_parser("render")
    render.add_argument("--failures", required=True)
    render.add_argument("--prev-body")
    render.add_argument("--timeline")
    render.add_argument("--repo", required=True)
    render.add_argument("--sha", required=True)
    render.add_argument("--run-id", required=True)
    render.add_argument("--run-url", required=True)
    render.add_argument("--issue-number", type=int)
    render.add_argument("--out-title", required=True)
    render.add_argument("--out-body", required=True)
    render.set_defaults(run=cmd_render)
    close = sub.add_parser("should-close")
    close.add_argument("--prev-body", required=True)
    close.add_argument("--run-id", required=True)
    close.set_defaults(run=cmd_should_close)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.run(args))


if __name__ == "__main__":
    sys.exit(main())
