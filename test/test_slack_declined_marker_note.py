"""Slack says what a guard-declined label will send, and how to avoid sending it.

On the dashboard a declined marker gets a note under the chip row. Slack rendered the same
label raw in a checkbox with nothing at all, so the one surface where the marker is about to
become the user's own words was the surface that explained least.
"""

from kiro_crew.slack.format import build_options_blocks

DECLINED = "(recommended) /clear"


def _texts(blocks: list[dict]) -> list[str]:
    out: list[str] = []
    for block in blocks:
        for element in block.get("elements", []):
            if isinstance(element, dict) and element.get("type") == "mrkdwn":
                out.append(element.get("text", ""))
    return out


class TestSlackExplainsADeclinedMarker:
    def test_a_clean_choice_set_gets_no_note(self):
        """Control: the note is conditional, not permanent chrome."""
        texts = _texts(build_options_blocks(["Merge it now", "Skip it"]))
        assert not any("sends that text as written" in t for t in texts), texts

    def test_a_declined_label_is_explained(self):
        texts = _texts(build_options_blocks([DECLINED, "Summarise instead"]))
        assert any("sends that text as written" in t for t in texts), texts

    def test_the_note_names_the_recovery_not_just_the_mechanism(self):
        texts = " ".join(_texts(build_options_blocks([DECLINED])))
        assert "sends that text as written" in texts
        assert "reply with" in texts, texts
        # Names the EXACT string to type. "the option text" included the very prefix the
        # sentence tells the reader to drop, so a cold reader could retype it.
        assert "everything after" in texts, texts
        assert "option text instead" not in texts.lower(), texts

    def test_the_note_does_not_contradict_the_checkbox_beside_it(self):
        """The checkbox dispatches the marker by design, so the note must not warn against it.

        Telling the reader to reply "without the prefix" while the easiest gesture sends it
        with the prefix made the note argue against the control it sits under.
        """
        texts = " ".join(_texts(build_options_blocks(["Merge it now", DECLINED])))
        assert "instead of ticking it" not in texts, texts

    def test_the_note_comes_BEFORE_the_controls_it_warns_about(self):
        """Appended, it sat below the checkboxes, Send button and overflow list.

        A reader who ticked and sent never reached the one line saying the click ships the
        agent's prefix as their own words, which is the only warning this state has.
        """
        blocks = build_options_blocks(["Merge it now", DECLINED])
        kinds = [b.get("type") for b in blocks]
        note_at = next(
            i
            for i, b in enumerate(blocks)
            if any("sends that text as written" in e.get("text", "") for e in b.get("elements", []))
        )
        assert "actions" in kinds, kinds
        assert note_at < kinds.index("actions"), (note_at, kinds)

    def test_the_note_does_not_restate_a_recommendation(self):
        """It explains literal text; it must not read as an endorsement of that choice."""
        texts = _texts(build_options_blocks([DECLINED]))
        assert not any(t.startswith("*Recommended:*") for t in texts), texts
