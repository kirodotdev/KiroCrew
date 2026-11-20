"""A lease refusal shows the service's own short message, cleaned.

``/api/tunnel/status`` carries the refusal text, and teammates can read it, so
only plain words reach it: anything else is dropped whole.
"""

from __future__ import annotations

import logging
import string
from pathlib import Path
from typing import Any, Optional

import pytest
from test_relay_client_lease import CREDENTIAL, FakeLeaseService, StepClock, make_source

from kiro_crew.relay_client import lease as lease_module
from kiro_crew.relay_client.token_source import HostTokenRetryLater, HostTokenUnavailable
from kiro_crew.testing.synthetic_credentials import (
    SAMPLE_SLACK_BOT_TOKEN,
    synth,
)


@pytest.mark.parametrize(
    "message,shown",
    [
        ("Invalid token", "Invalid token"),
        ("Invalid\n  token\t now", "Invalid token now"),
        (
            "profileArn is required for this operation when signed in as a person.",
            "profileArn is required for this operation when signed in as a person.",
        ),
        ("x" * 500, "x" * 200),
        # Its digit runs read as an account id, so it is dropped before redaction.
        (f"Invalid token {SAMPLE_SLACK_BOT_TOKEN}", None),
        ("bell\x07here", None),
        ("\x1b[31mred", None),
        ("not caf\u00e9", None),
        ('quote " here', None),
        ("ask bob@example.com", None),
        ("role arn:aws:iam::x:role/y", None),
        # Built at run time so the source carries no account-id shaped literal.
        ("account " + "4" * 12 + " is off", None),
        ("account " + ",".join(["4444"] * 3) + " is off", None),
        ("account " + ":".join(["4444"] * 3) + " is off", None),
        ("try again at 10:30:00", "try again at 10:30:00"),
        ("   ", None),
        ("", None),
        (5, None),
        (None, None),
    ],
    ids=[
        "plain",
        "whitespace",
        "workload",
        "capped",
        "credential",
        "control",
        "ansi",
        "non-ascii",
        "quote",
        "email",
        "arn",
        "account-id",
        "account-id-commas",
        "account-id-colons",
        "time-kept",
        "blank",
        "empty",
        "not-a-string",
        "missing",
    ],
)
def test_only_plain_words_of_the_message_are_shown(message: Any, shown: Optional[str]) -> None:
    assert lease_module.REFUSAL_MESSAGE_MAX_CHARS == 200
    assert (
        lease_module.refusal_message({"__type": "AccessDeniedException", "message": message})
        == shown
    )


@pytest.mark.parametrize("body", [None, [], "Invalid token", {"reason": "X"}])
def test_a_body_with_no_message_shows_none(body: Any) -> None:
    assert lease_module.refusal_message(body) is None


@pytest.mark.asyncio
async def test_an_access_refusal_names_the_message_in_the_status_and_the_log(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="kiro_crew.relay_client.lease")
    async with FakeLeaseService() as lease:
        lease.queue(400, {"__type": "AccessDeniedException", "message": "Invalid  token"})
        source = make_source(lease, tmp_path, StepClock(), token_type="API_KEY")
        try:
            with pytest.raises(HostTokenRetryLater) as later:
                await source.current()
        finally:
            await source.aclose()
    assert str(later.value).endswith(
        "or remote access is turned off for this Crew's account or organization. "
        'The service said: "Invalid token"'
    )
    assert CREDENTIAL not in str(later.value)
    assert 'The service said: "Invalid token"' in caplog.text


@pytest.mark.asyncio
async def test_a_stop_names_the_message_and_an_unshown_one_keeps_todays_text(
    tmp_path: Path,
) -> None:
    async with FakeLeaseService() as lease:
        lease.queue(400, {"__type": "ResourceNotFoundException", "message": "No such Crew."})
        source = make_source(lease, tmp_path, StepClock())
        try:
            with pytest.raises(HostTokenUnavailable):
                await source.current()
            assert source.stopped_reason == (
                "the lease service refused this Crew (ResourceNotFoundException): the "
                'Crew\'s identity is disabled or deleted. The service said: "No such Crew."'
            )
        finally:
            await source.aclose()
        lease.queue(400, {"__type": "ResourceNotFoundException", "message": "id " + "4" * 12})
        source = make_source(lease, tmp_path, StepClock())
        try:
            with pytest.raises(HostTokenUnavailable):
                await source.current()
            assert source.stopped_reason == (
                "the lease service refused this Crew (ResourceNotFoundException): the "
                "Crew's identity is disabled or deleted"
            )
        finally:
            await source.aclose()


#: An AWS access key id and a temporary one, built at run time from pieces.
_KEY_ID = "".join(["AK", "IA"]) + synth(16, string.ascii_uppercase + string.digits, seed=5)
_TEMP_KEY_ID = "".join(["AS", "IA"]) + synth(16, string.ascii_uppercase + string.digits, seed=6)
_DIGITS = "4" * 12


@pytest.mark.parametrize(
    "message",
    [
        "role arn :aws:iam::x:role/y",
        "role ARN  : aws",
        "account " + " ".join([_DIGITS[:4], _DIGITS[4:8], _DIGITS[8:]]) + " is off",
        "account " + "-".join([_DIGITS[:4], _DIGITS[4:8], _DIGITS[8:]]) + " is off",
        "account " + ".".join([_DIGITS[:3], _DIGITS[3:6], _DIGITS[6:9], _DIGITS[9:]]),
        "ARN AWS IAM " + " ".join([_DIGITS[:3], _DIGITS[3:6], _DIGITS[6:9], _DIGITS[9:]]),
        f"key {_KEY_ID} refused",
        f"key {_TEMP_KEY_ID} refused",
    ],
    ids=[
        "arn-spaced",
        "arn-upper",
        "id-spaces",
        "id-dashes",
        "id-dots",
        "id-groups",
        "key",
        "temp-key",
    ],
)
def test_an_arn_an_account_id_or_a_key_id_in_any_spelling_hides_the_message(
    message: str,
) -> None:
    assert lease_module.refusal_message({"message": message}) is None


@pytest.mark.parametrize(
    "message",
    ["Retry in 15 minutes.", "Learn more in the guide.", "error 4040 then 12 more"],
)
def test_plain_text_with_a_few_digits_is_still_shown(message: str) -> None:
    assert lease_module.refusal_message({"message": message}) == message
