"""Exploit targets — the sandbox a PoC runs against.

A ``Target`` is the boundary between a generated proof-of-concept and something
it can attack. The interface is deliberately tiny so a future adapter (another
codebase's dev instance, a container) can be dropped in — but v1 ships exactly
one: :class:`KiroCrewPodTarget`, wrapping an isolated ``kirocrew pod``.

**Hard constraint (SECURITY_NOTES.md #1).** :meth:`Target.assert_safe` MUST
refuse any target that resolves to the live gateway port (5476 /
``KIROCREW_POD_LIVE_PORT``). The executor calls it before every run, so a PoC
can never be pointed at the production gateway even if a base_url is
mis-supplied.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlparse


class UnsafeTargetError(RuntimeError):
    """Raised when a target resolves to a forbidden (live/production) endpoint."""


def _live_ports() -> set[int]:
    ports = {5476}
    env = os.environ.get("KIROCREW_POD_LIVE_PORT")
    if env and env.isdigit():
        ports.add(int(env))
    return ports


class Target(Protocol):
    name: str
    base_url: str
    token: str

    def assert_safe(self) -> None: ...

    def env(self) -> dict[str, str]: ...


@dataclass
class KiroCrewPodTarget:
    """An isolated ``kirocrew pod`` instance. Obtain ``base_url`` + ``token``
    from ``kirocrew pod up <wt> --json``."""

    base_url: str
    token: str = ""
    name: str = "kirocrew-pod"

    def assert_safe(self) -> None:
        parsed = urlparse(self.base_url)
        host = (parsed.hostname or "").lower()
        port = parsed.port
        if host not in ("127.0.0.1", "localhost", "::1", ""):
            raise UnsafeTargetError(
                f"target host {host!r} is not loopback — exploits run only against a local pod"
            )
        if port in _live_ports():
            raise UnsafeTargetError(
                f"target port {port} is the live gateway port — refusing to run exploits against it"
            )
        if port is None:
            raise UnsafeTargetError("target must specify an explicit pod port")

    def env(self) -> dict[str, str]:
        """Env handed to a PoC so it can reach the pod. Only the target handle
        is exposed — no ambient credentials."""
        return {
            "SECSCAN_TARGET_URL": self.base_url,
            "SECSCAN_TARGET_TOKEN": self.token,
        }
