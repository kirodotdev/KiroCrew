"""A lease refusal ends with the service's own message, once it is safe to show.

The refusal text reaches ``/api/tunnel/status``, which teammates read over the
relay, so a message is shown only when :func:`service_message` passes it, and
is otherwise left out whole while the Crew's own text stays as it is.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, AsyncIterator

import pytest
import pytest_asyncio
from test_relay_client_lease import CREDENTIAL, FakeLeaseService, StepClock, answer, make_source

from kiro_crew.relay_client import lease as lease_module
from kiro_crew.relay_client.lease import RETRY_AFTER_MAX_SECONDS
from kiro_crew.relay_client.token_source import HostTokenRetryLater, HostTokenUnavailable

pytestmark = pytest.mark.asyncio

#: The longest message shown, as ``lease.SERVICE_MESSAGE_MAX_CHARS`` sets it.
_CAP = 120

#: The Crew's own text for an access refusal with no reason, unchanged by this fix.
_ACCESS_DENIED_TEXT = (
    "the lease service refused this Crew (AccessDeniedException): this sign-in cannot "
    "host this Crew now, or remote access is turned off for this Crew's account or "
    "organization"
)


@pytest_asyncio.fixture
async def lease() -> AsyncIterator[FakeLeaseService]:
    async with FakeLeaseService() as service:
        yield service


async def test_an_invalid_token_refusal_shows_the_services_words_in_the_status_and_the_log(
    lease: FakeLeaseService, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # The issue's repro: a junk API key answered 400 AccessDeniedException.
    caplog.set_level(logging.WARNING, logger=lease_module.logger.name)
    lease.queue(400, {"__type": "AccessDeniedException", "message": "Invalid token"})
    lease.queue(200, answer(0))
    clock = StepClock()
    source = make_source(lease, tmp_path, clock, token_type="API_KEY")
    try:
        with pytest.raises(HostTokenRetryLater) as first:
            await source.current()
        # What the status reads while the Crew waits out the hold.
        with pytest.raises(HostTokenRetryLater) as status:
            await source.current()
        # The hold and its timing are today's.
        assert len(lease.requests) == 1
        assert await clock.next_sleep() == RETRY_AFTER_MAX_SECONDS
    finally:
        await source.aclose()
    said = '. The service said: "Invalid token"'
    assert str(first.value) == _ACCESS_DENIED_TEXT + said
    assert str(status.value) == (
        f"{_ACCESS_DENIED_TEXT}{said}; next try in {RETRY_AFTER_MAX_SECONDS:.0f}s"
    )
    assert f"(AccessDeniedException){said}; trying again in 900s" in caplog.text
    assert CREDENTIAL not in caplog.text + str(status.value)


async def test_a_stop_ends_with_the_services_words(lease: FakeLeaseService, tmp_path: Path) -> None:
    lease.queue(404, {"__type": "ResourceNotFoundException", "message": "Not found."})
    source = make_source(lease, tmp_path, StepClock())
    try:
        with pytest.raises(HostTokenUnavailable) as stopped:
            await source.current()
        reason = source.stopped_reason
    finally:
        await source.aclose()
    for text in (str(stopped.value), reason):
        assert text == (
            "the lease service refused this Crew (ResourceNotFoundException): the Crew's "
            'identity is disabled or deleted. The service said: "Not found."'
        )


_ACCOUNT = "123456789012"


@pytest.mark.parametrize(
    "message",
    [
        f"User arn:aws:iam::{_ACCOUNT}:user/x is not authorized",
        f"Account {_ACCOUNT} has no remote Crews",
        "Invalid key kck_0123456789abcdef0123",
        "Invalid key AbCdEf0123456789",
        "See https://example.com/help",
        "token=abc is invalid",
        "Accès refusé",
        "Invalid\ntoken",
        "x" * (_CAP + 1),
        "",
        "   ",
        42,
        None,
        ["Invalid token"],
    ],
    ids=[
        "arn",
        "account-id",
        "crew-key",
        "opaque-id",
        "url",
        "key-value",
        "non-ascii",
        "newline",
        "too-long",
        "empty",
        "blank",
        "number",
        "null",
        "list",
    ],
)
async def test_a_message_that_does_not_pass_is_left_out_and_the_text_stays_as_today(
    lease: FakeLeaseService,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    message: Any,
) -> None:
    caplog.set_level(logging.DEBUG, logger=lease_module.logger.name)
    lease.queue(400, {"__type": "AccessDeniedException", "message": message})
    source = make_source(lease, tmp_path, StepClock())
    try:
        with pytest.raises(HostTokenRetryLater) as later:
            await source.current()
    finally:
        await source.aclose()
    assert str(later.value) == _ACCESS_DENIED_TEXT
    assert "The service said" not in caplog.text
    assert _ACCOUNT not in caplog.text + str(later.value)
    assert "kck_" not in caplog.text + str(later.value)


def test_a_message_the_redactor_would_change_is_left_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service_message = lease_module.service_message
    assert service_message({"message": "Invalid token"}) == "Invalid token"
    monkeypatch.setattr(
        lease_module, "redact_external_text", lambda text: text.replace("token", "[REDACTED]")
    )
    assert service_message({"message": "Invalid token"}) is None


def test_the_length_cap_is_inclusive_and_edges_are_trimmed() -> None:
    service_message = lease_module.service_message
    assert lease_module.SERVICE_MESSAGE_MAX_CHARS == _CAP
    at_cap = "a" * _CAP
    assert service_message({"message": at_cap}) == at_cap
    assert service_message({"message": "  Invalid token  "}) == "Invalid token"
    assert service_message({"message": "Access denied."}) == "Access denied."
    assert service_message({"message": "Too many calls (try later), sorry."}) == (
        "Too many calls (try later), sorry."
    )
    assert service_message("Invalid token") is None
