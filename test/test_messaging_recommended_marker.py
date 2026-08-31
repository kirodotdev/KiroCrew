"""A channel does not strip a ``(recommended)`` marker; it renders it as label text.

No channel session is ever given the producer rule that instructs the marker, so a
channel label carrying one is model drift rather than protocol. Stripping it there
bought a guard against drift nobody has observed, and that guard alone pulled in the
sigil-less wecom/weixin command list plus its parity suite. Leaving the marker in the
label is base behaviour: the text renders as written and dispatches as written, which
is what every unmarked label already does.

The dashboard-facing Slack path keeps its strip -- it has a real producer, because the
dashboard mirror is instructed to mark a choice.
"""

from __future__ import annotations

import pytest

from kiro_crew.messaging.renderer import split_options_trailer


def _choices(*labels: str) -> list[str]:
    body, choices = split_options_trailer("Answer.\n\n[OPTIONS: " + " | ".join(labels) + "]")
    assert body.startswith("Answer.")
    return choices


class TestTheChannelLeavesTheMarkerAlone:
    @pytest.mark.parametrize(
        "label",
        [
            "(recommended) Merge it now",
            "(RECOMMENDED) Merge it now",
            "  (recommended)   Merge it now",
        ],
    )
    def test_a_leading_marker_survives_into_the_label(self, label):
        assert _choices(label) == [label.strip()]

    @pytest.mark.parametrize(
        "label",
        [
            "Merge it now (recommended)",
            "Merge it now (RECOMMENDED)",
        ],
    )
    def test_a_trailing_marker_also_survives(self, label):
        assert _choices(label) == [label.strip()]

    def test_an_unmarked_label_is_unchanged_which_is_the_same_path(self):
        assert _choices("Merge it now") == ["Merge it now"]

    def test_a_marker_shaped_like_a_channel_command_is_not_special_cased(self):
        """The sigil-less command list went with the strip, so nothing inspects these.

        A drifted marker on such a label renders as text; it cannot dispatch as a
        command because a channel option click carries no command interpretation.
        """
        assert _choices("(recommended) 停止") == ["(recommended) 停止"]

    def test_several_labels_each_keep_their_own_text(self):
        assert _choices("(recommended) Merge it now", "Hold it") == [
            "(recommended) Merge it now",
            "Hold it",
        ]
