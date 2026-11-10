"""A streamed Outlook link that a delta cut is saved whole; a secret split the same way is not.

Each streamed delta is redacted on its own, so a delta that holds only part of an
Outlook item id sees a key-shaped run and removes it. The segment flush then
redacts the whole text, where the link is intact and allowed. Without the link
exemption it reads that removed piece as a leak and withholds the entire reply.
"""

from __future__ import annotations

import pytest

from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.state import _ChatSlot

# The canonical AWS documentation example secret key, never a live credential,
# and the same key without its `/`, so it fits an id or a field whole.
KEY = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
SEGMENT_KEY = "wJalrXUtnFEMIzK7MDENGqbPxRfiCYEXAMPLEKEY"
# A real-shaped Outlook item id (see test_document_link_redaction.OUTLOOK_ID).
OUTLOOK_ID = (
    "AAMkAGVmMDEzMTM4LTZmYWUtNDdkNC1hMDZiLTU1OGY5OTZhYmY4OABGAAAAAAAiQ8W967B7TKBjgx9rVEURBwAi"
    "IsqMbYjsT5e%2BT7KzowPTAAAAAAEMAAAiIsqMbYjsT5e%2FT7KzowPTAAAYbvZyAAA%3D"
)
WEB_LINK = (
    f"https://outlook.office365.com/owa/?ItemID={OUTLOOK_ID}&exvsurl=1&viewmodel=ReadMessageItem"
)
PATH_LINK = f"https://outlook.office.com/mail/drafts/id/{OUTLOOK_ID}"


def _flush(text: str, cuts: list[int]) -> str:
    """Stream *text* cut at *cuts* through the per-delta removers, then flush it."""
    slot = _ChatSlot("outlook-flush")
    shown = ""
    for start, end in zip([0, *cuts], [*cuts, len(text)]):
        piece, removed = chat_runner._redact_delta(text[start:end])
        chat_runner._accumulate_segment_raw(slot, text[start:end], piece, removed)
        shown += piece
    return chat_runner._redact_segment(slot, shown)[0]


def _every(size: int, text: str) -> list[int]:
    return list(range(size, len(text), size))


def _cuts(name: str, text: str, link: str) -> list[int]:
    start = text.index(link)
    if name == "mid-id":
        return [text.index(OUTLOOK_ID) + 60]
    if name == "before-fields":
        return [start + link.index("&exvsurl")]
    return _every(int(name), text)


SPLITS = ["4", "8", "16", "32", "64", "mid-id"]


@pytest.mark.parametrize("split", SPLITS + ["before-fields"])
def test_a_streamed_web_link_is_saved_whole(split: str) -> None:
    text = f"Draft: {WEB_LINK} is ready."
    assert _flush(text, _cuts(split, text, WEB_LINK)) == text


@pytest.mark.parametrize("split", SPLITS)
def test_a_streamed_path_link_is_saved_whole(split: str) -> None:
    text = f"Draft: {PATH_LINK} is ready."
    assert _flush(text, _cuts(split, text, PATH_LINK)) == text


def test_the_unsplit_links_are_saved_whole() -> None:
    text = f"See {WEB_LINK} and {PATH_LINK} now."
    assert _flush(text, []) == text


def _hidden(saved: str) -> bool:
    return KEY not in saved and SEGMENT_KEY not in saved


@pytest.mark.parametrize("size", [4, 8, 16, 32, 64])
@pytest.mark.parametrize(
    "text",
    [
        # A key on its own, and in an allowed-looking link in place of the id.
        f"Here it is: {KEY} done.",
        f"Draft: https://outlook.office365.com/owa/?ItemID={SEGMENT_KEY}"
        "&exvsurl=1&viewmodel=ReadMessageItem done.",
        f"Draft: https://outlook.office.com/mail/drafts/id/{SEGMENT_KEY} done.",
        # A key glued right after an allowed link, or after a sentence `.`.
        f"Draft: {WEB_LINK}{SEGMENT_KEY} done.",
        f"Draft: {PATH_LINK}{SEGMENT_KEY} done.",
        f"Draft: {WEB_LINK}.{SEGMENT_KEY} done.",
        # A key right after an allowed link, as its own word.
        f"Draft: {WEB_LINK} {KEY} done.",
        f"Draft: {PATH_LINK} {KEY} done.",
    ],
)
def test_a_split_secret_in_or_near_an_allowed_link_stays_hidden(text: str, size: int) -> None:
    saved = _flush(text, _every(size, text))
    assert _hidden(saved), saved


def test_the_reply_is_still_withheld_when_a_removed_value_shows_outside_a_link() -> None:
    """A removed value counts unless every occurrence lies wholly inside an allowed link."""
    kept = chat_runner._kept_removed_values
    saved = f"{WEB_LINK} {SEGMENT_KEY}"
    links = [(0, len(WEB_LINK))]
    # Wholly inside the link: not a value coming back.
    assert kept(saved, "", [WEB_LINK[:120]], links) == []
    # Outside the link: counted.
    assert kept(saved, "", [SEGMENT_KEY], links) == [SEGMENT_KEY]
    # One occurrence inside and one outside: counted.
    twice = f"{WEB_LINK} {WEB_LINK[:120]}"
    assert kept(twice, "", [WEB_LINK[:120]], links) == [WEB_LINK[:120]]
    # Straddling the link's end: counted.
    straddle = WEB_LINK[-20:] + " x"
    assert kept(WEB_LINK + " x", "", [straddle], links) == [straddle]
    # Straddling the link's start: counted.
    lead = "x " + WEB_LINK[:40]
    assert kept("x " + WEB_LINK, "", [lead], [(2, 2 + len(WEB_LINK))]) == [lead]
    # No allowed links at all: judged exactly as before.
    assert kept(saved, "", [WEB_LINK[:120]], []) == [WEB_LINK[:120]]


def test_a_non_outlook_chat_link_gets_no_exemption() -> None:
    # The message stamp is assembled so no literal names a real message.
    stamp = "p" + "1721434710877893"
    text = f"see https://app.slack.com/client/E6MI0YHZ0NA/CRTBNLZGCJN/{stamp} ok"
    assert chat_runner._outlook_link_spans(text) == []


def test_a_key_split_after_a_kept_link_is_masked_and_the_link_kept() -> None:
    """The exemption covers the link only: a key split after it is still masked."""
    text = f"Draft: {WEB_LINK} {SEGMENT_KEY} done."
    saved = _flush(text, [text.index(SEGMENT_KEY) + 20])
    assert saved == f"Draft: {WEB_LINK} [REDACTED: credential] done."


class _CountingSpans(list):
    """A span list that counts every span read, by index or by iteration."""

    reads = 0

    def __getitem__(self, index):  # type: ignore[override]
        type(self).reads += 1
        return super().__getitem__(index)

    def __iter__(self):  # type: ignore[override]
        for item in super().__iter__():
            type(self).reads += 1
            yield item


def test_a_reply_repeating_a_link_costs_occurrences_plus_spans_not_their_product() -> None:
    """Checking every occurrence against every link would stall the event loop."""
    copies = 2000
    text = " ".join([WEB_LINK] * copies)
    spans = _CountingSpans(chat_runner._outlook_link_spans(text))
    assert len(spans) == copies
    _CountingSpans.reads = 0
    assert chat_runner._only_inside(text, WEB_LINK[:120], spans)
    assert _CountingSpans.reads <= 4 * (copies + len(spans))
    # The same bound holds when the last occurrence falls outside every link.
    tail = f"{text} {WEB_LINK[:120]}"
    _CountingSpans.reads = 0
    assert not chat_runner._only_inside(tail, WEB_LINK[:120], spans)
    assert _CountingSpans.reads <= 4 * (copies + 1 + len(spans))
