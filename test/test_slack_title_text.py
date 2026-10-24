"""A Slack-started session's title reads as plain text, never as raw Slack mrkdwn.

The session catalog titled a conversation with no title of its own from its first
user message, cut to 80 characters, and a Slack message is stored as Slack sent
it. The sidebar showed ``how is <https://github.com/kirodotdev/KiroCrew/issues/
21964|gi…`` and the cut-open form was persisted as the title, so it stayed after
the message scrolled away. ``mrkdwn_title`` reads each construct, the catalog
converts first and cuts second, and a stored title is read the same way on every
listing, which cleans the ones an earlier build persisted.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew.history import ConversationLog
from kiro_crew.messaging import sessions_view as sv
from kiro_crew.mrkdwn_title import is_slack_session_key, slack_title_text

_GH = "https://github.com/kirodotdev/KiroCrew/issues/21964"
#: Kyle's own message from the test workspace, and the title it was stored under.
_RAW = f"how is <{_GH}|github.com/kirodotdev/KiroCrew/issues/21964> looking"
_STORED = f"how is <{_GH}|github.com/kirodotde"
_SLACK = "slack_1792501715.706339"


class TestEachForm:
    @pytest.mark.parametrize(
        "raw, plain",
        [
            # <url|label> -> label
            ("see <https://example.com/a_b|the doc>", "see the doc"),
            # Slack's auto-link labels a URL with itself: read like a bare URL.
            (_RAW, "how is kirodotdev/KiroCrew#21964 looking"),
            # bare <url>: a GitHub issue or PR is owner/repo#N ...
            ("<https://github.com/o/r/issues/12>", "o/r#12"),
            ("<https://github.com/o/r/pull/7/files>", "o/r#7"),
            ("<https://www.github.com/o/r/pull/7>", "o/r#7"),
            # ... anything else host + a short path
            ("<https://example.com/docs/guide>", "example.com/docs/guide"),
            ("<https://example.com/a/b/c/d?x=1>", "example.com/a/b/…"),
            ("<https://example.com>", "example.com"),
            ("<https://github.com/o/r>", "github.com/o/r"),
            ("<mailto:a@example.com|a@example.com>", "a@example.com"),
            # channel refs
            ("in <#C123|general>", "in #general"),
            ("in <#C123>", "in #channel"),
            # mentions: a label when Slack sent one, else never a raw id
            ("ask <@U123ABC>", "ask @someone"),
            ("ask <@U123ABC|kyle>", "ask @kyle"),
            ("ask <@W9>", "ask @someone"),
            # broadcasts and other specials
            ("<!here> deploy", "@here deploy"),
            ("<!channel> <!everyone>", "@channel @everyone"),
            ("<!subteam^S1|@devs> ping", "@devs ping"),
            ("<!subteam^S1> ping", "@team ping"),
            ("on <!date^1392734382^{date}|Feb 18>", "on Feb 18"),
            # entities, unescaped after the brackets are read
            ("a &amp; b &lt;tag&gt;", "a & b <tag>"),
            # ... unless the decoded text would be read as a link next time
            ("&lt;https://x.com&gt;", "&lt;https://x.com&gt;"),
            # anything else in brackets is left as written
            ("x <not a link> y", "x <not a link> y"),
            # whitespace folds
            ("two\nlines  here", "two lines here"),
            # a closed label that merely starts the URL is the person's label
            ("<https://github.com/o/r|github>", "github"),
            ("<https://github.com/o/r/issues/5|h>", "h"),
            # an empty label reads like a bare URL
            ("<https://github.com/o/r/issues/5|>", "o/r#5"),
            # unknown specials and non-Slack brackets stay as written
            ("<!-- note --> keep", "<!-- note --> keep"),
            ("<!foo> x", "<!foo> x"),
        ],
    )
    def test_reads_like_slack_shows_it(self, raw: str, plain: str) -> None:
        assert slack_title_text(raw) == plain

    @pytest.mark.parametrize(
        "raw, plain",
        [
            # A title made of only a link.
            (f"<{_GH}>", "kirodotdev/KiroCrew#21964"),
            ("<https://example.com/spec|Q3 spec>", "Q3 spec"),
        ],
    )
    def test_a_title_that_is_only_a_link(self, raw: str, plain: str) -> None:
        assert slack_title_text(raw) == plain

    @pytest.mark.parametrize(
        "stored, plain",
        [
            # What an earlier build persisted: cut inside the auto-link label.
            (_STORED, "how is kirodotdev/KiroCrew#21964"),
            # Cut inside a real label, and inside the URL itself.
            ("read <https://example.com/x|the design do", "read the design do…"),
            (
                "help with this one <https://github.com/kirodotdev/KiroCrew/iss",
                "help with this one github.com/kirodotdev/KiroCrew/…",
            ),
            ("ping <@U12", "ping @someone"),
            # Cut right after the bar: the URL is whole, the label never began.
            ("see <https://github.com/o/r/issues/5|", "see o/r#5"),
        ],
    )
    def test_a_construct_cut_open_by_a_truncation(self, stored: str, plain: str) -> None:
        assert slack_title_text(stored) == plain

    def test_plain_text_is_unchanged(self) -> None:
        assert slack_title_text("Fix KiroCrew issue 21964") == "Fix KiroCrew issue 21964"
        assert slack_title_text("") == ""

    def test_slack_keys(self) -> None:
        from kiro_crew.messaging.link import is_legacy_slack_key

        assert is_slack_session_key("slack_1792501715.706339")
        assert is_slack_session_key("slack:1792501715.706339")
        assert is_slack_session_key("slack:D0123ABC")
        # A legacy transcript is named by its bare thread ts, in the same shape
        # ``messaging.link`` recognises.
        assert is_slack_session_key("1712793600.123456")
        for key in ("1712793600.123456", "1712793600", "dashboard_1712793600.1"):
            assert is_slack_session_key(key) == (
                is_legacy_slack_key(key) or key.startswith(("slack:", "slack_"))
            )
        assert not is_slack_session_key("dashboard:chat-1")
        assert not is_slack_session_key("telegram:kirocrew:dm:42")
        assert not is_slack_session_key("unified:kirocrew")


# ── Converting a converted title changes nothing ─────────────────────────────
#
# A converted title is shown, saved back as the session's title by the tab, and
# converted again on the next listing, so the converter must be idempotent.

_CORPUS = [
    _RAW,
    _STORED,
    "use &lt;!here&gt; ok",
    "&lt;!-- note --&gt; keep",
    "a &amp;lt; b",
    "a &amp;amp; b &amp; c",
    "x &lt;https://foo.example/bar",
    "&lt;https://x.com|y&gt;",
    "&lt;#C1|general&gt; and &lt;@U1&gt;",
    "<https://example.com/x|a &amp; b>",
    "<https://x.com|&lt;!here&gt;>",
    "two\n<https://x.com\n> lines",
    "tail &lt;",
    "trailing <",
    "<<https://x.com>>",
    "a > b < c",
    "<!here>&lt;!channel&gt;",
    "café <https://例え.jp/パス|リンク> 🚀",
    "see <https://github.com/o/r/issues/5|",
    "<https://user:pw@host.example:8443/a/b/c|>",
]


@pytest.mark.parametrize("raw", _CORPUS)
def test_converting_twice_changes_nothing(raw: str) -> None:
    once = slack_title_text(raw)
    assert slack_title_text(once) == once


@pytest.mark.parametrize(
    "raw, plain",
    [
        # A person typed these as text in Slack; they stay text through any
        # number of passes rather than turning into a broadcast or vanishing.
        ("use &lt;!here&gt; ok", "use &lt;!here&gt; ok"),
        ("&lt;!-- note --&gt; keep", "<!-- note --> keep"),
        ("a &amp;lt; b", "a &amp;lt; b"),
    ],
)
def test_escaped_text_never_becomes_a_construct(raw: str, plain: str) -> None:
    assert slack_title_text(raw) == plain


def test_a_saved_converted_title_relists_unchanged(tmp_path: Path) -> None:
    _transcript(tmp_path, _SLACK, _RAW, title=_STORED)
    shown = _titles(tmp_path)[_SLACK]
    # The tab writes what it shows back as the stored title.
    _transcript(tmp_path, _SLACK, _RAW, title=shown)
    assert _titles(tmp_path)[_SLACK] == shown


def test_a_legacy_bare_ts_transcript_is_read_as_slack(tmp_path: Path) -> None:
    _transcript(tmp_path, "1712793600.123456", _RAW)
    [title] = [t for k, t in _titles(tmp_path).items() if "1712793600" in k]
    assert title == "how is kirodotdev/KiroCrew#21964 looking"


# ── The open tab: restore, resume, and the titler's own fallback ─────────────


def test_restoring_a_slack_tab_reads_its_stored_title_plain() -> None:
    from types import SimpleNamespace

    from kiro_crew.dashboard.slot_persistence.metadata_codec import _rehydrate_slot_title

    slack_tab = SimpleNamespace(key=_SLACK)
    _rehydrate_slot_title(slack_tab, _STORED, titled=True, metadata={})
    assert slack_tab.title == "how is kirodotdev/KiroCrew#21964"
    dashboard_tab = SimpleNamespace(key="chat-1")
    _rehydrate_slot_title(dashboard_tab, "a &amp; b", titled=True, metadata={})
    assert dashboard_tab.title == "a &amp; b"


def test_the_titlers_fallback_reads_a_slack_tabs_message_plain() -> None:
    from kiro_crew.dashboard.chat_title import (
        _fallback_title_from_messages,
        _is_low_signal_title,
    )

    rows = [{"role": "user", "content": _RAW}]
    assert _fallback_title_from_messages(rows, slack=True) == (
        "how is kirodotdev/KiroCrew#21964 looking"
    )
    # The low-signal check compares against the same reading.
    assert _is_low_signal_title("how is kirodotdev/KiroCrew#21964 looking", rows, slack=True)
    # A dashboard tab's own words are not Slack markup.
    assert "<https://" in _fallback_title_from_messages(
        [{"role": "user", "content": "x <https://a.b|c>"}]
    )


@pytest.mark.parametrize("key, slack", [(_SLACK, True), ("chat-1", False)])
def test_the_titler_knows_a_slack_tab(key: str, slack: bool) -> None:
    from types import SimpleNamespace

    from kiro_crew.dashboard.chat_title import _is_slack_slot

    assert _is_slack_slot(SimpleNamespace(key=key)) is slack


# ── Through the session catalog ──────────────────────────────────────────────


def _transcript(base: Path, stem: str, first_user: str, title: str | None = None) -> None:
    meta: dict = {"_type": "metadata", "created_at": "2026-10-20T13:08:40+00:00"}
    if title is not None:
        meta["title"] = title
    rows = [meta, {"role": "user", "content": first_user}, {"role": "assistant", "content": "ok"}]
    (base / f"{stem}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))


def _titles(base: Path) -> dict[str, str]:
    return {s["key"]: s["title"] for s in ConversationLog(base_dir=base).list_sessions()}


#: The link starts inside the first 80 characters and ends past them.
_LONG = "the deploy for payments fails again, see " + f"<{_GH}>"


def test_a_slack_first_message_title_is_plain(tmp_path: Path) -> None:
    _transcript(tmp_path, _SLACK, _RAW)
    assert _titles(tmp_path)[_SLACK] == "how is kirodotdev/KiroCrew#21964 looking"


def test_convert_first_then_cut(tmp_path: Path) -> None:
    # Raw, the link straddles the cut; converted, it fits.
    assert _LONG.index("<") < 80 < len(_LONG) and len(slack_title_text(_LONG)) < 80
    _transcript(tmp_path, _SLACK, _LONG + " " + "and more words " * 6)
    title = _titles(tmp_path)[_SLACK]
    assert title == slack_title_text(_LONG + " " + "and more words " * 6)[:80]
    assert "kirodotdev/KiroCrew#21964" in title
    assert "<" not in title and "|" not in title


def test_the_cached_message_path_is_plain_too(tmp_path: Path) -> None:
    _transcript(tmp_path, _SLACK, _RAW)
    log = ConversationLog(base_dir=tmp_path)
    log.read_messages(_SLACK)  # fills the message cache the catalog reads first
    titles = {s["key"]: s["title"] for s in log.list_sessions()}
    assert titles[_SLACK] == "how is kirodotdev/KiroCrew#21964 looking"


def test_a_stored_raw_title_lists_plain_and_the_file_is_untouched(tmp_path: Path) -> None:
    _transcript(tmp_path, _SLACK, _RAW, title=_STORED)
    before = (tmp_path / f"{_SLACK}.jsonl").read_bytes()
    assert _titles(tmp_path)[_SLACK] == "how is kirodotdev/KiroCrew#21964"
    assert (tmp_path / f"{_SLACK}.jsonl").read_bytes() == before


def test_a_dashboard_session_keeps_its_own_words(tmp_path: Path) -> None:
    # Only a Slack conversation's text is mrkdwn: a person may type "&amp;" or
    # angle brackets in the dashboard and mean them.
    _transcript(tmp_path, "dashboard_chat-1", "a &amp; b <https://x.com|y>")
    [title] = [t for k, t in _titles(tmp_path).items() if k.startswith("dashboard")]
    assert title == "a &amp; b <https://x.com|y>"


def test_the_sessions_view_reads_a_stored_raw_title_plain(tmp_path: Path) -> None:
    _transcript(tmp_path, _SLACK, _RAW, title=_STORED)
    [row] = sv._collect_recent_sessions(None, limit=5, sessions_dir=tmp_path, with_messages=False)
    assert row["title"] == "how is kirodotdev/KiroCrew#21964"


# ── Round two: settled output, cut labels, imported threads ──────────────────


def test_converting_twice_changes_nothing_for_any_input() -> None:
    # Seeded, so a failure reproduces: every character that means something to
    # the converter, plus the URL and id shapes it reads.
    import random

    rng = random.Random(22254)
    atoms = ["<", ">", "|", "&", "amp;", "lt;", "gt;", "!", "here", "#C1", "@U1",
             "https://", "github.com/o/r/issues/5", "x.com", "/", " ", "\n", "…", "a", "é"]
    for _ in range(3000):
        raw = "".join(rng.choice(atoms) for _ in range(rng.randint(1, 14)))
        once = slack_title_text(raw)
        assert slack_title_text(once) == once, raw


@pytest.mark.parametrize(
    "stored, plain",
    [
        # An earlier dashboard fallback already ended the cut label with "…".
        ("see <https://github.com/o/r/issues/1|github.com/o/r/is…", "see o/r#1"),
        # A short cut-off word is the person's label, not the URL's start.
        ("ask <https://help.x.com|help", "ask help…"),
    ],
)
def test_cut_labels_read_as_a_person_wrote_them(stored: str, plain: str) -> None:
    assert slack_title_text(stored) == plain


def test_a_row_copied_from_a_slack_thread_titles_as_plain_text() -> None:
    from kiro_crew.dashboard.chat_title import _fallback_title_from_messages
    from kiro_crew.mrkdwn_title import SLACK_TEXT_META_KEY

    copied = [{"role": "user", "content": _RAW, "meta": {SLACK_TEXT_META_KEY: True}}]
    assert _fallback_title_from_messages(copied) == "how is kirodotdev/KiroCrew#21964 looking"


def test_the_slack_text_mark_is_the_gateways_alone() -> None:
    # A client-supplied row meta cannot claim it (stripped like the other
    # host-written provenance keys).
    from kiro_crew.dashboard.chat_handlers import RESERVED_ROW_META_KEYS
    from kiro_crew.mrkdwn_title import SLACK_TEXT_META_KEY

    assert SLACK_TEXT_META_KEY in RESERVED_ROW_META_KEYS


@pytest.mark.asyncio
async def test_link_to_dashboard_marks_the_copied_user_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew.dashboard import chat_persistence
    from kiro_crew.mrkdwn_title import SLACK_TEXT_META_KEY
    from kiro_crew.slack.interactions_options import _import_thread_to_slot

    monkeypatch.setattr(chat_persistence, "save_slot_off_loop", AsyncMock())
    slot = MagicMock(key="chat-7")
    ds = MagicMock(get_linked_slot=MagicMock(return_value=None), _self_bot_id="B1")
    ds.get_or_create_slot.return_value = slot
    slack = MagicMock()
    slack.fetch_thread_replies = AsyncMock(
        return_value=[{"text": _RAW, "user": "U1"}, {"text": "on it", "bot_id": "B1"}]
    )
    assert await _import_thread_to_slot(slack, ds, "C1", "1792501715.706339") is slot
    (user_call, bot_call) = slot.append.call_args_list
    assert user_call.kwargs["meta"] == {SLACK_TEXT_META_KEY: True}
    assert bot_call.kwargs["meta"] is None
