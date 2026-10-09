"""The ``goal-board`` built-in: reduced motion, and needs-you rows that fill the chat box.

The page pops cards in, shimmers a live item, blinks its dot, grows the state bar and
glows the needs-you band. A reader who asked the OS for less motion must get none of
that, so the one ``prefers-reduced-motion`` block has to reach every element and both
pseudo-elements (the shimmer is an ``::after``) and stop animations AND transitions.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "src/kiro_crew/dashboard_templates/builtin/goal-board/template.html"


def _style() -> str:
    return TEMPLATE.read_text(encoding="utf-8").split("<style>")[1].split("</style>")[0]


def _script() -> str:
    return TEMPLATE.read_text(encoding="utf-8").split("<script>")[1].split("</script>")[0]


def _function(name: str) -> str:
    return _script().split(f"function {name}(")[1].split("\n      function ")[0]


def test_reduced_motion_stops_every_animation_and_transition() -> None:
    m = re.search(
        r"@media\s*\(prefers-reduced-motion:\s*reduce\)\s*\{(.*?)\}\s*\}", _style(), re.DOTALL
    )
    assert m, "no prefers-reduced-motion block"
    rule = m.group(1)
    for selector in (".board *", ".board *::before", ".board *::after"):
        assert selector in rule, selector
    assert "animation: none !important" in rule
    assert "transition: none !important" in rule


def test_every_animation_names_a_keyframes_this_page_defines() -> None:
    """A typo'd name animates nothing and slips past the block above unnoticed."""
    style = _style()
    used = set(re.findall(r"animation:\s*(gb-[\w-]+)", style))
    defined = set(re.findall(r"@keyframes\s+(gb-[\w-]+)", style))
    assert used, "the page animates nothing"
    assert used == defined


def test_a_needs_you_button_fills_the_chat_box_and_never_sends() -> None:
    """The host bridge office and project-report use: the page posts ``act`` and the
    host only fills the composer. Any other message type would be a new channel."""
    script = _script()
    assert sorted(re.findall(r"type:\s*'([\w:-]+)'", script)) == [
        "kirocrew-dashboard:act",
        "kirocrew-dashboard:open",
    ]
    assert script.count("postMessage(") == 2
    assert "postMessage(" in _function("act")
    assert "act(say)" in _function("button")
    for banned in ("send", "submit"):
        assert not re.search(rf"type:\s*'[^']*{banned}", script)


def test_a_row_without_an_asks_entry_still_gets_its_reply_button() -> None:
    """Reply is appended before, and regardless of, the agentic options lookup, so a
    board the conductor never wrote ``asks`` for is still actionable."""
    body = _function("actions")
    reply = body.index("button(i18n('reply'), 'reply', ref)")
    lookup = body.index("optionsFor(id)")
    assert reply < lookup
    assert "if" not in body[:reply].split("row.className")[1]
    # An absent entry, a non-object field and a missing options list all read as [].
    lookup_body = _function("optionsFor")
    assert "hasOwnProperty.call(a, id)" in lookup_body
    assert "Array.isArray(e.options) ? e.options : []" in lookup_body


#: The host's own allowlist (website/src/hooks/useFrameOpenLink.ts PR_URL_RE).
PR_URL = r"/^https:\/\/github\.com\/[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+\/pull\/[0-9]+\/?$/"


def test_a_pr_chip_asks_the_host_to_open_it_and_never_opens_it_itself() -> None:
    """The frame has no allow-popups: a PR opens only through the host's bridge."""
    script = _script()
    body = _function("openPr")
    assert "parent.postMessage({ type: 'kirocrew-dashboard:open', url: url }, '*')" in body
    assert PR_URL in script
    for banned in ("window.open", "href", "<a ", "target"):
        assert banned not in script, banned


def test_a_pr_link_is_built_from_the_items_own_repo_and_checked_before_drawing() -> None:
    body = _function("prUrl")
    assert "it.acceptance" in body and "acc.repo" in body
    assert "'https://github.com/' + (text(acc.repo) || '') + '/pull/' + pr" in body
    assert "PR_URL.test(url) ? url : null" in body
    draw = _function("drawItems")
    # No URL, no link: the same chip as before, as text.
    assert "} else if (pr) chips.appendChild(chip(" in draw


def test_live_keys_off_the_workers_status_not_a_masked_field() -> None:
    """``member_dashboard._page_safe`` drops ``worker_session_key`` before any field
    reaches a page, so a ``live`` test on it would never be true."""
    from kiro_crew.dashboard.handlers.member_dashboard import _MASKED_VALUE_KEYS

    body = _function("live")
    for masked in _MASKED_VALUE_KEYS:
        assert masked not in _script(), masked
    assert "it.status === 'progress'" in body


def test_no_animation_runs_forever_except_the_live_markers() -> None:
    """The band glows a few times, not for as long as the tab is open. Only the
    shimmer and the dot loop, and only on an item still moving."""
    infinite = re.findall(r"animation:\s*(gb-[\w-]+)[^;]*\binfinite\b", _style())
    assert sorted(infinite) == ["gb-blink", "gb-shimmer"]


def test_the_live_shimmer_is_not_clipped_by_the_card() -> None:
    """``.item`` clips with ``overflow: hidden`` at its padding box, so a shimmer set on
    the 3px top border (a negative ``top``) is drawn and never seen."""
    rule = re.search(r"\.item\.live::after\s*\{([^}]*)\}", _style())
    assert rule, "no shimmer rule"
    top = re.search(r"\btop:\s*(-?[\d.]+)", rule.group(1))
    assert top and float(top.group(1)) >= 0


def test_every_needs_you_button_says_it_drafts_rather_than_sends() -> None:
    script = _script()
    assert "reply: '\\u270e Write a reply'" in script
    body = _function("actions")
    assert "i18n('option', { o: clip(opts[i], 40) })" in body
    assert "ob.title = i18n('option_title')" in body


def test_items_that_need_the_reader_get_their_own_segment() -> None:
    """The bar's "open" must not swallow a task that waits on the reader."""
    assert "['needs_you', 'needs you', 'var(--warn, #ffd230)']" in _script()
    assert "needsYou(list[i]) ? 'needs_you'" in _function("drawStates")


def test_a_worker_status_reads_in_the_readers_language() -> None:
    """The band and the chip both say the status through the page's own table."""
    script = _script()
    assert "statusLabel(ask[j].status)" in script
    assert "worker_status', { v: statusLabel(status) }" in script
    assert "' \\u2014 ' + ask[j].status" not in script
