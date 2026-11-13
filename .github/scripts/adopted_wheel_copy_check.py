#!/usr/bin/env python3
"""PR Hygiene: a change to an adopted app's wheel copy names its app-repo twin.

Once ``src/kiro_crew/apps/adoption-pins.json`` pins a former built-in, a host
that adopted it runs the app's own repository at the pinned commit, never the
wheel copy. A change that lands only in the wheel copy
(``src/kiro_crew/apps/builtins/<dir>/`` or ``website/src/apps/<name>/``) is
therefore lost for every adopted user. The change goes to the app repository
first, and the PR body names that commit or pull request on its own line:

    App repo: <owner>/<repo>@<commit sha, 7-40 hex>
    App repo: <owner>/<repo>#<pull request number>

where ``<owner>/<repo>`` is the pin row's ``repository``. One line per changed
app. An optional list bullet is accepted; the slug compares case-insensitively.

Exempt: deleting a wheel-copy file (removing the copy loses nothing), and test
files (they never ship to a user). A name counts as pinned when the merge
base, the base tip or the head pins it.

Usage:
    adopted_wheel_copy_check.py NAME_STATUS_FILE PINS_FILE [PINS_FILE ...]

NAME_STATUS_FILE is ``git diff --name-status --no-renames <merge-base> <head>``.
Each PINS_FILE is one revision's ``adoption-pins.json``; an empty file is a
revision without the table. The PR body comes from the PR_BODY environment
variable and is only ever read as data.

LIMIT, stated plainly: this checks that a well-formed line names the right
repository, not that the commit exists or carries the same change. Judging
that is the reviewers' job.

Exit 0 = pass; 1 = fail.
"""

from __future__ import annotations

import json
import os
import re
import sys

BUILTINS_DIR = "src/kiro_crew/apps/builtins/"
WEBSITE_APPS_DIR = "website/src/apps/"

_REPOSITORY_RE = re.compile(r"https://github\.com/([A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100})")
MARKER_RE = re.compile(
    r"^ {0,3}(?:[-*+] +)?App repo: +(?P<slug>[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}?)"
    r"(?:@(?P<sha>[0-9a-f]{7,40})|#(?P<pr>[1-9][0-9]*))\.? *$"
)


def load_pins(texts: list[str]) -> dict[str, str]:
    """Map each pinned name to its ``owner/repo`` slug, across every revision.

    Raises ValueError on a table that is present but unreadable: a gate that
    cannot see the pins must not pass.
    """
    pins: dict[str, str] = {}
    for text in texts:
        if not text.strip():
            continue
        data = json.loads(text)
        for row in data.get("pins", []):
            name = row.get("name")
            match = _REPOSITORY_RE.fullmatch(str(row.get("repository", "")))
            if not isinstance(name, str) or not name or match is None:
                raise ValueError(f"unreadable pin row: {row!r}")
            pins[name] = match.group(1)
    return pins


def wheel_copy_dirs(name: str) -> tuple[str, str]:
    """The two wheel-copy trees of a pinned app: backend package and frontend."""
    return f"{BUILTINS_DIR}{name.replace('-', '_')}/", f"{WEBSITE_APPS_DIR}{name}/"


def _is_test_path(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return (
        "/test/" in path
        or "/tests/" in path
        or "/__tests__/" in path
        or name.startswith("test_")
        or name == "conftest.py"
        or ".test." in name
        or ".spec." in name
    )


def changed_apps(name_status: str, pins: dict[str, str]) -> dict[str, list[str]]:
    """Pinned name -> the wheel-copy paths the diff adds or modifies."""
    hits: dict[str, list[str]] = {}
    for line in name_status.splitlines():
        parts = line.split("\t")
        if len(parts) < 2 or parts[0].startswith("D"):
            continue
        for path in parts[1:]:
            if _is_test_path(path):
                continue
            for name in pins:
                if path.startswith(wheel_copy_dirs(name)):
                    hits.setdefault(name, []).append(path)
    return hits


def named_slugs(body: str) -> set[str]:
    """Lower-cased ``owner/repo`` of every well-formed marker line."""
    out: set[str] = set()
    for raw in body.replace("\r", "").split("\n"):
        match = MARKER_RE.match(raw.rstrip())
        if match:
            out.add(match.group("slug").lower())
    return out


def check(name_status: str, pin_texts: list[str], body: str) -> list[str]:
    """One error line per changed pinned app whose repository the body never names."""
    pins = load_pins(pin_texts)
    named = named_slugs(body)
    errors = []
    for name, paths in sorted(changed_apps(name_status, pins).items()):
        slug = pins[name]
        if slug.lower() not in named:
            shown = ", ".join(paths[:3]) + (f" (+{len(paths) - 3} more)" if len(paths) > 3 else "")
            errors.append(
                f"{name}: {shown} -- add 'App repo: {slug}@<commit>' or 'App repo: {slug}#<PR>'"
            )
    return errors


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__, file=sys.stderr)
        return 1
    with open(argv[1], encoding="utf-8") as fh:
        name_status = fh.read()
    pin_texts = []
    for path in argv[2:]:
        with open(path, encoding="utf-8") as fh:
            pin_texts.append(fh.read())
    try:
        errors = check(name_status, pin_texts, os.environ.get("PR_BODY", ""))
    except ValueError as exc:
        print(
            f"::error::adoption-pins.json is unreadable, so the wheel-copy check cannot run: {exc}"
        )
        return 1
    if not errors:
        print("No adopted app's wheel copy changes without its app-repo commit.")
        return 0
    print(
        "::error::This PR changes the wheel copy of an adopted app. Adopted hosts run the app's"
        " own repository, so they never get this change. Land it in the app repository first,"
        " then name that commit or PR in the PR body (no push needed)."
    )
    for line in errors:
        print(f"  {line}")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
