"""Fail a Windows payload whose deepest file would break an in-place update.

electron-updater applies an update by re-running the installer over the
existing install. Before the new payload is published, the installer runs the
previous version's uninstaller with ``--updated``, and that uninstaller
(electron-builder's ``un.atomicRMDir``) renames every installed file to::

    %TEMP%\\nsaXXXX.tmp\\old-install\\<path relative to the install root>

NSIS names that temp directory with ``GetTempFileName`` and a three-letter
prefix (``ns`` plus a letter from ``a`` to ``z``), which appends up to four hex
digits and ``.tmp``: 11 characters, such as ``nsd3D2F.tmp``.

The first rename that fails aborts it with exit code 2, the installer's retries
fail the same way, and the update is abandoned. A rename fails when the target
reaches Windows' ``MAX_PATH`` (260 characters including the terminating NUL),
so the budget for a path relative to the install root is::

    259                      the longest usable path
  -  48  %TEMP%              C:\\Users\\<profile folder>\\AppData\\Local\\Temp
                             (9 + 20 + 19: a local or domain account name, and
                             so its profile folder, is at most 20 characters)
  -  25  \\nsaXXXX.tmp\\old-install\\  the uninstaller's move directory
  = 186

The budget does not depend on where the app is installed: the moved path is
built from %TEMP% and the relative path alone. A fresh install never shows the
problem, because the installed path is shorter than the moved one.

``--root`` is the directory to measure. ``--prefix`` is that directory's own
path relative to the install root, for a payload measured before packaging
(``resources\\backend-dist\\kirocrew-backend`` for the bundled backend); it is
empty when ``--root`` is the install root itself.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

MAX_USABLE_PATH = 259
TEMP_ALLOWANCE = len("C:\\Users\\") + 20 + len("\\AppData\\Local\\Temp")
MOVE_DIRECTORY = len("\\nsaXXXX.tmp\\old-install\\")
RELATIVE_PATH_BUDGET = MAX_USABLE_PATH - TEMP_ALLOWANCE - MOVE_DIRECTORY


def relative_paths(root: Path, prefix: str = "") -> list[str]:
    """Every file under ``root`` as a backslash path relative to the install root."""

    lead = prefix.strip("\\/").replace("/", "\\")
    found: list[str] = []
    for directory, _dirnames, filenames in os.walk(root):
        relative = Path(directory).relative_to(root).parts
        for name in filenames:
            found.append("\\".join(part for part in (lead, *relative, name) if part))
    return found


def over_budget(paths: list[str], budget: int = RELATIVE_PATH_BUDGET) -> list[str]:
    """The paths longer than ``budget``, longest first."""

    return sorted((path for path in paths if len(path) > budget), key=len, reverse=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--prefix", default="")
    parser.add_argument("--show", type=int, default=5)
    args = parser.parse_args(argv)

    paths = relative_paths(args.root, args.prefix)
    if not paths:
        print(f"no files under {args.root}", file=sys.stderr)
        return 1
    longest = sorted(paths, key=len, reverse=True)[: args.show]
    print(
        f"{len(paths)} file(s); longest path relative to the install root is "
        f"{len(longest[0])} characters (budget {RELATIVE_PATH_BUDGET} = {MAX_USABLE_PATH} usable "
        f"- {TEMP_ALLOWANCE} %TEMP% - {MOVE_DIRECTORY} uninstaller move directory)"
    )
    for path in longest:
        print(f"  {len(path)}: {path}")
    offenders = over_budget(paths)
    if offenders:
        print(
            f"{len(offenders)} path(s) exceed the {RELATIVE_PATH_BUDGET}-character budget: an update "
            "over this payload can fail to move them out of the way and abandon the update.",
            file=sys.stderr,
        )
        for path in offenders[: args.show]:
            print(f"  {len(path)}: {path}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
