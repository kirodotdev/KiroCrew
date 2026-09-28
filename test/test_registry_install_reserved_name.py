"""Pin: ``install_from_registry`` refuses an inadmissible app name BEFORE setup.

The manifest/self-registration gates repeat the same check, but for a
self-managed (``resources: "app"``) registry entry they only fire at runtime
self-registration — so without the early refusal the install path would
clone, build, and run ``onInstall`` for an app that can never register, then
report success. The early gate must fire before the registry lookup and any
subprocess work, and must carry the machine-readable ``code`` for reserved
names only (same contract as ``register_external_app``).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from kiro_crew.apps.manifest import RESERVED_APP_NAME_CODE
from kiro_crew.apps.registry import install_from_registry


@pytest.mark.asyncio
async def test_reserved_route_name_refused_before_any_setup() -> None:
    with (
        patch("kiro_crew.apps.registry._resolve_install_entry") as resolve,
        patch("kiro_crew.apps.registry._clone_build_app", new_callable=AsyncMock) as clone,
    ):
        result = await install_from_registry("library")
    assert result["ok"] is False
    assert result.get("code") == RESERVED_APP_NAME_CODE
    assert "reserved" in result["error"]
    # The refusal precedes the registry lookup and clone/build — no
    # third-party code ran and no registry state was consulted.
    resolve.assert_not_called()
    clone.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_reserved_invalid_name_refused_without_reserved_code() -> None:
    with patch("kiro_crew.apps.registry._clone_build_app", new_callable=AsyncMock) as clone:
        result = await install_from_registry("Bad_Name!")
    assert result["ok"] is False
    # Kebab-case refusal is not a reservation: no reserved code on the wire.
    assert result.get("code") != RESERVED_APP_NAME_CODE
    clone.assert_not_awaited()
