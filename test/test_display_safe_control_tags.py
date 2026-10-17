"""Channel outbound sinks strip agent control-tag comments.

The prompt rule only contains the EMITTER (channel sessions are never taught
the marker); these tests pin the deterministic backstop on the MESSAGE: a
dashboard-authored ``<!-- keep-visible -->`` delivered to a channel via the
neutral sinks must not reach end users as literal text, while tags quoted in
code (fenced or inline) stay visible.
"""

from kiro_crew.constants import strip_control_comments
from kiro_crew.messaging.renderer import display_safe, display_safe_for
from kiro_crew.messaging.transport import TransportCapabilities


class TestStripControlCommentsTailAnchoredFenceGuarded:
    def test_tag_inside_fence_is_preserved_and_trailing_line_stripped(self) -> None:
        # A tag quoted mid-message — in a fence, inline code, or prose — is
        # rendered content the tail-anchored grammar never touches; only the
        # trailing tag LINE is a control tag.
        text = "see\n```html\n<!-- keep-visible -->\n```\ndone\n<!-- keep-visible -->"
        out = strip_control_comments(text)
        assert "```html\n<!-- keep-visible -->\n```" in out
        assert out.rstrip().endswith("done")

    def test_tail_inside_unterminated_fence_is_visible_code(self) -> None:
        # A message ending inside an UNTERMINATED fence renders the tail tag
        # line as literal code — visible content — so the fence-parity guard
        # rejects the match on BOTH recognizers (the frontend applies the
        # identical guard). An unterminated fence wins over the tail position.
        text = "```\n<!-- deliver:slack -->"
        assert strip_control_comments(text) == text
        tilde = "~~~html\n<!-- keep-visible -->"
        assert strip_control_comments(tilde) == tilde

    def test_closed_fence_then_tail_tag_still_stripped(self) -> None:
        # Parity: a CLOSED fence earlier in the message does not disable the
        # tail strip.
        text = "```\ncode\n```\ndone\n<!-- keep-visible -->"
        assert strip_control_comments(text).rstrip().endswith("done")

    def test_marker_case_insensitive(self) -> None:
        # The frontend recognizer is /i; the backend grammar must match, or
        # a mirrored uppercase marker reaches channel users literally
        # (round-8 GPT validation finding).
        assert strip_control_comments("done\n<!-- KEEP-VISIBLE -->").rstrip() == "done"


class TestDisplaySafeStripsControlTags:
    def test_display_safe_strips_marker(self) -> None:
        out = display_safe("report body\n<!-- keep-visible -->")
        assert "keep-visible" not in out
        assert "report body" in out

    def test_display_safe_for_strips_marker_all_capability_shapes(self) -> None:
        for grammars in (False, True):
            caps = TransportCapabilities(mention_grammars=grammars)
            out = display_safe_for("done\n<!-- deliver:dashboard -->", caps)
            assert "deliver:" not in out

    def test_display_safe_keeps_quoted_tag_in_inline_code(self) -> None:
        out = display_safe("the `<!-- keep-visible -->` tag")
        assert "keep-visible" in out


class TestBrowserInstallMarkerStripped:
    """The dashboard-only browser install-card marker never reaches a channel.

    A ``dashboard:`` session mirrored to Slack gets the dashboard variant of the
    browser tool's not-set-up text, so its reply carries the marker; the mirror
    has no card to render, so the egress strip must remove it like the other
    control tags.
    """

    MARKER = "<!-- kirocrew:browser-install -->"

    def test_trailing_marker_stripped_and_prose_survives(self) -> None:
        text = f"Browser support needs a one-time install.\n\n{self.MARKER}\n"
        assert strip_control_comments(text) == "Browser support needs a one-time install.\n"

    def test_marker_whitespace_and_case_tolerance(self) -> None:
        # Same tolerance as the frontend detector (/<!--\s*kirocrew:browser-install\s*-->/i).
        for marker in (
            "<!--kirocrew:browser-install-->",
            "<!--   kirocrew:browser-install   -->",
            "<!-- KIROCREW:BROWSER-INSTALL -->",
        ):
            assert strip_control_comments(f"done\n{marker}") == "done", marker

    def test_display_safe_strips_marker(self) -> None:
        out = display_safe(f"report body\n{self.MARKER}")
        assert "kirocrew:browser-install" not in out
        assert "report body" in out

    def test_marker_quoted_as_code_stays_visible(self) -> None:
        fenced = f"```\n{self.MARKER}"
        assert strip_control_comments(fenced) == fenced
        assert "kirocrew:browser-install" in display_safe(f"write `{self.MARKER}` to offer it")

    def test_streaming_partial_marker_is_held(self) -> None:
        assert strip_control_comments("done\n<!-- kirocrew:browser-in", hide_partial=True) == "done"
        # A diverging sibling is an ordinary comment and is not held.
        assert strip_control_comments("done\n<!-- kirocrew:open", hide_partial=True) == (
            "done\n<!-- kirocrew:open"
        )

    def test_unrelated_kirocrew_comment_unchanged(self) -> None:
        # The open-file marker is not a control tag; its behaviour does not move.
        text = 'see the note\n<!-- kirocrew:open-file path="/a" -->'
        assert strip_control_comments(text) == text


class TestSharedConformanceCorpus:
    """Both recognizers assert the SAME corpus (Design round-9 parity pin).

    ``test/fixtures/control_tag_corpus.json`` is also asserted by the
    frontend suite (``website/src/test/keepVisibleMarker.test.ts``); grammar
    drift on either side goes red locally instead of shipping.
    """

    def test_backend_matches_corpus(self):
        import json
        import pathlib

        from kiro_crew.constants import strip_control_comments

        corpus = json.loads(
            (pathlib.Path(__file__).parent / "fixtures" / "control_tag_corpus.json").read_text()
        )
        for case in corpus["cases"]:
            got = strip_control_comments(case["input"])
            assert got == case["backend_stripped"], case["name"]
