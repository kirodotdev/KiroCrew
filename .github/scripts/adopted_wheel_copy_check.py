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

A PR that adds or changes a pin row also names, per changed row, the core
commit whose wheel copy the pinned app-repo commit was ported from:

    Wheel-copy base: <name>@<core commit sha, 7-40 hex>

and that app's wheel copy (non-test files) at the base tip must equal the
wheel copy at that commit. A wheel-copy change on the base after the port base
therefore fails the pin PR until the app repository takes it and the line
names a newer base. The base tip is the one at the PR event that ran the
check: nothing re-runs it when the base moves, so a pin PR's approver edits
the body (or pushes) right before approving to compare the current tip. The
base tip, not the head, is compared: a pin PR
often changes the wheel copy itself (its ``app.json`` version, which a first
pin must equal), and the PR's own wheel-copy changes are what the
``App repo:`` line above already covers. A row is changed when the head
table's row differs from the merge base table's row of the same name: the
first PINS_FILE is the merge base, the last is the head. The base tip comes
from the BASE_TIP environment variable, and the comparison reads git trees
only (``git diff-tree``), so the named commit must be in the checkout.

Exit 0 = pass; 1 = fail.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from typing import Callable

BUILTINS_DIR = "src/kiro_crew/apps/builtins/"
WEBSITE_APPS_DIR = "website/src/apps/"
BASE_PREFIX = "Wheel-copy base:"
_HEX = frozenset("0123456789abcdef")

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


class CannotCompare(Exception):
    """The wheel copy at a named base cannot be read, so the check must not pass."""


def _rows(text: str) -> dict[str, dict]:
    if not text.strip():
        return {}
    return {row.get("name"): row for row in json.loads(text).get("pins", [])}


def changed_rows(pin_texts: list[str]) -> list[str]:
    """Names whose head row (last table) differs from the merge-base row (first table)."""
    if len(pin_texts) < 2:
        return []
    before, after = _rows(pin_texts[0]), _rows(pin_texts[-1])
    return sorted(name for name, row in after.items() if before.get(name) != row)


def named_bases(body: str) -> dict[str, set[str]]:
    """App name -> every sha its well-formed ``Wheel-copy base:`` lines name."""
    out: dict[str, set[str]] = {}
    for raw in body.replace("\r", "").split("\n"):
        line = raw.rstrip()
        text = line.lstrip(" ")
        if len(line) - len(text) > 3:
            continue
        if text[:2] in ("- ", "* ", "+ "):
            text = text[2:].lstrip(" ")
        if not text.startswith(BASE_PREFIX):
            continue
        value = text[len(BASE_PREFIX) :].strip()
        if value.endswith("."):
            value = value[:-1]
        name, sep, sha = value.partition("@")
        if sep and name and 7 <= len(sha) <= 40 and set(sha) <= _HEX:
            out.setdefault(name, set()).add(sha)
    return out


def git_wheel_copy_drift(revision: str) -> Callable[[str, str], list[str]]:
    """A comparer: non-test wheel-copy paths that differ between ``base`` and ``revision``."""

    def compare(base: str, name: str) -> list[str]:
        if not revision:
            raise CannotCompare("BASE_TIP is not set, so there is no base tip to compare")
        proc = subprocess.run(
            ["git", "diff-tree", "-r", "-z", "--name-only", "--no-renames"]
            + [f"{base}^{{commit}}", f"{revision}^{{commit}}", "--", *wheel_copy_dirs(name)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        if proc.returncode != 0:
            raise CannotCompare(
                f"{name}: cannot read the wheel copy at {base} ({proc.stderr.strip()});"
                " name a full commit sha on main"
            )
        return sorted(p for p in proc.stdout.split("\0") if p and not _is_test_path(p))

    return compare


def check_bases(
    pin_texts: list[str], body: str, compare: Callable[[str, str], list[str]]
) -> list[str]:
    """One error line per changed pin row whose wheel copy moved after its named base."""
    pins = load_pins(pin_texts)
    bases = named_bases(body)
    errors = []
    for name in changed_rows(pin_texts):
        shas = bases.get(name, set())
        if not shas:
            errors.append(
                f"{name}: this PR adds or changes its pin row -- add 'Wheel-copy base:"
                f" {name}@<commit>' naming the core commit the pinned app-repo commit was"
                " ported from"
            )
            continue
        if len(shas) > 1:
            errors.append(f"{name}: {len(shas)} 'Wheel-copy base:' lines differ -- keep one")
            continue
        (sha,) = shas
        drift = compare(sha, name)
        if drift:
            shown = ", ".join(drift[:3]) + (f" (+{len(drift) - 3} more)" if len(drift) > 3 else "")
            errors.append(
                f"{name}: the wheel copy changed after {sha}: {shown} -- port it to"
                f" {pins[name]}, pin that commit, then name the new base"
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
    body = os.environ.get("PR_BODY", "")
    try:
        errors = check(name_status, pin_texts, body)
        base_errors = check_bases(
            pin_texts, body, git_wheel_copy_drift(os.environ.get("BASE_TIP", ""))
        )
    except ValueError as exc:
        print(
            f"::error::adoption-pins.json is unreadable, so the wheel-copy check cannot run: {exc}"
        )
        return 1
    except CannotCompare as exc:
        print(f"::error::The pinned app's wheel copy cannot be compared with its base: {exc}")
        return 1
    if not errors and not base_errors:
        print("No adopted app's wheel copy changes without its app-repo commit.")
        return 0
    if errors:
        print(
            "::error::This PR changes the wheel copy of an adopted app. Adopted hosts run the"
            " app's own repository, so they never get this change. Land it in the app repository"
            " first, then name that commit or PR in the PR body (no push needed)."
        )
        for line in errors:
            print(f"  {line}")
    if base_errors:
        print(
            "::error::This PR pins an app whose wheel copy is not the one its app-repo commit was"
            " ported from, so adopting it would drop a wheel-copy change. Port every change since"
            " the base to the app repository, pin that commit, and name the base in the PR body."
        )
        for line in base_errors:
            print(f"  {line}")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
