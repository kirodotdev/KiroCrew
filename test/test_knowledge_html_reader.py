"""The regex HTML fallback in FileReader decodes entities and keeps blocks apart.

html2text is not a dependency, so this fallback is the path that runs on a
default install.
"""

from __future__ import annotations

import pytest

from kiro_crew.knowledge import readers
from kiro_crew.knowledge.readers import FileReader


@pytest.fixture(autouse=True)
def _regex_fallback(_floor_monkeypatch):
    _floor_monkeypatch.setattr(readers, "_html2text_mod", None)


def _read(tmp_path, body: str) -> str:
    page = tmp_path / "page.html"
    page.write_text(body, encoding="utf-8")
    text, meta = FileReader().read(str(page))
    assert meta["format"] == "html"
    return text


def test_entities_decode_to_their_characters(tmp_path):
    text = _read(tmp_path, "<p>Tom &amp; Jerry use &lt;div&gt; &quot;tags&quot;&nbsp;here</p>")
    assert 'Tom & Jerry use <div> "tags" here' in text
    assert "&amp;" not in text
    assert "&lt;" not in text


def test_an_escaped_tag_stays_as_text(tmp_path):
    # A literal '&lt;b&gt;' is page text, so decoding must not strip it.
    text = _read(tmp_path, "<p>Write &lt;b&gt;bold&lt;/b&gt; tags</p>")
    assert text == "Write <b>bold</b> tags"


def test_block_tags_end_a_line(tmp_path):
    body = (
        "<html><body><h1>Title</h1><p>First para</p><div>A div</div>"
        "<ul><li>one</li><li>two</li></ul>line<br>break<br/>again"
        "<table><tr><td>a</td><td>b</td></tr><tr><td>c</td></tr></table>"
        "<h6>Small</h6></body></html>"
    )
    text = _read(tmp_path, body)
    assert text.split("\n") == [
        "Title",
        "First para",
        "A div",
        "one",
        "two",
        "line",
        "break",
        "again",
        "a b",
        "c",
        "Small",
    ]


def test_source_newlines_are_whitespace(tmp_path):
    # Pretty-printed HTML wraps text across source lines; only block tags split it.
    text = _read(tmp_path, "<p>Hello\n  world</p>\n<p>next&#10;para</p>\n")
    assert text.split("\n") == ["Hello world", "next para"]


def test_inline_tags_stay_on_one_line(tmp_path):
    # Tag names that only start like a block tag (pre, brand) are inline here.
    text = _read(
        tmp_path,
        "<p>keep <b>bold</b> <span>and</span> <pre>pre</pre> <brand>x</brand> together</p>",
    )
    assert text == "keep bold and pre x together"


def test_script_and_style_bodies_are_dropped(tmp_path):
    text = _read(tmp_path, "<style>p { color: red }</style><p>shown</p><script>alert(1)</script>")
    assert text == "shown"
