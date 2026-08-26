"""Tunnel integration — stub. Not available in the OSS build (see manager.py)."""

from kiro_crew.tunnel.manager import TunnelManager, TunnelState

_tunnel_public_url: str = ""


def set_tunnel_url(url: str) -> None:
    """Called by the tunnel manager on connect/disconnect."""
    global _tunnel_public_url
    _tunnel_public_url = url


def get_tunnel_url() -> str:
    """Return the active tunnel URL, or empty string if unavailable."""
    return _tunnel_public_url


__all__ = ["TunnelManager", "TunnelState", "set_tunnel_url", "get_tunnel_url"]
