"""The status payload: the served bundle, uptime and governance status it reports."""

from __future__ import annotations

from pathlib import Path

#: Cache for :meth:`DashboardState.served_bundle_id` — the served frontend
#: entry point's ``(mtime_ns, size)`` -> short content hash. One slot: there is
#: exactly one served bundle per process, and the snapshot that reads it sits
#: on the hot status path.
_BUNDLE_ID_CACHE: dict[str, tuple[tuple[int, int], str]] = {}


#: The entry point the gateway serves (``server.py``'s ``_DIST_DIR``).
_SERVED_INDEX = Path(__file__).resolve().parent.parent.parent / "static" / "dist" / "index.html"


def _fmt_duration(secs: int) -> str:
    """Format seconds as human-readable duration."""
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m}m" if h > 0 else f"{m}m {s}s"


def _governance_status() -> str:
    """Governance health for the status snapshot (never raises)."""
    try:
        from kiro_crew.platform.governance_health import governance_status

        return governance_status()
    except Exception:
        return "unknown"
