#!/usr/bin/env python3
"""Share the architecture-independent half of a universal macOS build.

A universal KiroCrew.app ships two complete backend trees
(``backend-dist/kirocrew-backend-arm64/`` and ``-x64/``) and two KAS engine
roots (``backend-dist/kas-arm64/`` and ``-x64/``), because neither a PBS Python
tree nor a Node binary can be lipo-merged. Most of what those pairs hold is the
same bytes twice: every pure-Python package, the dashboard's ``static/dist``,
the stdlib's ``.py`` modules and their checked-hash ``.pyc`` caches, and the
KAS engine's own JavaScript (``darwin-universal`` is ONE engine tarball; only
the Node binary beside it is per-arch).

That duplication is what pushed the universal update zip past 1 GiB, and the
Squirrel.Mac inside every already-installed app reads the whole zip into one
``NSData`` whose capacity doubles -- past 1 GiB it asks for 2 GiB, which
Electron's PartitionAlloc refuses by design, and the app dies mid-update
(docs/build/desktop-app.md, "the update zip must stay under 1 GiB"). So
the duplicate half is shared: the arm64
tree stays a complete, untouched tree, and in the x64 tree each entry whose
bytes are identical is replaced by a RELATIVE symlink into the arm64 tree.

Granularity is one ENTRY of a container directory -- a whole top-level package
directory, a top-level module file, a ``*.dist-info`` directory -- never a file
inside a package. A package is shared only when every file in it is identical,
so ``Path(__file__).resolve()`` inside a shared package lands in a directory
that holds everything the package expects beside itself; a package carrying one
arch-specific ``.so`` stays whole in both trees. ``bin/`` and ``lib-dynload/``
are never touched: ``sys.executable``, ``sysconfig`` paths and every compiled
extension stay real files in the tree that runs them.

Symlinks are the one sharing primitive the whole pipeline already carries:
``codesign`` seals them as links, ``@electron/universal`` resolves them when it
merges the two interim apps, the signing manifest skips them (it signs the real
Mach-O once, under the arm64 tree), ``ditto -c -k`` keeps them in the update zip
and the DMG, and ``Electron Framework.framework/Versions/Current`` rides every
one of those steps today. Hard links do not survive the zip, which is why they
are not used.

Usage (from packaging/build-desktop.sh, universal builds only)::

    dedupe_universal_backend.py --backend KEEP LINK [--kas KEEP LINK]

``KEEP`` is the tree that keeps its real files (arm64); ``LINK`` is the tree
whose identical entries become symlinks (x64). Exit status is non-zero on a
pair that should be identical and is not (a KAS pair recording the same engine
sha256 whose trees differ), never on a backend entry that merely differs.
"""

from __future__ import annotations

import argparse
import filecmp
import json
import os
import shutil
import stat
import sys
from pathlib import Path

# Containers whose ENTRIES are candidates, relative to a backend tree. The
# stdlib directory is listed before its site-packages so the latter is seen as
# an entry of the former and skipped there (it is a container of its own).
BACKEND_CONTAINERS = (
    "lib/python3.12",
    "lib/python3.12/site-packages",
)

# Entries of the stdlib container that are never shared: the per-arch compiled
# extensions, the per-arch build configuration and its data module, and the
# site-packages container handled on its own.
STDLIB_EXCLUDED = frozenset({"lib-dynload", "site-packages"})
STDLIB_EXCLUDED_PREFIXES = ("config-", "_sysconfigdata")

KAS_VERSION_FILE = "BUNDLED-VERSION"
KAS_INSTALL_RECORD = "install.json"
KAS_SHARED_SUBDIR = "bundle"


def _same_file(a: Path, b: Path) -> bool:
    """Byte-identical regular files with the same permission bits."""
    sa, sb = a.lstat(), b.lstat()
    if sa.st_size != sb.st_size or stat.S_IMODE(sa.st_mode) != stat.S_IMODE(sb.st_mode):
        return False
    return filecmp.cmp(a, b, shallow=False)


def trees_identical(a: Path, b: Path) -> bool:
    """Whether *a* and *b* are the same entry: same shape, same bytes.

    Symlinks must point at the same target text; files must be byte-identical
    with the same mode; directories must hold the same names, each identical.
    Nothing is followed, so a tree is judged by what the zip will carry.
    """
    if a.is_symlink() or b.is_symlink():
        return a.is_symlink() and b.is_symlink() and os.readlink(a) == os.readlink(b)
    if a.is_file() and b.is_file():
        return _same_file(a, b)
    if a.is_dir() and b.is_dir():
        names_a, names_b = sorted(os.listdir(a)), sorted(os.listdir(b))
        if names_a != names_b:
            return False
        return all(trees_identical(a / n, b / n) for n in names_a)
    return False


def tree_bytes(path: Path) -> int:
    """Bytes of regular files under *path*, symlinks not followed."""
    if path.is_symlink():
        return 0
    if path.is_file():
        return path.lstat().st_size
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            p = Path(root) / name
            if not p.is_symlink():
                total += p.lstat().st_size
    return total


def replace_with_symlink(keep: Path, link: Path) -> None:
    """Remove *link* and plant a relative symlink to *keep* in its place."""
    if link.is_dir() and not link.is_symlink():
        shutil.rmtree(link)
    else:
        link.unlink()
    link.symlink_to(os.path.relpath(keep, link.parent))


def _stdlib_excluded(name: str) -> bool:
    return name in STDLIB_EXCLUDED or name.startswith(STDLIB_EXCLUDED_PREFIXES)


def dedupe_backend(keep: Path, link: Path, *, out=sys.stdout) -> int:
    """Share every identical container entry of *link* into *keep*.

    Returns the bytes removed from *link*.
    """
    saved = 0
    for container in BACKEND_CONTAINERS:
        keep_dir, link_dir = keep / container, link / container
        if not (keep_dir.is_dir() and link_dir.is_dir()):
            print(f"    {container}: not present in both trees; skipped", file=out)
            continue
        shared = kept = 0
        for name in sorted(os.listdir(link_dir)):
            if container == BACKEND_CONTAINERS[0] and _stdlib_excluded(name):
                kept += 1
                continue
            keep_entry, link_entry = keep_dir / name, link_dir / name
            if link_entry.is_symlink() or not keep_entry.exists():
                kept += 1
                continue
            if not trees_identical(keep_entry, link_entry):
                kept += 1
                continue
            saved += tree_bytes(link_entry)
            replace_with_symlink(keep_entry, link_entry)
            shared += 1
        print(f"    {container}: {shared} entries shared, {kept} kept per-arch", file=out)
    return saved


def dedupe_kas(keep: Path, link: Path, *, out=sys.stdout) -> int:
    """Point *link*'s engine ``bundle/`` at *keep*'s when both hold one engine.

    The two roots record the engine tarball they were installed from
    (``install.json`` ``engine_sha256``). Equal records and identical trees
    share; unequal records keep both (two different engines were staged, which
    is a pin problem, not this script's); equal records with differing trees
    are an error, because something rewrote one copy after extraction.
    """
    version = (link / KAS_VERSION_FILE).read_text(encoding="utf-8").strip()
    keep_bundle = keep / version / KAS_SHARED_SUBDIR
    link_bundle = link / version / KAS_SHARED_SUBDIR
    rec_keep = json.loads((keep / version / KAS_INSTALL_RECORD).read_text(encoding="utf-8"))
    rec_link = json.loads((link / version / KAS_INSTALL_RECORD).read_text(encoding="utf-8"))
    if rec_keep.get("engine_sha256") != rec_link.get("engine_sha256"):
        print(
            f"    kas: {keep.name} and {link.name} record different engines; both kept",
            file=out,
        )
        return 0
    if not (keep_bundle.is_dir() and link_bundle.is_dir() and not link_bundle.is_symlink()):
        raise SystemExit(f"ERROR: {keep_bundle} / {link_bundle}: expected two real bundle dirs")
    if not trees_identical(keep_bundle, link_bundle):
        raise SystemExit(
            f"ERROR: {link_bundle} differs from {keep_bundle} although both record "
            f"engine {rec_keep.get('engine_sha256')}; refusing to share a rewritten tree"
        )
    saved = tree_bytes(link_bundle)
    replace_with_symlink(keep_bundle, link_bundle)
    print(f"    kas: {link.name}/{version}/{KAS_SHARED_SUBDIR} -> {keep.name} (shared)", file=out)
    return saved


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument(
        "--backend",
        nargs=2,
        metavar=("KEEP", "LINK"),
        required=True,
        help="backend trees: the one that keeps real files, the one that gets symlinks",
    )
    parser.add_argument(
        "--kas",
        nargs=2,
        metavar=("KEEP", "LINK"),
        help="KAS engine roots, same order",
    )
    args = parser.parse_args(argv)
    saved = dedupe_backend(Path(args.backend[0]), Path(args.backend[1]))
    if args.kas:
        saved += dedupe_kas(Path(args.kas[0]), Path(args.kas[1]))
    print(f"    shared {saved / 1e6:.0f} MB of duplicate payload (uncompressed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
