"""Masked text is a fixed point of every redaction surface, and display keeps markup.

For every secret and benign fixture and every snippet of the independent verifier's
corpus (:mod:`redaction_verifier_corpus`), redacting the redacted text changes no
byte: on the batch plan, on each channel's egress parts, on the display floor and
over repeated transcript saves. The verifier's webhook, inline-code, display-cost and
stream-holdback findings are pinned below. Every value is synthetic.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from redaction_structured_cases import benign_cases, secret_cases
from redaction_verifier_corpus import AMBIGUOUS, BENIGN, LEAKS

from kiro_crew import security
from kiro_crew.dashboard.slot_persistence.message_entries import _build_message_entry_uncached
from kiro_crew.discord.transport import DISCORD_CAPABILITIES
from kiro_crew.messaging.display_safety import redact_for_display
from kiro_crew.messaging.egress import channel_egress_parts
from kiro_crew.messaging.renderer import _default_redactor, display_safe_for
from kiro_crew.security.redaction_structured import engine
from kiro_crew.security.redaction_structured.encoded import TEXT_HOLD_MAX
from kiro_crew.security.text_redaction import _STREAM_HOLDBACK_MAX
from kiro_crew.slack.transport import SLACK_CAPABILITIES
from kiro_crew.telegram.transport import TELEGRAM_CAPABILITIES
from kiro_crew.testing.synthetic_credentials import github_classic

TAG = security.REDACTED_CREDENTIAL_TAG
_CAPS = {
    "slack": SLACK_CAPABILITIES,
    "telegram": TELEGRAM_CAPABILITIES,
    "discord": DISCORD_CAPABILITIES,
}
_PLATFORMS = sorted(_CAPS)

_TEXTS: list[tuple[str, str]] = (
    [(f"secret:{name}", text) for name, text, _expected, _rules in secret_cases()]
    + [(f"benign:{name}", text) for name, text in benign_cases()]
    + [(iid, text) for iid, _cat, text in BENIGN]
    + [(iid, text) for iid, _cat, text, _values in LEAKS]
    + [(iid, text) for iid, _cat, text, _note in AMBIGUOUS]
)
_LEAK = {cat: (text, values) for _iid, cat, text, values in LEAKS}


def _moved(surface: Callable[[str], str]) -> list[tuple[str, str, str]]:
    """``(id, once, twice)`` for each text whose surface output moves when read again."""
    moved = []
    for name, text in _TEXTS:
        once = surface(text)
        twice = surface(once)
        if twice != once:
            moved.append((name, once[:200], twice[:200]))
    return moved


def _saved(text: str) -> str:
    entry = _build_message_entry_uncached({"role": "assistant", "content": text})
    assert entry is not None
    return entry["content"]


# ─── Fixed point, every text, every surface ───


def test_the_batch_plan_is_a_fixed_point() -> None:
    assert _moved(lambda text: security.redact_credentials(text)[0]) == []
    assert _moved(security.redact) == []


def test_the_display_floor_is_a_fixed_point() -> None:
    assert _moved(lambda text: redact_for_display(text, _default_redactor)[0]) == []


def test_a_saved_transcript_is_a_fixed_point_of_saving() -> None:
    assert _moved(_saved) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", _PLATFORMS)
async def test_each_channel_egress_part_is_a_fixed_point(platform: str) -> None:
    caps = _CAPS[platform]
    moved = []
    for name, text in _TEXTS:
        for part in await channel_egress_parts(text, caps):
            again = await channel_egress_parts(part, caps)
            if again != [part]:
                moved.append((name, part[:200], again))
    assert moved == []


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", _PLATFORMS)
async def test_channel_egress_keeps_every_underscore_the_batch_plan_keeps(platform: str) -> None:
    """Display markup is read for the scan, never stripped from a text with no
    markup-split key: a label like ``SLACK_WEBHOOK_URL`` reaches the channel whole."""
    lost = []
    for name, text in _TEXTS:
        batch = security.redact_credentials(text)[0]
        shown = "".join(await channel_egress_parts(text, _CAPS[platform]))
        if shown.count("_") < batch.count("_"):
            lost.append((name, shown[:200]))
    assert lost == []


# ─── Webhooks: only the secret path segment, once, everywhere ───


@pytest.mark.parametrize("category", ["slack-webhook", "discord-webhook"])
def test_a_webhook_masks_its_secret_segment_once(category: str) -> None:
    text, (secret,) = _LEAK[category]
    expected = text.replace(secret, TAG)
    once = security.redact_credentials(text)[0]
    assert once == expected
    assert security.redact_credentials(once)[0] == expected
    assert redact_for_display(once, _default_redactor) == (expected, False)


@pytest.mark.parametrize("category", ["slack-webhook", "discord-webhook"])
def test_saving_a_webhook_again_and_again_adds_nothing(category: str) -> None:
    text, (secret,) = _LEAK[category]
    expected = text.replace(secret, TAG)
    content = text
    for _ in range(4):
        content = _saved(content)
        assert content == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", _PLATFORMS)
@pytest.mark.parametrize("category", ["slack-webhook", "discord-webhook"])
async def test_a_channel_masks_only_the_webhook_secret(platform: str, category: str) -> None:
    text, (secret,) = _LEAK[category]
    expected = text.replace(secret, TAG)
    assert await channel_egress_parts(text, _CAPS[platform]) == [expected]
    assert display_safe_for(text, _CAPS[platform]) == expected


def test_a_tag_in_a_url_is_never_read_as_its_value() -> None:
    """A URL ends at a tag's blank; neither the path-secret reader nor the parameter
    reader takes the tag's head for a value, and the engine drops a Secret hit that
    reads only a tag's own text."""
    assert engine.PLAN_TAGS == security.CREDENTIAL_REDACTION_TAGS
    for tag in engine.PLAN_TAGS:
        for text in (
            f"https://hooks.slack.com/services/T0ABCDEFGH/B0ABCDEFGHI/{tag} next",
            f"https://discord.com/api/webhooks/123456789012345678/{tag}",
            f"https://auth.example.test/cb?state=abc&client_secret={tag}&x=1",
            f"https://example.test/?data={tag}",
        ):
            assert security.redact_credentials(text)[0] == text
            assert [span for span in engine.redact(text)[1] if span[3] == "secret"] == []


# ─── Display keeps inline code when no key is shown ───

#: The verifier's documentation line: two placeholders, four inline-code spans.
_DOC_LINE = (
    "Set `Authorization: Bearer <token>` on every request, where `<token>` is the value"
    " returned by `POST /oauth/token`. Example:"
    " `curl -H 'Authorization: Bearer YOUR_ACCESS_TOKEN' https://api.example.com/v1/me`"
)
_DOC_FENCE = (
    "Example:\n```bash\n"
    "curl -H 'Authorization: Bearer YOUR_ACCESS_TOKEN' https://api.example.com/v1/me\n"
    "```\ndone"
)


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", _PLATFORMS)
@pytest.mark.parametrize("text", [_DOC_LINE, _DOC_FENCE], ids=["inline-code", "fence"])
async def test_a_placeholder_in_code_keeps_the_markup(platform: str, text: str) -> None:
    assert redact_for_display(text, _default_redactor) == (text, False)
    assert await channel_egress_parts(text, _CAPS[platform]) == [text]
    assert display_safe_for(text, _CAPS[platform]) == text


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", _PLATFORMS)
async def test_a_key_split_by_markup_beside_a_placeholder_is_still_masked(platform: str) -> None:
    """The placeholder keeps its underscores; a key the markup splits still falls back."""
    key = github_classic()
    split = f"{key[:8]}**{key[8:]}**"
    shown = "".join(await channel_egress_parts(f"{_DOC_LINE} and {split}", _CAPS[platform]))
    for mark in "*_`":
        shown = shown.replace(mark, "")
    assert key.replace("_", "") not in shown and key not in shown


def test_an_underscore_inside_code_is_kept_only_between_word_characters() -> None:
    """A delimiter run inside code still joins: ``**`` and a lone edge ``_`` drop."""
    key = github_classic()
    for text in (f"`{key[:8]}**{key[8:]}**`", f"`x _{key}_ y`"):
        safe, changed = redact_for_display(text, _default_redactor)
        assert changed and key not in safe


# ─── Display cost: each distinct form is scanned once ───


def _counting() -> tuple[list[str], Callable[[str], str]]:
    calls: list[str] = []

    def redactor(text: str) -> str:
        calls.append(text)
        return _default_redactor(text)

    return calls, redactor


def test_text_without_markup_is_scanned_once() -> None:
    calls, redactor = _counting()
    text = "plain words only, nothing a renderer acts on.\n" * 200
    assert redact_for_display(text, redactor) == (text, False)
    assert calls == [text]


@pytest.mark.parametrize(
    "text",
    [
        "Run `make build` then **ship** it. A [link](https://example.com/a_b) too.\n" * 60,
        "Run `make build` then **ship** it, _quickly_ ~now~.\n" * 60,
        _DOC_LINE + "\n" + _DOC_FENCE,
    ],
    ids=["with-link", "no-link", "doc"],
)
def test_no_form_of_a_clean_message_is_scanned_twice(text: str) -> None:
    calls, redactor = _counting()
    assert redact_for_display(text, redactor) == (text, False)
    assert len(calls) == len(set(calls))


# ─── Stream holdback ───


def _streamed(text: str, size: int = 40) -> tuple[str, int]:
    """The streamed output and the most raw text it held back after any feed."""
    sr = security.StreamRedactor()
    out, held = [], 0
    for i in range(0, len(text), size):
        out.append(sr.feed(text[i : i + size]))
        held = max(held, len(sr.held))
    out.append(sr.flush())
    return "".join(out), held


_DATA_URIS = [(iid, text) for iid, _cat, text in BENIGN if "iVBORw0KGgo" in text]


@pytest.mark.parametrize(("iid", "text"), _DATA_URIS, ids=[i for i, _ in _DATA_URIS])
def test_an_image_payload_streams_within_the_holdback_floor(iid: str, text: str) -> None:
    """A data URI or bare image run is an Identifier value: it replaces nothing, so the
    stream holds no more of it than the floor, and still writes the batch text."""
    assert len(text) > _STREAM_HOLDBACK_MAX
    out, held = _streamed(text)
    assert held <= _STREAM_HOLDBACK_MAX
    assert out == security.redact(text)


@pytest.mark.parametrize(("iid", "text"), _DATA_URIS, ids=[i for i, _ in _DATA_URIS])
@pytest.mark.parametrize("size", [7, 40])
def test_a_key_after_an_image_payload_is_still_masked(iid: str, text: str, size: int) -> None:
    key = github_classic()
    whole = f"{text}\ntoken {key} end"
    out, held = _streamed(whole, size)
    assert key not in out
    assert out == security.redact(whole)
    assert held <= _STREAM_HOLDBACK_MAX


def test_benign_text_holds_no_more_than_the_floor() -> None:
    """Only an encoded run whose head is text holds more, up to ``TEXT_HOLD_MAX``.

    The batch plan masks such a run whole when its decode holds a credential (a
    kubeconfig's ``client-key-data``), so the stream holds it to its end. The one in
    this corpus is a kubeconfig's ``certificate-authority-data``.
    """
    over = {}
    for iid, _cat, text in BENIGN:
        held = _streamed(text)[1]
        if held > _STREAM_HOLDBACK_MAX:
            over[iid] = held
    assert sorted(over) == ["B068"]
    assert max(over.values()) <= TEXT_HOLD_MAX


@pytest.mark.parametrize("size", [7, 40])
def test_no_value_the_batch_plan_masks_streams_raw(size: int) -> None:
    shown = []
    for iid, _cat, text, values in LEAKS:
        batch = security.redact(text)
        out = _streamed(text, size)[0]
        shown += [(iid, value[:12]) for value in values if value not in batch and value in out]
    assert shown == []
