"""Channel outbound sinks strip agent control-tag comments (#7948 round 6).

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
        # Round-8 GPT: a message ending inside an UNTERMINATED fence renders
        # the tail tag line as literal code — visible content. The
        # fence-parity guard rejects the match on BOTH recognizers (the
        # frontend applies the identical guard), reversing round 7's
        # tail-wins reading.
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
