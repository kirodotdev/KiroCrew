"""Scans the gateway's interpreter seal uses: which conda install a prefix belongs
to, the count of sealed files that have a second hard-linked name, how much of a
non-venv prefix is sealed, the interpreter's own files, the unused ``site-packages``
left writable inside a seal, and which spelling of each start-up path entry is sealed.

Split out of :mod:`kiro_crew.sandbox_tiers`, which joins their results into the seal.
"""

from __future__ import annotations

import logging
import os
import stat
import sys

logger = logging.getLogger("kiro_crew.sandbox")


def _conda_root(prefix: str) -> str:
    """The conda install *prefix* is (a base) or sits in (``<root>/envs/<name>``), or ``""``.

    A conda install, and each of its environments, is marked by a ``conda-meta``
    directory; pyenv and venvs are not. The owning install wins over the env itself.
    """
    parent = os.path.dirname(prefix)
    for root in (os.path.dirname(parent) if os.path.basename(parent) == "envs" else "", prefix):
        if root and os.path.isdir(os.path.join(root, "conda-meta")):
            return root
    return ""


#: How many files :func:`_hard_linked_sealed_files` looks at, at most.
_LINK_CHECK_FILE_CAP = 50_000


def _hard_linked_sealed_files(
    targets: tuple[str, ...] | list[str], cap: int = _LINK_CHECK_FILE_CAP
) -> tuple[int, str, bool]:
    """``(count, example, truncated)``: regular files under the sealed *targets*
    with more than one name (``st_nlink > 1``), links not followed, after at most
    *cap* files. A second name outside the sealed directories (a uv or conda cache)
    writes the same inode, which a path seal does not cover."""
    tops: list[str] = []
    for top in sorted(dict.fromkeys(os.path.realpath(t) for t in targets)):
        if not any(os.path.commonpath((top, t)) == t for t in tops):
            tops.append(top)
    count, example, seen = 0, "", 0
    for top in tops:
        # A file target (the interpreter binary) is its own one-entry walk.
        walk = (
            [(os.path.dirname(top), [], [os.path.basename(top)])]
            if os.path.isfile(top)
            else os.walk(top)
        )
        for parent, _dirs, files in walk:
            for name in files:
                seen += 1
                if seen > cap:
                    return count, example, True
                path = os.path.join(parent, name)
                try:
                    st = os.lstat(path)
                except OSError:
                    continue
                if stat.S_ISREG(st.st_mode) and st.st_nlink > 1:
                    count += 1
                    example = example or path
    return count, example, False


def prefix_seal_entries(prefix: str) -> list[str]:
    """What to seal of one non-conda interpreter *prefix* as a whole.

    A venv (``pyvenv.cfg``) is the gateway's own environment: all of it. Any other
    prefix (a pyenv, mise, uv or Homebrew Python, the base of a venv) is not sealed
    as a tree: what its interpreter loads at start is sealed piece by piece, its
    stdlib and ``site`` dirs through the start-up search path and its binary and
    ``libpython`` through :func:`interpreter_files`, so its unused ``site-packages``,
    ``bin``, ``include``, ``share`` and other environments stay writable. A macOS
    framework build is no different: its stdlib is ``lib/pythonX.Y`` under the same
    prefix, and :func:`interpreter_files` adds its launcher and framework binary.
    """
    if os.path.isfile(os.path.join(prefix, "pyvenv.cfg")):
        return [prefix]
    return []


def interpreter_files(executables: tuple[str, ...], prefixes: tuple[str, ...]) -> list[str]:
    """The regular files an interpreter start loads outside its search path: each of
    *executables* resolved through its links, and each prefix's ``libpython``. On a
    macOS framework build ``bin/python3`` is a stub that runs the
    ``Resources/Python.app`` launcher, which loads the framework binary ``Python``.

    Only files this uid can write or owns: a root-owned ``/usr/bin/python3`` needs
    no seal. The links on the way are rename-guarded by the plan host.
    """
    import glob

    found: list[str] = []
    mapped = _mapped_libpython()
    if mapped is None:  # no /proc (macOS): every libpython the prefixes hold
        version = "%d.%d" % sys.version_info[:2]
        libs = [
            path
            for prefix in prefixes
            for pattern in (f"libpython{version}*", "libpython3.dylib")
            for path in glob.glob(os.path.join(glob.escape(prefix), "lib", pattern))
        ] + [
            os.path.join(prefix, *parts)
            for prefix in prefixes
            for parts in (("Python",), ("Resources", "Python.app", "Contents", "MacOS", "Python"))
        ]
    else:  # only the one this process loaded; a static build (uv, mise) loads none
        libs = mapped
    for path in (*executables, *libs):
        real = os.path.realpath(path) if path else ""
        if not real or not os.path.isfile(real):
            continue
        try:
            owned = os.stat(real).st_uid == getattr(os, "getuid", lambda: -1)()
        except OSError:
            continue
        if os.access(real, os.W_OK) or owned:
            found.append(real)
    return list(dict.fromkeys(found))


def gateway_console_script() -> str:
    """The console script the gateway started through when it is Crew's own (a pipx
    ``~/.local/bin/kirocrew`` link, for one), else empty."""
    script = sys.argv[0] if sys.argv else ""
    return script if os.path.basename(script).startswith("kirocrew") else ""


def _mapped_libpython() -> list[str] | None:
    """The ``libpython`` files mapped into this process, or ``None`` without ``/proc``."""
    try:
        with open("/proc/self/maps", encoding="utf-8", errors="replace") as fh:
            lines = fh.read(1 << 22).splitlines()
    except OSError:
        return None
    paths = (line.split(None, 5)[-1] for line in lines if line.count(" ") >= 5)
    return list(dict.fromkeys(p for p in paths if os.path.basename(p).startswith("libpython")))


def unused_site_holes(
    prefixes: tuple[str, ...], in_use: tuple[str, ...], targets: tuple[str, ...]
) -> list[str]:
    """The ``site-packages`` dirs of *prefixes* the interpreter does not use, to leave
    writable inside a seal: a venv without system site-packages never reads its
    base's, so a ``.pth`` or ``sitecustomize.py`` there does not run in the gateway.

    *in_use* is every ``site`` dir and start-up path entry. A dir is a hole only when a
    *target* strictly holds it (the stdlib dir does) and it holds no target itself.
    """
    import site

    used = {os.path.realpath(p) for p in in_use if p}
    found: list[str] = []
    for raw in site.getsitepackages(list(dict.fromkeys(prefixes))):
        for spelling in dict.fromkeys((os.path.normpath(raw), os.path.realpath(raw))):
            if os.path.realpath(spelling) in used or not os.path.isdir(spelling):
                continue
            inside = any(_strictly_within(spelling, t) for t in targets)
            if inside and not any(_within(t, spelling) for t in targets):
                found.append(spelling)
    return list(dict.fromkeys(found))


def _within(path: str, root: str) -> bool:
    try:
        return os.path.commonpath((path, root)) == root
    except ValueError:
        return False


def _strictly_within(path: str, root: str) -> bool:
    return path != root and _within(path, root)


def _sealable_spellings(path: str, homes: tuple[str, ...]) -> list[str]:
    """The lexical and canonical spellings of *path* worth sealing: an existing
    directory this uid can write or owns (an owner can ``chmod`` a read-only one
    back) that neither is nor holds a home. A path no OS call accepts (a NUL
    byte, as a ``.condarc`` or ``.pth`` line may carry) is dropped on its own."""
    if not path or "\0" in path:
        return []
    lexical = os.path.normpath(os.path.abspath(path))
    found: list[str] = []
    for spelling in dict.fromkeys((lexical, os.path.realpath(lexical))):
        # shortcut: checks the dir itself, not each file below it; a dir that is
        # root-owned but has a user-writable subtree stays unsealed.
        holds_home = any(os.path.commonpath((spelling, h)) == spelling for h in homes)
        if os.path.isdir(spelling) and not holds_home and _writable_or_owned(spelling):
            found.append(spelling)
    return found


def _search_path_spellings(
    entry: str, homes: tuple[str, ...], sealed: tuple[str, ...] | list[str] = ()
) -> list[str]:
    """:func:`_sealable_spellings` for one search-path *entry* (a ``sys.path``,
    ``site`` or ``.pth`` directory), which may not exist yet.

    A missing entry is created (``mkdir -p``, as the gateway user; a ``.zip`` one as
    an empty file) and sealed, so a child cannot create it first; its parents stay
    as they are. An existing zip or egg file on the path is sealed as a file.
    Nothing is created when the nearest existing parent already lies in a
    *sealed* directory or this uid cannot write it (then no child can either).
    A failed mkdir logs one WARNING and leaves the entry unsealed.
    """
    if not entry or "\0" in entry:
        return []
    lexical = os.path.normpath(os.path.abspath(entry))
    found: list[str] = []
    # The canonical spelling first: once it exists, a lexical one through a
    # dangling link resolves too.
    spellings = tuple(dict.fromkeys((os.path.realpath(lexical), lexical)))
    # A user tree by either spelling leaves the whole entry writable: sealing the
    # alias would seal the tree it resolves to.
    for broad in (s for s in spellings if _broad_user_dir(s, homes)):
        logger.warning("sandbox: gateway Python path %s is left writable (user tree)", broad)
        return []
    for spelling in spellings:
        if not os.path.lexists(spelling):
            parent = _nearest_existing(spelling)
            if _is_within(parent, sealed) or not _writable_or_owned(parent):
                continue
            try:
                if spelling.endswith(".zip"):  # zipimport reads an empty file as no archive
                    os.makedirs(os.path.dirname(spelling), exist_ok=True)
                    open(spelling, "xb").close()
                else:
                    os.makedirs(spelling, exist_ok=True)
            except FileExistsError:
                pass  # created meanwhile (a concurrent seal pass): checked and sealed below
            except OSError as exc:
                logger.warning(
                    "sandbox: could not create missing gateway Python path %s (%s); "
                    "it is not sealed",
                    spelling,
                    exc,
                )
                continue
        if _never_sealed(spelling, homes):
            continue
        if os.path.isfile(spelling):  # a zip or egg on the path is read like a dir
            if _writable_or_owned(spelling):
                found.append(spelling)
            continue
        found.extend(_sealable_spellings(spelling, homes))
    return list(dict.fromkeys(found))


def _broad_user_dir(path: str, homes: tuple[str, ...]) -> bool:
    """*path* is a home's direct child (``~/projects``) or holds the crew workspace."""
    if any(os.path.dirname(path) == h for h in homes):
        return True
    try:
        from kiro_crew.config.loader_locations import workspace_root

        root = os.path.realpath(str(workspace_root(create=False)))
        return os.path.commonpath((path, root)) == path
    except Exception:  # an unresolvable workspace adds no exclusion
        return False


def _is_within(path: str, roots: tuple[str, ...] | list[str]) -> bool:
    """*path* is one of *roots* or below one."""
    try:
        return any(os.path.commonpath((path, r)) == r for r in roots)
    except ValueError:  # different drives
        return False


def _never_sealed(path: str, homes: tuple[str, ...]) -> bool:
    """*path* is a home, ``/``, the gateway's cwd or crew data home (where the
    workspaces live), an ancestor of one, or world-writable or sticky (``/tmp``).

    shortcut: a workspace outside the crew data home and cwd is not known here.
    """
    from kiro_crew.config.paths import config_dir

    keep = (*homes, os.getcwd(), str(config_dir()))
    try:
        # Both spellings on both sides: an alias of an excluded dir is excluded too.
        keep = (*keep, *(os.path.realpath(k) for k in keep))
        for cand in dict.fromkeys((path, os.path.realpath(path))):
            if any(os.path.commonpath((cand, k)) == cand for k in keep):
                return True
        return bool(os.stat(path).st_mode & (stat.S_IWOTH | stat.S_ISVTX))
    except (OSError, ValueError):
        return True


def _nearest_existing(path: str) -> str:
    """*path*, or its nearest ancestor that exists (``/`` at worst)."""
    while not os.path.lexists(path):
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    return path


def _writable_or_owned(path: str) -> bool:
    """This uid can write *path*, or owns it and so can make it writable."""
    if os.access(path, os.W_OK):
        return True
    getuid = getattr(os, "getuid", None)
    try:
        return getuid is not None and os.stat(path).st_uid == getuid()
    except OSError:
        return False


def _pth_dirs(site_dirs: tuple[str, ...]) -> list[str]:
    """The directories the ``.pth`` files in *site_dirs* add to ``sys.path``, as
    :func:`site.addpackage` reads them: one path per line, relative to the site
    directory; ``#`` comments and ``import`` lines add none."""
    found: list[str] = []
    for sitedir in site_dirs:
        try:
            # site skips a dot-prefixed .pth file, so it adds no directory.
            names = sorted(
                n for n in os.listdir(sitedir) if n.endswith(".pth") and not n.startswith(".")
            )
        except OSError:
            continue
        for name in names:
            try:
                with open(os.path.join(sitedir, name), encoding="utf-8", errors="replace") as fh:
                    lines = fh.read(1 << 20).splitlines()
            except OSError:
                continue
            for line in lines:
                line = line.rstrip()
                if line and not line.startswith(("#", "import ", "import\t")):
                    found.append(os.path.join(sitedir, line))
    return found
