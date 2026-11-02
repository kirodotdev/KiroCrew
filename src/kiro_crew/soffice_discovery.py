"""Where LibreOffice's ``soffice`` lives on this host, and how to install it if absent.

The one implementation in the repository. The file panel's slide previews
(``dashboard/handlers/office_slides.py``) render a deck through ``soffice``, a system
package the product neither ships nor installs from a browser request, so finding it
is a core capability that holds with no app installed. An app that renders office
documents (PPTX Maker) imports these helpers rather than carrying its own copy.

Discovery order: ``PATH`` first, so the user's own install wins; then the fixed
Windows install roots, because the LibreOffice installer does not extend ``PATH``
and a by-name lookup cannot reach it there. Nothing else. A hit is a CANDIDATE: the
caller still gates it through its own provenance check before executing it.

Everything here is blocking (a ``PATH`` walk, a filesystem probe, an os-release read)
and belongs off the event loop.
"""

from __future__ import annotations

import os
import shutil
import sys
from functools import lru_cache

from kiro_crew import platform_compat
from kiro_crew.browser_cli import os_deps

SOFFICE = "soffice"

#: Fixed install roots of the LibreOffice Windows installer, which writes ``soffice``
#: into its own ``program`` directory and adds nothing to ``PATH``.
#:
#: Literal roots rather than ``%ProgramFiles%``, for the reason
#: ``platform_compat._WINDOWS_GIT_DIRS`` records: ``HKCU\Environment`` is writable
#: without elevation, so resolving through an environment variable would let a
#: poisoned value redirect this lookup into a directory the user can write. An
#: install on another drive misses and is reported missing, which is honest.
#:
#: Naming these roots is NOT by itself what keeps the search out of writable
#: directories: :func:`which_within` accepts a hit only when its parent is the root it
#: asked about, and that check is what makes this tuple the reachable set.
_SOFFICE_WINDOWS_DIRS: tuple[str, ...] = (
    r"C:\Program Files\LibreOffice\program",
    r"C:\Program Files (x86)\LibreOffice\program",
)

#: Per-OS install instruction, shown verbatim; the user runs it themselves, which is
#: the contract for a system package. macOS and Windows each have one conventional
#: manager; Linux has several, so :func:`_linux_hint` decides it per host.
_HINTS = {
    "darwin": "brew install --cask libreoffice",
    "win32": "winget install TheDocumentFoundation.LibreOffice",
}

#: Linux hints keyed by the distribution's os-release ``ID``/``ID_LIKE`` tokens.
_LINUX_APT_HINT = "sudo apt install libreoffice"
_LINUX_RPM_HINT = "sudo {manager} install libreoffice"
_APT_IDS = frozenset({"debian", "ubuntu"})
#: The Fedora/RHEL lineage, whose manager is ``dnf``, ``yum`` or ``microdnf`` by
#: generation and is probed per host. SUSE is absent on purpose: its manager is
#: ``zypper``.
_RPM_IDS = frozenset({"fedora", "rhel", "centos"})
#: Distributions with no LibreOffice package in any enabled repo. Amazon Linux 2023
#: reports ``ID_LIKE=fedora``, yet ``dnf install libreoffice`` finds nothing there, so
#: it is checked before the rpm family can claim it.
_UNPACKAGED_IDS = frozenset({"amzn"})


def which_within(name: str, directory: str | os.PathLike[str]) -> str | None:
    """*name* resolved INSIDE *directory* as an absolute path, or ``None``.

    ``shutil.which(path=…)`` does not bound where it looks. On Windows it inserts the
    process's working directory AHEAD of the given path whenever
    ``_winapi.NeedCurrentDirectoryForExePath`` says to, and that is the default —
    having ``NoDefaultCurrentDirectoryInExePath`` set is the exception, not the rule.
    Measured on CPython 3.12.14, with ``soffice.com`` planted in the working directory
    and ``path=`` an empty directory: with the variable unset the call returns
    ``.\\soffice.COM``, and with it set the same call returns ``None``. So a probe of a
    trusted directory silently answers with whatever the process happens to be standing
    next to, and the answer is RELATIVE — parent ``.`` — which then follows the process.

    Every caller feeds the result to something that EXECUTES it, directly or by putting
    its parent on a ``PATH``. A hit is therefore accepted only when its containing
    directory really is the one that was asked about. The LEAF is not link-resolved, so
    a launcher that is a symlink to a real binary elsewhere still counts; only its
    parent has to match.
    """
    found = shutil.which(name, path=str(directory))
    if not found:
        return None
    parent = os.path.realpath(os.path.dirname(os.path.abspath(found)))
    expected = os.path.realpath(str(directory))
    if os.path.normcase(parent) != os.path.normcase(expected):
        return None
    return os.path.abspath(found)


def windows_install_path() -> str | None:
    """``soffice`` at a fixed Windows install root, or ``None`` (always off Windows).

    Goes through :func:`which_within`, so the Windows executable suffixes still come
    from ``PATHEXT`` while the reachable set stays the fixed roots rather than "the
    fixed roots, plus wherever the process is standing".
    """
    if not platform_compat.IS_WINDOWS:
        return None
    for directory in _SOFFICE_WINDOWS_DIRS:
        found = which_within(SOFFICE, directory)
        if found:
            return found
    return None


def find_soffice() -> str | None:
    """A candidate ``soffice`` path on this host, or ``None`` when there is none."""
    return shutil.which(SOFFICE) or windows_install_path()


@lru_cache(maxsize=1)
def _linux_hint() -> str | None:
    """Cached: the distribution does not change under a running process.

    Names only what this host has: the apt family by lineage, the rpm family by the
    manager :func:`os_deps.rpm_package_manager` finds (the probe the browser installer
    uses). Any other distribution, SUSE included, gets ``None``.
    """
    ids = os_deps.os_release_ids()
    if ids & _UNPACKAGED_IDS:
        return None
    if ids & _APT_IDS:
        return _LINUX_APT_HINT
    if ids & _RPM_IDS:
        manager = os_deps.rpm_package_manager()
        return _LINUX_RPM_HINT.format(manager=manager) if manager else None
    return None


def soffice_install_hint() -> str | None:
    """The install command for LibreOffice on this host, or ``None``.

    Data for a UI to display, never executed: running a package manager on the
    operator's behalf from a browser request is a host mutation the product refuses.
    ``None`` means there is no command to give (an unpackaged or unrecognized
    distribution, or no rpm manager on ``PATH``): every surface frames the hint as
    "install it with:", so a guessed command that fails on its own first argument is
    worse than none.
    """
    if sys.platform.startswith("linux"):
        return _linux_hint()
    return _HINTS.get(sys.platform, "install LibreOffice for your platform")
