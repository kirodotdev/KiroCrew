"""The capability matrix's two plumbing rows are pinned to the declarations.

``channel-capabilities.md`` is the one page a reader consults INSTEAD of opening a
transport, so a stale cell there is worse than an absent row: it answers the
question confidently and wrongly. ``scripts/docs_lint.py`` gates the page's
symbols and paths, never what its cells claim, and ``test_capability_ledger.py``
compares the code to itself — neither one reads this file.

Two rows are pinned here rather than all fifteen, and that is deliberate. Most
rows are user-visible behaviour a reader can check by using the channel
(streaming, uploads, threads). ``returns_message_id`` and ``mention_grammars`` are
not: nothing in the interface shows them, so a wrong cell survives every amount of
manual use. They are also the two newest fields, which is exactly when a matrix
drifts.

The test reads the DOC and maps each cell through the header row, so a reordered
column cannot silently re-point a value at the wrong channel, and it demands both
rows exist — deleting a row is the other way a matrix stops being true.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew.discord.transport import DISCORD_CAPABILITIES
from kiro_crew.feishu.transport import FEISHU_CAPABILITIES
from kiro_crew.imessage.transport import IMESSAGE_CAPABILITIES
from kiro_crew.messaging.transport import TransportCapabilities
from kiro_crew.slack.transport import SLACK_CAPABILITIES
from kiro_crew.teams.transport import TEAMS_CAPABILITIES
from kiro_crew.telegram.transport import TELEGRAM_CAPABILITIES
from kiro_crew.webex.transport import WEBEX_CAPABILITIES
from kiro_crew.wecom.transport import WECOM_CAPABILITIES
from kiro_crew.weixin.transport import WEIXIN_CAPABILITIES
from kiro_crew.whatsapp.transport import WHATSAPP_CAPABILITIES

DOC = Path(__file__).parent.parent / "src" / "kiro_crew" / "docs" / "channel-capabilities.md"

#: The doc's column label -> that channel's live declaration.
CHANNELS: dict[str, TransportCapabilities] = {
    "Slack": SLACK_CAPABILITIES,
    "Discord": DISCORD_CAPABILITIES,
    "Telegram": TELEGRAM_CAPABILITIES,
    "Teams": TEAMS_CAPABILITIES,
    "Webex": WEBEX_CAPABILITIES,
    "WeCom": WECOM_CAPABILITIES,
    "Weixin": WEIXIN_CAPABILITIES,
    "iMessage": IMESSAGE_CAPABILITIES,
    "WhatsApp": WHATSAPP_CAPABILITIES,
    "Feishu": FEISHU_CAPABILITIES,
}

#: The doc's row label -> the capability field it prints.
PINNED_ROWS = {
    "Answers a send with a message id": "returns_message_id",
    "Parses `@everyone`-style mentions": "mention_grammars",
}

_YES = "✅"
_NO = "❌"


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


@pytest.fixture(scope="module")
def matrix() -> dict[str, dict[str, str]]:
    """``{row label: {channel label: cell}}`` for the doc's capability matrix.

    Keyed through the header row on purpose: a column moved in the doc moves with
    its channel instead of handing the next channel's value to this test.
    """
    rows: dict[str, dict[str, str]] = {}
    header: list[str] | None = None
    for line in DOC.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = _cells(stripped)
        if header is None:
            header = cells[1:]
            continue
        if set(cells[0]) <= {"-", ":"}:
            continue
        rows[cells[0]] = dict(zip(header, cells[1:]))
    assert header is not None, f"{DOC.name} has no matrix table"
    assert header == list(CHANNELS), (
        f"the matrix's columns are {header}, this test knows {list(CHANNELS)}. "
        "Add the channel here (and its declaration) in the same change."
    )
    return rows


class TestThePinnedRowsExist:
    def test_both_rows_are_present(self, matrix: dict[str, dict[str, str]]) -> None:
        missing = [label for label in PINNED_ROWS if label not in matrix]
        assert not missing, (
            f"{DOC.name} lost {missing}. Neither field is visible in any channel's "
            "interface, so the matrix is the only place a reader can learn it."
        )


class TestTheCellsMatchTheDeclarations:
    @pytest.mark.parametrize("row_label,field_name", sorted(PINNED_ROWS.items()))
    @pytest.mark.parametrize("channel", sorted(CHANNELS))
    def test_cell_matches(
        self,
        matrix: dict[str, dict[str, str]],
        row_label: str,
        field_name: str,
        channel: str,
    ) -> None:
        declared = getattr(CHANNELS[channel], field_name)
        expected = _YES if declared else _NO
        actual = matrix[row_label][channel]
        assert actual == expected, (
            f"{DOC.name} prints {actual!r} for {channel} in '{row_label}', but "
            f"{channel}'s transport declares {field_name}={declared}."
        )


class TestTheProseNamesTheTwoExceptions:
    """The rows say WHICH channels differ; the prose has to say WHY, correctly.

    Both ``❌`` values are a different convention rather than a missing feature, so
    the page explains them by name. A channel joining or leaving either set makes
    that explanation wrong while every cell above stays green, which is why the
    membership is asserted rather than the wording.
    """

    def test_only_wecom_and_feishu_return_no_message_id(self) -> None:
        no_id = {name for name, caps in CHANNELS.items() if not caps.returns_message_id}
        assert no_id == {"WeCom", "Feishu"}, (
            f"{DOC.name} names WeCom and Feishu as the two id-less channels; the "
            f"code now says {sorted(no_id)}. Update the prose under the matrix."
        )

    def test_only_webex_parses_no_broadcast_mention(self) -> None:
        no_grammar = {name for name, caps in CHANNELS.items() if not caps.mention_grammars}
        assert no_grammar == {"Webex"}, (
            f"{DOC.name} names Webex as the only channel skipping the mention "
            f"defang; the code now says {sorted(no_grammar)}."
        )


class TestTheIMessageCaveatTracksTheDeclaration:
    """The one caveat inside the ✅ column, pinned to the condition that creates it.

    iMessage declares ``returns_message_id=True`` while ``IMessageClient.send``
    returns the bridge's best-effort ``guid``, so a delivered message can answer with
    ``""`` and ``delivery_confirmed`` records it as undelivered. The matrix cell
    reports the declaration (that is what the rest of the code acts on), so the page
    carries a sentence naming the gap between the two.

    This pin is CONDITIONAL on purpose: while the declaration says ``True`` the
    caveat must be there, and the moment the declaration is corrected the caveat
    becomes wrong and this test says so. Either way the doc cannot drift silently.
    """

    def test_the_caveat_is_present_while_the_declaration_is_strict(self) -> None:
        text = DOC.read_text(encoding="utf-8")
        declares_strict = IMESSAGE_CAPABILITIES.returns_message_id
        has_caveat = "its bridge does not keep it" in text
        if declares_strict:
            assert has_caveat, (
                f"iMessage declares returns_message_id=True while its bridge reports "
                f"the id as best-effort, so {DOC.name} must keep the caveat naming "
                "that gap in the row's prose."
            )
        else:
            assert not has_caveat, (
                f"iMessage no longer declares the strict reading, so {DOC.name}'s "
                "caveat about its bridge is stale — remove it."
            )


class TestTheZeroWidgetSection:
    """The canonical no-buttons section, and the one channel that breaks its rule.

    ``max_buttons=0`` does not mean one behaviour. Four channels route the whole
    list through the shared helper and deliver it as numbered lines; WhatsApp's
    renderer removes a completed trailer instead, so the choices are gone. The doc
    says both, and both halves are pinned here: the membership of the zero-widget
    set from the declarations, and WhatsApp's stripping from the function that does
    it. A doc that said only the first half would be confidently wrong about the
    one channel where the user loses the answers.
    """

    def test_the_zero_button_channels_are_the_five_named(self) -> None:
        zero = {name for name, caps in CHANNELS.items() if caps.max_buttons == 0}
        assert zero == {"WeCom", "Weixin", "iMessage", "WhatsApp", "Feishu"}, (
            f"{DOC.name} names five zero-widget channels; the code now says "
            f"{sorted(zero)}. Update 'Choices on a channel with no buttons'."
        )

    def test_the_section_exists_and_names_them(self) -> None:
        text = DOC.read_text(encoding="utf-8")
        assert "## Choices on a channel with no buttons" in text
        section = text.split("## Choices on a channel with no buttons", 1)[1]
        section = section.split("\n## ", 1)[0]
        for name in ("WeCom", "Weixin", "iMessage", "WhatsApp", "Feishu"):
            assert name in section, f"the zero-widget section does not name {name}"

    #: Every file that records the WhatsApp strip, and the SHORTEST anchor that
    #: proves it still does. One test owns all of them, so a change to the behaviour
    #: reds ONE place whose message names every file to update, rather than leaving
    #: copies to find by grep.
    #:
    #: The anchors are deliberately short and semantic rather than whole sentences: a
    #: ratchet that reds on any rewording teaches authors to delete it. The four sites
    #: that may name a private helper anchor on ``_strip_options``, which is the fact
    #: itself; a user-facing doc must not, so those two anchor on the shortest phrase
    #: that still carries the meaning.
    STRIP_SITES = {
        "src/kiro_crew/docs/channel-capabilities.md": "drops the list",
        "src/kiro_crew/docs/whatsapp-integration.md": "dropped, not numbered",
        "src/kiro_crew/messaging/transport.py": "_strip_options",
        "src/kiro_crew/messaging/renderer.py": "_strip_options",
        "src/kiro_crew/whatsapp/transport.py": "_strip_options",
        "src/kiro_crew/whatsapp/commands.py": "strips a complete",
        "docs/system-specs/modules/messaging.md": "_strip_options",
    }

    def test_every_site_recording_the_strip_agrees_with_the_code(self) -> None:
        """All seven restatements, pinned together to the behaviour they describe.

        The claim lives in seven files because a reader of the ``max_buttons``
        honesty contract must see the exception inline rather than follow a link.
        The cost of that is seven places to update, so this test is the single place
        that goes red when the behaviour changes, and its failure message names every
        file still carrying the stale sentence.
        """
        from kiro_crew.whatsapp.turn_renderer import _strip_options

        repo = Path(__file__).parent.parent
        still_strips = _strip_options("Pick.\n\n[OPTIONS: a | b]") == "Pick."
        missing = [
            rel
            for rel, phrase in sorted(self.STRIP_SITES.items())
            if phrase not in (repo / rel).read_text(encoding="utf-8")
        ]
        if still_strips:
            assert not missing, (
                "WhatsApp still strips a completed [OPTIONS:] trailer, but these files "
                f"no longer record it: {missing}. Every site states the exception "
                "inline on purpose -- restore the sentence or move the whole set."
            )
        else:
            assert len(missing) == len(self.STRIP_SITES), (
                "WhatsApp no longer strips the trailer, so every file below is stale "
                f"and must be updated in this change: {sorted(set(self.STRIP_SITES) - set(missing))}"
            )

    def test_whatsapp_really_removes_a_completed_trailer(self) -> None:
        """The doc's exception claim, driven through the code that makes it true.

        If WhatsApp ever starts numbering the list, this fails and the paragraph
        calling it a gap has to go — which is the direction a reader most needs the
        doc to be right about, because today the answers vanish silently.
        """
        from kiro_crew.whatsapp.turn_renderer import _strip_options

        stripped = _strip_options("Pick one.\n\n[OPTIONS: Ship it | Hold]")
        assert stripped == "Pick one.", (
            "WhatsApp no longer strips a completed [OPTIONS:] trailer; "
            f"{DOC.name} still calls that a gap. Got {stripped!r}."
        )
        assert "Ship it" not in stripped and "Hold" not in stripped
