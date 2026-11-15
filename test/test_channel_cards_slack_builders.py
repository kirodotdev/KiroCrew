"""The Stop and working cards, built from card specs, return what main returned.

The goldens in ``fixtures/channel_cards/slack_stop_working_goldens.json`` were
captured from the hand-built builders before they moved. Each builder must
return a payload equal to its golden, and byte-equal as canonical JSON (sorted
keys). Key order inside a button can differ from the hand-built dicts: Slack reads
a JSON object by key, so the order has no effect on what it shows or routes.
"""

from __future__ import annotations

import importlib
import json
import logging
import re
from pathlib import Path

import pytest

from kiro_crew.messaging.cards import SLACK_LIMITS
from kiro_crew.slack import blocks

#: The module, not the ``render`` function the package exports under its name.
render_mod = importlib.import_module("kiro_crew.messaging.cards.render")
GOLDENS = json.loads(
    (Path(__file__).parent / "fixtures/channel_cards/slack_stop_working_goldens.json").read_text(
        encoding="utf-8"
    )
)
KEY = GOLDENS["session_key"]


def _canonical(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


def _built(session_key: str) -> dict[str, list[dict]]:
    return {
        "build_stopping_blocks": blocks.build_stopping_blocks(session_key),
        "build_stopped_blocks": blocks.build_stopped_blocks(),
        "build_stop_failed_blocks": blocks.build_stop_failed_blocks(),
        "build_working_blocks": blocks.build_working_blocks(session_key),
        "build_working_blocks_with_dashboard_link": blocks.build_working_blocks(
            session_key, include_dashboard_link=True
        ),
    }


def _hand_built(session_key: str) -> dict[str, list[dict]]:
    """The four builders' hand-built output, by its formula, for any session key."""

    def swap(node):
        if isinstance(node, dict):
            return {k: swap(v) for k, v in node.items()}
        if isinstance(node, list):
            return [swap(v) for v in node]
        return node.replace(KEY, session_key) if isinstance(node, str) else node

    return swap(GOLDENS["payloads"])


@pytest.mark.parametrize("name", sorted(GOLDENS["payloads"]))
def test_each_builder_returns_its_golden(name: str) -> None:
    got = _built(KEY)[name]
    assert got == GOLDENS["payloads"][name]
    assert _canonical(got) == _canonical(GOLDENS["payloads"][name])


def test_the_goldens_cover_every_builder_and_both_working_variants() -> None:
    assert set(GOLDENS["payloads"]) == set(_built(KEY))


@pytest.mark.parametrize(
    "session_key",
    [
        "",
        "1731400000.000100",
        # Fills the action id ("mc_inline_stop_" + key) to Slack's 255 exactly.
        "k" * (SLACK_LIMITS.action_id - len("mc_inline_stop_")),
        # No code bounds a session key, so one far past every Slack cap.
        "slack:C0123ABCD:" + "x" * (SLACK_LIMITS.button_value + 500),
        "<!channel> @everyone `` & é",
    ],
    ids=["empty", "ts", "at-action-id-cap", "over-every-cap", "markup"],
)
def test_a_builder_returns_main_payload_for_any_session_key(session_key: str, caplog) -> None:
    """A builder never raises where main returned blocks: the session key goes
    into ids and the button value unchanged, under ``Fit.KEEP``."""
    with caplog.at_level(logging.INFO, logger=render_mod.__name__):
        got = _built(session_key)
    assert got == _hand_built(session_key)
    over = len("mc_inline_stop_" + session_key) > SLACK_LIMITS.action_id
    assert ("too_long" in caplog.text) is over


def test_the_working_card_dashboard_button_is_the_shared_one() -> None:
    from kiro_crew.slack.format import build_link_dashboard_button

    elements = blocks.build_working_blocks(KEY, include_dashboard_link=True)[1]["elements"]
    assert elements[1] == build_link_dashboard_button()


def test_the_renderer_slack_pieces_match_the_slack_modules() -> None:
    """The neutral renderer cannot import a channel package, so it holds its own
    copy of two Slack facts; each is pinned to its source here."""
    from kiro_crew.slack.format import escape_mrkdwn
    from kiro_crew.slack.transport import SLACK_CAPABILITIES

    for text in ("a & b", "<!channel>", "&amp;", "x>y<z", "plain", "&&<<>>"):
        assert render_mod._escape(text) == escape_mrkdwn(text)
    slack_caps = render_mod._CAPABILITIES[render_mod.Platform.SLACK]
    assert slack_caps.mention_grammars == SLACK_CAPABILITIES.mention_grammars


def test_the_builders_hold_no_hand_built_block_literal() -> None:
    """The four builders describe specs; ``render`` owns the Block Kit shape."""
    source = Path(blocks.__file__).read_text(encoding="utf-8")
    for name in (
        "build_stopping_blocks",
        "build_stopped_blocks",
        "build_stop_failed_blocks",
        "build_working_blocks",
    ):
        body = re.search(rf"def {name}\(.*?(?=\ndef |\Z)", source, re.S)
        assert body is not None, name
        assert '"type"' not in body.group(0), name
