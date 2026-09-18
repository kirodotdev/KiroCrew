"""Which Linux hosts ``install-browser --with-deps`` can actually serve.

Playwright's OS-dependency installer is **apt-only**. On a distribution it does
not recognize it does not decline -- it picks its nearest Ubuntu package set and
runs ``apt-get`` anyway, as root. On an rpm host that is wrong twice over: the
package names do not exist, and the command needs a privilege the operator of a
managed workstation usually does not have. The observed shape on Amazon Linux
2023 is a sudo policy refusal quoting a 60-package ``apt-get`` line the user
never typed, and because the flag and the browser download are one CLI
invocation, that refusal takes the download down with it.

So the flag is offered only where it means something, and everywhere else the
operator is handed the one command that does work on their distribution. The
package list is the remedy for a failure they must fix with root; nothing here
elevates, and nothing here runs a package manager.

The read blocks (the os-release file), so a caller on the event loop offloads
them -- the same contract as the rest of this package.

This module also owns the check that the downloaded browser can actually RESOLVE
its libraries (:func:`missing_shared_libraries`), because Playwright's own host
validation cannot be relied on to say so -- on linux-arm64 it scans a directory
that does not exist and concludes the host is fine. See that function.
"""

from __future__ import annotations

import logging
import os
import platform
import struct
from collections.abc import Iterable, Iterator
from functools import lru_cache
from pathlib import Path

from kiro_crew import platform_compat

logger = logging.getLogger(__name__)

#: Family names this module reports. Not free-form strings: callers branch on
#: them, so they are named here and nowhere else.
FAMILY_DEBIAN = "debian"
FAMILY_RPM = "rpm"
FAMILY_UNKNOWN = "unknown"

#: ``ID``/``ID_LIKE`` tokens that mean apt. Matched against both fields because a
#: derivative (Linux Mint, Pop!_OS, elementary) names itself in ``ID`` and its
#: base only in ``ID_LIKE``.
_DEBIAN_IDS = frozenset({"debian", "ubuntu"})

#: ``ID``/``ID_LIKE`` tokens that mean dnf/yum. ``amzn`` reports
#: ``ID_LIKE=fedora``, so the ``ID_LIKE`` scan covers Amazon Linux without
#: naming it, but it is listed anyway: Amazon Linux 2 omits ``ID_LIKE``.
_RPM_IDS = frozenset(
    {
        "rhel",
        "fedora",
        "centos",
        "amzn",
        "rocky",
        "almalinux",
        "ol",
        "opensuse",
        "sles",
        "suse",
    }
)

#: Chromium's shared-library dependencies as rpm package names.
#:
#: Chromium alone, not all three engines: it is the engine ``attach`` supports
#: and the one ``browser_ok`` gates on, so it is what "browsing works" means. A
#: list covering Firefox and WebKit too would be longer, would ask for more root,
#: and would still not be what the blocked operator needs first.
#:
#: These are NOT a translation of Playwright's Debian list. rpm splits and names
#: the same libraries differently (``mesa-libgbm`` for ``libgbm1``, ``cups-libs``
#: for ``libcups2``), so a mechanically mapped list fails on the first package
#: and teaches the operator that the remedy is broken.
_RPM_CHROMIUM_PACKAGES: tuple[str, ...] = (
    "alsa-lib",
    "at-spi2-atk",
    "at-spi2-core",
    "atk",
    "cairo",
    "cups-libs",
    "dbus-libs",
    "expat",
    "fontconfig",
    "freetype",
    "gdk-pixbuf2",
    "glib2",
    "gtk3",
    "lcms2",
    "libX11",
    "libXcomposite",
    "libXcursor",
    "libXdamage",
    "libXext",
    "libXfixes",
    "libXi",
    "libXrandr",
    "libXrender",
    "libdrm",
    "libjpeg-turbo",
    "libpng",
    "libwebp",
    "libxcb",
    "libxkbcommon",
    "libxml2",
    "libxslt",
    "mesa-libgbm",
    "nspr",
    "nss",
    "pango",
)


#: Remedy for the apt family. Deliberately not a package list: Playwright installs
#: its own, correct, per-version set there, and a copy here would go stale against
#: the CLI the user actually has.
_APT_DEPS_COMMAND = "sudo npx playwright install-deps chromium"

#: Remedy for the rpm family, completed with :data:`_RPM_CHROMIUM_PACKAGES`.
_DNF_DEPS_COMMAND_PREFIX = "sudo dnf install -y "

#: What a blocked operator is told. One sentence of cause, then the command, so the
#: actionable part is last and survives being appended after a truncated stderr.
_MISSING_DEPS_HINT = (
    "The browser needs OS libraries that only root can install. "
    "Run this yourself, then retry the install:\n{command}"
)


def _os_release_ids() -> set[str]:
    """Lowercased ``ID`` and ``ID_LIKE`` tokens identifying this distribution.

    Read through :func:`platform.freedesktop_os_release` rather than opening
    ``/etc/os-release`` directly. The stdlib consults BOTH locations the
    freedesktop specification defines -- a minimal or immutable image may ship
    only ``/usr/lib/os-release`` -- and it applies the spec's shell-style
    unquoting, so a hand-rolled parser here would be a less correct copy of it.

    An absent or unreadable file yields an empty set, which reports as
    :data:`FAMILY_UNKNOWN` -- the conservative answer, since that is the family
    for which no package manager is assumed.
    """
    try:
        release = platform.freedesktop_os_release()
    except OSError:
        logger.debug("no freedesktop os-release on this host", exc_info=True)
        return set()
    # ``ID`` is single-valued; ``ID_LIKE`` is a space-separated list naming the
    # bases a derivative inherits from. Both are scanned so a derivative that
    # names itself in ``ID`` still resolves through its base.
    ids: set[str] = set()
    for key in ("ID", "ID_LIKE"):
        ids.update(token for token in release.get(key, "").lower().split() if token)
    return ids


@lru_cache(maxsize=1)
def linux_family() -> str:
    """Package-manager family of this host.

    Cached because a distribution does not change under a running process, and
    every browser install attempt asks twice (once for the flag, once for the
    remedy). Tests that fake the os-release data call
    ``linux_family.cache_clear()``.
    """
    if not platform_compat.IS_LINUX:
        return FAMILY_UNKNOWN
    ids = _os_release_ids()
    # Debian first: a Debian derivative never claims an rpm ID, but checking rpm
    # first would let a host listing both resolve to the manager it lacks.
    if ids & _DEBIAN_IDS:
        return FAMILY_DEBIAN
    if ids & _RPM_IDS:
        return FAMILY_RPM
    return FAMILY_UNKNOWN


def with_deps_supported() -> bool:
    """Whether ``--with-deps`` can install this host's OS packages.

    True only on the apt family. Elsewhere the flag does not decline, it
    mis-fires -- see the module docstring -- and takes the browser download with
    it, so it is not passed at all.
    """
    return linux_family() == FAMILY_DEBIAN


def manual_deps_command() -> str | None:
    """The command the operator can run with root to install the OS libraries.

    ``None`` off Linux, where the browser download alone is sufficient and there
    is nothing to install. On an unknown Linux the return is also ``None``: a
    guessed package manager is worse than silence, because a command that fails
    on its own first argument reads as the product being broken rather than as
    the host being unrecognized.
    """
    family = linux_family()
    if family == FAMILY_DEBIAN:
        return _APT_DEPS_COMMAND
    if family == FAMILY_RPM:
        return _DNF_DEPS_COMMAND_PREFIX + " ".join(_RPM_CHROMIUM_PACKAGES)
    return None


def missing_deps_hint() -> str:
    """One line for a failed browser step, or ``""`` when there is nothing to add.

    Appended to a step's failure detail rather than raised as its own state: the
    settings panel already shows that detail verbatim, so this turns an opaque
    package-manager refusal into the command that resolves it without adding a
    surface to the UI or a string to the translation catalogs.
    """
    command = manual_deps_command()
    if command is None:
        return ""
    return _MISSING_DEPS_HINT.format(command=command)


#: How Playwright announces that the browser it just downloaded cannot run.
#:
#: MEASURED on Amazon Linux 2023: with libraries missing, ``install-browser``
#: prints this block and **exits 0**. Playwright classifies it as a warning, so
#: the exit code alone reports a browser that cannot launch as installed --
#: the panel goes green, and the real error arrives at the user's first browse
#: as an opaque stack trace instead. The output is therefore the only signal.
#:
#: Two markers rather than one: the header and the message body are emitted by
#: different call sites, so a reworded box still trips the other. Matched
#: case-insensitively on a substring, never parsed -- the box is decoration.
_HOST_VALIDATION_MARKERS = (
    "host validation warning",
    "missing dependencies to run browsers",
)


def host_deps_unsatisfied(text: str) -> bool:
    """Whether *text* carries Playwright's missing-library host validation.

    Read the exit code AND this, never the exit code alone -- see
    :data:`_HOST_VALIDATION_MARKERS` for the measurement.

    NOT sufficient on its own either: Playwright's validation produces FALSE
    NEGATIVES on linux-arm64, so a host with libraries missing can emit no
    marker at all. :func:`missing_shared_libraries` is the check that does not
    depend on Playwright noticing.
    """
    lowered = (text or "").lower()
    return any(marker in lowered for marker in _HOST_VALIDATION_MARKERS)


#: ELF constants for reading a build's declared dependencies. Only what is used.
_ELF_MAGIC = b"\x7fELF"
_PT_LOAD = 1
_PT_DYNAMIC = 2
_DT_NULL = 0
_DT_NEEDED = 1
_DT_STRTAB = 5

#: Ceilings applied to every count read out of the file being parsed. The files
#: come from a writable cache, so each header field is hostile input until it has
#: been checked: nothing is allocated or seeked from an unvalidated number.
_MAX_PHNUM = 4096
_MAX_DYN_ENTRIES = 100_000
_MAX_SONAME_LEN = 4096

#: Ceiling on entries VISITED while walking one cache directory. Bounds the walk
#: itself rather than any field inside a file: the directory is writable, so its
#: breadth is hostile input too, and a link or a deep tree planted there would
#: otherwise drive an uncapped recursive scan during a privileged install.
_MAX_WALK_ENTRIES = 20_000


# Bounds the NUMBER of dependency names collected, in one file and in aggregate.
# Each name comes out of a dynamic table in a writable cache, so the count is as
# attacker-shaped as the walk's breadth was. MEASURED: chromium's build declares
# 32 unique sonames across 9 candidates and firefox 50 across 24, so a real build
# is two orders of magnitude below this. Truncating can only shorten a report --
# a name never added cannot invent a missing library.
_MAX_SONAMES = 4_000


def _needed_sonames(path: Path) -> set[str]:
    """Sonames *path* declares as dependencies, read WITHOUT executing it.

    ``ldd`` cannot be used here. glibc's ``ldd`` is documented to execute the
    binary it inspects, and a crafted ``PT_INTERP`` makes that arbitrary code:
    the interpreter travels inside the ELF, so neither an absolute argv, nor a
    scrubbed environment, nor an unexported search path prevents it. These files
    come from a writable cache whose subdirectories are matched by name prefix,
    so they must be treated as data, never as something to run.

    So the dynamic table is read directly: ``PT_DYNAMIC`` gives the ``DT_NEEDED``
    string-table offsets and ``DT_STRTAB`` gives the table's virtual address,
    which is mapped back to a file offset through the ``PT_LOAD`` segment that
    contains it.

    An empty set means "declares nothing we can read" -- not a parseable ELF, a
    static binary with no dynamic section, or a field that failed a bound. Never
    raises: a file this cannot parse must not be able to fail an install.
    """
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            ident = fh.read(16)
            if len(ident) < 16 or ident[:4] != _ELF_MAGIC:
                return set()
            if ident[4] not in (1, 2) or ident[5] not in (1, 2):
                return set()
            is64 = ident[4] == 2
            endian = "<" if ident[5] == 1 else ">"
            ph_entry = 56 if is64 else 32

            fh.seek(32 if is64 else 28)
            raw = fh.read(8 if is64 else 4)
            if len(raw) < (8 if is64 else 4):
                return set()
            phoff = struct.unpack(endian + ("Q" if is64 else "I"), raw)[0]
            fh.seek(54 if is64 else 42)
            raw = fh.read(4)
            if len(raw) < 4:
                return set()
            phentsize, phnum = struct.unpack(endian + "HH", raw)

            if phnum > _MAX_PHNUM or phentsize < ph_entry:
                return set()
            if phoff <= 0 or phoff + phnum * phentsize > size:
                return set()

            loads: list[tuple[int, int, int]] = []
            dynamic: tuple[int, int] | None = None
            for index in range(phnum):
                fh.seek(phoff + index * phentsize)
                hdr = fh.read(ph_entry)
                if len(hdr) < ph_entry:
                    return set()
                if is64:
                    p_type, _flags, p_off, p_vaddr, _paddr, p_filesz = struct.unpack(
                        endian + "IIQQQQ", hdr[:40]
                    )
                else:
                    p_type, p_off, p_vaddr, _paddr, p_filesz = struct.unpack(
                        endian + "IIIII", hdr[:20]
                    )
                if p_type == _PT_LOAD:
                    loads.append((p_vaddr, p_filesz, p_off))
                elif p_type == _PT_DYNAMIC:
                    dynamic = (p_off, p_filesz)

            if dynamic is None:
                return set()
            dyn_off, dyn_size = dynamic
            if dyn_off + dyn_size > size:
                return set()

            step = 16 if is64 else 8
            entry_fmt = endian + ("Qq" if is64 else "Ii")
            count = min(dyn_size // step, _MAX_DYN_ENTRIES)
            fh.seek(dyn_off)
            table = fh.read(count * step)
            offsets: list[int] = []
            strtab_vaddr: int | None = None
            for index in range(len(table) // step):
                tag, value = struct.unpack_from(entry_fmt, table, index * step)
                if tag == _DT_NULL:
                    break
                if tag == _DT_NEEDED:
                    offsets.append(value)
                elif tag == _DT_STRTAB:
                    strtab_vaddr = value
            if strtab_vaddr is None or not offsets:
                return set()

            strtab_off: int | None = None
            for vaddr, filesz, file_off in loads:
                if vaddr <= strtab_vaddr < vaddr + filesz:
                    strtab_off = file_off + (strtab_vaddr - vaddr)
                    break
            if strtab_off is None or strtab_off >= size:
                return set()

            names: set[str] = set()
            for relative in offsets:
                if len(names) >= _MAX_SONAMES:
                    # Bounded inside the file too, not only in aggregate: one file's
                    # dynamic table is as attacker-shaped as a thousand of them, so
                    # the cap has to hold before the set leaves this function.
                    break
                if relative < 0:
                    continue
                start = strtab_off + relative
                if start >= size:
                    continue
                fh.seek(start)
                raw = fh.read(_MAX_SONAME_LEN)
                end = raw.find(b"\x00")
                if end <= 0:
                    continue
                names.add(raw[:end].decode("ascii", errors="replace"))
            return names
    except (OSError, struct.error, ValueError):
        return set()


def _host_provided_sonames() -> set[str]:
    """File names of shared objects this host provides, by directory listing.

    Answers "can the host satisfy this soname" without asking the loader, which is
    the whole point: asking the loader means running something.

    Only the loader's own configured directories are listed --
    :func:`platform_compat.system_library_dirs`. ``LD_LIBRARY_PATH`` is NOT read,
    even though the loader honours it, because a directory named by process
    environment is an environment-controlled path and listing one from a
    privileged install is the security class this repository fences. The cost is a
    host that supplies a library that way being named in the report; since the
    report is advisory (see ``install._verify_browser_libraries``), that costs a
    spurious muted line rather than a blocked install.

    Matched by NAME alone. Architecture is deliberately not compared: a multiarch
    host can carry one soname for several architectures, so a name match can in
    principle be satisfied by the wrong one -- but that costs a report this check
    would otherwise have made, and the report is advisory, so the cost is a missing
    muted line. Comparing architectures means reading an ELF header for every
    library the host provides: MEASURED at 962 extra file reads and 6x the runtime
    on an arm64 host, to move the provided set from 970 names to 962 and change no
    verdict.

    Listing names rather than resolving them is the deliberate bias. A name that is
    present but somehow unloadable reads as satisfied, so the check errs towards NOT
    reporting: a false positive would block an install that works, which is worse
    than the failure being fixed.
    """
    names: set[str] = set()
    for candidate in platform_compat.system_library_dirs():
        try:
            if not candidate.is_dir():
                continue
            for child in candidate.iterdir():
                if ".so" in child.name:
                    names.add(child.name)
        except OSError:
            continue
    return names


def _sonames_shipped_with_build(directories: Iterable[Path], budget: list[int]) -> set[str]:
    """File names of shared objects the build carries itself.

    The ONLY guard against reporting a bundled library as host-missing, and it is
    enough because it does not depend on the loader: a library present in the tree
    is the build's own, so naming it "missing on this host" is wrong regardless of
    whether ``ldd`` managed to resolve it. That independence is what allowed the
    ``LD_LIBRARY_PATH`` export to be removed -- it resolved exactly the libraries
    this set already names, since both are built from the same ``rglob("*.so*")``
    walk, and exporting it into a shell-script ``ldd`` was a code-execution path.

    It also absorbs the loader quirks the search path never handled: ``$ORIGIN``
    handling, or a nested layout the search path missed.

    Bounded by :func:`_bounded_walk` like every other walk of a cache directory.
    Every walk must share the ceiling: one uncapped walk over the same tree makes
    the others' bound worth nothing, since the unbounded work happens either way.
    """
    names: set[str] = set()
    for directory in directories:
        for path in _bounded_walk(directory, budget, ".so"):
            try:
                if path.is_file():
                    names.add(path.name)
            except OSError:
                continue
    return names


def _bounded_walk(
    directory: Path, budget: list[int], suffix_match: str | None = None
) -> Iterator[Path]:
    """Entries under *directory*, stopping when the walk budget runs out.

    *budget* is a ONE-ELEMENT list holding the work still allowed, shared by every
    walk AND every parse in one probe, and it is REQUIRED. That sharing is the
    point: a per-call ceiling is multiplied by however many directories the caller
    passes, and the caller's list comes from a prefix match over a writable cache
    (``install._engine_cache_dirs``), so its length is attacker-shaped too. A
    default would silently hand the next caller the per-call ceiling this replaced,
    so there is none.

    Always walks ``"*"`` and filters AFTER charging the budget. ``rglob`` with a
    narrower pattern still descends the entire tree but yields only MATCHES, so
    charging for yields charges for the wrong thing: a cache of a million
    non-matching files is traversed in full while the counter barely moves. The
    bound has to cover the work, not the results.

    The generator is also consumed LAZILY: wrapping ``rglob`` in ``sorted()``
    materialises and sorts everything before a ceiling check inside the loop can
    ever fire, which likewise enforces a bound only after doing the unbounded work.

    ``rglob`` yields top-down, so a truncated walk keeps the SHALLOW entries. That
    is what makes truncating safe for the probe: a browser's main executable sits
    at the top of its build directory, so it is seen before the satellites, and a
    cut tail cannot produce a "nothing missing" answer for a build whose main
    binary was never read.

    MEASURED for scale: chromium ships 14 ELF candidates and firefox 44, against a
    ceiling of :data:`_MAX_WALK_ENTRIES`. It bounds a hostile tree, not a supported
    one.
    """
    left = budget
    try:
        for path in directory.rglob("*"):
            if left[0] <= 0:
                logger.warning("walk of %s stopped: entry budget spent", directory)
                return
            left[0] -= 1
            if suffix_match is not None and suffix_match not in path.name:
                continue
            yield path
    except OSError:
        return


def _elf_candidates(directory: Path, budget: list[int]) -> list[Path]:
    """Executables and shared objects under *directory*, most-important first.

    Bounded by :func:`_bounded_walk`, and symlinks are never descended into.

    A symlink swapped in at the top of the walk can redirect it: this reads a path,
    and a path is re-resolved on every use. The probe does not try to win that
    race, because its output is ADVISORY (see
    ``install._verify_browser_libraries``) -- the worst a won race yields is a
    misleading warning, and nothing is executed or written either way.

    Deliberately NOT pinned with :func:`platform_compat.pin_directory`: that
    primitive's contract requires POSIX callers to use the returned descriptor for
    their own opens, and a walk that goes by path string gets nothing from holding
    it. A pin here would read as protection without being any, which is worse than
    no pin at all, because it is believed.

    The order puts an executable before a ``.so`` and a shallow path before a deep
    one, so the main program is probed first.
    """
    found: list[Path] = []
    for path in _bounded_walk(directory, budget):
        try:
            if path.is_symlink() or not path.is_file():
                continue
            is_executable = os.access(path, os.X_OK)
            is_shared_object = ".so" in path.name
            if is_executable or is_shared_object:
                found.append(path)
        except OSError:
            continue
    found.sort(key=lambda p: (not os.access(p, os.X_OK), len(p.parts), str(p)))
    return found


def missing_shared_libraries(directories: Iterable[Path]) -> set[str] | None:
    """Sonames the downloaded browser needs that this host cannot resolve.

    Exists because Playwright's own host validation CANNOT be trusted to notice.
    MEASURED on Amazon Linux 2023 arm64: its registry declares the directory to
    scan as ``chrome-linux``, while the arm64 build unpacks into
    ``chrome-linux-arm64``, so it scans a path that does not exist, finds no
    dependencies at all, concludes the host is fine and writes its
    ``DEPENDENCIES_VALIDATED`` marker -- which then suppresses re-validation for
    30 days. The marker was written 23 minutes BEFORE the libraries were
    installed on that host. A false negative, not a missing warning: the
    download reports success, ``browser_ok`` reports true, the panel goes green,
    and the failure only arrives at the user's first browse as an opaque stack
    trace.

    So this reads the real files, and never hardcodes a build's subdirectory
    name -- that assumption is the upstream bug being worked around.

    It reads them as DATA. The dependency list comes from each ELF's own dynamic
    table (:func:`_needed_sonames`) and the answer from a listing of the host's
    library directories (:func:`_host_provided_sonames`); nothing is executed.
    ``ldd`` cannot be used, because glibc's ``ldd`` runs the binary it inspects
    and a crafted ``PT_INTERP`` turns that into arbitrary code -- and these files
    live in a writable cache whose subdirectories are matched by name prefix, so a
    planted ELF is reachable. The interpreter travels inside the file, so no
    absolute argv, scrubbed environment or withheld search path prevents it.

    Only libraries the HOST must provide are reported; the build's own bundled
    ones are excluded by name (see :func:`_sonames_shipped_with_build`), because
    reporting those would block an install that works.

    ``None`` means "cannot determine", NOT "nothing missing": off Linux, when no
    directory exists, or when nothing readable declared a dependency at all.
    Callers must not turn an unknown into a failed install -- an absent probe is
    not evidence of a broken browser.
    """
    if not platform_compat.IS_LINUX:
        return None
    dirs = [d for d in directories]
    # ONE budget for the whole probe. A per-directory ceiling is multiplied by the
    # number of directories, and `dirs` comes from a prefix match over a writable
    # cache (`install._engine_cache_dirs`), so its length is attacker-shaped as
    # surely as each tree's depth is.
    budget = [_MAX_WALK_ENTRIES]
    candidates: list[Path] = []
    for directory in dirs:
        try:
            if directory.is_dir():
                candidates.extend(_elf_candidates(directory, budget))
        except OSError:
            continue
    if not candidates:
        return None
    shipped = _sonames_shipped_with_build(dirs, budget)
    declared: set[str] = set()
    for path in candidates:
        if budget[0] <= 0:
            # PARSING is charged to the same budget as walking. Reading a file's
            # headers, program headers and string table is work, and a cache of
            # files that each declare NOTHING never trips the name cap while still
            # being parsed one by one. One budget covers every quantity this probe
            # takes from the cache.
            logger.warning("ELF parsing for %s stopped: work budget spent", candidates[0])
            break
        budget[0] -= 1
        declared |= _needed_sonames(path)
        if len(declared) >= _MAX_SONAMES:
            # The COUNT of names is bounded, not only each name's length and the
            # number of files walked. Every dependency name comes out of a dynamic
            # table in the writable cache, so a build with vast tables -- or many
            # files each with a large one -- grows this set without a cap here.
            # Truncating loses reports, never invents them: a name that would have
            # been added can only have made the answer longer.
            logger.warning("dependency names for %s truncated at %d", candidates[0], _MAX_SONAMES)
            break
    if not declared:
        # Nothing readable declared a dependency. That is "cannot determine": a
        # browser build always needs host libraries, so an empty answer here means
        # the files could not be parsed, not that the host is complete.
        return None
    provided = _host_provided_sonames()
    return {name for name in declared if name not in provided and name not in shipped}
