"""Unit tests for mcp_playwright_proxy — compression, framing, error handling."""

from kiro_crew.mcp_playwright_proxy import (
    _compress_to_outline,
    _is_accessibility_tree,
    _maybe_compress_response,
)

SAMPLE_TREE = """- navigation "Main" [ref=e1]:
  - link "Home" [ref=e2]
  - link "Products" [ref=e3]
  - link "About" [ref=e4]
- main [ref=e5]:
  - heading "Welcome to Amazon" [level=1] [ref=e6]
  - paragraph "Shop millions of products"
  - button "Sign In" [ref=e7]
  - textbox "Search" [ref=e8]
  - img "Product image" [ref=e9]
  - div:
    - div:
      - div:
        - span "decorative"
"""

LARGE_TREE = SAMPLE_TREE * 100  # ~10K chars


class TestIsAccessibilityTree:
    def test_detects_valid_tree(self):
        assert _is_accessibility_tree(SAMPLE_TREE) is True

    def test_rejects_plain_text(self):
        assert _is_accessibility_tree("Hello world. This is plain text.") is False

    def test_rejects_short_tree(self):
        assert _is_accessibility_tree("- link \"Home\"") is False

    def test_detects_tree_with_many_elements(self):
        tree = "- button \"A\"\n- heading \"B\"\n- link \"C\"\n- navigation \"D\"\n"
        assert _is_accessibility_tree(tree) is True


class TestCompressToOutline:
    def test_keeps_interactive_elements(self):
        result = _compress_to_outline(SAMPLE_TREE)
        assert "link" in result
        assert "button" in result
        assert "heading" in result
        assert "textbox" in result
        assert "img" in result
        assert "[ref=e2]" in result

    def test_strips_decorative_elements(self):
        result = _compress_to_outline(SAMPLE_TREE)
        assert "decorative" not in result
        assert "paragraph" not in result

    def test_returns_original_if_no_interactive(self):
        plain = "line 1\nline 2\nline 3\n"
        result = _compress_to_outline(plain)
        assert result == plain

    def test_includes_element_count_header(self):
        result = _compress_to_outline(SAMPLE_TREE)
        assert "[Compressed:" in result
        assert "interactive]" in result

    def test_truncates_at_max_lines(self):
        result = _compress_to_outline(LARGE_TREE)
        lines = result.split("\n")
        assert any("truncated" in line for line in lines)

    def test_preserves_refs(self):
        result = _compress_to_outline(SAMPLE_TREE)
        assert "[ref=e7]" in result
        assert "[ref=e8]" in result


class TestMaybeCompressResponse:
    def test_compresses_large_tree_response(self):
        msg = {
            "jsonrpc": "2.0",
            "id": 3,
            "result": {
                "content": [{"type": "text", "text": LARGE_TREE}]
            },
        }
        result = _maybe_compress_response(msg)
        text = result["result"]["content"][0]["text"]
        assert "[Compressed:" in text
        assert len(text) < len(LARGE_TREE)

    def test_passes_through_small_response(self):
        msg = {
            "jsonrpc": "2.0",
            "id": 3,
            "result": {
                "content": [{"type": "text", "text": "Navigated to https://example.com"}]
            },
        }
        result = _maybe_compress_response(msg)
        assert result["result"]["content"][0]["text"] == "Navigated to https://example.com"

    def test_passes_through_non_tree_large_text(self):
        large_text = "This is a long article. " * 500
        msg = {
            "jsonrpc": "2.0",
            "id": 3,
            "result": {
                "content": [{"type": "text", "text": large_text}]
            },
        }
        result = _maybe_compress_response(msg)
        assert result["result"]["content"][0]["text"] == large_text

    def test_saves_image_to_file(self):
        import base64
        import os
        fake_img = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100).decode()
        msg = {
            "jsonrpc": "2.0",
            "id": 3,
            "result": {
                "content": [{"type": "image", "data": fake_img, "mimeType": "image/png"}]
            },
        }
        result = _maybe_compress_response(msg)
        content = result["result"]["content"][0]
        assert content["type"] == "text"
        assert "Screenshot saved:" in content["text"]
        filepath = content["text"].split(": ")[1].split("\n")[0]
        assert os.path.exists(filepath)
        os.unlink(filepath)

    def test_passes_through_error_response(self):
        msg = {
            "jsonrpc": "2.0",
            "id": 3,
            "error": {"code": -32000, "message": "Something failed"},
        }
        result = _maybe_compress_response(msg)
        assert result["error"]["message"] == "Something failed"

    def test_handles_missing_result(self):
        msg = {"jsonrpc": "2.0", "id": 3}
        result = _maybe_compress_response(msg)
        assert result == msg
