"""The install summary lists memory only when the app asks for it.

A JSON ``false`` or ``null`` for ``permissions.memory`` is stored as the text
"False" / "None"; the CLI summary printed that as a requested permission.
"""

from __future__ import annotations

import pytest

from kiro_crew.apps.manifest import AppManifest
from kiro_crew.apps.permissions import format_permissions_summary


def _summary(memory: object) -> str:
    manifest = AppManifest.from_dict(
        {"name": "demo", "version": "1.0.0", "description": "d", "permissions": {"memory": memory}}
    )
    return format_permissions_summary(manifest)


@pytest.mark.parametrize("memory", [False, None, ""])
def test_a_false_or_null_memory_requests_nothing(memory: object) -> None:
    assert _summary(memory) == "  No special permissions required"


def test_a_real_memory_request_is_listed() -> None:
    assert "  Memory:    shared" in _summary("shared")
