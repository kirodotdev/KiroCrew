"""CDK-generated AWS ARNs survive the Telegram-bot-token credential branch.

The Telegram bot token branch (``[0-9]{6,}:[A-Za-z0-9_-]{30,}``, bot id, then a
30+ char secret) is shape-only: an ARN's ``<partition>:<service>:<region>:
<account-id>:<resource>`` fields happen to share that exact shape at the
account-id/resource boundary. The account id is a bare 12-digit run, and a
CDK-generated resource name (a logical id plus its auto-generated hash suffix)
is routinely 30+ characters, so a long CDK ARN reads as a bot token and has its
account id and resource name replaced with ``[REDACTED: credential]``.

A short ARN or a bare account id never reaches the Telegram branch's ``{30,}``
floor and is unaffected either way; this file pins that regression guard
alongside the fix.

Out of scope, deliberately: the bare-secret entropy pass (pass 3) is a DIFFERENT
rule that over-redacts a short, slash-heavy ARN resource id such as an ELB
target-group ARN or an IAM role ARN. That is tracked and claimed separately and
is not touched here.
"""

from __future__ import annotations

import random
import re
import string

import pytest

from kiro_crew.security import redact, redact_credentials
from kiro_crew.security import redaction_patterns as _rp

TAG = "[REDACTED: credential]"

# A REAL Telegram-bot-token shape has no ARN prefix and must still be redacted.
TELEGRAM_TOKEN = "123456789:ABCDefghIJKLmnopQRSTuvwxYZ0123456789"

# CDK-generated ARNs: a long logical-id + hash suffix after the account id, one per
# partition / service / resource-type shape the account-id field can sit in front of.
FIRING_ARNS = [
    "arn:aws:sns:us-west-2:123456789012:MyStackMyTopicA1B2C3D4-N1zQX9yJZ8wK",
    "arn:aws-cn:sns:cn-north-1:123456789012:MyStackMyTopicA1B2C3D4-N1zQX9yJZ8wK",
    "arn:aws-us-gov:sns:us-gov-west-1:123456789012:" "MyStackMyFunctionABCDEFGHIJKLMNOPQRSTUVWXYZ",
    "arn:aws:sqs:eu-west-1:123456789012:MyStackMyQueueA1B2C3D4E5F6G7H8I9J0KLMN",
]

# Short ARNs and a bare account id: the resource name never clears the Telegram
# branch's `{30,}` floor, so these were never affected -- regression guard only.
UNAFFECTED = [
    "arn:aws:sns:us-west-2:123456789012:MyTopic",
    "arn:aws:iam::123456789012:role/MyRole",
    "123456789012",
]


def test_the_fixtures_are_what_they_claim() -> None:
    assert _rp._TELEGRAM_AT_RE.fullmatch(TELEGRAM_TOKEN)
    for arn in FIRING_ARNS:
        assert _rp._TELEGRAM_AT_RE.search(arn), arn
    for text in UNAFFECTED:
        tail = text.rsplit(":", 1)[-1] if ":" in text else text
        assert len(tail) < 30 or not _rp._TELEGRAM_AT_RE.search(text), text


@pytest.mark.parametrize("url", FIRING_ARNS)
def test_the_cdk_arn_trips_the_telegram_branch_without_the_arn_guard(
    url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_rp, "_ARN_ACCOUNT_ID_PREFIX_RE", re.compile(r"(?!)"))
    assert list(_rp._credential_matches(url))
    assert redact_credentials(url)[0] != url


@pytest.mark.parametrize("arn", FIRING_ARNS)
@pytest.mark.parametrize(
    "template",
    ["{}", "the topic ARN is {}.", "[topic]({})", "<{}>", "`{}`", '{{"arn":"{}"}}'],
)
def test_cdk_arn_survives(arn: str, template: str) -> None:
    text = template.format(arn)
    assert redact_credentials(text) == (text, [])
    assert redact(text) == text


@pytest.mark.parametrize("text", UNAFFECTED)
def test_short_arns_and_bare_account_ids_are_unaffected(text: str) -> None:
    """Regression guard: these never reached the `{30,}` floor either way."""
    assert redact_credentials(text) == (text, [])


def test_a_telegram_token_with_no_arn_prefix_is_still_redacted() -> None:
    result, warnings = redact_credentials(TELEGRAM_TOKEN)
    assert TELEGRAM_TOKEN not in result
    assert TAG in result
    assert warnings


@pytest.mark.parametrize(
    "text",
    [
        f"bot_token={TELEGRAM_TOKEN}",
        f"{TELEGRAM_TOKEN} is our bot token",
        # A longer identifier that merely CONTAINS "arn:...:" as a substring must not
        # borrow the exemption -- only a real ARN prefix (anchored by a non-identifier
        # character before the literal `arn:`) does.
        f"notanarn:aws:sns:us-west-2:{TELEGRAM_TOKEN}",
        f"myarn:aws:s3:us-east-1:{TELEGRAM_TOKEN}",
    ],
)
def test_a_telegram_token_beside_an_arn_lookalike_is_still_redacted(text: str) -> None:
    result, warnings = redact_credentials(text)
    assert TELEGRAM_TOKEN not in result
    assert TAG in result
    assert warnings


def test_a_key_glued_right_after_a_cdk_arn_is_still_redacted() -> None:
    """The ARN exemption only withholds the ARN's own account-id/resource span."""
    arn = FIRING_ARNS[0]
    text = f"{arn}{TELEGRAM_TOKEN}"
    result, warnings = redact_credentials(text)
    assert TELEGRAM_TOKEN not in result
    assert TAG in result
    assert warnings
    # The ARN itself, immediately before the glued token, still survives.
    assert arn in result


# A real AWS account id is exactly 12 digits; a Telegram bot id is 6-10. So a
# genuine token placed right after a REAL `arn:<partition>:<service>:<region>:`
# prefix -- whose leading digit run is NOT 12 digits -- must still be redacted.
# Otherwise `arn:aws:sns:us-west-2:123456789:<35-char secret>` (a 9-digit bot id)
# would be exempted and leak, and the bare-secret pass (40+ chars) would not catch it.
@pytest.mark.parametrize(
    "digits",
    ["12345678", "123456789", "1234567890", "12345678901", "1234567890123"],
)
def test_a_telegram_token_after_a_real_arn_prefix_with_a_non_12_digit_id_is_redacted(
    digits: str,
) -> None:
    secret = "ABCDefghIJKLmnopQRSTuvwxYZ0123456789"  # 36 chars, clears {30,}
    assert len(digits) != 12
    text = f"arn:aws:sns:us-west-2:{digits}:{secret}"
    token = f"{digits}:{secret}"
    assert _rp._TELEGRAM_AT_RE.search(token)
    result, warnings = redact_credentials(text)
    assert token not in result
    assert TAG in result
    assert warnings


def test_a_real_12_digit_account_arn_is_still_exempt() -> None:
    """The 12-digit guard must not break the legitimate fix: a real account id survives."""
    arn = "arn:aws:sns:us-west-2:123456789012:MyStackMyTopicA1B2C3D4-N1zQX9yJZ8wK"
    assert len("123456789012") == 12
    assert redact_credentials(arn) == (arn, [])


# A 12-digit run is not proof of an account id: a 6-10 digit bot id padded with
# leading zeros is 12 digits too. Telegram issues secrets of 35 characters starting
# `AA` (BotFather's documented example is `110201543:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw`),
# but does not promise the prefix, so the padded-id rule stands on its own.
AA_SECRET = "AA" + "Hq7Tcv1vGWJxfSeofSAs0K5PALDs" + "aw3Xy"
# The Bot API docs' illustrative token secret: 34 characters, NOT starting `AA`, so
# the secret-signature rule does not see it and only the padded-id rule can.
DOCS_STYLE_SECRET = "ABC-DEF1234ghIkl-zyx57W2v1u123ew11"


def test_the_bot_token_fixtures_are_what_they_claim() -> None:
    assert len(AA_SECRET) == 35 and AA_SECRET.startswith("AA")
    assert len(DOCS_STYLE_SECRET) == 34 and not DOCS_STYLE_SECRET.startswith("AA")
    assert not DOCS_STYLE_SECRET.startswith(_rp._TELEGRAM_SECRET_PREFIX)
    # A real CDK ARN tail is ALSO 35 characters here, so length alone cannot tell a
    # resource name from a secret.
    assert len(FIRING_ARNS[0].rsplit(":", 1)[1]) == 35


@pytest.mark.parametrize("bot_id", ["110201543", "1234567890", "123456"])
@pytest.mark.parametrize("secret", [AA_SECRET, DOCS_STYLE_SECRET])
def test_a_zero_padded_bot_id_behind_a_real_arn_prefix_is_redacted(
    bot_id: str, secret: str
) -> None:
    """Padding a 6-10 digit bot id to 12 digits must not buy the account-id exemption."""
    padded = bot_id.zfill(12)
    text = f"arn:aws:sns:us-west-2:{padded}:{secret}"
    result, warnings = redact_credentials(text)
    assert secret not in result
    assert TAG in result
    assert warnings


def test_a_telegram_secret_signature_behind_a_real_account_id_is_redacted() -> None:
    """A 35-char `AA` tail is read as a bot secret even after an unpadded 12-digit run."""
    text = f"arn:aws:sns:us-west-2:123456789012:{AA_SECRET}"
    result, warnings = redact_credentials(text)
    assert AA_SECRET not in result
    assert TAG in result
    assert warnings


def test_a_real_shaped_bot_token_outside_any_arn_is_redacted() -> None:
    token = f"110201543:{AA_SECRET}"
    result, warnings = redact_credentials(f"the bot token is {token}")
    assert token not in result
    assert TAG in result
    assert warnings


@pytest.mark.parametrize("bot_id", ["110201543", "1234567890", "123456"])
@pytest.mark.parametrize("suffix", ["-", "x", "_trailer"])
def test_a_non_zero_padded_bot_id_with_a_trailing_character_is_redacted(
    bot_id: str, suffix: str
) -> None:
    """Padding with non-zero digits skips the padded-id rule; a trailing character
    must not then lift the secret signature, or the whole token survives."""
    padded = "9" * (12 - len(bot_id)) + bot_id
    assert not padded.startswith("0")
    text = f"arn:aws:sns:us-west-2:{padded}:{AA_SECRET}{suffix}"
    result, warnings = redact_credentials(text)
    assert f"{bot_id}:{AA_SECRET}" not in result
    assert AA_SECRET not in result
    assert TAG in result
    assert warnings


@pytest.mark.parametrize("keep", [30, 33, 34])
def test_a_truncated_aa_secret_behind_a_non_zero_padded_id_is_redacted(keep: int) -> None:
    """All but a few characters of a secret is still a secret: no length rule may leave
    a prefix of it exposed."""
    fragment = AA_SECRET[:keep]
    text = f"arn:aws:sns:us-west-2:999110201543:{fragment}"
    result, warnings = redact_credentials(text)
    assert fragment not in result
    assert TAG in result
    assert warnings


@pytest.mark.parametrize("padding", ["x", "x7", "MyStack-"])
def test_an_aa_secret_behind_public_resource_padding_is_redacted(padding: str) -> None:
    """Characters in front of the secret must not move it out of the prefix test."""
    text = f"arn:aws:sns:us-west-2:999110201543:{padding}{AA_SECRET}"
    result, warnings = redact_credentials(text)
    assert AA_SECRET not in result
    assert TAG in result
    assert warnings


@pytest.mark.parametrize(
    ("resource", "redacts"),
    [
        # An `AA` mid-name with 30+ characters from it to the end could be a secret.
        ("MyStack" + "AAQueueA1B2C3D4E5F6G7H8I9J0KLMNOP", True),
        # An `AA` inside a short hash suffix is too close to the end to be one.
        ("MyStackMyTopicFunctionHandlerAAB3C4D5", False),
    ],
)
def test_an_aa_inside_a_cdk_name_redacts_only_with_a_secret_length_run(
    resource: str, redacts: bool
) -> None:
    arn = f"arn:aws:sns:us-west-2:123456789012:{resource}"
    first = resource.find("AA")
    assert (len(resource) - first >= 30) is redacts
    result, _ = redact_credentials(arn)
    assert (result != arn) is redacts


@pytest.mark.parametrize(
    ("resource", "redacts"),
    [
        # Any tail long enough to reach the Telegram branch (30+) that starts `AA`
        # redacts. Over-redaction is the safe direction.
        ("AAStackMyTopicA1B2C3D4-N1zQX9yJZ", True),
        ("AAStackAATopicA1B2C3D4-N1zQX9yJZ8wK", True),
        ("AAStackMyQueueA1B2C3D4E5F6G7H8I9J0KLMN", True),
        # Under the branch's 30-character floor: never a Telegram hit at all.
        ("AAStackMyTopicA1B2C3D4", False),
        # A single `A` is not the prefix.
        ("AStackMyTopicA1B2C3D4-N1zQX9yJZ8wK", False),
    ],
)
def test_a_cdk_name_starting_aa_redacts_once_it_reaches_the_branch(
    resource: str, redacts: bool
) -> None:
    arn = f"arn:aws:sns:us-west-2:123456789012:{resource}"
    assert (resource.startswith("AA") and len(resource) >= 30) is redacts
    result, _ = redact_credentials(arn)
    assert (result != arn) is redacts


def test_a_real_account_id_starting_00_redacts_a_long_resource() -> None:
    """Documented cost of the padded-id rule: `00` + a 6-10 digit value reads as a bot id."""
    arn = "arn:aws:sns:us-west-2:001234567890:MyStackMyTopicA1B2C3D4-N1zQX9yJZ8wK"
    assert redact_credentials(arn)[0] != arn


def test_a_real_account_id_with_one_leading_zero_stays_exempt() -> None:
    """One leading zero leaves an 11-digit value, past any bot id, so the exemption holds."""
    arn = "arn:aws:sns:us-west-2:012345678901:MyStackMyTopicA1B2C3D4-N1zQX9yJZ8wK"
    assert redact_credentials(arn) == (arn, [])


def test_no_seeded_cdk_resource_name_fires() -> None:
    """A random CDK-shaped logical-id + hash suffix is exempted, not by luck of one fixture.

    Shaped like CDK's own generated names: a readable PascalCase logical id (the part
    a human wrote) followed by CDK's 8-13 character uppercase-alnum hash suffix -- not
    a fully random 30+ character string, which can independently look like a bare
    secret key to the ENTROPY pass regardless of any ARN prefix (a different rule's
    question, not this branch's).
    """
    rng = random.Random("22475-cdk-arn")
    words = ["My", "Stack", "Topic", "Queue", "Function", "Bucket", "Table", "Role", "Policy"]
    hash_alphabet = string.ascii_uppercase + string.digits
    for _ in range(200):
        logical_id = "".join(rng.choice(words) for _ in range(rng.randint(2, 4)))
        suffix = "".join(rng.choice(hash_alphabet) for _ in range(rng.randint(8, 13)))
        resource = f"{logical_id}{suffix}"
        arn = f"arn:aws:sns:us-west-2:123456789012:{resource}"
        assert redact_credentials(arn) == (arn, []), arn


def test_arn_account_id_prefix_rejects_a_lookalike_prefix() -> None:
    """The look-behind guard: a longer identifier merely ending in ``arn:...:`` is not an ARN."""
    assert not _rp._ARN_ACCOUNT_ID_PREFIX_RE.search("notanarn:aws:sns:us-west-2:", 0, 27)
    assert _rp._ARN_ACCOUNT_ID_PREFIX_RE.search("arn:aws:sns:us-west-2:", 0, 22)
    assert _rp._ARN_ACCOUNT_ID_PREFIX_RE.search("my arn:aws:sns:us-west-2:", 0, 25)


def test_arn_account_id_prefix_admits_an_empty_region_field() -> None:
    """IAM, S3 and CloudFront ARNs carry no region field (``arn:aws:iam::<account-id>:...``)."""
    text = "arn:aws:iam::"
    assert _rp._ARN_ACCOUNT_ID_PREFIX_RE.search(text, 0, len(text))


def test_scanning_many_cdk_arns_stays_bounded_per_hit() -> None:
    """The ARN-prefix check never scans more than a bounded window before each hit.

    The guard opens on a negative look-behind, so an unbounded ``search(text, 0, hit)``
    would retry every offset from 0 on each Telegram-shaped hit, which every long CDK
    ARN is -- O(hits x len(text)), the loop-stall shape ``test_security_gate_liveness``
    exists to catch. This asserts the WORK done, not wall-clock time (which depends on
    runner load, not the code): every ``_ARN_ACCOUNT_ID_PREFIX_RE.search`` call starts
    no earlier than ``hit - _ARN_PREFIX_MAX_LOOKBACK``, so the span it scans is capped
    at a constant and the whole redaction stays linear over the corpus.
    """
    arn = "arn:aws:sns:us-west-2:123456789012:MyStackMyTopicA1B2C3D4-N1zQX9yJZ8wK"
    corpus = "\n".join(arn for _ in range(4_000))  # ~290 KB, ~4k firing hits

    real_search = _rp._ARN_ACCOUNT_ID_PREFIX_RE.search
    spans: list[int] = []

    class _SpyRe:
        def search(self, text: str, pos: int = 0, endpos: int | None = None):  # type: ignore[no-untyped-def]
            # endpos is the hit start; record how far back this call was allowed to scan.
            spans.append((endpos if endpos is not None else len(text)) - pos)
            return real_search(text, pos, endpos if endpos is not None else len(text))

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(_rp, "_ARN_ACCOUNT_ID_PREFIX_RE", _SpyRe())
        result, _ = redact_credentials(corpus)

    # Every ARN is exempt, so the text is returned intact.
    assert result == corpus
    # The check ran once per firing hit, and never over an unbounded span.
    assert spans, "the ARN-prefix guard was never exercised"
    assert max(spans) <= _rp._ARN_PREFIX_MAX_LOOKBACK, max(spans)


def test_arn_prefix_just_within_the_window_still_exempts() -> None:
    """A real ARN prefix sitting just inside the lookback window exempts the hit."""
    arn = "arn:aws:sns:us-west-2:123456789012:MyStackMyTopicA1B2C3D4-N1zQX9yJZ8wK"
    # Pad so the ARN starts well into the text but its prefix still ends at the hit.
    text = ("x" * 1000) + " " + arn
    assert redact_credentials(text) == (text, [])


def test_lookback_window_covers_the_longest_realistic_arn_prefix() -> None:
    """The window is wider than any real ``arn:<partition>:<service>:<region>:<account>:``."""
    # A deliberately maximal-but-real prefix: long gov partition, long service and
    # region names, 12-digit account. Its length must sit under the lookback bound.
    prefix = "arn:aws-us-gov:elasticloadbalancing:us-gov-west-1:123456789012:"
    assert len(prefix) <= _rp._ARN_PREFIX_MAX_LOOKBACK
    token_shape = "A1B2C3D4E5F6G7H8I9J0KLMNOPQRST"  # 30 chars, clears the {30,} floor
    text = prefix + token_shape
    assert _rp._TELEGRAM_AT_RE.search(text)
    assert redact_credentials(text) == (text, [])
