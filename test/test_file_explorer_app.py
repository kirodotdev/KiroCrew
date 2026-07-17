"""Tests for the file-explorer builtin app backend (server.py).

Covers path safety, sensitive path blocking, directory listing, file reading,
search, git status parsing, and HTTP handler routing. Targets ≥60% new line
coverage for Coverlay.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew.apps.builtins.file_explorer import server


@pytest.fixture
def tmp_tree(tmp_path):
    """Create a temp directory tree for testing."""
    (tmp_path / "file.txt").write_text("hello world")
    (tmp_path / "code.py").write_text("print('hi')\n")
    (tmp_path / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
    (tmp_path / "subdir").mkdir()
    (tmp_path / "subdir" / "nested.md").write_text("# Title\n")
    (tmp_path / ".ssh").mkdir()
    (tmp_path / ".ssh" / "id_rsa").write_text("PRIVATE KEY")
    (tmp_path / ".git").mkdir()
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "pkg.js").write_text("module")
    return tmp_path


@pytest.fixture(autouse=True)
def patch_allowed_roots(tmp_tree):
    """Allow the tmp_path in ALLOWED_ROOTS and mock security/sel functions."""

    def mock_is_sensitive(path_str):
        """Check sensitive dirs by path component."""
        parts = Path(path_str).parts
        return any(s in parts for s in server.SENSITIVE_DIRS)

    def mock_wrap_argv(argv, mode="auto"):
        return argv, None

    with patch.object(server, "ALLOWED_ROOTS", [tmp_tree]):
        with patch.object(server, "_HOME", tmp_tree):
            with patch.object(server, "is_sensitive_path", mock_is_sensitive):
                with patch.object(server, "sel", MagicMock()):
                    with patch.object(server, "wrap_argv", mock_wrap_argv):
                        yield


class TestPathSafety:
    def test_expand_empty_raises(self):
        with pytest.raises(server.PathError, match="path is required"):
            server._expand("")

    def test_expand_resolves_path(self, tmp_tree):
        p = server._expand(str(tmp_tree / "file.txt"))
        assert p == tmp_tree / "file.txt"

    def test_safe_path_allowed(self, tmp_tree):
        p = server._safe_path(str(tmp_tree / "file.txt"))
        assert p == tmp_tree / "file.txt"

    def test_safe_path_outside_denied(self, tmp_tree):
        with pytest.raises(server.PathError) as exc_info:
            server._safe_path("/etc/passwd")
        assert exc_info.value.status == 403

    def test_safe_path_not_found(self, tmp_tree):
        with pytest.raises(server.PathError) as exc_info:
            server._safe_path(str(tmp_tree / "nonexistent"))
        assert exc_info.value.status == 404

    def test_safe_path_sensitive_blocked(self, tmp_tree):
        with pytest.raises(server.PathError) as exc_info:
            server._safe_path(str(tmp_tree / ".ssh" / "id_rsa"))
        assert exc_info.value.status == 403


class TestIsSensitive:
    def test_ssh_is_sensitive(self, tmp_tree):
        assert server._is_sensitive(tmp_tree / ".ssh" / "id_rsa") is True

    def test_aws_is_sensitive(self, tmp_tree):
        assert server._is_sensitive(Path("/home/otheruser/.aws/credentials")) is True

    def test_kirocrew_is_sensitive(self, tmp_tree):
        assert server._is_sensitive(tmp_tree / ".kirocrew" / ".env") is True

    def test_regular_file_not_sensitive(self, tmp_tree):
        assert server._is_sensitive(tmp_tree / "file.txt") is False

    def test_subdir_not_sensitive(self, tmp_tree):
        assert server._is_sensitive(tmp_tree / "subdir" / "nested.md") is False


class TestListDir:
    def test_lists_children(self, tmp_tree):
        entries, _ = server._list_dir(tmp_tree, depth=1, ignore=True)
        names = {e["name"] for e in entries}
        assert "file.txt" in names
        assert "subdir" in names

    def test_ignores_node_modules(self, tmp_tree):
        entries, _ = server._list_dir(tmp_tree, depth=1, ignore=True)
        names = {e["name"] for e in entries}
        assert "node_modules" not in names

    def test_ignores_sensitive_dirs(self, tmp_tree):
        entries, _ = server._list_dir(tmp_tree, depth=1, ignore=True)
        names = {e["name"] for e in entries}
        assert ".ssh" not in names

    def test_depth_2_includes_nested(self, tmp_tree):
        entries, _ = server._list_dir(tmp_tree, depth=2, ignore=True)
        subdir = next(e for e in entries if e["name"] == "subdir")
        assert "children" in subdir
        child_names = {c["name"] for c in subdir["children"]}
        assert "nested.md" in child_names

    def test_no_ignore_shows_all(self, tmp_tree):
        entries, _ = server._list_dir(tmp_tree, depth=1, ignore=False)
        names = {e["name"] for e in entries}
        assert "node_modules" in names


class TestFileHelpers:
    def test_file_kind_file(self, tmp_tree):
        assert server._file_kind(tmp_tree / "file.txt") == "file"

    def test_file_kind_dir(self, tmp_tree):
        assert server._file_kind(tmp_tree / "subdir") == "dir"

    def test_is_binary_file_png(self, tmp_tree):
        assert server._is_binary_file(tmp_tree / "image.png") is True

    def test_is_binary_file_text(self, tmp_tree):
        assert server._is_binary_file(tmp_tree / "file.txt") is False

    def test_guess_mime_md(self, tmp_tree):
        assert server._guess_mime(tmp_tree / "subdir" / "nested.md") == "text/markdown"

    def test_guess_mime_py(self, tmp_tree):
        mime = server._guess_mime(tmp_tree / "code.py")
        assert "python" in mime or "text" in mime

    def test_entry_meta_file(self, tmp_tree):
        meta = server._entry_meta(tmp_tree / "file.txt")
        assert meta["name"] == "file.txt"
        assert meta["type"] == "file"
        assert meta["size"] == 11

    def test_entry_meta_dir(self, tmp_tree):
        meta = server._entry_meta(tmp_tree / "subdir")
        assert meta["type"] == "dir"
        assert meta["size"] == 0


class TestGitStatus:
    def test_git_repo_root_found(self, tmp_tree):
        root = server._git_repo_root(tmp_tree / "subdir")
        assert root == tmp_tree

    def test_git_repo_root_not_found(self, tmp_path):
        # A path with NO .git in any parent returns None. Use an isolated tmp_path, NOT the
        # shared /tmp: some build hosts (incl. the brazil farm sandbox) have a stray .git on
        # the path above /tmp, and _git_repo_root walks to the filesystem root — an ambient
        # .git there legitimately defeats the premise. If one exists on this host's walk-up
        # path, skip (the _found test already proves the walk); else assert None.
        probe = tmp_path / "no_git_here"
        probe.mkdir()
        for cand in [probe, *probe.parents]:
            if (cand / ".git").exists():
                pytest.skip(f"ambient .git on walk-up path ({cand}/.git) - premise not testable here")
        assert server._git_repo_root(probe) is None

    def test_git_status_parsing(self, tmp_tree):
        """Test _git_status with a mocked subprocess."""
        with patch("subprocess.run") as mock_run:
            # Mock branch
            branch_result = MagicMock()
            branch_result.returncode = 0
            branch_result.stdout = "main\n"
            # Mock status
            status_result = MagicMock()
            status_result.returncode = 0
            status_result.stdout = " M file.txt\x00?? new.py\x00"
            mock_run.side_effect = [branch_result, status_result]

            result = server._git_status(tmp_tree)
            assert result["branch"] == "main"
            assert result["statuses"]["file.txt"] == "M"
            assert result["statuses"]["new.py"] == "??"

    def test_git_copy_entries_handled(self, tmp_tree):
        """Test that C (copy) entries skip the source path."""
        with patch("subprocess.run") as mock_run:
            branch_result = MagicMock()
            branch_result.returncode = 0
            branch_result.stdout = "main\n"
            status_result = MagicMock()
            status_result.returncode = 0
            # C100 dest.txt\x00src.txt\x00M other.txt\x00
            status_result.stdout = "C  dest.txt\x00src.txt\x00 M other.txt\x00"
            mock_run.side_effect = [branch_result, status_result]

            result = server._git_status(tmp_tree)
            assert "dest.txt" in result["statuses"]
            assert "other.txt" in result["statuses"]


class TestSearch:
    def test_search_python_finds_match(self, tmp_tree):
        results = server._search_python(tmp_tree, "hello", "", "")
        assert len(results) == 1
        assert results[0]["file"].endswith("file.txt")
        assert results[0]["line"] == 1

    def test_search_python_no_match(self, tmp_tree):
        results = server._search_python(tmp_tree, "zzzznotfound", "", "")
        assert len(results) == 0

    def test_search_python_respects_include_glob(self, tmp_tree):
        results = server._search_python(tmp_tree, "print", "*.py", "")
        assert len(results) == 1
        assert results[0]["file"].endswith("code.py")

    def test_search_python_skips_binary(self, tmp_tree):
        results = server._search_python(tmp_tree, "PNG", "", "")
        assert len(results) == 0

    def test_search_python_skips_sensitive_dirs(self, tmp_tree):
        results = server._search_python(tmp_tree, "PRIVATE", "", "")
        assert len(results) == 0

    def test_search_empty_query(self, tmp_tree):
        results = server._search(tmp_tree, "", "", "")
        assert results == []


class TestSelAudit:
    def test_sel_audit_calls_sel(self, tmp_tree):
        mock_sel_instance = MagicMock()
        mock_sel = MagicMock(return_value=mock_sel_instance)
        with patch.object(server, "sel", mock_sel):
            server._sel_audit("file_read", "/tmp/test")
            mock_sel_instance.log_api_access.assert_called_once()


class TestHTTPHandler:
    """Test the HTTP handler routing via a minimal mock."""

    def _make_request(self, path):
        """Create a mock handler and dispatch a GET request."""
        handler = server.FileExplorerHandler.__new__(server.FileExplorerHandler)
        handler.path = path
        handler.responses = []

        def mock_json(code, payload):
            handler.responses.append((code, payload))

        handler._json = mock_json
        try:
            handler._dispatch("GET")
        except server.PathError as exc:
            handler._json(exc.status, {"error": str(exc)})
        return handler.responses

    def test_health_endpoint(self, tmp_tree):
        responses = self._make_request("/health")
        assert len(responses) == 1
        code, body = responses[0]
        assert code == 200
        assert body["status"] == "ok"

    def test_resolve_endpoint(self, tmp_tree):
        responses = self._make_request(f"/resolve?path={tmp_tree}/file.txt")
        assert responses[0][0] == 200
        assert responses[0][1]["exists"] is True

    def test_tree_endpoint(self, tmp_tree):
        responses = self._make_request(f"/tree?path={tmp_tree}&depth=1")
        assert responses[0][0] == 200
        assert "entries" in responses[0][1]

    def test_read_endpoint(self, tmp_tree):
        responses = self._make_request(f"/read?path={tmp_tree}/file.txt")
        assert responses[0][0] == 200
        assert responses[0][1]["content"] == "hello world"

    def test_read_sensitive_blocked(self, tmp_tree):
        responses = self._make_request(f"/read?path={tmp_tree}/.ssh/id_rsa")
        assert responses[0][0] == 403

    def test_search_endpoint(self, tmp_tree):
        responses = self._make_request(f"/search?path={tmp_tree}&q=hello")
        assert responses[0][0] == 200
        assert len(responses[0][1]["results"]) == 1

    def test_complete_endpoint(self, tmp_tree):
        responses = self._make_request(f"/complete?path={tmp_tree}/")
        assert responses[0][0] == 200
        names = {e["name"] for e in responses[0][1]["entries"]}
        assert "file.txt" not in names  # kind=dir by default
        assert "subdir" in names

    def test_404_unknown_route(self, tmp_tree):
        responses = self._make_request("/unknown")
        assert responses[0][0] == 404

    def test_oversized_image_returns_binary(self, tmp_tree):
        """Images exceeding max_bytes should return binary=True, not garbage."""
        responses = self._make_request(f"/read?path={tmp_tree}/image.png&max_bytes=10")
        assert responses[0][0] == 200
        assert responses[0][1]["binary"] is True
        assert responses[0][1]["content"] == ""
