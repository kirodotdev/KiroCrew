"""Fixtures, store writers and session doubles shared by the
``test_external_logout_detection*.py`` modules.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew import kiro_prerequisite as kp


@pytest.fixture(autouse=True)
def _private_sel_root_per_test(sel_private_root):
    """Every test in this module gets its OWN SEL root.

    ``identity_fingerprint`` is audit-or-deny: it returns "absent" unless a
    CRITICAL SEL event lands first. On the event-loop thread the chain-lock
    acquire is a single non-blocking attempt that refuses rather than stall the
    loop -- correct product behaviour -- so on the worker's SHARED SEL root an
    async test asserting a NON-EMPTY fingerprint is racing writers it never
    created (another test still flushing, another xdist worker on the same
    path). It then reads "" and fails on a property it never meant to test.
    ``sel_private_root`` removes the concurrent writer: a fresh per-test,
    per-worker directory nothing else writes.
    """
    yield


def _write_store(
    path: Path,
    *,
    token_value: str = "access-token-v1",
    start_url: str = "https://company.awsapps.com/start",
    profile: str = "arn:aws:codewhisperer:us-east-1:1111:profile/COMPANY",
    auth_keys: tuple[str, ...] = ("kirocli:odic:device-registration", "kirocli:odic:token"),
    client_id: str = "client-registration-aaa",
    region: str = "us-east-1",
    state_rows: bool = True,
) -> None:
    """Write a minimal store shaped like kiro-cli's real one.

    ``state_rows=False`` models a Builder ID login: no Identity Center marker rows
    and no CodeWhisperer profile, so the identity has to come from the credential
    blob's stable claims instead.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    with con:
        con.execute("CREATE TABLE IF NOT EXISTS auth_kv (key TEXT PRIMARY KEY, value TEXT)")
        con.execute("CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT)")
        con.execute("DELETE FROM auth_kv")
        con.execute("DELETE FROM state")
        for key in auth_keys:
            if key.endswith(":device-registration"):
                blob = {
                    "client_id": client_id,
                    "client_secret": "SECRET-must-never-be-fingerprinted",
                    "client_secret_expires_at": "2099-01-01T00:00:00Z",
                    "oauth_flow": "device_code",
                    "region": region,
                    "scopes": ["codewhisperer:completions", "codewhisperer:analysis"],
                }
            else:
                blob = {
                    "access_token": token_value,
                    "refresh_token": f"refresh-of-{token_value}",
                    "expires_at": "2099-01-01T00:00:00Z",
                    "oauth_flow": "device_code",
                    "region": region,
                    "scopes": ["codewhisperer:completions", "codewhisperer:analysis"],
                    "start_url": start_url,
                }
            con.execute("INSERT INTO auth_kv (key, value) VALUES (?, ?)", (key, json.dumps(blob)))
        if state_rows:
            con.execute(
                "INSERT INTO state (key, value) VALUES (?, ?)", ("auth.idc.start-url", start_url)
            )
            con.execute("INSERT INTO state (key, value) VALUES (?, ?)", ("auth.idc.region", region))
            con.execute(
                "INSERT INTO state (key, value) VALUES (?, ?)",
                ("api.codewhisperer.profile", profile),
            )
        # Unrelated local state must not participate in the fingerprint.
        con.execute("INSERT INTO state (key, value) VALUES (?, ?)", ("telemetry.client-id", "abc"))
    con.close()


def _expire_identity_cache(service: "kp.KiroPrerequisiteService") -> None:
    """Drop the reader's real-time cache.

    The fingerprint is cached for a few seconds so a dashboard poll storm cannot
    turn into one SQLite read and one SEL audit event per poll. A test that
    rewrites the store and immediately re-reads is outside that design, so it
    expires the cache explicitly rather than sleeping.
    """

    service._identity_cache_at = 0.0


class _FakeSemaphore:
    def __init__(self, locked: bool) -> None:
        self._locked = locked

    def locked(self) -> bool:
        return self._locked


class _FakeProvider:
    def __init__(self, backend: str, *, sid: str = "") -> None:
        self.backend = backend
        self.client = SimpleNamespace(_session_id=sid, backend=backend)
        self.cwd = ""
        self.shutdown_calls = 0

    @property
    def uses_kiro_identity_store(self) -> bool:
        from kiro_crew.acp.types import backends_retired_by_host_logout

        return self.backend in backends_retired_by_host_logout()

    def is_process_alive(self) -> bool:
        return True

    def is_alive(self) -> bool:
        return True

    async def shutdown(self) -> None:
        self.shutdown_calls += 1


class _DeadProvider(_FakeProvider):
    """A provider whose child has already exited.

    Drives the dead-provider removal branch in ``_get_or_create_impl``, the one
    path that pops a session without going through ``_evict_stale_session``.
    """

    def is_process_alive(self) -> bool:
        return False

    def is_alive(self) -> bool:
        return False


class _FakeRuntime:
    """Stands in for AcpRuntime in the retirement sweep."""

    def __init__(
        self, backend: str = "", *, active: bool = False, initializing: bool = False
    ) -> None:
        self._acp_backend = backend
        self._active = active
        self._initializing = initializing
        self.killed = 0
        self.kill_reasons: list[str] = []

    @property
    def uses_kiro_identity_store(self) -> bool:
        from kiro_crew.acp.types import backends_retired_by_host_logout

        return self._acp_backend in backends_retired_by_host_logout()

    def has_active_sessions(self) -> bool:
        return self._active

    def has_active_or_initializing_sessions(self) -> bool:
        return self._active or self._initializing

    async def kill(self, *, expected: bool = False, reason: str = "") -> None:
        # Mirrors the real signature: a double that refuses ``reason`` turns an
        # attributed kill into a swallowed TypeError, which the sweep logs as a
        # failed teardown instead of performing one.
        self.killed += 1
        self.kill_reasons.append(reason)
