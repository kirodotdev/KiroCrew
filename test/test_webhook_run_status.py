"""``GET /api/hooks/agent/status``: a webhook caller reads how its own run ended.

The read is authorized by the source token that started the run and by nothing
else: another source's token, or no token, gets no run data. The middleware
half (only the GET skips the dashboard gate) is in ``test_webhooks_auth_surface.py``.
"""

from __future__ import annotations

import pytest
from webhooks_api_helpers import wired_fixture  # noqa: F401
from webhooks_api_helpers import _payload, _req

from kiro_crew import webhooks
from kiro_crew.dashboard.handlers import hooks as H
from kiro_crew.dashboard.token_auth_paths import AGENT_HOOK_STATUS_PATH

pytestmark = pytest.mark.asyncio

KEY = "hook:review:pr-123"


def _status(raw: str | None, key: str = KEY):
    headers = {"Authorization": f"Bearer {raw}"} if raw else {}
    return _req("GET", f"{AGENT_HOOK_STATUS_PATH}?sessionKey={key}", headers=headers)


def _bearer_source(label: str) -> tuple[str, str]:
    raw, _secret, entry = webhooks.token_store().create(label, require_signature=False)
    return raw, entry["id"]


async def test_own_token_reads_its_degraded_run(wired):
    raw, token_id = _bearer_source("Poller")
    webhooks.run_store().record(
        outcome=webhooks.OUTCOME_DEGRADED,
        session_key=KEY,
        started_at=1785372000.0,
        duration_ms=31500,
        token_id=token_id,
        missing_servers=("azure-devops",),
    )

    resp = await H.api_hooks_agent_status(_status(raw))

    assert resp.status == 200
    assert await _payload(resp) == {
        "outcome": "degraded",
        "startedAt": 1785372000.0,
        "endedAt": 1785372031.5,
        "missingServers": ["azure-devops"],
    }


async def test_another_sources_run_reads_as_an_unknown_key(wired):
    _owner_raw, owner_id = _bearer_source("Poller")
    other_raw, _other_id = _bearer_source("Deploy bot")
    webhooks.run_store().record(
        outcome=webhooks.OUTCOME_COMPLETED, session_key=KEY, token_id=owner_id
    )

    foreign = await H.api_hooks_agent_status(_status(other_raw))
    unknown = await H.api_hooks_agent_status(_status(other_raw, key="hook:never-used"))

    assert foreign.status == unknown.status == 404
    assert await _payload(foreign) == await _payload(unknown)


async def test_no_token_gets_no_run_data(wired):
    _raw, token_id = _bearer_source("Poller")
    webhooks.run_store().record(
        outcome=webhooks.OUTCOME_COMPLETED, session_key=KEY, token_id=token_id
    )

    resp = await H.api_hooks_agent_status(_status(None))

    assert resp.status == 401
    assert await _payload(resp) == {"error": "unauthorized", "code": "unauthorized"}


async def test_unknown_session_key_is_not_found(wired):
    raw, _token_id = _bearer_source("Poller")

    resp = await H.api_hooks_agent_status(_status(raw, key="hook:never-used"))

    assert resp.status == 404
    assert (await _payload(resp))["code"] == "run_not_found"


async def test_a_reused_key_reads_its_newest_ended_turn(wired):
    """A refusal on the same key answers the request itself, so it is skipped."""
    raw, token_id = _bearer_source("Poller")
    store = webhooks.run_store()
    store.record(outcome="completed", session_key=KEY, token_id=token_id, started_at=1.0)
    store.record(outcome="timeout", session_key=KEY, token_id=token_id, started_at=2.0)
    store.record(outcome="rejected_capacity", session_key=KEY, token_id=token_id)

    payload = await _payload(await H.api_hooks_agent_status(_status(raw)))

    assert (payload["outcome"], payload["startedAt"]) == ("timeout", 2.0)


async def test_a_signing_source_reads_with_its_bearer_alone(wired):
    raw, _secret, entry = webhooks.token_store().create("Signed poller")
    webhooks.run_store().record(
        outcome=webhooks.OUTCOME_COMPLETED, session_key=KEY, token_id=entry["id"]
    )

    resp = await H.api_hooks_agent_status(_status(raw))

    assert resp.status == 200
    assert (await _payload(resp))["outcome"] == "completed"


async def test_a_read_without_a_credential_does_not_throttle_its_address(wired):
    """A cross-origin page can send this GET but no credential header."""
    raw, token_id = _bearer_source("Poller")
    webhooks.run_store().record(
        outcome=webhooks.OUTCOME_COMPLETED, session_key=KEY, token_id=token_id
    )
    for _ in range(webhooks._AUTH_FAIL_LIMIT + 1):
        assert (await H.api_hooks_agent_status(_status(None))).status == 401

    assert (await H.api_hooks_agent_status(_status(raw))).status == 200
