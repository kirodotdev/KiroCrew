"""The capability matrix's two plumbing rows are pinned to the declarations.

``scripts/docs_lint.py`` checks the page's symbols, not its cells, and
``test_capability_ledger.py`` never reads it. Only these two rows are pinned because
nothing in the interface shows them, so manual use cannot catch a wrong cell.

Cells map through the header row, so a reordered column cannot re-point a value,
and both rows must exist.
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

    def test_only_wecom_feishu_and_imessage_return_no_message_id(self) -> None:
        no_id = {name for name, caps in CHANNELS.items() if not caps.returns_message_id}
        assert no_id == {"WeCom", "Feishu", "iMessage"}, (
            f"{DOC.name} names WeCom, Feishu and iMessage as the id-less channels; the "
            f"code now says {sorted(no_id)}. Update the prose under the matrix."
        )

    def test_only_webex_parses_no_broadcast_mention(self) -> None:
        no_grammar = {name for name, caps in CHANNELS.items() if not caps.mention_grammars}
        assert no_grammar == {"Webex"}, (
            f"{DOC.name} names Webex as the only channel skipping the mention "
            f"defang; the code now says {sorted(no_grammar)}."
        )


class TestTheZeroWidgetSection:
    """The canonical no-buttons section, and the one channel that breaks its rule.

    Four ``max_buttons=0`` channels deliver the list as numbered lines; WhatsApp's
    renderer drops a completed trailer instead. Both halves are pinned to the code.
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

    #: Every file that records the WhatsApp strip, and the shortest anchor that proves
    #: it. Anchors are short so a rewording does not red the check; user-facing docs
    #: anchor on a phrase rather than the private ``_strip_options``.
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

        Readers must see the exception inline, so it lives in seven files; on a change
        this one test fails and names every file still carrying the stale sentence.
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
