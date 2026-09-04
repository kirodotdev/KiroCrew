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
"""

from __future__ import annotations

import logging
import platform
from functools import lru_cache

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
    """
    lowered = (text or "").lower()
    return any(marker in lowered for marker in _HOST_VALIDATION_MARKERS)
