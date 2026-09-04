"""S1: the front process.

The only listener the network reaches. It authenticates the loopback call to the
Kiro Crew backend, strips the crew route prefix, serializes turns per conversation,
projects the backend's stream down to a customer-safe allowlist, and keeps the
customer surface separate from the owner's control surface.

Public seam (see ``container/CONTRACT.md``):
    container.front.app:build_app(settings)
    container.front.__main__:main()
"""

from .app import build_app

__all__ = ["build_app"]
