"""On a workload host the dashboard never reads or refreshes the person sign-in vault.

``vault_holds_identity`` answers yes on a workload host because the Crew owns its
spawn, not because a person signed in. The usage poll and the local identity
asked that same question, so they must stop before the vault.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from test_workload_identity import svid_file, workload  # noqa: F401

from kiro_crew.dashboard.handlers import sessions


@pytest.mark.asyncio
async def test_the_usage_refresh_reads_no_vault_and_asks_no_sign_in(
    workload: Path, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    # The workload fixture fails every vault read, a write-stamp stat included.
    published: list = []
    monkeypatch.setattr(sessions, "_publish_usage", published.append)
    await sessions._refresh_from_vault()
    # No reading and no "sign in" reason: the Crew has no person sign-in to ask for.
    assert published == [{"available": False}]


@pytest.mark.asyncio
async def test_the_local_identity_reads_no_person_vault(
    workload: Path, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    async def owns() -> bool:
        return True

    async def no_vault_read() -> Any:
        raise AssertionError("the person vault was read")

    monkeypatch.setattr(sessions, "_vault_owns_usage", owns)
    monkeypatch.setattr(sessions, "vault_identity", no_vault_read)
    # Unknown, not "no sign-in": the launch form then asks for a target.
    assert await sessions.fetch_local_identity() is None
