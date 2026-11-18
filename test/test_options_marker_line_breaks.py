"""Every channel reader ends an ``[OPTIONS:]`` line at each break the web parser does."""

from __future__ import annotations

import pytest

from kiro_crew.constants import reflow_and_label_glued_option_marker, split_trailing_protocol_suffix
from kiro_crew.dashboard.state_parts.slot_transcript import _parse_options, _strip_options
from kiro_crew.messaging.renderer import split_options_trailer
from kiro_crew.preview_text import strip_markdown_preview
from kiro_crew.slack import handler as h
from kiro_crew.slack.format import extract_options

BREAKS = ["\r", "\u2028", "\u2029"]


def _line_readers(raw: str) -> dict[str, tuple[str, list[str]]]:
    return {
        "slack": extract_options(raw),
        "dashboard": (_strip_options(raw), _parse_options(raw)),
    }


def _every_reader(raw: str) -> dict[str, tuple[str, list[str]]]:
    return {**_line_readers(raw), "renderer": split_options_trailer(raw)}


@pytest.mark.parametrize("br", BREAKS)
@pytest.mark.parametrize(
    ("raw", "text", "options"),
    [
        ("Body\n**[OPTIONS: A | B]**", "Body", ["A", "B"]),
        ("[OPTIONS: dropped\n[OPTIONS: A | B]", "[OPTIONS: dropped", ["A", "B"]),
        ("Body\n[OPTIONS: A] [OPTIONS: B]", "Body", ["B"]),
        (
            "[OPTIONS: A] [OPTIONS: B then arr[0]\n[OPTIONS: C]",
            "[OPTIONS: A] [OPTIONS: B then arr[0]",
            ["C"],
        ),
    ],
)
def test_a_marker_ending_the_reply_reads_alike_on_every_reader(br, raw, text, options):
    assert all(got == (text, options) for got in _every_reader(raw).values())
    for name, got in _every_reader(raw.replace("\n", br)).items():
        assert got == (text.replace("\n", br), options), name


@pytest.mark.parametrize("br", BREAKS)
def test_a_marker_line_followed_by_prose_parses_on_the_line_readers(br):
    for name, got in _line_readers(f"[OPTIONS: A | B]{br}Then restart.").items():
        assert got == ("Then restart.", ["A", "B"]), name


@pytest.mark.parametrize("br", BREAKS)
def test_a_marker_whose_labels_cross_a_break_stays_text(br):
    raw = f"[OPTIONS: A{br}B | C]"
    for name, got in _line_readers(raw).items():
        assert got == (raw, []), name
    text, options = split_options_trailer("[OPTIONS: A\nB | C]")
    assert split_options_trailer(raw) == (text, [o.replace("\n", br) for o in options])


@pytest.mark.parametrize("br", BREAKS)
def test_a_glued_footer_is_repaired_after_any_break(br):
    assert extract_options(f"Body{br}[OPTIONS: A | B]Anytime.") == (f"Body{br}Anytime.", ["A", "B"])
    _, glued = reflow_and_label_glued_option_marker(f"Body{br}[OPTIONS: A | B]Anytime.")
    assert glued == ["Anytime."]


@pytest.mark.parametrize("br", BREAKS)
def test_a_streaming_wrapper_is_held_after_any_break(br):
    visible, suffix = split_trailing_protocol_suffix("Pick:\n**[OPTIONS: A | B")
    assert split_trailing_protocol_suffix(f"Pick:{br}**[OPTIONS: A | B") == (
        visible.replace("\n", br),
        suffix,
    )
    assert suffix.startswith("**")


def test_a_crlf_reply_parses_its_marker_line():
    for name, got in _every_reader("Body\r\n[OPTIONS: A | B]\r\n").items():
        assert got == ("Body", ["A", "B"]), name


@pytest.mark.parametrize("br", BREAKS)
def test_the_preview_drops_a_marker_line_ended_by_any_break(br):
    assert strip_markdown_preview(f"[OPTIONS: A | B]{br}Then restart.") == "Then restart."


@pytest.mark.parametrize("br", BREAKS)
def test_the_slack_stream_settles_an_open_head_at_any_break(br):
    text = f"Pick:{br}**[OPTIONS: Retry | Abort\u3011**{br}Thanks"
    hold, buf = h._filter_options_brackets(text, "", "")
    hold, released = h._resolve_options_hold(hold, buf)
    assert hold == ""
    assert buf + released == f"Pick:{br}**{br}Thanks"
