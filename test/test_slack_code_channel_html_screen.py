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

TOKEN = "ghp_" + "A" * 36
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
        "<style>p{color:red}/* \\67 hp_" + "A" * 36 + " */</style>",
        '<style>p{background:url("\\67 hp_' + "A" * 36 + '")}</style>',
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
        "<!--><style>p::before{content:'\\67hp_" + "A" * 36 + "'}</style><p>x</p><!-- -->",
        "<!---><style>p::before{content:'\\67hp_" + "A" * 36 + "'}</style><p>x</p><!-- -->",
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
