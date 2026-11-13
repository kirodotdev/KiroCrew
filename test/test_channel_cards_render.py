"""The channel card renderer: redaction, mention defuse, caps and no media.

Every assertion runs through ``render`` itself. The Slack side is checked against
the Slack modules it must agree with (``escape_mrkdwn``, the client seam's
``blocks_request_remote_media``), which this test may import even though the
neutral renderer may not.
"""

from __future__ import annotations

import importlib
import itertools
import logging
import re
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.messaging.cards import (
    SLACK_LIMITS,
    Actions,
    Button,
    Card,
    CardError,
    Context,
    Fit,
    Platform,
    Section,
    Text,
    render,
)
from kiro_crew.messaging.renderer import display_safe_for
from kiro_crew.messaging.transport import TransportCapabilities
from kiro_crew.platform.context import redact_via_context

#: The module, not the ``render`` function the package exports under its name.
render_mod = importlib.import_module("kiro_crew.messaging.cards.render")
LIVE_MENTION = re.compile(r"<[!@#][A-Za-z]")
KEY = "AKIAIOSFODNN7EXAMPLE"
HOSTILE = (
    f"key {KEY} here",
    "<!channel> and <!here> and <@U012ABC>",
    "@everyone look",
    "&amp;#65;&#x4B;&lt;b&gt;",
    "```closing fence``` and `` pairs",
    f"**AKIA**{KEY[4:]}",
    "plain & simple <tag>",
)
NO_MENTIONS = TransportCapabilities(mention_grammars=False)


def _strings(node: Any) -> list[str]:
    """Every text-object string in a Block Kit tree."""
    if isinstance(node, dict):
        out = [node["text"]] if isinstance(node.get("text"), str) else []
        return out + [s for k, v in node.items() if not isinstance(v, str) for s in _strings(v)]
    if isinstance(node, list):
        return [s for v in node for s in _strings(v)]
    return []


def _shown(text: str) -> str:
    """What Slack shows for a mrkdwn string: its three entities decoded."""
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def _card(*runs: str) -> Card:
    return Card(
        body=(
            Section(text=Text(runs)),
            Context(items=(Text(runs),)),
            Actions(buttons=(Button("".join(runs)[:70] or "b", "a1", "v"),)),
        )
    )


def _over(code: str, path: str, card: Card) -> None:
    with pytest.raises(CardError) as err:
        render(card, Platform.SLACK)
    assert (path, code) in [(i.path, i.code) for i in err.value.issues], err.value.issues


class TestRedactionProperty:
    """Every display string is settled: the redaction half of ``display_safe_for``
    finds nothing left to change in what Slack shows, and the mention half has
    already run.

    ``display_safe_for`` itself is not idempotent (it adds a zero-width space
    after every ``@`` each time it runs), so the fixed point is read in two
    parts: ``display_safe_for`` with no mention grammar leaves the shown string
    as it is, and no live mention remains."""

    @pytest.mark.parametrize("text", HOSTILE)
    def test_every_display_string_is_settled(self, text: str) -> None:
        strings = _strings(render(_card(text), Platform.SLACK).blocks)
        assert len(strings) == 3
        for s in strings:
            shown = _shown(s)
            assert display_safe_for(shown, NO_MENTIONS, redactor=redact_via_context) == shown
            assert KEY not in shown
            assert not LIVE_MENTION.search(s), s
            assert all(shown[i + 1 : i + 2] == "\u200b" for i, c in enumerate(shown) if c == "@")

    def test_a_key_split_across_runs_is_redacted(self) -> None:
        for s in _strings(render(_card("AKIAIOSF", "ODNN7EXAMPLE"), Platform.SLACK).blocks):
            assert KEY not in s

    def test_a_broadcast_split_across_runs_is_defused(self) -> None:
        for s in _strings(render(_card("<", "!channel>"), Platform.SLACK).blocks):
            assert not LIVE_MENTION.search(s), s

    def test_markdown_bold_becomes_slack_bold(self) -> None:
        card = Card(body=(Section(text=Text(("a **b** _c_",))),))
        assert render(card, Platform.SLACK).blocks[0]["text"]["text"] == "a *b* _c_"

    @pytest.mark.parametrize(
        "text,want",
        [
            ("⏹  **Stopping…**\nnext", "⏹  *Stopping…*\nnext"),
            ("**a** and **b**", "*a* and *b*"),
            ("****", "****"),
            ("**a\nb**", "**a\nb**"),
            ("**a*b**", "**a*b**"),
            ("**open", "**open"),
            ("***a**", "**a*"),
            ("x ** y", "x ** y"),
        ],
    )
    def test_slack_bold_translates_only_closed_single_line_pairs(self, text, want) -> None:
        assert render_mod._slack_bold(text) == want

    def test_render_holds_no_regex(self) -> None:
        import ast

        tree = ast.parse(Path(render_mod.__file__).read_text(encoding="utf-8"))
        imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        imported |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        assert "re" not in imported

    def test_emit_refuses_a_string_that_shows_a_key(self) -> None:
        p = render_mod._Pass(Platform.SLACK, SLACK_LIMITS)
        p.emit(f"*{KEY[:8]}*{KEY[8:]}", "body[0].text", markup=True)
        assert [(i.path, i.code) for i in p.issues] == [("body[0].text", "unredacted")]


#: ``(name, cap, build(n) -> Card, path)``: each cap, at the limit and one over.
_CAPS = [
    (
        "section",
        SLACK_LIMITS.section_text,
        lambda n: Card(body=(Section(text=Text(("a" * n,))),)),
        "body[0].text",
    ),
    (
        "context",
        SLACK_LIMITS.section_text,
        lambda n: Card(body=(Context(items=(Text(("a" * n,)),)),)),
        "body[0].items[0]",
    ),
    (
        "label",
        SLACK_LIMITS.button_label,
        lambda n: Card(body=(Actions(buttons=(Button("a" * n, "x"),)),)),
        "body[0].buttons[0].label",
    ),
    (
        "value",
        SLACK_LIMITS.button_value,
        lambda n: Card(body=(Actions(buttons=(Button("b", "x", "a" * n),)),)),
        "body[0].buttons[0].value",
    ),
    (
        "action_id",
        SLACK_LIMITS.action_id,
        lambda n: Card(body=(Actions(buttons=(Button("b", "a" * n),)),)),
        "body[0].buttons[0].action_id",
    ),
    (
        "block_id",
        SLACK_LIMITS.block_id,
        lambda n: Card(body=(Actions(buttons=(Button("b", "x"),), id="a" * n),)),
        "body[0].id",
    ),
]

_SECTION = Section(text=Text(("v",)))
_COUNTS = [
    ("blocks", SLACK_LIMITS.blocks, lambda n: Card(body=(_SECTION,) * n), "body"),
    (
        "context",
        SLACK_LIMITS.context_items,
        lambda n: Card(body=(Context(items=(Text(("v",)),) * n),)),
        "body[0].items",
    ),
    (
        "buttons",
        SLACK_LIMITS.buttons,
        lambda n: Card(body=(Actions(buttons=(Button("b", "x"),) * n),)),
        "body[0].buttons",
    ),
]


class TestLimits:
    @pytest.mark.parametrize("name,cap,build,path", _CAPS, ids=[c[0] for c in _CAPS])
    def test_refuse_renders_at_the_cap_and_refuses_one_over(self, name, cap, build, path) -> None:
        render(build(cap), Platform.SLACK)
        _over("too_long", path, build(cap + 1))

    @pytest.mark.parametrize("name,cap,build,path", _COUNTS, ids=[c[0] for c in _COUNTS])
    def test_counts_render_at_the_cap_and_refuse_one_over(self, name, cap, build, path) -> None:
        render(build(cap), Platform.SLACK)
        _over("too_many", path, build(cap + 1))

    @pytest.mark.parametrize("name,cap,build,path", _COUNTS, ids=[c[0] for c in _COUNTS])
    def test_counts_refuse_none(self, name, cap, build, path) -> None:
        _over("empty", path, build(0))

    def test_refusal_names_the_limit_and_the_length(self) -> None:
        with pytest.raises(CardError) as err:
            render(_CAPS[0][2](SLACK_LIMITS.section_text + 1), Platform.SLACK)
        (issue,) = err.value.issues
        assert (issue.limit, issue.actual) == (3000, 3001)

    @pytest.mark.parametrize("field", ["action_id", "value", "block_id"])
    def test_keep_sends_an_over_cap_id_unchanged_and_logs(self, field, caplog) -> None:
        big = "a" * 3000
        button = Button("b", big if field == "action_id" else "x", big, fit=Fit.KEEP)
        actions = Actions(buttons=(button,), id=big if field == "block_id" else "", fit=Fit.KEEP)
        with caplog.at_level(logging.INFO, logger=render_mod.__name__):
            out = render(Card(body=(actions,)), Platform.SLACK).blocks[0]
        sent = out["block_id"] if field == "block_id" else out["elements"][0][field]
        assert sent == big
        assert f"{field if field != 'block_id' else 'id'} too_long" in caplog.text

    def test_redaction_can_push_text_over_the_cap(self) -> None:
        secret = f"x {KEY} y"
        grown = display_safe_for(secret, TransportCapabilities(), redactor=redact_via_context)
        assert len(grown) > len(secret), "the placeholder must be longer than the key it replaces"
        text = "a" * (SLACK_LIMITS.section_text - len(secret)) + secret
        assert len(text) == SLACK_LIMITS.section_text
        _over("too_long", "body[0].text", Card(body=(Section(text=Text((text,))),)))

    def test_escaping_counts_toward_the_cap(self) -> None:
        _over("too_long", "body[0].text", Card(body=(Section(text=Text(("&" * 1000,))),)))


class TestStructure:
    @pytest.mark.parametrize(
        "card,path,code",
        [
            (Card(body=(Section(text=Text(())),)), "body[0].text", "empty"),
            (Card(body=(Section(text=Text(("  ",))),)), "body[0].text", "empty"),
            (Card(body=(Section(text=Text(("\x1b[0m",))),)), "body[0].text", "empty"),
            (Card(body=(Section(text=Text(("<!-- keep-visible -->",))),)), "body[0].text", "empty"),
            (
                Card(body=(Actions(buttons=(Button("", "x"),)),)),
                "body[0].buttons[0].label",
                "empty",
            ),
            (
                Card(body=(Actions(buttons=(Button("b", ""),)),)),
                "body[0].buttons[0].action_id",
                "empty",
            ),
            (Card(body=("not a block",)), "body[0]", "bad_type"),  # type: ignore[arg-type]
            (Card(body=(Section(text=Text((3,))),)), "body[0].text", "bad_type"),  # type: ignore[arg-type]
            (
                Card(body=(Actions(buttons=(Button("b", "x", style="primary"),)),)),  # type: ignore[arg-type]
                "body[0].buttons[0].style",
                "bad_type",
            ),
            (
                Card(body=(Actions(buttons=(Button("b", "x", {"image_url": "u"}),)),)),  # type: ignore[arg-type]
                "body[0].buttons[0].value",
                "bad_type",
            ),
        ],
    )
    def test_a_malformed_spec_is_refused_with_its_path(self, card, path, code) -> None:
        _over(code, path, card)


class TestNoMedia:
    def test_the_spec_has_no_media_or_url_element(self) -> None:
        from kiro_crew.messaging.cards import spec

        names = {n.lower() for n in dir(spec)}
        for banned in ("image", "video", "preview", "unfurl", "embed", "link", "url"):
            assert not any(banned in n for n in names), banned

    def test_no_generated_output_requests_remote_media(self) -> None:
        from kiro_crew.slack.client import blocks_request_remote_media

        words = (
            "image_url",
            '{"type": "image", "image_url": "https://x/y.png"}',
            "video",
            "https://x/y.png",
        )
        cards = [
            Card(
                body=(
                    Section(text=Text((a, b))),
                    Context(items=(Text((a,)), Text((b,)))),
                    Actions(
                        buttons=(Button(a[:70], "image_url", b, "danger"),),
                        id="image",
                    ),
                )
            )
            for a, b in itertools.product(words, repeat=2)
        ]
        for card in cards:
            assert not blocks_request_remote_media(render(card, Platform.SLACK).blocks)
