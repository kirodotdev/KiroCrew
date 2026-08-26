"""Tests for kiro_crew.webex.cards — Adaptive Cards, Webex's Block Kit analogue.

The properties that matter here are not "does the JSON look right" but the two
security ones: a press must not be able to inject text, and a press on a card
whose decision already resolved must not be honoured. The second is load-bearing
because Webex refuses to edit a message carrying an attachment, so a resolved
card's buttons stay clickable forever — the nonce is the only thing retiring them.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew.webex.cards import (
    CARD_CONTENT_TYPE,
    CARD_VERSION,
    KEY_CHOICE,
    KEY_KIND,
    KEY_NONCE,
    KEY_REQUEST,
    KIND_APPROVAL,
    KIND_OPTIONS,
    MAX_CARD_ACTIONS,
    LiveChoices,
    approval_card,
    options_card,
    read_press,
    usable_choices,
)
from kiro_crew.webex.renderer import _safe_tool_label


def _actions(card: dict) -> list[dict]:
    return card["content"]["actions"]


class TestCardShape:
    def test_the_attachment_declares_the_type_webex_accepts(self) -> None:
        card = approval_card("fs_write", nonce="n1", request_id="7")
        assert card["contentType"] == CARD_CONTENT_TYPE
        assert card["content"]["type"] == "AdaptiveCard"

    def test_the_schema_version_is_the_one_webex_supports(self) -> None:
        # Webex rejects a later version outright, so this is a compatibility
        # ceiling rather than a preference.
        assert CARD_VERSION == "1.3"
        assert approval_card("t", nonce="n", request_id="1")["content"]["version"] == "1.3"

    def test_the_card_is_json_serialisable(self) -> None:
        # It goes on the wire as JSON; a non-serialisable value would fail at send
        # time, inside a turn, rather than here.
        json.dumps(approval_card("t", nonce="n", request_id="1"))
        json.dumps(options_card(["a", "b"], nonce="n"))

    def test_a_tool_name_carries_no_markdown_control_characters(self) -> None:
        """A tool title is model-influenced text, and a TextBlock is not a shield.

        An Adaptive Cards 1.3 TextBlock renders a markdown subset — including
        ``[text](url)`` — with no per-block switch, so what keeps a title from
        styling itself or smuggling a link into the prompt the user is deciding on
        is the renderer's sanitizer, not the block type. This pins the sanitized
        label, because pinning verbatim storage would pin the wrong property.
        """
        raw = "**bold** [link](http://x) `code` <a href=x>"
        card = approval_card(_safe_tool_label(raw), nonce="n", request_id="1")
        blocks = card["content"]["body"]
        assert all(b["type"] == "TextBlock" for b in blocks)
        rendered = " ".join(b["text"] for b in blocks)
        assert not set(rendered) & set("*`[]()<>{}")
        # Neutralized, not dropped: the user still has to be able to tell WHICH
        # tool they are approving.
        assert "bold" in rendered and "link" in rendered

    def test_stripping_the_markup_cannot_reassemble_a_credential(self) -> None:
        """The sanitizer is itself the reassembling transformation.

        ``AKIA**IOSF**ODNN7EXAMPLE`` does not match a credential pattern as the
        driver streamed it, and removing the ``*`` characters — which the sanitizer
        does on purpose — would hand the room the intact key. So the scan runs on
        the stripped form, after the strip.
        """
        label = _safe_tool_label("AKIA**IOSF**ODNN7EXAMPLE")

        assert "AKIAIOSFODNN7EXAMPLE" not in label
        assert "REDACTED" in label

    def test_a_tool_name_keeps_its_underscores(self) -> None:
        """``fs_write`` must not render as ``fswrite``.

        Identifying the tool IS the job of this string, and every real tool name
        has underscores. Emphasis is cosmetic — it can neither remove text nor
        create a clickable target — and inside the prompt body's code span an
        underscore is literal.
        """
        assert _safe_tool_label("mcp__server__do_thing") == "mcp__server__do_thing"


class TestApprovalCard:
    def test_it_offers_exactly_approve_and_deny(self) -> None:
        actions = _actions(approval_card("fs_write", nonce="n1", request_id="7"))
        assert [a["data"][KEY_CHOICE] for a in actions] == ["approve", "deny"]

    def test_every_action_carries_the_routing_key(self) -> None:
        for action in _actions(approval_card("t", nonce="n1", request_id="7")):
            data = action["data"]
            assert data[KEY_KIND] == KIND_APPROVAL
            assert data[KEY_NONCE] == "n1"
            assert data[KEY_REQUEST] == "7"

    def test_inputs_are_not_gathered(self) -> None:
        # These cards collect nothing, so asking Webex to gather inputs would only
        # widen what comes back from a press.
        for action in _actions(approval_card("t", nonce="n", request_id="1")):
            assert action["associatedInputs"] == "none"

    def test_a_missing_tool_name_still_renders(self) -> None:
        card = approval_card("", nonce="n", request_id="1")
        assert any("this tool" in b["text"] for b in card["content"]["body"])


class TestOptionsCard:
    def test_one_action_per_choice(self) -> None:
        actions = _actions(options_card(["Yes", "No", "Maybe"], nonce="n"))
        assert [a["title"] for a in actions] == ["Yes", "No", "Maybe"]

    def test_the_choice_travels_as_an_INDEX(self) -> None:
        """The single most important property in this file.

        The press handler resolves the returned value as an index into the
        choices it rendered. If the choice TEXT round-tripped instead, a forged
        press could put arbitrary words into the turn.
        """
        actions = _actions(options_card(["Yes", "No"], nonce="n"))
        assert [a["data"][KEY_CHOICE] for a in actions] == ["0", "1"]
        assert all(a["data"][KEY_KIND] == KIND_OPTIONS for a in actions)

    def test_no_choices_yields_no_card(self) -> None:
        assert options_card([], nonce="n") is None
        assert options_card(["", "  "], nonce="n") is None

    def test_blank_choices_are_dropped_and_do_not_shift_the_index(self) -> None:
        actions = _actions(options_card(["Yes", "", "No"], nonce="n"))
        assert [(a["title"], a["data"][KEY_CHOICE]) for a in actions] == [("Yes", "0"), ("No", "1")]

    def test_a_long_list_is_truncated_rather_than_rejected(self) -> None:
        """Webex rejects a card with too many actions.

        The shared ``apply_options_cap`` caps first and puts the remainder in the
        body as numbered text; this is the last-resort guard, and truncating beats
        losing every choice to a 400.
        """
        actions = _actions(options_card([f"C{i}" for i in range(MAX_CARD_ACTIONS + 4)], nonce="n"))
        assert len(actions) == MAX_CARD_ACTIONS

    def test_a_very_long_choice_is_shortened_for_the_button(self) -> None:
        actions = _actions(options_card(["x" * 500], nonce="n"))
        assert len(actions[0]["title"]) <= 60


class TestReadPress:
    def test_a_well_formed_press_destructures(self) -> None:
        actions = _actions(approval_card("t", nonce="n1", request_id="7"))
        assert read_press(actions[0]["data"]) == (KIND_APPROVAL, "approve", "n1", "7")

    @pytest.mark.parametrize("junk", [None, "", 42, [], "a string", {"unrelated": "x"}])
    def test_anything_that_is_not_ours_yields_empty_strings(self, junk: object) -> None:
        """``inputs`` is a map Webex assembled from a client.

        It is untrusted input rather than a structure whose shape can be trusted,
        and every caller reads an empty kind as "not ours".
        """
        assert read_press(junk)[0] == ""

    def test_values_are_coerced_to_strings(self) -> None:
        # A client could send a number where a string is expected; comparing an
        # int against a nonce string would silently never match.
        kind, choice, nonce, request_id = read_press(
            {KEY_KIND: KIND_OPTIONS, KEY_CHOICE: 1, KEY_NONCE: 2, KEY_REQUEST: 3}
        )
        assert (kind, choice, nonce, request_id) == (KIND_OPTIONS, "1", "2", "3")

    def test_the_reserved_keys_are_namespaced(self) -> None:
        """A card input named "action" must not be able to forge a decision.

        Namespacing is what keeps a user-supplied input from colliding with the
        routing key.
        """
        for key in (KEY_KIND, KEY_CHOICE, KEY_NONCE, KEY_REQUEST):
            assert key.startswith("kirocrew_")


class TestUsableChoices:
    def test_blank_choices_are_dropped_and_the_cap_applies(self) -> None:
        assert usable_choices([" a ", "   ", "b"]) == ["a", "b"]
        assert len(usable_choices([f"c{i}" for i in range(MAX_CARD_ACTIONS + 4)])) == (
            MAX_CARD_ACTIONS
        )

    def test_it_is_idempotent(self) -> None:
        """Applied at both the card builder and the publisher, so it must be.

        If the two sides could derive different lists, dropping one blank choice
        would shift every index after it and a button would answer with its
        neighbour.
        """
        once = usable_choices([" a", "", "b ", "  ", "c"])
        assert usable_choices(once) == once

    def test_the_card_and_the_publisher_agree_on_button_order(self) -> None:
        raw = ["Keep going", "   ", "Stop", "", "Explain"]
        usable = usable_choices(raw)
        actions = _actions(options_card(raw, nonce="n"))
        # The press's data index must select the same string the button shows.
        for action in actions:
            index = int(action["data"][KEY_CHOICE])
            assert usable[index] == action["title"]


class TestLiveChoices:
    def test_a_press_resolves_to_the_choice_it_shows(self) -> None:
        live = LiveChoices()
        live.publish("s1", "n1", ["A", "B"])
        assert live.take("s1", "1", "n1") == "B"

    def test_a_press_is_one_shot(self) -> None:
        """Webex cannot retire a card that carries an attachment.

        So its buttons stay clickable forever, and the ENTRY is what has to expire
        — otherwise one card answers every future prompt in the conversation.
        """
        live = LiveChoices()
        live.publish("s1", "n1", ["A", "B"])
        assert live.take("s1", "0", "n1") == "A"
        assert live.take("s1", "0", "n1") == ""

    def test_a_stale_nonce_resolves_to_nothing(self) -> None:
        live = LiveChoices()
        live.publish("s1", "n1", ["A"])
        live.publish("s1", "n2", ["X", "Y"])
        assert live.take("s1", "0", "n1") == ""
        assert live.take("s1", "1", "n2") == "Y"

    @pytest.mark.parametrize("index", ["", "-1", "9", "abc", "0.5"])
    def test_an_index_that_is_not_a_slot_resolves_to_nothing(self, index: str) -> None:
        live = LiveChoices()
        live.publish("s1", "n1", ["A", "B"])
        assert live.take("s1", index, "n1") == ""

    def test_an_unknown_session_and_an_empty_nonce_resolve_to_nothing(self) -> None:
        live = LiveChoices()
        live.publish("s1", "n1", ["A"])
        assert live.take("other", "0", "n1") == ""
        assert live.take("s1", "0", "") == ""

    def test_tracking_is_bounded(self) -> None:
        """A ``/new`` mints a new session key, so an abandoned entry is
        unreachable but still resident. Without a bound they accumulate for the
        life of the gateway."""
        live = LiveChoices()
        for i in range(LiveChoices.MAX_TRACKED + 10):
            live.publish(f"s{i}", "n", ["A"])
        assert len(live._live) == LiveChoices.MAX_TRACKED
        # The newest survives; the oldest was evicted.
        assert live.take(f"s{LiveChoices.MAX_TRACKED + 9}", "0", "n") == "A"
        assert live.take("s0", "0", "n") == ""
