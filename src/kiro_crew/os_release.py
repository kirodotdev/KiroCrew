"""Which Linux distribution this is, and which rpm-family package manager it has.

Two reads every per-distribution hint in the product shares, so no two surfaces
can disagree about the host: the lowercased ``ID``/``ID_LIKE`` tokens of the
freedesktop os-release file (:func:`os_release_ids`) and the supported
rpm-family manager (:func:`rpm_package_manager`). Nothing here runs a package
manager or elevates; the manager probe is a ``PATH`` stat.

The os-release read blocks, so a caller on the event loop offloads it.
"""

from __future__ import annotations

import logging
import platform
import shutil

logger = logging.getLogger(__name__)

#: The SUSE side of the rpm family. rpm-packaged, but their manager is ``zypper``
#: and their package names differ from the Fedora/RHEL set, so no Fedora-shaped
#: command works there even when ``dnf`` or ``yum`` is installed from SUSE's own
#: repos. Lineage decides, not which binaries the host carries.
_SUSE_IDS = frozenset({"opensuse", "sles", "suse"})


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


def os_release_ids() -> frozenset[str]:
    """Lowercased ``ID`` and ``ID_LIKE`` tokens identifying this distribution.

    An absent or unreadable file yields an empty set: the conservative answer,
    for which no package manager is assumed. ``ID`` is single-valued;
    ``ID_LIKE`` is a space-separated list naming the bases a derivative inherits
    from. Both are scanned so a derivative that names itself in ``ID`` still
    resolves through its base.
    """
    release = _os_release()
    ids: set[str] = set()
    for key in ("ID", "ID_LIKE"):
        ids.update(token for token in release.get(key, "").lower().split() if token)
    return frozenset(ids)


def rpm_package_manager() -> str | None:
    """The supported rpm-family manager for this host, or ``None``.

    Lineage first, then the probe. A SUSE host (:data:`_SUSE_IDS` in its
    os-release tokens) is answered ``None`` outright: a command completed from a
    Fedora-shaped package list would fail on its package names instead of its
    first argument, and that stays true even when ``dnf`` or ``yum`` is
    installed there. Everyone else in the family is probed in a deterministic
    order: ``dnf`` (Fedora, RHEL/CentOS 8+, Amazon Linux 2023), then ``yum``
    (Amazon Linux 2, 7-era RHEL/CentOS), then ``microdnf`` (minimal RHEL/UBI
    images). All three take the same package names and the same ``install -y``
    syntax; only the manager token differs.
    """
    if os_release_ids() & _SUSE_IDS:
        return None
    for manager in ("dnf", "yum", "microdnf"):
        if shutil.which(manager):
            return manager
    return None
