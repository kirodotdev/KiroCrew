"""Unit tests for mcp_playwright_proxy — compression, framing, error handling."""

from kiro_crew import mcp_playwright_proxy as proxy
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


class TestScreenshotDir:
    """The screenshot dir must use tempfile.gettempdir(), not a hardcoded
    ``/tmp`` fallback that fails on Windows where ``/tmp`` does not exist."""

    def test_uses_platform_tempdir(self):
        import tempfile

        # The module resolves _SCREENSHOT_DIR at import time from
        # tempfile.gettempdir(); assert the result lives under the current
        # process's platform-native temp root.
        assert proxy._SCREENSHOT_DIR.startswith(tempfile.gettempdir())
        assert proxy._SCREENSHOT_DIR.endswith("kirocrew-screenshots")

    def test_source_uses_tempfile_gettempdir_not_hardcoded_slash_tmp(self):
        # Regression: the old code used ``os.environ.get("TMPDIR", "/tmp")``
        # whose fallback did not exist on Windows and crashed os.makedirs.
        # Read the source to prove the constant is derived from tempfile,
        # without reload side effects on the module-level pump state.
        import inspect

        src = inspect.getsource(proxy)
        assert "tempfile.gettempdir()" in src
        # And the deprecated hardcoded fallback is no longer present.
        assert 'os.environ.get("TMPDIR", "/tmp")' not in src


class TestResolvePlaywrightCmd:
    """Regression: on Windows npx is ``npx.CMD``. The resolver must return the
    RESOLVED path (not the bare name, which CreateProcess cannot spawn), and
    run_proxy must still inject the @playwright/mcp arg for a ``.CMD`` launcher.
    """

    def test_returns_resolved_npx_path_not_bare_name(self, monkeypatch):
        monkeypatch.delenv("KIROCREW_PLAYWRIGHT_CMD", raising=False)

        def fake_which(name, **kw):
            # Standalone binaries absent; only npx resolves, as npx.CMD.
            return r"C:\node\npx.CMD" if name == "npx" else None

        monkeypatch.setattr(proxy.shutil, "which", fake_which)
        resolved = proxy._resolve_playwright_cmd()
        assert resolved == r"C:\node\npx.CMD"
        # The bug returned the bare "npx"; the fix returns the full path.
        assert resolved != "npx"

    def test_run_proxy_injects_playwright_arg_for_cmd_launcher(self, monkeypatch):
        # The extension-insensitive basename check must still add @playwright/mcp
        # when the resolved launcher is npx.CMD, else a bare interactive npx runs.
        # Build the path with the HOST separator: the product parses it with the
        # host os.path, so a hardcoded C:\...\npx.CMD would not parse on Linux CI.
        import os as _os

        launcher = _os.path.join("node-bin", "npx.CMD")
        monkeypatch.setattr(proxy, "_resolve_playwright_cmd", lambda: launcher)
        captured = {}

        class _FakeProc:
            returncode = 0

            def __init__(self, cmd, **kw):
                captured["cmd"] = cmd
                captured["env"] = kw.get("env") or {}
                self.stdin = None
                self.stdout = None

            def wait(self, timeout=None):
                return 0

        monkeypatch.setattr(proxy.subprocess, "Popen", _FakeProc)
        # No-op the stdin forwarder thread and end the relay loop immediately so
        # run_proxy returns right after building + spawning the command.
        monkeypatch.setattr(proxy.threading, "Thread", lambda *a, **k: _NoopThread())
        monkeypatch.setattr(proxy, "_read_message", lambda *a, **k: None)

        try:
            proxy.run_proxy([])
        except SystemExit:
            pass

        assert captured["cmd"][0] == launcher
        # ``--yes`` (npx flag) precedes the pinned package spec.
        assert captured["cmd"][1] == "--yes"
        # No version pinned in this isolated home -> falls back to @latest.
        assert captured["cmd"][2] == "@playwright/mcp@latest"
        # The public registry is pinned in the child env so a private/stale-token
        # default .npmrc cannot 401 this public package.
        assert captured["env"].get("npm_config_registry") == proxy.PUBLIC_NPM_REGISTRY

    def test_run_proxy_launches_pinned_version_when_recorded(self, monkeypatch):
        # When the enable-time prime recorded a version, the runtime launches THAT
        # exact spec (offline-deterministic, no drift), not @latest.
        import os as _os

        launcher = _os.path.join("node-bin", "npx")
        monkeypatch.setattr(proxy, "_resolve_playwright_cmd", lambda: launcher)
        from kiro_crew.browser import setup as _setup

        monkeypatch.setattr(_setup, "get_pinned_playwright_version", lambda: "0.0.78")
        captured = {}

        class _FakeProc:
            returncode = 0

            def __init__(self, cmd, **kw):
                captured["cmd"] = cmd
                captured["env"] = kw.get("env") or {}
                self.stdin = None
                self.stdout = None

            def wait(self, timeout=None):
                return 0

        monkeypatch.setattr(proxy.subprocess, "Popen", _FakeProc)
        monkeypatch.setattr(proxy.threading, "Thread", lambda *a, **k: _NoopThread())
        monkeypatch.setattr(proxy, "_read_message", lambda *a, **k: None)

        try:
            proxy.run_proxy([])
        except SystemExit:
            pass

        assert captured["cmd"][2] == "@playwright/mcp@0.0.78"
        # prefer-offline so a cached pinned version launches without a registry
        # round-trip (offline host still starts); the registry pin still applies for
        # the fetch-when-missing case.
        assert captured["env"].get("npm_config_prefer_offline") == "true"
        assert captured["env"].get("npm_config_registry") == proxy.PUBLIC_NPM_REGISTRY

    def test_run_proxy_does_not_pin_registry_for_binary_launcher(self, monkeypatch):
        # A standalone binary launcher is not an npm fetch, so no registry pin is
        # injected — we must not perturb the env for a non-npx launcher.
        launcher = "/usr/local/bin/mcp-server-playwright"
        monkeypatch.setattr(proxy, "_resolve_playwright_cmd", lambda: launcher)
        captured = {}

        class _FakeProc:
            returncode = 0

            def __init__(self, cmd, **kw):
                captured["cmd"] = cmd
                captured["env"] = kw.get("env") or {}
                self.stdin = None
                self.stdout = None

            def wait(self, timeout=None):
                return 0

        monkeypatch.setattr(proxy.subprocess, "Popen", _FakeProc)
        monkeypatch.setattr(proxy.threading, "Thread", lambda *a, **k: _NoopThread())
        monkeypatch.setattr(proxy, "_read_message", lambda *a, **k: None)
        monkeypatch.delenv("npm_config_registry", raising=False)

        try:
            proxy.run_proxy([])
        except SystemExit:
            pass

        assert captured["cmd"] == [launcher]
        assert "npm_config_registry" not in captured["env"]


class _NoopThread:
    def start(self):
        pass

    def join(self, timeout=None):
        pass
