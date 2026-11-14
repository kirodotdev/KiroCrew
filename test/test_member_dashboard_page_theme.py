"""``_page_theme``: the bounded set of page colour tokens the chat may follow.

The chat follows exactly the page's ``--bg`` and ``--accent`` tokens and nothing else,
so a free-form page palette cannot push an arbitrary custom property onto the chat.
"""

from __future__ import annotations

from kiro_crew.dashboard.handlers.member_dashboard import _page_theme


def test_extracts_only_the_two_reserved_tokens():
    package = {
        "theme": {
            "tokens": {
                "--bg": "#101418",
                "--accent": "#7c3aed",
                # A free-form token the page author picked: not followed by the chat.
                "--panel-glow": "0 0 8px red",
                "--heading": "#ffcc00",
            }
        }
    }
    assert _page_theme(package) == {"--bg": "#101418", "--accent": "#7c3aed"}


def test_absent_tokens_are_omitted_so_the_chat_keeps_its_look():
    # Only --accent declared: --bg is simply absent from the result, never ''.
    assert _page_theme({"theme": {"tokens": {"--accent": "#7c3aed"}}}) == {"--accent": "#7c3aed"}
    # No colour tokens at all -> empty, which is the default-off state as data.
    assert _page_theme({"theme": {"tokens": {"--panel-glow": "red"}}}) == {}


def test_a_missing_or_malformed_theme_is_the_empty_one():
    assert _page_theme({}) == {}
    assert _page_theme({"theme": None}) == {}
    assert _page_theme({"theme": {"tokens": None}}) == {}
    # A non-string value is not a colour the chat can use.
    assert _page_theme({"theme": {"tokens": {"--bg": 123, "--accent": ""}}}) == {}


def test_a_credential_shaped_token_is_dropped_not_returned_verbatim():
    # `_validate_theme` bounds only a token's shape, so an agent (or a
    # prompt-injected write) can slip a credential-shaped string into a colour
    # token. The route redacts every value it returns; a value the egress
    # redactor would rewrite is dropped rather than shipped, so no secret
    # reaches the client -- even with the preview off.
    package = {
        "theme": {
            "tokens": {
                "--bg": "ghp_0123456789abcdefghijklmnopqrstuvwxyzAB",
                "--accent": "#7c3aed",
            }
        }
    }
    # The secret-shaped --bg is gone; the ordinary colour --accent stays.
    assert _page_theme(package) == {"--accent": "#7c3aed"}
