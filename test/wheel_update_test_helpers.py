"""Shared wiring for tests that drive a managed-venv shadow apply.

The gateway's unattended apply and ``POST /api/update/approve`` both run
:func:`kiro_crew.platform.wheel_apply.run_wheel_apply`. These tests keep that
helper real and replace only what reaches outside the process: the CDN bases,
the policy source pin, the AppArmor question, and the engine itself.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable
from unittest.mock import MagicMock

import pytest

FEED_BASE = "https://feed.example"
ARTIFACT_BASE = "https://bytes.example"


def wire_wheel_apply(
    monkeypatch: pytest.MonkeyPatch,
    *,
    safe: bool | Callable[[], bool] = True,
    blocked: Callable[[str], str | None] = lambda _base: None,
    apply: Callable[..., Path] | None = None,
    reattach: bool = False,
    reaches: bool = True,
) -> Callable[..., Path]:
    """Wire the preflight and the engine; return the engine stand-in.

    The default stand-in promotes at once (a ``MagicMock`` returning a tree
    path), so a test asserts on its ``call_args``. *reaches* is the answer to
    "would a restart now run the promoted tree" (``wheel_apply.restart_reaches``),
    which a stand-in promotion cannot make true on its own.
    """
    monkeypatch.setattr(
        "kiro_crew.platform.update_layout.cdn_bases", lambda: (FEED_BASE, ARTIFACT_BASE)
    )
    monkeypatch.setattr(
        "kiro_crew.platform.update_layout.cdn_bases_are_safe",
        safe if callable(safe) else (lambda: safe),
    )
    monkeypatch.setattr("kiro_crew.platform.update_governance.update_blocked_reason", blocked)
    monkeypatch.setattr(
        "kiro_crew.platform.wheel_apply.userns_reattach_needed", lambda _version: reattach
    )
    monkeypatch.setattr("kiro_crew.platform.wheel_apply.restart_reaches", lambda _version: reaches)
    # A module-global set of loop-bound futures: each test starts with its own.
    monkeypatch.setattr("kiro_crew.platform.wheel_apply._IN_FLIGHT", set())
    engine = apply if apply is not None else MagicMock(return_value=Path("/x/crew-venv-9.9.9"))
    monkeypatch.setattr("kiro_crew.platform.wheel_engine.apply_wheel_update", engine)
    return engine
