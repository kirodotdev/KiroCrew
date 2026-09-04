"""Install-shape detection -> login transport selection.

The one rule that makes login work across deployments: pick the transport that can
receive the result. A loopback callback only works when the browser and the gateway
share a machine's loopback; otherwise the device-code flow (no callback port) is the
safe choice. This mirrors kiro-cli's ``is_remote()`` branch.

Detection is advisory: ``device`` works everywhere, so it is the fallback whenever the
shape is remote/headless or uncertain.
"""

from __future__ import annotations

import enum
import logging
import os

from kiro_crew.sandbox import is_docker_container

logger = logging.getLogger(__name__)


class Transport(enum.Enum):
    """Which login transport to drive."""

    LOOPBACK = "loopback"  # PKCE + local callback listener (browser shares loopback)
    DEVICE = "device"  # device-code, no callback (remote / headless / uncertain)


class InstallShape(enum.Enum):
    DESKTOP = "desktop"  # native / bundled app, browser + gateway same machine
    CONTAINER = "container"  # local Docker/Podman; loopback works only if port-mapped
    REMOTE = "remote"  # SSH / cloud / remote desk; remote localhost != user's laptop


def _is_ssh_remote() -> bool:
    return bool(os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_CLIENT"))


def detect_shape() -> InstallShape:
    """Best-effort classification of where this gateway runs."""
    # An explicit operator override wins (e.g. a container that HAS mapped the ports,
    # or a forced device flow for a locked-down desktop).
    forced = os.environ.get("KIRO_AUTH_INSTALL_SHAPE", "").strip().lower()
    if forced in {s.value for s in InstallShape}:
        return InstallShape(forced)

    if _is_ssh_remote():
        return InstallShape.REMOTE
    if is_docker_container():
        return InstallShape.CONTAINER
    return InstallShape.DESKTOP


def select_transport(shape: InstallShape | None = None) -> Transport:
    """Choose the login transport for the given (or detected) shape.

    - desktop: loopback (best UX — browser hits the local listener directly)
    - container: loopback IF the operator declares the callback ports are mapped
      (``KIRO_AUTH_CONTAINER_PORTS_MAPPED=1``), else device (a container without the
      mapping cannot receive the callback)
    - remote: device (remote loopback is not the user's browser loopback)

    ``device`` is the safe default: it works on every shape, so anything uncertain
    resolves to it.
    """
    shape = shape or detect_shape()

    # A hard override for the transport itself (bypasses shape reasoning entirely).
    forced = os.environ.get("KIRO_AUTH_TRANSPORT", "").strip().lower()
    if forced in {t.value for t in Transport}:
        return Transport(forced)

    if shape is InstallShape.DESKTOP:
        return Transport.LOOPBACK
    if shape is InstallShape.CONTAINER:
        mapped = os.environ.get("KIRO_AUTH_CONTAINER_PORTS_MAPPED", "").strip().lower() in (
            "1",
            "true",
            "yes",
        )
        return Transport.LOOPBACK if mapped else Transport.DEVICE
    return Transport.DEVICE
