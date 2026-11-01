"""Conservative client-observed UI latency probe for KiroCrew 0.11.

This is intentionally not the host's persisted Turn duration or Provider time.
The script abstains rather than inventing elapsed time for ambiguous sessions.
"""
from __future__ import annotations

from importlib.resources import files
from pathlib import Path


def script(*, app_asset: Path | None = None) -> str:
    """Use the App being installed, not a possibly stale copied Gateway runtime.

    The integrated app owns this JavaScript resource. A gateway site-packages
    copy can legitimately lag behind the App until the transaction commits.
    Direct callers/tests without an App root continue using package data.
    """
    if app_asset is not None:
        return app_asset.read_text(encoding="utf-8")
    return files("kiro_byo").joinpath("assets", "crew11_ui_timing.js").read_text(encoding="utf-8")
