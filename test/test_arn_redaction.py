"""CDK-generated AWS ARNs survive the credential plan, and a bot token outside one does not.

The fixed Telegram branch (``[0-9]{6,}:[A-Za-z0-9_-]{30,}``, bot id, then a 30+
char secret) is shape-only: an ARN's ``<partition>:<service>:<region>:
<account-id>:<resource>`` fields share that shape at the account-id/resource
boundary, and a CDK-generated resource name is routinely 30+ characters. The plan
reads a bot token only in its published form (an 8 to 10 digit bot id, ``:``, 35
characters) and never glued after ``:`` or ``/``, and an ARN is never masked, so
these pin that an ARN stays as written and that a bot token beside one is masked.
"""

from __future__ import annotations

import random
import string

import pytest

from kiro_crew.security import redact, redact_credentials
from kiro_crew.security import redaction_patterns as _rp

TAG = "[REDACTED: credential]"

# A REAL Telegram-bot-token shape has no ARN prefix and must still be redacted.
TELEGRAM_TOKEN = "123456789:ABCDefghIJKLmnopQRSTuvwxYZ012345678"

# CDK-generated ARNs: a long logical-id + hash suffix after the account id, one per
# partition / service / resource-type shape the account-id field can sit in front of.
FIRING_ARNS = [
    "arn:aws:sns:us-west-2:123456789012:MyStackMyTopicA1B2C3D4-N1zQX9yJZ8wK",
    "arn:aws-cn:sns:cn-north-1:123456789012:MyStackMyTopicA1B2C3D4-N1zQX9yJZ8wK",
    "arn:aws-us-gov:sns:us-gov-west-1:123456789012:MyStack-MyFunction3BAA72D1-mjnQ2ZJ1Kf7Y",
    "arn:aws:sqs:eu-west-1:123456789012:MyStack-MyQueueA1B2C3D4-1HW2Q8VZQ0ABC",
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
    ],
)
def test_a_telegram_token_beside_an_arn_lookalike_is_still_redacted(text: str) -> None:
    result, warnings = redact_credentials(text)
    assert TELEGRAM_TOKEN not in result
    assert TAG in result
    assert warnings


def test_a_key_right_after_a_cdk_arn_is_still_redacted() -> None:
    """The ARN exemption only withholds the ARN's own account-id/resource span."""
    arn = FIRING_ARNS[0]
    text = f"{arn} {TELEGRAM_TOKEN}"
    result, warnings = redact_credentials(text)
    assert TELEGRAM_TOKEN not in result
    assert TAG in result
    assert warnings
    # The ARN itself, immediately before the glued token, still survives.
    assert arn in result


def test_a_real_12_digit_account_arn_is_still_exempt() -> None:
    """The 12-digit guard must not break the legitimate fix: a real account id survives."""
    arn = "arn:aws:sns:us-west-2:123456789012:MyStackMyTopicA1B2C3D4-N1zQX9yJZ8wK"
    assert len("123456789012") == 12
    assert redact_credentials(arn) == (arn, [])


# Telegram issues secrets of 35 characters starting `AA` (BotFather's documented
# example is `110201543:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw`), but does not promise the
# prefix, so no test relies on it.
AA_SECRET = "AA" + "Hq7Tcv1vGWJxfSeofSAs0K5PALDs" + "aw3Xy"
# The Bot API docs' illustrative token secret: 34 characters, NOT starting `AA`.
DOCS_STYLE_SECRET = "ABC-DEF1234ghIkl-zyx57W2v1u123ew11"


def test_the_bot_token_fixtures_are_what_they_claim() -> None:
    assert len(AA_SECRET) == 35 and AA_SECRET.startswith("AA")
    assert len(DOCS_STYLE_SECRET) == 34 and not DOCS_STYLE_SECRET.startswith("AA")
    # A real CDK ARN tail is ALSO 35 characters here, so length alone cannot tell a
    # resource name from a secret.
    assert len(FIRING_ARNS[0].rsplit(":", 1)[1]) == 35


def test_a_real_shaped_bot_token_outside_any_arn_is_redacted() -> None:
    token = f"110201543:{AA_SECRET}"
    result, warnings = redact_credentials(f"the bot token is {token}")
    assert token not in result
    assert TAG in result
    assert warnings


def test_a_tail_under_the_branch_floor_is_never_a_telegram_hit() -> None:
    """Under 30 characters the Telegram branch never fires, whatever the tail holds."""
    arn = "arn:aws:sns:us-west-2:123456789012:AAStackMyTopicA1B2C3D4"
    assert redact_credentials(arn) == (arn, [])


def test_a_real_account_id_starting_00_keeps_a_long_resource() -> None:
    """An ARN is never masked, whatever its account id starts with."""
    arn = "arn:aws:sns:us-west-2:001234567890:MyStackMyTopicA1B2C3D4-N1zQX9yJZ8wK"
    assert redact_credentials(arn) == (arn, [])


def test_a_real_account_id_with_one_leading_zero_stays_exempt() -> None:
    """One leading zero leaves an 11-digit value, past any bot id, so the exemption holds."""
    arn = "arn:aws:sns:us-west-2:012345678901:MyStackMyTopicA1B2C3D4-N1zQX9yJZ8wK"
    assert redact_credentials(arn) == (arn, [])


def test_no_seeded_cdk_resource_name_fires() -> None:
    """A random CDK-shaped logical-id + hash suffix is exempted, not by luck of one fixture.

    Shaped like CDK's own generated names: an optional stack name, a readable
    PascalCase logical id (the part a human wrote), CDK's 8-character uppercase-hex
    hash, and an optional CloudFormation suffix of 8-13 letters and digits.
    """
    rng = random.Random("22475-cdk-arn")
    words = ["My", "Stack", "Topic", "Queue", "Function", "Bucket", "Table", "Role", "Policy"]
    hex_upper = "0123456789ABCDEF"
    suffix_alphabet = string.ascii_letters + string.digits
    for _ in range(200):
        stack = rng.choice(["", "MyStack-", "ProdApiStack-"])
        logical_id = "".join(rng.choice(words) for _ in range(rng.randint(2, 4)))
        cdk_hash = "".join(rng.choice(hex_upper) for _ in range(8))
        suffix = rng.choice(
            ["", "-" + "".join(rng.choice(suffix_alphabet) for _ in range(rng.randint(8, 13)))]
        )
        resource = f"{stack}{logical_id}{cdk_hash}{suffix}"
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

    The guard runs in the linear scan ``iter_credential_matches`` (the deploy scan and the
    preview and ledger readers call it); the rule table never reads a token glued after
    ``:``, so the redactor does not reach it. The guard opens on a negative look-behind, so
    an unbounded ``search(text, 0, hit)`` would retry every offset from 0 on each
    Telegram-shaped hit, which every long CDK ARN is -- O(hits x len(text)), the
    loop-stall shape ``test_security_gate_liveness`` exists to catch. This asserts the
    WORK done, not wall-clock time (which depends on runner load, not the code): every
    ``_ARN_ACCOUNT_ID_PREFIX_RE.search`` call starts no earlier than
    ``hit - _ARN_PREFIX_MAX_LOOKBACK``, so the span it scans is capped at a constant and
    the whole scan stays linear over the corpus.
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
        hits = list(_rp.iter_credential_matches(corpus))

    # Every ARN is exempt, so the scan finds nothing and the redactor returns the text intact.
    assert hits == []
    assert redact_credentials(corpus)[0] == corpus
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
    cdk_name = "MyStackMyTopicA1B2C3D4-N1zQX9yJZ8wK"  # 35 chars, clears the {30,} floor
    text = prefix + cdk_name
    assert _rp._TELEGRAM_AT_RE.search(text)
    assert redact_credentials(text) == (text, [])


@pytest.mark.parametrize(
    "resource",
    [
        "MyStackMyTopicA1B2C3D4-N1zQX9yJZ8wK",
        "MyStack-MyFunction3BAA72D1-mjnQ2ZJ1Kf7Y",
        "ProdApiStack-OrdersQueue5E2F9A01-1HW2Q8VZQ0ABC",
        "MyStackMyBucketTableFunctionHandler9C1D2E3F",
    ],
)
def test_a_real_cdk_arn_is_kept_under_the_positive_shape(resource: str) -> None:
    assert _rp._CDK_RESOURCE_NAME_RE.fullmatch(resource)
    arn = f"arn:aws:sns:us-west-2:123456789012:{resource}"
    assert redact_credentials(arn) == (arn, [])


def test_a_tail_past_the_cdk_name_cap_stays() -> None:
    """A tail longer than any generated name is still an ARN's, and an ARN stays."""
    resource = "My" + "Stack" * 60 + "A1B2C3D4"
    arn = f"arn:aws:sns:us-west-2:123456789012:{resource}"
    assert redact_credentials(arn) == (arn, [])
