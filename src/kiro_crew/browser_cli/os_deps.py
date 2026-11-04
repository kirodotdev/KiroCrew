"""Which Linux hosts ``install-browser --with-deps`` can serve, and the remedy for the rest.

Playwright's OS-dependency installer is **apt-only**. On a distribution it does
not recognize it does not decline -- it picks its nearest Ubuntu package set and
runs ``apt-get`` anyway, as root. On an rpm host that is wrong twice over: the
package names do not exist, and the command needs a privilege the operator of a
managed workstation usually does not have. The observed shape on Amazon Linux
2023 is a sudo policy refusal quoting a 60-package ``apt-get`` line the user
never typed, and because the flag and the browser download are one CLI
invocation, that refusal takes the download down with it.

So the flag is offered only where it means something (:func:`with_deps_supported`),
and everywhere else the operator is handed the one command that does work on
their distribution. The package list is the remedy for a failure they must fix
with root; nothing here elevates, and nothing here runs a package manager. Every
remedy names the engine it was composed for, because a Chromium package list
does not make Firefox or WebKit launch.

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
import shutil
import stat
import struct
from collections import deque
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

#: The SUSE side of the rpm family. rpm-packaged, so they belong in
#: :data:`_RPM_IDS`, but their manager is ``zypper`` and their Chromium
#: dependency packages are named differently from the Fedora/RHEL set
#: (``libgbm1`` for ``mesa-libgbm``) -- so no command this module could compose
#: from :data:`_RPM_CHROMIUM_PACKAGES` works there, even when ``dnf`` or
#: ``yum`` happens to be installed from SUSE's own repos. Lineage decides,
#: not which binaries the host carries: see :func:`rpm_package_manager`.
_SUSE_IDS = frozenset({"opensuse", "sles", "suse"})

#: ``ID``/``ID_LIKE`` tokens that mean rpm packaging. Family membership says how
#: packages are NAMED, not which manager installs them: Fedora-lineage hosts
#: carry ``dnf`` or ``yum``, while the SUSE tokens mean ``zypper`` -- so the
#: manager is probed per host (see :func:`rpm_package_manager`), never assumed
#: from the family. ``amzn`` reports ``ID_LIKE=fedora``, so the ``ID_LIKE`` scan
#: covers Amazon Linux without naming it, but it is listed anyway: Amazon Linux 2
#: omits ``ID_LIKE``.
_RPM_IDS = (
    frozenset(
        {
            "rhel",
            "fedora",
            "centos",
            "amzn",
            "rocky",
            "almalinux",
            "ol",
        }
    )
    | _SUSE_IDS
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
#: the CLI the user actually has. ``install-deps`` takes the engine name, so the
#: command is completed per engine and never claims Chromium's set fixes Firefox
#: or WebKit. Privilege elevation is composed separately by :func:`_sudo_prefix`
#: so minimal root containers do not need a ``sudo`` binary.
_APT_DEPS_COMMAND = "npx playwright install-deps {engine}"

#: Remedy prefix for the rpm family, completed with :data:`_RPM_CHROMIUM_PACKAGES`.
#: ``{manager}`` is filled with whichever supported manager this host actually
#: has -- see :func:`rpm_package_manager` -- because half the rpm family does
#: not carry ``dnf``, and a hardcoded manager fails on its own first argument
#: exactly the way the module docstring warns about. Privilege elevation is
#: composed separately by :func:`_sudo_prefix`.
_RPM_DEPS_COMMAND_PREFIX = "{manager} install -y "

#: What a blocked operator is told. One sentence of cause, then the command, so the
#: actionable part is last and survives being appended after a truncated stderr.
_MISSING_DEPS_HINT = (
    "The {title} browser needs OS libraries that only root can install. "
    "Run this yourself, then retry the download:\n{command}"
)

#: What an rpm-family operator is told for an engine this module has no verified
#: package list for. It names the engine and points at the library names
#: Playwright printed above it, rather than offering Chromium's list as a fix.
_UNLISTED_ENGINE_HINT = (
    "The {title} browser needs OS libraries that only root can install. Kiro Crew "
    "has no verified package list for {title} on this distribution: install the "
    "libraries Playwright names above with your package manager, then retry the "
    "download."
)

#: What an operator is told for an engine measured not to run on this
#: distribution (:func:`engine_unsupported_here`): the engine cannot run on this
#: host, and no command composed here or by the operator changes that. It names
#: the engine that failed and the one this host does support, and like every hint
#: it is the LAST line of the failure detail -- the position that survives
#: truncation.
_UNSUPPORTED_ENGINE_HINT = (
    "{title} is not supported on this operating system: Kiro Crew has no way to "
    "install the OS libraries it needs on this Linux distribution. Chromium is "
    "the supported browser engine on this host."
)

#: Engines whose rpm package set is :data:`_RPM_CHROMIUM_PACKAGES`.
_RPM_LISTED_ENGINES = frozenset({"chromium"})

#: Display names for the hint text. The engine argument is already allowlisted
#: by the caller; an unlisted value falls back to itself.
_ENGINE_TITLES = {"chromium": "Chromium", "firefox": "Firefox", "webkit": "WebKit"}


def _sudo_prefix() -> str:
    """Use ``sudo`` only when the host actually provides it.

    A host without ``sudo`` is either already root, where no prefix is needed,
    or unprivileged, where no command this module can compose can elevate it. In
    the latter case a bare command's permission error is the truthful remedy;
    failing first on a nonexistent ``sudo`` token is not.
    """
    return "sudo " if shutil.which("sudo") else ""


def _os_release() -> dict[str, str]:
    """This host's freedesktop os-release fields, or ``{}`` when there are none.

    Read through :func:`platform.freedesktop_os_release` rather than opening
    ``/etc/os-release`` directly. The stdlib consults BOTH locations the
    freedesktop specification defines -- a minimal or immutable image may ship
    only ``/usr/lib/os-release`` -- and it applies the spec's shell-style
    unquoting, so a hand-rolled parser here would be a less correct copy of it.
    """
    try:
        return platform.freedesktop_os_release()
    except OSError:
        logger.debug("no freedesktop os-release on this host", exc_info=True)
        return {}


def _os_release_ids() -> set[str]:
    """Lowercased ``ID`` and ``ID_LIKE`` tokens identifying this distribution.

    An absent or unreadable file yields an empty set, which reports as
    :data:`FAMILY_UNKNOWN` -- the conservative answer, since that is the family
    for which no package manager is assumed.
    """
    release = _os_release()
    # ``ID`` is single-valued; ``ID_LIKE`` is a space-separated list naming the
    # bases a derivative inherits from. Both are scanned so a derivative that
    # names itself in ``ID`` still resolves through its base.
    ids: set[str] = set()
    for key in ("ID", "ID_LIKE"):
        ids.update(token for token in release.get(key, "").lower().split() if token)
    return ids


def os_release_ids() -> frozenset[str]:
    """Public read of :func:`_os_release_ids`, for other per-distribution hints."""
    return frozenset(_os_release_ids())


def _os_release_id() -> str:
    """The lowercased ``ID`` alone: the distribution itself, not its lineage.

    What :func:`engine_unsupported_here` compares against. ``ID_LIKE`` is left
    out on purpose, in both directions: a base's measurement does not transfer
    to a derivative that merely names it, and a derivative's does not transfer
    to its base.
    """
    return _os_release().get("ID", "").strip().lower()


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


def rpm_package_manager() -> str | None:
    """The supported rpm-family manager for this host, or ``None``.

    Public because two surfaces name this host's manager to the operator:
    :func:`manual_deps_command` here and the PPTX Maker LibreOffice hint. Both ask
    this one probe, so they cannot disagree about which manager the host has.

    Lineage first, then the probe. A SUSE host (:data:`_SUSE_IDS` in its
    os-release tokens) is answered ``None`` outright: its packages are named
    differently, so a command completed from this module's list would fail on
    its package list instead of its first argument -- the same defect the probe
    exists to remove, wearing a different hat -- and that stays true even when
    ``dnf`` or ``yum`` is installed there from SUSE's own repos. The caller
    falls back to the silence the :func:`manual_deps_command` docstring
    prescribes.

    Everyone else in the family is probed in a deterministic order: ``dnf``
    (the current generation -- Fedora, RHEL/CentOS 8+, Amazon Linux 2023), then
    ``yum`` (the older one -- Amazon Linux 2, 7-era RHEL/CentOS), then
    ``microdnf`` (minimal RHEL/UBI images, which ship it exclusively). All
    three take the same rpm package names and the same ``install -y`` syntax,
    so :data:`_RPM_CHROMIUM_PACKAGES` completes any of them; only the manager
    token differs. The probe is a ``PATH`` stat, not a spawn -- this module
    still never runs a package manager.
    """
    if _os_release_ids() & _SUSE_IDS:
        return None
    for manager in ("dnf", "yum", "microdnf"):
        if shutil.which(manager):
            return manager
    return None


def manual_deps_command(engine: str = "chromium") -> str | None:
    """The command the operator can run with root to install *engine*'s OS libraries.

    ``None`` off Linux, where the browser download alone is sufficient and there
    is nothing to install. On an unknown Linux the return is also ``None``: a
    guessed package manager is worse than silence, because a command that fails
    on its own first argument reads as the product being broken rather than as
    the host being unrecognized. The same rule governs the rpm family's SUSE
    side (whose packages this module's list cannot name), any rpm host with no
    supported manager on ``PATH``, and every engine other than Chromium on the
    rpm family (:data:`_RPM_LISTED_ENGINES`): no verified command can be
    composed, so none is -- see :func:`rpm_package_manager`.
    """
    family = linux_family()
    if family == FAMILY_DEBIAN:
        return _sudo_prefix() + _APT_DEPS_COMMAND.format(engine=engine)
    if family == FAMILY_RPM:
        if engine not in _RPM_LISTED_ENGINES:
            return None
        manager = rpm_package_manager()
        if manager is None:
            return None
        return (
            _sudo_prefix()
            + _RPM_DEPS_COMMAND_PREFIX.format(manager=manager)
            + " ".join(_RPM_CHROMIUM_PACKAGES)
        )
    return None


def engine_unsupported_here(engine: str) -> bool:
    """Whether *engine* is measured not to run on this distribution.

    One measured pair: WebKit on Amazon Linux (os-release ``ID`` ``amzn``).
    MEASURED on Amazon Linux 2023: ``install-browser webkit`` exits 0 and names
    27 missing libraries (``libgtk-4.so.1``, ``libflite*``, ``libmanette-0.2``,
    ``libhyphen``, ``libavif``, ``libx264``, ICU 74 sonames where the host ships
    ICU 67), most with no package in the distribution's repositories, and
    Playwright's own dependency installer is apt-only. ``install-browser
    firefox`` on the same host passes host validation, so Firefox is not here:
    it stays an unlisted engine whose libraries, when one is missing, the
    operator can install by name.

    Compared on ``ID`` alone (:func:`_os_release_id`), never on ``ID_LIKE`` or
    the family: a measurement is of one distribution's repositories, not of a
    lineage. ``amzn`` reports ``ID_LIKE=fedora``, and Fedora packages gtk4, ICU,
    libavif, flite, libmanette and hyphen, so an operator there can follow the
    manual instruction (:data:`_UNLISTED_ENGINE_HINT`), which is what every
    other rpm distribution keeps. A second measured pair would be a second
    comparison, with its capture.

    The install layer reads this to attach the verdict only to a failure that
    names the missing libraries: the verdict is a claim about the host, and that
    report is its only evidence, so a download that failed for another reason
    keeps its own error instead.
    """
    if not platform_compat.IS_LINUX:
        return False
    return engine == "webkit" and _os_release_id() == "amzn"


def missing_deps_hint(engine: str = "chromium") -> str:
    """One line for a failed *engine* download, or ``""`` when there is nothing to add.

    Appended to a step's failure detail rather than raised as its own state: the
    settings panel already shows that detail verbatim, so this turns an opaque
    host-validation failure into the command that resolves it without adding a
    surface to the UI or a string to the translation catalogs.

    A distribution measured not to run *engine* at all
    (:func:`engine_unsupported_here`) gets the verdict that the engine is
    unsupported on this operating system (:data:`_UNSUPPORTED_ENGINE_HINT`); no
    instruction could be followed there. Any other rpm-family host downloading
    an engine with no verified package list gets an engine-named manual
    instruction instead of a command (:data:`_UNLISTED_ENGINE_HINT`). An
    unrecognized host still gets nothing.
    """
    title = _ENGINE_TITLES.get(engine, engine)
    if engine_unsupported_here(engine):
        return _UNSUPPORTED_ENGINE_HINT.format(title=title)
    command = manual_deps_command(engine)
    if command is not None:
        return _MISSING_DEPS_HINT.format(title=title, command=command)
    if linux_family() == FAMILY_RPM and engine not in _RPM_LISTED_ENGINES:
        return _UNLISTED_ENGINE_HINT.format(title=title)
    return ""


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
        # Opened non-blocking and checked on the descriptor itself: the cache is
        # writable, so a candidate checked as a regular file can be swapped for
        # a FIFO before the open, and a blocking open of a FIFO waits for a
        # writer forever, holding the install slot. `O_NOFOLLOW` refuses a link
        # swapped in at the name.
        fd = platform_compat.open_file_no_reparse(path, nonblocking=True)
        try:
            info = os.fstat(fd)
        except OSError:
            os.close(fd)
            raise
        if not stat.S_ISREG(info.st_mode):
            os.close(fd)
            return set()
        size = info.st_size
        with os.fdopen(fd, "rb") as fh:
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
            # Each distinct offset is read once, and READS are capped, not only
            # names: a table of 100,000 entries pointing at one string keeps the
            # name set at one while costing 100,000 seeks per file.
            for reads, relative in enumerate(dict.fromkeys(offsets)):
                if reads >= _MAX_SONAMES or len(names) >= _MAX_SONAMES:
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
    reporting: a false positive tells the operator to install a library they
    already have, which costs more trust than the advisory earns.
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
    this set already names, since both are built from the same ``.so`` walk, and
    exporting it into a shell-script ``ldd`` was a code-execution path.

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
    (:func:`engine_build_dirs`), so its length is attacker-shaped too. A
    default would silently hand the next caller the per-call ceiling this replaced,
    so there is none.

    Charges EVERY entry read and filters AFTER charging. A glob with a narrower
    pattern still descends the entire tree but yields only MATCHES, so charging for
    yields charges for the wrong thing: a cache of a million non-matching files is
    traversed in full while the counter barely moves. The bound has to cover the
    work, not the results.

    The walk is LAZY down to the directory read: ``os.scandir`` yields entries as
    it reads them, where ``Path.rglob`` lists each directory in full first and
    ``sorted()`` would materialise the whole tree. Either would enforce the bound
    only after doing the unbounded work.

    The walk is breadth-first, so a truncated walk keeps the SHALLOW entries. A
    browser's main executable sits at the top of its build directory, so it is
    seen before the satellites. If the budget still runs out,
    :func:`missing_shared_libraries` answers "cannot determine".

    MEASURED for scale: chromium ships 14 ELF candidates and firefox 44, against a
    ceiling of :data:`_MAX_WALK_ENTRIES`. It bounds a hostile tree, not a supported
    one.
    """
    # `os.scandir` reads a directory incrementally, so each entry is charged as it
    # is read. `Path.rglob` lists a whole directory before yielding its first
    # entry, which would let one wide directory be read in full past the ceiling.
    # Breadth-first: every entry of a directory is yielded before anything below
    # it, which keeps the shallow entries on truncation. Every queued directory
    # was charged when it was read, so the queue is bounded by the budget too.
    pending: deque[Path] = deque([directory])
    while pending:
        current = pending.popleft()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    if budget[0] <= 0:
                        logger.warning("walk of %s stopped: entry budget spent", directory)
                        # Negative marks "stopped with entries left unread", which
                        # a walk that ended exactly at the ceiling is not.
                        budget[0] = -1
                        return
                    budget[0] -= 1
                    path = Path(entry.path)
                    try:
                        # Never descends into a symlink: see `_elf_candidates`.
                        if entry.is_dir(follow_symlinks=False):
                            pending.append(path)
                    except OSError:
                        pass
                    if suffix_match is not None and suffix_match not in entry.name:
                        continue
                    yield path
        except OSError:
            continue


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
    reporting those would send the operator to install a library the build
    already carries.

    ``None`` means "cannot determine", NOT "nothing missing": off Linux, when no
    directory exists, or when nothing readable declared a dependency at all.
    Callers must not turn an unknown into a failed install -- an absent probe is
    not evidence of a broken browser.
    """
    if not platform_compat.IS_LINUX:
        return None
    return _missing_in(list(directories), [_MAX_WALK_ENTRIES])


def engine_build_dirs(cache: Path, engine: str, budget: list[int]) -> Iterator[Path]:
    """Build directories for *engine* in the browser *cache*, read lazily.

    Prefix-matched rather than composed from a revision, so the CHROMIUM entry
    also picks up ``chromium_headless_shell-<rev>``. That is not a bonus: headless
    is the default launch mode, so the headless shell is the binary a browse
    actually starts, and a check that looked only at ``chromium-<rev>`` would
    clear the build that is not the one being run.

    A symlinked child is REFUSED, not followed: the yielded path anchors a
    recursive walk, and this cache is writable, so a prefix-named link would aim
    that walk at a tree of the planter's choosing. Playwright's own downloads are
    real directories.

    Every entry of the cache root is charged to *budget* as it is read, matching
    or not, so a cache padded with prefix-named directories (or anything else)
    spends the same bound as the walks below it.
    """
    try:
        with os.scandir(cache) as entries:
            for entry in entries:
                if budget[0] <= 0:
                    logger.warning("scan of %s stopped: entry budget spent", cache)
                    budget[0] = -1
                    return
                budget[0] -= 1
                try:
                    if entry.name.startswith(engine) and entry.is_dir(follow_symlinks=False):
                        yield Path(entry.path)
                except OSError:
                    continue
    except OSError:
        return


def missing_engine_libraries(cache: Path | None, engine: str) -> set[str] | None:
    """:func:`missing_shared_libraries` for every *engine* build in *cache*.

    ONE budget covers the cache-root scan, every walk and every parse: a
    per-directory ceiling is multiplied by the number of directories, and the
    directories come from a prefix match over a writable cache, so their count is
    attacker-shaped as surely as each tree's depth is.
    """
    if cache is None or not platform_compat.IS_LINUX:
        return None
    budget = [_MAX_WALK_ENTRIES]
    return _missing_in(list(engine_build_dirs(cache, engine, budget)), budget)


def _missing_in(dirs: list[Path], budget: list[int]) -> set[str] | None:
    """The probe itself, over *dirs*, charging everything to *budget*."""
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
            budget[0] = -1
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
    if budget[0] < 0:
        # A walk or the parse loop stopped early, so the build's main binary may
        # never have been read: shallow planted decoys sort ahead of it. A partial
        # look must not read as "nothing missing".
        return None
    provided = _host_provided_sonames()
    return {name for name in declared if name not in provided and name not in shipped}
