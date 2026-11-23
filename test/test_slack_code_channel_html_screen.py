"""An HTML view never shows a credential through a path the text screen skips.

A browser shows more than an HTML view's text nodes: a nested document
(``iframe``/``object``/``embed``, a ``data:`` URL), CSS ``content`` and
``attr()``, and attribute text such as ``alt`` or an input's ``value``. Each of
these is screened or refused, and a hit refuses the whole view.
"""

from __future__ import annotations

from typing import Any

import pytest
from slack_code_channel_surface_helpers import (  # noqa: F401 - fixtures by name
    _bind,
    _calls,
    _run,
    env_fixture,
)

from kiro_crew.slack.code_channels import tools
from kiro_crew.testing.synthetic_credentials import github_classic

#: A GitHub token with a valid checksum; ``\\67`` below is its first letter escaped.
TOKEN = github_classic(seed=127)
HEAD, TAIL = TOKEN[:18], TOKEN[18:]


def _publish(env: Any, content: str) -> Any:
    _bind(env)
    env.api.responses["agents.conversations.setView"] = {"ok": True, "view_id": "V1"}
    return _run(env, tools.SET_VIEW, {"view_key": "r", "content": content})


def _refused(env: Any, content: str) -> None:
    out = _publish(env, content)
    assert not out.ok and "nothing was published" in out.text, out.text
    assert _calls(env, "agents.conversations.setView") == []


@pytest.mark.parametrize(
    "content",
    [
        f'<iframe src="data:text/html,%67hp_{"A" * 36}"></iframe>',
        f'<iframe src="data:text/html,{HEAD}<b></b>{TAIL}"></iframe>',
        f'<embed src="data:text/html,%67hp_{"A" * 36}">',
        f'<object data="data:text/html,%67hp_{"A" * 36}"></object>',
        f'<meta http-equiv=refresh content="0;url=data:text/html,%67hp_{"A" * 36}">',
        '<iframe src="https://example.test/"></iframe>',
        '<meta http-equiv=" Refresh" content="0;url=https://example.test/">',
        '<img src=" DA&#x09;TA:image/svg+xml,x">',  # browsers drop tabs in a scheme
        '<a href="&#100;ata:text/html,x">x</a>',
        '<img srcset="https://example.test/a.png 1x, data:image/png;base64,AA 2x">',
        '<img src="\x0cdata:image/png,AA">',  # a URL parser drops leading C0 controls
        '<img srcset="https://example.test/a 1x,&#12;data:x 2x">',
        # a marked section ends at the first > in HTML, not at ]]>
        "<![CDATA[ > <iframe src=https://example.test/>",
        '<![CDATA[ > <style>p::after{content:"x"}</style> ]]>',
        # an external stylesheet, a rebased URL, and scripting-dependent markup
        '<link rel=stylesheet href="https://example.test/x.css"><p>x</p>',
        '<base href="https://example.test/"><p>x</p>',
        '<noscript><p title="</noscript><style>p{}</style>">x</p></noscript>',
    ],
)
def test_a_nested_document_or_data_url_is_refused(env: Any, content: str) -> None:
    _refused(env, content)


@pytest.mark.parametrize(
    "content",
    [
        f"<style>p::before{{content:attr(data-a) attr(data-b)}}</style>"
        f'<p data-a="{HEAD}" data-b="{TAIL}"></p>',
        f'<style>p::before{{content:"{HEAD}" "{TAIL}"}}</style><p></p>',
        f"<p style=\"--x:1; con\\74 ent: '{HEAD}'\">x</p>",
        "<style>p{color:red}/* \\67 " + TOKEN[1:] + " */</style>",
        '<style>p{background:url("\\67 ' + TOKEN[1:] + '")}</style>',
        # other properties that render author strings
        f'<q style=\'quotes:"{HEAD}" "{TAIL}"\'></q>',
        f"<li style='list-style-type:\"{HEAD}\";list-style-position:inside'>{TAIL}</li>",
        f"<style>@counter-style t{{system:cyclic;symbols:x;suffix:{TAIL}}}</style>",
        # parser desync: a self-closed raw-text tag, and foreign content
        f'<style/>p::before{{content:"{HEAD}" "{TAIL}"}}</style><p>x</p>',
        f'<svg><style>p::before{{c&#111;ntent:"{HEAD}" "{TAIL}"}}</style></svg><p></p>',
        # resources loaded from CSS
        '<style>@import url("data:text/css,p{}");</style>',
        '<p style="background:url(d\\61ta:image/svg+xml,x)">x</p>',
        "<style>p{background:url(http://example.test/x.png)}</style>",
        "<style>p{background:src(http://example.test/x.png)}</style>",
        # an abruptly closed comment does not hide the style after it
        "<!--><style>p::before{content:'\\67" + TOKEN[1:] + "'}</style><p>x</p><!-- -->",
        "<!---><style>p::before{content:'\\67" + TOKEN[1:] + "'}</style><p>x</p><!-- -->",
    ],
)
def test_css_that_can_show_text_or_hides_a_credential_is_refused(env: Any, content: str) -> None:
    _refused(env, content)


@pytest.mark.parametrize(
    "content",
    [
        f'<img alt="{HEAD}">{TAIL}',
        f'<input value="{HEAD}"><input value="{TAIL}">',
        f'<abbr title="{"".join(f"&#{ord(c)};" for c in TOKEN)}">x</abbr>',
        f'<input placeholder="{HEAD}&#{ord(TAIL[0])};{TAIL[1:]}">',
    ],
)
def test_attribute_text_is_screened(env: Any, content: str) -> None:
    _refused(env, content)


@pytest.mark.parametrize(
    "content",
    [f'<input placeholder="{TOKEN}">', f'<p data-k="{TOKEN}">x</p>'],
)
def test_a_whole_credential_in_one_attribute_publishes_redacted(env: Any, content: str) -> None:
    out = _publish(env, content)
    assert out.ok, out.text
    assert TOKEN not in _calls(env, "agents.conversations.setView")[0]["content"]


@pytest.mark.parametrize(
    "content",
    [
        "<style>p{color:#333;margin:0 auto}@media (max-width:600px){p{font-size:12px}}</style>"
        "<p class=x>Hello</p>",
        '<img alt="Build graph" src="https://example.test/g.png"><input value="ready">',
        '<a href="https://x.test/?a=1&amp;b=2" title="Docs">docs</a>',
        '<p style="color:red">contents of the report</p>',
        '<p title="Raw data: 5 rows" class="content">x</p>',
        '<img alt="data: none" src="https://example.test/a.png"><input value="a, data: b">',
        "<!-- a note --><p>after the comment</p>",
        "<style>.content:hover{color:red}"
        "p{background:url(https://example.test/bg.png)}</style><p>x</p>",
    ],
)
def test_ordinary_views_still_publish(env: Any, content: str) -> None:
    out = _publish(env, content)
    assert out.ok, out.text
    assert _calls(env, "agents.conversations.setView")[0]["content"] == content


#: A static todo-app page as an agent writes one for a preview: quoted font
#: names, attribute selectors, an empty ``content`` box, url() and a script
#: (passed through as before: Slack sandboxes the view).
TODO_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Todo</title>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, 'Helvetica Neue', Arial, sans-serif;
         margin: 0; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); }
  h1 { font: 600 2rem/1.2 "Inter", system-ui, sans-serif; color: #fff; }
  .app { display: grid; grid-template-areas: "head" "list"; max-width: 32rem; margin: 2rem auto; }
  input[type="text"], button[aria-label='Add'] { font-family: inherit; padding: .5rem; }
  li.done::before { content: ""; display: inline-block; width: 1em; height: 1em; }
  code { font-family: "SF Mono", ui-monospace, monospace; font-feature-settings: "liga" 0; }
  .hero { background: url("https://example.test/bg.png") center / cover no-repeat; }
  @media (max-width: 600px) { .app { margin: 0; } h1 { font-family: "Inter"; } }
</style>
</head>
<body>
<main class="app">
  <h1>My todos</h1>
  <form><input type="text" placeholder="What needs doing?"><button aria-label="Add">Add</button></form>
  <ul><li class="done">Buy milk</li><li>Ship the preview</li></ul>
</main>
<script>document.querySelector("form").addEventListener("submit", e => e.preventDefault());</script>
</body>
</html>
"""


def test_a_static_preview_page_is_sent_as_an_html_view(env: Any) -> None:
    out = _publish(env, TODO_PAGE)
    assert out.ok, out.text
    (call,) = _calls(env, "agents.conversations.setView")
    assert call["type"] == "html" and call["content"] == TODO_PAGE


@pytest.mark.parametrize(
    "content",
    [
        "<p style=\"font-family: 'Segoe UI', sans-serif\">x</p>",
        "<style>p{font-family:f\\6f nt}</style>",  # an escaped property name
        '<style>a[href^="https://"]::after{content:""}</style>',
        "<style>.a{color:red;&[data-x='1']{color:blue}}</style>",  # a nested rule
        '<style>@charset "utf-8"; p{color:red}</style>',
        "<style>p{background:url( 'https://example.test/a.png' )}</style>",
        '<style>p{font-family:"/*"; color:red} /* "x" */</style>',
    ],
)
def test_quoted_css_that_shows_nothing_publishes(env: Any, content: str) -> None:
    out = _publish(env, content)
    assert out.ok, out.text


@pytest.mark.parametrize(
    ("content", "hint"),
    [
        ('<style>li::before{content:"\\2713"}</style>', "put text to show in the HTML"),
        ('<style>:root{--f:"Inter"}</style>', "name fonts directly in font-family"),
        ("<style>p{\\63 ontent:'x'}</style>", "put text to show in the HTML"),
        # a string after a ``;`` a browser reads as a new declaration
        ('<style>p{font-family:"a"; content:"x"}</style>', "put text to show in the HTML"),
        # a stray closer would put the scanner a level off from the browser
        ('<style>p{font-family:x (}) ; content:"x"}</style>', "unmatched bracket"),
        ("<p style=\"font-family:x}; content:'x'\">x</p>", "unmatched bracket"),
        # the comment opener sits in a string, so the content after it is CSS
        ('<style>p{font-family:"/*"; content:"x" /* */}</style>', "put text to show"),
        ('<style>@media "x" {p{}}</style>', "quoted string in @media"),
        # declarations directly in @scope apply to its root
        ('<style>@scope (.a) { content: "x" }</style>', "put text to show"),
        # a browser may end this rule at its block and read on as declarations
        ('<style>.a{content:"x" {} ;}</style>', "put text to show"),
        ('<style>.a{font-family:"a" {} content:"x"}</style>', "put text to show"),
        ('<style>p{counter-reset:x}p::after{content:counters(x,".")}</style>', "show"),
        ("<style>@import 'https://example.test/x.css';</style>", "put the stylesheet"),
        (
            "<style>@font-face{font-family:F;src:url(https://example.test/f.woff)}</style>",
            "name an installed font",
        ),
        ("<style>p{background:url('data:image/png;base64,AA')}</style>", "style it with CSS"),
        ('<style>p{background:url("file:///etc/passwd")}</style>', "style it with CSS"),
        ('<style>p{background:src("http://example.test/a.png")}</style>', "style it with CSS"),
        ('<style>p{font-family:"a\nb"}</style>', "line break"),
        ('<img src="data:image/png;base64,AAAA" alt="logo">', "draw it with HTML and CSS"),
        ('<svg viewBox="0 0 1 1"></svg>', "plain HTML elements"),
    ],
)
def test_a_refusal_says_what_to_change(env: Any, content: str, hint: str) -> None:
    out = _publish(env, content)
    assert not out.ok and "nothing was published" in out.text and hint in out.text, out.text
    assert _calls(env, "agents.conversations.setView") == []


def test_a_whole_credential_in_an_allowed_css_string_publishes_redacted(env: Any) -> None:
    out = _publish(env, f'<style>p{{font-family:"{TOKEN}"}}</style><p>x</p>')
    assert out.ok, out.text
    assert TOKEN not in _calls(env, "agents.conversations.setView")[0]["content"]


@pytest.mark.parametrize(
    "content",
    [
        f"<style>p{{font-family:'\\67 {TOKEN[1:]}'}}</style>",
        # a line continuation a browser drops inside a string
        f'<style>p{{font-family:"{HEAD}\\\n{TAIL}"}}</style><p>x</p>',
        f'<style>p{{font-family:"{HEAD}\\\r\n{TAIL}"}}</style><p>x</p>',
        f"<style>a[title='{HEAD}\\\n{TAIL}']{{color:red}}</style>",
        f"<p style=\"font-family:'{HEAD}\\&#10;{TAIL}'\">x</p>",
        f'<style>p{{background:url("https://x.test/{HEAD}\\\n{TAIL}")}}</style>',
        # a URL parser drops a tab, LF or CR an escape or reference puts in
        f'<style>p{{background:url("https://x.test/{HEAD}\\9 {TAIL}")}}</style><p>x</p>',
        f"<style>p{{background:url(https://x.test/{HEAD}\\a {TAIL})}}</style><p>x</p>",
        f'<a href="https://x.test/{HEAD}&#9;{TAIL}">x</a>',
        f'<img src="https://x.test/{HEAD}&#13;{TAIL}" alt="x">',
    ],
)
def test_a_credential_hidden_in_an_allowed_css_string_is_refused(env: Any, content: str) -> None:
    _refused(env, content)
