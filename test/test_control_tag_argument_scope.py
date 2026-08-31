"""The in-band placement argument is scoped to the pipelines where it actually holds.

The spec claims a control tag after ``[OPTIONS:]`` breaks a renderer's option parse. That is
true only of renderers that parse the buffer WITHOUT stripping control comments first. The
shared messaging projection strips first, so the trailer parses normally there and that path
is not evidence for in-band placement. This pins both halves, so a renderer that later gains
(or loses) the strip cannot leave the spec quietly wrong.
"""

from pathlib import Path

from kiro_crew.constants import strip_control_comments
from kiro_crew.messaging.renderer import split_options_trailer

SRC = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"
TAG = "<!-- keep-visible -->"
BUF = "Body text.\n\n[OPTIONS: Alpha | Beta]\n" + TAG

RAW_PARSERS = (
    "discord/renderer.py",
    "teams/renderer.py",
    "telegram/renderer.py",
    "webex/renderer.py",
    "wecom/renderer.py",
)


class TestTheControlTagArgument:
    def test_the_probe_parses_a_clean_trailer(self):
        """Positive control: without it, an empty result proves nothing."""
        assert split_options_trailer("Body text.\n\n[OPTIONS: Alpha | Beta]\n")[1] == [
            "Alpha",
            "Beta",
        ]

    def test_an_untouched_trailing_tag_does_break_the_parse(self):
        assert split_options_trailer(BUF)[1] == []

    def test_stripping_first_restores_the_parse(self):
        """So a pipeline that strips is NOT evidence for in-band placement."""
        assert split_options_trailer(strip_control_comments(BUF))[1] == ["Alpha", "Beta"]

    def test_the_named_channel_renderers_still_parse_without_stripping(self):
        for rel in RAW_PARSERS:
            body = (SRC / rel).read_text(encoding="utf-8")
            assert "split_options_trailer" in body, rel
            assert (
                "strip_control_comments" not in body
            ), f"{rel} now strips control comments, so the spec's per-pipeline list is stale"

    def test_the_shared_projection_still_strips(self):
        body = (SRC / "messaging" / "renderer.py").read_text(encoding="utf-8")
        assert "strip_control_comments" in body
