#!/usr/bin/env python3
"""Main-red watch: which pytest tests failed on a red push to main, and the issue that says so.

`main-red-watch.yml` runs this after a push run of `CI` on `main` ends. Every
function takes text or plain data and returns plain data, so the tests need no
network and a later workflow can import the same parsing.

    extract DIR              print {node id: first 80 characters of the message}
                             read from every <job id>.json (the job's check-run
                             annotations) and <job id>.log (its log) in DIR
    names-test DIR JOB       exit 0 when job JOB's files in DIR name a failing test
    confirmed DIR            exit 0 when a test is red in two or more jobs; else print
                             the tests red in one job only and exit 1
    pick-issue ISSUES.json   print the number of the open main-red issue
    render                   write the issue title and body for this red run
    should-close             exit 0 when a green run ends the open streak

The node ids come from the annotations the rootdir `conftest.py` writes, one per
failing test, with the node id as the title and `<node id> - <reason>` as the
message. The Actions token's read of a job log fails in this repository, so the
log is only the fallback for a job whose annotations could not be read or name
no test, and a job whose annotations name a test is read from them.

A run that names no failing test (a Fast Gate barrier timeout, a cancelled run,
a Coverage Gate failure) gives `{}`, and the workflow then writes nothing.

Log and annotation text is untrusted. It is read from files, never placed in a shell command,
and everything that reaches an issue is cut to printable ASCII, has its
backticks removed and sits inside a code span, so it cannot @-mention anyone or
break out of the issue body.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any

# The issue is written as `.github/ISSUE_TEMPLATE/ci_failure.yml` renders one: its
# title prefix, its labels, and one `### <label>` section per form field, in the
# form's order. The step checks out `.github/scripts` only, so the form is not
# read at run time; `test/test_main_red_issue_form.py` fails when it and these
# constants differ.
FORM_TITLE_PREFIX = "ci: "
FORM_LABELS = ("bug", "area: ci")
FORM_FIELDS = (
    ("search", "Existing issues"),
    ("run", "Failing run"),
    ("target", "Test or job"),
    ("commit", "Commit"),
    ("pattern", "How it fails"),
    ("runner", "Runner OS"),
    ("first-bad", "First bad commit"),
    ("output", "Error output"),
    ("context", "Anything else"),
)
FORM_SEARCHED = "I searched open issues and this is not already reported."
FORM_PATTERN_ONCE = "Not sure"
FORM_PATTERN_REPEATED = "Red every time on this commit"
FORM_RUNNERS = ("Linux", "macOS", "Windows", "Several")
NO_RESPONSE = "_No response_"
OWN_TITLE = "Main is red: "
TITLE_PREFIX = FORM_TITLE_PREFIX + OWN_TITLE
# An issue this watcher filed before its title took the form's prefix is still its own.
TITLE_PREFIXES = (TITLE_PREFIX, OWN_TITLE)
STATE_MARKER = "main-red-state"
MESSAGE_CHARS = 80
MAX_NODEIDS = 100
MAX_LISTED = 50
MAX_ONCE_SHOWN = 5
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


def annotation_entry(row: Any) -> tuple[str, str] | None:
    """``(node id, message)`` for one check-run annotation of a failing test, else ``None``.

    The title is the node id as data, so nothing is split out of rendered text. An
    annotation of another kind (the runner's `Process completed with exit code 1`,
    a collection error titled by its file, a notice) has no ``path.py::name`` title.
    """
    if not isinstance(row, dict) or row.get("annotation_level") != "failure":
        return None
    title, message = row.get("title"), row.get("message")
    if not isinstance(title, str) or not isinstance(message, str):
        return None
    path, sep, name = title.partition("::")
    if not sep or not name or _PATH.fullmatch(path) is None:
        return None
    lead = title + " - "
    reason = message[len(lead) :] if message.startswith(lead) else message
    return clean(title.replace("\\", "/"), 200), clean(reason, MESSAGE_CHARS)


def parse_annotations(rows: Any) -> dict[str, str]:
    """``{node id: message prefix}`` from the annotations of one job's check run."""
    found: dict[str, str] = {}
    for row in rows if isinstance(rows, list) else []:
        entry = annotation_entry(row)
        if entry is not None:
            found.setdefault(entry[0], entry[1])
    return found


def job_failures(annotations_text: str | None, log_text: str | None) -> dict[str, str]:
    """One job's failing tests: from its annotations when they name one, else from its log.

    A shard that died before the conftest wrote its annotations has readable
    annotations that name no test, and its log may still hold a summary.
    """
    if annotations_text is not None:
        try:
            found = parse_annotations(json.loads(annotations_text))
        except ValueError:
            found = {}
        if found:
            return found
    return parse_failures(log_text or "")


def repeated(per_job: list[dict[str, str]]) -> list[str]:
    """Node ids red in two or more jobs of one run, in first-seen order.

    A test that fails on two legs of a run (a Linux and a Windows shard) is a
    break; one that fails on a single leg is as often a flake, and a flake must
    not open the issue.
    """
    seen: dict[str, int] = {}
    for failures in per_job:
        for nodeid in failures:
            seen[nodeid] = seen.get(nodeid, 0) + 1
    return [nodeid for nodeid, count in seen.items() if count >= 2]


def merge_failures(per_job: list[dict[str, str]]) -> dict[str, str]:
    """Union over jobs, first message wins, cut to ``MAX_NODEIDS``.

    Node ids red in two or more jobs come first, so the issue is titled by one.
    """
    merged: dict[str, str] = {}
    for failures in per_job:
        for nodeid, message in failures.items():
            merged.setdefault(nodeid, message)
    first = repeated(per_job)
    ordered = first + [nodeid for nodeid in merged if nodeid not in set(first)]
    return {nodeid: merged[nodeid] for nodeid in ordered[:MAX_NODEIDS]}


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


def _fix_line_module() -> Any:
    path = Path(__file__).resolve().with_name("main_red_fix_line.py")
    spec = importlib.util.spec_from_file_location("main_red_fix_line", str(path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_fix = _fix_line_module()
fix_prs, fix_line = _fix.fix_prs, _fix.fix_line


def pick_issue(issues: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The oldest open issue this watcher filed: its author, title prefix and record.

    Needs `author` in the issue list. An issue without it, or by a person, is never ours.
    """
    ours = [
        issue
        for issue in issues
        if is_watcher_author(issue.get("author"))
        and str(issue.get("title", "")).startswith(TITLE_PREFIXES)
        and parse_state(str(issue.get("body") or "")) is not None
    ]
    return min(ours, key=lambda item: int(item["number"])) if ours else None


def title_for(failures: dict[str, str]) -> str:
    return TITLE_PREFIX + clean(next(iter(failures)), 150)


def runner_os(job_names: list[str]) -> str | None:
    """The form's `Runner OS` answer for the failed jobs of a run, or ``None``.

    A job name says its OS in any case: `Backend Tests (Windows)`, and the matrix
    legs `(IPv6, windows-latest)` and `(IPv6, macos-latest)`. A name that says
    neither runs on Linux.
    """
    folded = [name.casefold() for name in job_names if name.strip()]
    seen = {
        "Windows" if "windows" in name else "macOS" if "macos" in name else "Linux"
        for name in folded
    }
    return "Several" if len(seen) > 1 else next(iter(seen), None)


def render_body(
    state: dict[str, Any],
    prs: list[dict[str, str]] | None,
    issue_number: int | None = None,
    runner: str | None = None,
) -> str:
    """The issue body: the CI failure form's sections, then the state record."""
    failures: dict[str, str] = state["failures"]
    listed = list(failures.items())[:MAX_LISTED]
    more = len(failures) - len(listed)
    first = next(iter(failures))
    output = [f"{nodeid} - {message}" if message else nodeid for nodeid, message in listed]
    context = [
        f"Main's `CI` push run [{state['last_sha']}]({state['last_url']}) ended red with "
        f"{len(failures)} failing test(s). First red: [{state['first_sha']}]({state['first_url']}). "
        f"Streak: {state['streak']} red main run(s) that named a failing test.",
        "",
        fix_line(prs, issue_number),
        "",
        "Failing tests, with the first 80 characters of each message:",
        "",
    ]
    context += [
        f"- `{nodeid}`" + (f": `{message}`" if message else "") for nodeid, message in listed
    ]
    if more > 0:
        context.append(f"- and {more} more in the run's job logs")
    context += [
        "",
        "A push to `main` that runs the tests green closes this issue. A run that names "
        "no failing test (a Fast Gate timeout, a cancelled run) changes nothing here.",
        "",
        "<sub>Filed by `.github/workflows/main-red-watch.yml`. It never assigns anyone.</sub>",
    ]
    answers = {
        "search": f"- [x] {FORM_SEARCHED}",
        "run": state["last_url"],
        "target": f"`{first}`" + (f" and {len(failures) - 1} more" if len(failures) > 1 else ""),
        "commit": f"{state['last_sha']} (push to `main`)",
        "pattern": (
            f"{FORM_PATTERN_REPEATED}: {state['streak']} red push runs in a row"
            if state["streak"] > 1 and state["first_sha"] == state["last_sha"]
            else FORM_PATTERN_ONCE
        ),
        "runner": runner if runner in FORM_RUNNERS else NO_RESPONSE,
        "first-bad": f"{state['first_sha']}, the commit of the first red run of this streak",
        "output": "\n".join(["```shell", *output, "```"]),
        "context": "\n".join(context),
    }
    lines: list[str] = []
    for key, label in FORM_FIELDS:
        lines += [f"### {label}", "", answers[key], ""]
    lines += [f"<!-- {STATE_MARKER} {dump_state(state)} -->", ""]
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


def _job(folder: Path, stem: str) -> dict[str, str]:
    notes, log = folder / f"{stem}.json", folder / f"{stem}.log"
    return job_failures(
        _read_text(str(notes)) if notes.is_file() else None,
        _read_text(str(log)) if log.is_file() else None,
    )


def _per_job(logdir: str) -> list[dict[str, str]]:
    folder = Path(logdir)
    stems = {path.stem for kind in ("*.json", "*.log") for path in folder.glob(kind)}
    ordered = sorted(stems, key=lambda name: (len(name), name))
    return [_job(folder, stem) for stem in ordered]


def cmd_extract(args: argparse.Namespace) -> int:
    print(json.dumps(merge_failures(_per_job(args.logdir))))
    return 0


def cmd_confirmed(args: argparse.Namespace) -> int:
    per_job = _per_job(args.logdir)
    if repeated(per_job):
        return 0
    once = list(merge_failures(per_job))
    shown = ", ".join(once[:MAX_ONCE_SHOWN])
    more = f" and {len(once) - MAX_ONCE_SHOWN} more" if len(once) > MAX_ONCE_SHOWN else ""
    print(f"{len(once)} test(s) red in one job only ({shown}{more})")
    return 1


def cmd_names_test(args: argparse.Namespace) -> int:
    return 0 if args.job.isdigit() and _job(Path(args.logdir), args.job) else 1


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
    prs: list[dict[str, str]] | None
    try:
        prs = fix_prs(read_json_values(_read_text(args.timeline)), args.repo, args.issue_number)
    except Exception as exc:  # the issue is still written; its fix line says unknown
        print(f"::warning::The fix line could not be built ({clean(str(exc), 200)}).")
        prs = None
    Path(args.out_title).write_text(title_for(failures), encoding="utf-8")
    body = render_body(state, prs, args.issue_number, runner_os(_red_job_names(args)))
    Path(args.out_body).write_text(body, encoding="utf-8")
    return 0


def _red_job_names(args: argparse.Namespace) -> list[str]:
    """Names of the failed jobs that named a failing test, from `<id>\\t<name>` rows.

    A job that failed beside the tests (a Coverage Gate on Linux next to a
    Windows-only break) says nothing about where the test fails.
    """
    names = []
    for row in _read_text(args.jobs).splitlines():
        job, _, name = row.partition("\t")
        if args.logdir and not (job.isdigit() and _job(Path(args.logdir), job)):
            continue
        names.append(name)
    return names


def cmd_should_close(args: argparse.Namespace) -> int:
    return 0 if should_close(parse_state(_read_text(args.prev_body)), int(args.run_id)) else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    extract = sub.add_parser("extract")
    extract.add_argument("logdir")
    extract.set_defaults(run=cmd_extract)
    sure = sub.add_parser("confirmed")
    sure.add_argument("logdir")
    sure.set_defaults(run=cmd_confirmed)
    names = sub.add_parser("names-test")
    names.add_argument("logdir")
    names.add_argument("job")
    names.set_defaults(run=cmd_names_test)
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
    render.add_argument("--jobs")
    render.add_argument("--logdir")
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
