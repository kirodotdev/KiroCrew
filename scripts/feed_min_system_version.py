#!/usr/bin/env python3
"""Print the ``minimumSystemVersion`` the macOS update feed declares for an app ZIP.

electron-updater declines an update whose feed entry names a ``minimumSystemVersion``
above the running OS (``AppUpdater.isUpdateSupported``: ``semver.lt(os.release(),
minimumSystemVersion)``), but electron-builder never writes that field, and our feed
is written by hand in ``sign-and-notarize.yml``. Without it, a client on a macOS the
new build cannot run is offered the update, Squirrel replaces the bundle, and the
relaunch is refused: the user is left with an app that will not open.

The floor is read from the artifact itself, so an Electron major that moves the real
floor moves the declared one with it: the ``LSMinimumSystemVersion`` that Electron
stamps into the app's ``Contents/Info.plist`` from its own deployment target.

The two fields use different version schemes. ``LSMinimumSystemVersion`` is the
marketing version (``13.0``); the feed field is compared with ``os.release()``, the
Darwin kernel version (macOS 13 is Darwin ``22.0.0``). For macOS 11 to 15, Darwin's
major is the marketing major plus nine; Apple then renumbered the marketing version
by year, so macOS 26 is Darwin 25 and later majors are the marketing major minus one.
Majors 16 to 25 were never released and are refused. A ``.0`` release is Darwin
``N.0.0``; macOS 10.x is Darwin ``x + 4``. A marketing minor above ``.0`` has no fixed kernel
mapping (13.3 is Darwin 22.4), so it is refused rather than rounded down to a floor
that would let a too-old OS update.

Usage: feed_min_system_version.py <app.zip>   -> prints e.g. ``22.0.0``

Exits non-zero with a reason when the ZIP holds no app, no plist, or no usable
``LSMinimumSystemVersion``: the feed step fails closed rather than publishing an
entry with no floor.
"""

from __future__ import annotations

import plistlib
import re
import sys
import zipfile

_MARKETING_RE = re.compile(r"\A(\d+)\.(\d+)(?:\.(\d+))?\Z")


class FeedFloorError(ValueError):
    """The artifact does not name a floor this script can translate."""


def darwin_version(marketing: str) -> str:
    """The Darwin kernel version (``os.release()``) of a macOS marketing version."""
    match = _MARKETING_RE.match(marketing.strip())
    if not match:
        raise FeedFloorError(f"LSMinimumSystemVersion {marketing!r} is not a macOS version")
    major, minor, patch = int(match.group(1)), int(match.group(2)), int(match.group(3) or 0)
    if major == 10:
        if patch:
            raise FeedFloorError(f"no fixed Darwin version for macOS {marketing}")
        return f"{minor + 4}.0.0"
    if major >= 11:
        if minor or patch:
            raise FeedFloorError(
                f"no fixed Darwin version for macOS {marketing}; only a .0 floor maps exactly"
            )
        if major <= 15:
            return f"{major + 9}.0.0"
        if major >= 26:
            return f"{major - 1}.0.0"
        raise FeedFloorError(f"macOS {marketing} was never released; refusing to guess")
    raise FeedFloorError(f"LSMinimumSystemVersion {marketing!r} predates macOS 10")


def app_minimum_system_version(zip_path: str) -> str:
    """``LSMinimumSystemVersion`` of the one top-level ``.app`` in *zip_path*."""
    with zipfile.ZipFile(zip_path) as archive:
        plists = [
            name
            for name in archive.namelist()
            if re.fullmatch(r"[^/]+\.app/Contents/Info\.plist", name)
        ]
        if len(plists) != 1:
            raise FeedFloorError(
                f"expected one top-level .app/Contents/Info.plist in {zip_path}, found {plists}"
            )
        info = plistlib.loads(archive.read(plists[0]))
    value = info.get("LSMinimumSystemVersion") if isinstance(info, dict) else None
    if not isinstance(value, str) or not value:
        raise FeedFloorError(f"{plists[0]} has no LSMinimumSystemVersion")
    return value


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: feed_min_system_version.py <app.zip>", file=sys.stderr)
        return 2
    try:
        print(darwin_version(app_minimum_system_version(argv[1])))
    except (FeedFloorError, OSError, zipfile.BadZipFile, plistlib.InvalidFileException) as err:
        print(f"feed_min_system_version: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
