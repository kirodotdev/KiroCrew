"""Tests for the file-explorer builtin app backend (server.py).

Covers path safety, sensitive path blocking, directory listing, file reading,
search, git status parsing, and HTTP handler routing. Targets ≥60% new line
coverage for Coverlay.
"""

import os
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
        """Check sensitive dirs by path component (excluding .kirocrew which is handled
        granularly)."""
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

    def test_crew_home_nonsafe_file_is_sensitive(self, tmp_tree):
        # config.json (Slack tokens) is NOT a safe subdir → blocked under both
        # crew-home spellings.
        assert server._is_sensitive(tmp_tree / ".kiro" / "crew" / "config.json") is True
        assert server._is_sensitive(tmp_tree / ".kirocrew" / "config.json") is True

    def test_crew_home_marker_match_is_case_insensitive(self, tmp_tree):
        # SECURITY regression: on a case-INSENSITIVE filesystem (macOS/Windows)
        # ~/.KIRO/crew/config.json opens the same inode as ~/.kiro/crew but
        # Path.resolve() keeps the typed case. A case-SENSITIVE marker match would
        # let the uppercase path slip past deny-by-default and leak the crew
        # home's non-keystone files (config.json Slack tokens, sessions, PII).
        # _crew_home_index must casefold both sides so all spellings are blocked.
        for variant in (
            tmp_tree / ".KIRO" / "crew" / "config.json",
            tmp_tree / ".kiro" / "CREW" / "config.json",
            tmp_tree / ".KiRo" / "CrEw" / "sessions" / "s.json",
            tmp_tree / ".KIROCREW" / "config.json",
        ):
            assert server._is_sensitive(variant) is True, variant

    def test_crew_home_safe_subdir_still_allowed(self, tmp_tree):
        # The fix must not over-block: a genuine safe subdir stays accessible.
        assert server._is_sensitive(tmp_tree / ".kiro" / "crew" / "workspace" / "a.txt") is False

    def test_crew_home_root_detection_case_insensitive(self, tmp_tree):
        assert server._is_crew_home_root(tmp_tree / ".KIRO" / "crew") is True
        assert server._is_crew_home_root(tmp_tree / ".kiro" / "crew") is True

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
                pytest.skip(
                    f"ambient .git on walk-up path ({cand}/.git) - premise not testable here"
                )
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


class TestSearchTccPruning:
    """macOS TCC: a search rooted at the bare ``$HOME`` must not descend into the
    gated top-level folders. Recursing into ``~/Pictures`` (the Photos library)
    or ``~/Music`` (the media library) makes macOS pop a per-folder consent
    dialog -- once for the bundled ``rg`` binary and again for the Python
    fallback, which is why the same folder gets prompted more than once. A search
    rooted AT such a folder (root != ``$HOME``) is deliberate and stays fully
    searched. Off macOS the prune is a no-op.
    """

    @staticmethod
    def _home_ctx(tmp_tree):
        # expanduser("~") reads $HOME on POSIX and %USERPROFILE% on Windows;
        # pin both so the "is root the home directory" test in platform_compat
        # fires on every CI shard.
        return patch.dict(
            os.environ,
            {"HOME": str(tmp_tree), "USERPROFILE": str(tmp_tree)},
            clear=False,
        )

    def test_python_fallback_prunes_gated_dirs_at_home(self, tmp_tree):
        for name in ("Pictures", "Music", "Downloads"):
            (tmp_tree / name).mkdir()
            (tmp_tree / name / "note.txt").write_text("needle here")
        (tmp_tree / "code").mkdir()
        (tmp_tree / "code" / "keep.txt").write_text("needle here")
        with patch.object(server.platform_compat, "IS_MACOS", True), self._home_ctx(tmp_tree):
            results = server._search_python(tmp_tree, "needle", "", "")
        names = {Path(r["file"]).name for r in results}
        assert "keep.txt" in names, "a non-gated top-level dir must still be searched"
        assert "note.txt" not in names, "Pictures/Music/Downloads must not be descended"

    def test_python_fallback_scoped_gated_dir_is_searched(self, tmp_tree):
        music = tmp_tree / "Music"
        music.mkdir()
        (music / "note.txt").write_text("needle here")
        # root == ~/Music (NOT $HOME) -> deliberate navigation, searched in full.
        with patch.object(server.platform_compat, "IS_MACOS", True), self._home_ctx(tmp_tree):
            results = server._search_python(music, "needle", "", "")
        assert any(Path(r["file"]).name == "note.txt" for r in results)

    def test_python_fallback_no_prune_off_macos(self, tmp_tree):
        (tmp_tree / "Music").mkdir()
        (tmp_tree / "Music" / "note.txt").write_text("needle here")
        with patch.object(server.platform_compat, "IS_MACOS", False), self._home_ctx(tmp_tree):
            results = server._search_python(tmp_tree, "needle", "", "")
        assert any(Path(r["file"]).name == "note.txt" for r in results)

    def _capture_rg_cmd(self, root, home):
        captured: dict = {}

        def fake_run(cmd, *a, **k):
            captured["cmd"] = list(cmd)
            return MagicMock(stdout="", returncode=0)

        with patch.object(server.platform_compat, "IS_MACOS", True), self._home_ctx(home), \
                patch.object(server, "cgroup_scope_argv", lambda argv: argv), \
                patch.object(server, "resource_limit_preexec", lambda: None), \
                patch("subprocess.run", fake_run):
            server._search_rg(root, "needle", "", "")
        return captured["cmd"]

    def test_rg_excludes_gated_dirs_at_home(self, tmp_tree):
        cmd = self._capture_rg_cmd(tmp_tree, tmp_tree)
        assert "!/Pictures" in cmd
        assert "!/Music" in cmd

    def test_rg_no_tcc_globs_for_scoped_root(self, tmp_tree):
        music = tmp_tree / "Music"
        music.mkdir()
        # HOME is tmp_tree; the search root is ~/Music, so it is NOT home.
        cmd = self._capture_rg_cmd(music, tmp_tree)
        assert "!/Music" not in cmd
        assert "!/Pictures" not in cmd


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

    def test_health_reports_home_dir(self, tmp_tree):
        """Health payload carries the resolved home dir so the frontend can
        open there by default instead of guessing from allowedRoots (the old
        heuristic picked /opt on macOS)."""
        responses = self._make_request("/health")
        code, body = responses[0]
        assert code == 200
        assert body["home"] == str(tmp_tree)
        assert body["home"] in body["allowedRoots"]

    def test_allowed_roots_order_is_deterministic_home_first(self, tmp_path):
        """_compute_allowed_roots must preserve insertion order (home first) —
        the frontend falls back to roots[0], so a set-based dedupe would make
        the default root random across restarts."""
        home = tmp_path / "home-dir"
        tmp = tmp_path / "tmp-dir"
        for d in (home, tmp):
            d.mkdir()
        roots = server._compute_allowed_roots(home, tmp)
        assert roots[0] == home
        assert roots[1] == tmp
        # Dedupe keeps first occurrence: home==tmp collapses to one entry.
        same = server._compute_allowed_roots(home, home)
        assert same[0] == home
        assert same.count(home) == 1

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


class TestKirocrewGranularSensitive:
    """Regression tests for .kirocrew granular sensitive path policy.

    .kirocrew/workspace/, uploads/, skills/, artifacts/ etc. should be accessible.
    .kirocrew/config.json, sessions/, *.key should be blocked.
    """

    @pytest.fixture(autouse=True)
    def kirocrew_tree(self, tmp_tree):
        """Create a .kirocrew directory structure for testing."""
        mc = tmp_tree / ".kirocrew"
        mc.mkdir()
        # Safe subdirs
        (mc / "workspace").mkdir()
        (mc / "workspace" / "notes").mkdir()
        (mc / "workspace" / "notes" / "update.md").write_text("# Update\nContent here")
        (mc / "uploads").mkdir()
        (mc / "uploads" / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 50)
        (mc / "skills").mkdir()
        (mc / "skills" / "my-skill").mkdir()
        (mc / "skills" / "my-skill" / "SKILL.md").write_text("# Skill\n")
        (mc / "artifacts").mkdir()
        (mc / "artifacts" / "data.json").write_text("{}")
        # Sensitive items
        (mc / "config.json").write_text('{"secret": "token123"}')
        (mc / "sel_hmac.key").write_text("secret-key-data")
        (mc / "token_signing.key").write_text("signing-key-data")
        (mc / "sessions").mkdir()
        (mc / "sessions" / "sess-001.json").write_text('{"auth": "tok"}')
        (mc / "memory.db").write_text("sqlite-binary-data")
        # Governance trust-root files (the fork keystone): the security
        # ceiling, profiles, and admission policy must NEVER be reachable
        # through the explorer — "profiles" is deliberately absent from
        # _KIROCREW_SAFE_SUBDIRS.
        (mc / "security_policy.json").write_text('{"ceiling": true}')
        (mc / "admission_policy.json").write_text('{"admission": true}')
        (mc / "profiles").mkdir()
        (mc / "profiles" / "default.json").write_text('{"profile": true}')
        self.mc = mc
        return tmp_tree

    def test_workspace_notes_accessible(self, tmp_tree):
        """User notes under .kirocrew/workspace/ should be readable."""
        p = server._safe_path(str(tmp_tree / ".kirocrew" / "workspace" / "notes" / "update.md"))
        assert p.exists()

    def test_uploads_accessible(self, tmp_tree):
        """Uploaded images should be accessible."""
        p = server._safe_path(str(tmp_tree / ".kirocrew" / "uploads" / "image.png"))
        assert p.exists()

    def test_skills_accessible(self, tmp_tree):
        """Skills directory should be accessible."""
        p = server._safe_path(str(tmp_tree / ".kirocrew" / "skills" / "my-skill" / "SKILL.md"))
        assert p.exists()

    def test_artifacts_accessible(self, tmp_tree):
        """Artifacts should be accessible."""
        p = server._safe_path(str(tmp_tree / ".kirocrew" / "artifacts" / "data.json"))
        assert p.exists()

    def test_config_json_blocked(self, tmp_tree):
        """config.json contains tokens — must be blocked."""
        with pytest.raises(server.PathError) as exc_info:
            server._safe_path(str(tmp_tree / ".kirocrew" / "config.json"))
        assert exc_info.value.status == 403

    def test_key_files_blocked(self, tmp_tree):
        """Signing keys must be blocked."""
        with pytest.raises(server.PathError) as exc_info:
            server._safe_path(str(tmp_tree / ".kirocrew" / "sel_hmac.key"))
        assert exc_info.value.status == 403

    def test_sessions_blocked(self, tmp_tree):
        """Sessions directory (auth tokens) must be blocked."""
        with pytest.raises(server.PathError) as exc_info:
            server._safe_path(str(tmp_tree / ".kirocrew" / "sessions" / "sess-001.json"))
        assert exc_info.value.status == 403

    def test_memory_db_blocked(self, tmp_tree):
        """memory.db contains full conversation transcripts — must be blocked."""
        with pytest.raises(server.PathError) as exc_info:
            server._safe_path(str(tmp_tree / ".kirocrew" / "memory.db"))
        assert exc_info.value.status == 403

    def test_kirocrew_root_listing_blocked_by_safe_path(self, tmp_tree):
        """Listing .kirocrew/ root is blocked at _safe_path level (deny-by-default).
        Tree/complete handlers use _kirocrew_safe_children() instead."""
        with pytest.raises(server.PathError) as exc_info:
            server._safe_path(str(tmp_tree / ".kirocrew"))
        assert exc_info.value.status == 403

    def test_kirocrew_safe_children_returns_only_safe_subdirs(self, tmp_tree):
        """_kirocrew_safe_children() exposes only allowlisted dirs."""
        mc = tmp_tree / ".kirocrew"
        entries = server._kirocrew_safe_children(mc)
        names = {e["name"] for e in entries}
        # Only dirs in _KIROCREW_SAFE_SUBDIRS should appear
        assert "workspace" in names
        assert "uploads" in names
        assert "skills" in names
        assert "artifacts" in names
        # Sensitive items must not appear
        assert "sessions" not in names
        assert "config.json" not in names

    # ── Governance trust-root keystone (fork-only additions) ──
    # ~/.kirocrew/security_policy.json, profiles/, admission_policy.json are
    # the governance ceiling's trust root. The granular branch alone must
    # block them (is_sensitive_path is mocked to SENSITIVE_DIRS parts in this
    # suite) — "profiles" must stay OUT of _KIROCREW_SAFE_SUBDIRS.

    def test_security_policy_blocked(self, tmp_tree):
        """The governance security ceiling must be blocked."""
        with pytest.raises(server.PathError) as exc_info:
            server._safe_path(str(tmp_tree / ".kirocrew" / "security_policy.json"))
        assert exc_info.value.status == 403

    def test_admission_policy_blocked(self, tmp_tree):
        """The signed-plugin admission policy must be blocked."""
        with pytest.raises(server.PathError) as exc_info:
            server._safe_path(str(tmp_tree / ".kirocrew" / "admission_policy.json"))
        assert exc_info.value.status == 403

    def test_governance_profile_blocked(self, tmp_tree):
        """Governance profiles (per-surface ceilings) must be blocked."""
        with pytest.raises(server.PathError) as exc_info:
            server._safe_path(str(tmp_tree / ".kirocrew" / "profiles" / "default.json"))
        assert exc_info.value.status == 403

    def test_profiles_not_in_safe_children_or_listing(self, tmp_tree):
        """'profiles' never appears in safe-children output or root listings."""
        mc = tmp_tree / ".kirocrew"
        assert "profiles" not in server._KIROCREW_SAFE_SUBDIRS
        child_names = {e["name"] for e in server._kirocrew_safe_children(mc)}
        assert "profiles" not in child_names
        entries, _ = server._list_dir(mc, depth=1)
        listing_names = {e["name"] for e in entries}
        assert "profiles" not in listing_names
        assert "security_policy.json" not in listing_names
        assert "admission_policy.json" not in listing_names


class TestAutoSdeRound1Findings:
    """Regression tests for review-bot round-1 findings on the granular policy.

    #15 security-controls: listing .kirocrew/ root must not leak sensitive
        entry NAMES (config.json, *.key, memory.db, sessions/).
    #17 rg allowlist side-effect: searching a root OUTSIDE .kirocrew must not
        restrict results to .kirocrew safe subdirs (non-negated globs
        allowlist-restrict ripgrep); searching INSIDE a safe subdir adds no
        .kirocrew globs at all.
    #16 auto-skill-namespace: the file explorer must remain read-only (GET
        only) so opening skills/ cannot create an auto-skill write path.
    """

    @pytest.fixture(autouse=True)
    def kirocrew_tree(self, tmp_tree):
        mc = tmp_tree / ".kirocrew"
        mc.mkdir()
        (mc / "workspace").mkdir()
        (mc / "workspace" / "note.md").write_text("needle in workspace\n")
        (mc / "config.json").write_text('{"secret": "token123"}')
        (mc / "sel_hmac.key").write_text("secret-key-data")
        (mc / "sessions").mkdir()
        (mc / "memory.db").write_text("db")
        (tmp_tree / "src").mkdir()
        (tmp_tree / "src" / "main.py").write_text("needle in src\n")
        self.mc = mc
        return tmp_tree

    def test_root_listing_hides_sensitive_names(self, tmp_tree):
        """#15: /tree of .kirocrew root shows ONLY safe subdirs — no
        config.json / *.key / sessions / memory.db names."""
        entries, _ = server._list_dir(tmp_tree / ".kirocrew", depth=1)
        names = {e["name"] for e in entries}
        assert "workspace" in names
        for leaked in ("config.json", "sel_hmac.key", "sessions", "memory.db"):
            assert leaked not in names, f"sensitive name leaked in listing: {leaked}"

    def test_python_search_outside_kirocrew_finds_project_files(self, tmp_tree):
        """#17: searching the project root must return matches OUTSIDE
        .kirocrew (the old glob set silently excluded them under rg)."""
        results = server._search_python(tmp_tree, "needle", "", "")
        files = {r["file"] for r in results}
        assert any(
            f.endswith("src/main.py") for f in files
        ), f"src/main.py missing from results — allowlist side-effect: {files}"

    def test_python_search_never_surfaces_kirocrew_root_files(self, tmp_tree):
        """#15/defense: .kirocrew root files (config.json) never appear in
        search results even when the walk passes through .kirocrew."""
        results = server._search_python(tmp_tree, "token123", "", "")
        assert results == [], f".kirocrew root file content leaked: {results}"

    def test_rg_glob_set_has_no_nonnegated_kirocrew_globs(self, tmp_tree):
        """#17: the rg command for an outside-root search contains only
        NEGATED crew-home globs (a non-negated glob would allowlist-restrict
        the entire search). The data home spans two prefixes — the current
        ~/.kiro/crew and the pre-move legacy ~/.kirocrew — so both variants
        must appear and both must be negated."""
        captured: dict = {}

        def fake_wrap(cmd):
            captured["cmd"] = list(cmd)
            raise FileNotFoundError("intercepted before spawn")

        with patch.object(server, "wrap_argv", side_effect=fake_wrap):
            try:
                server._search_rg(tmp_tree, "needle", "", "")
            except Exception:
                pass
        cmd = captured.get("cmd")
        assert cmd, "wrap_argv never called — _search_rg did not build an rg command"
        globs = [cmd[i + 1] for i, a in enumerate(cmd[:-1]) if a == "--glob"]
        crew_globs = [g for g in globs if ".kiro/crew" in g or ".kirocrew" in g]
        assert crew_globs == [
            "!**/.kiro/crew/**",
            "!**/.kirocrew/**",
        ], crew_globs
        assert all(g.startswith("!") for g in crew_globs)

    def test_rg_no_kirocrew_globs_when_root_inside_safe_subdir(self, tmp_tree):
        """#17: searching inside .kirocrew/workspace adds no .kirocrew globs
        (path gate already validated the subtree)."""
        captured: dict = {}

        def fake_wrap(cmd):
            captured["cmd"] = list(cmd)
            raise FileNotFoundError("intercepted before spawn")

        with patch.object(server, "wrap_argv", side_effect=fake_wrap):
            try:
                server._search_rg(tmp_tree / ".kirocrew" / "workspace", "needle", "", "")
            except Exception:
                pass
        cmd = captured.get("cmd")
        assert cmd, "wrap_argv never called — _search_rg did not build an rg command"
        globs = [cmd[i + 1] for i, a in enumerate(cmd[:-1]) if a == "--glob"]
        assert not any(".kirocrew" in g for g in globs), globs

    def test_app_is_read_only_get_routes_only(self):
        """#16: the file explorer exposes no write verbs — every dispatch
        route is GET. A write path here would bypass the auto-skill guards
        (redaction, SEL audit, slug validation)."""
        import inspect

        src = inspect.getsource(server)
        for verb in ("do_POST", "do_PUT", "do_DELETE", "do_PATCH"):
            assert verb not in src, f"unexpected write verb handler: {verb}"

    def test_kirocrew_root_outside_allowed_roots_denied(self, tmp_tree, monkeypatch):
        """Security: a .kirocrew dir outside ALLOWED_ROOTS must be denied even
        via the special-case listing path in _h_tree. Exercises the _expand ->
        _is_within check that bypasses _safe_path."""
        import tempfile

        with tempfile.TemporaryDirectory() as attacker_dir:
            fake_mc = Path(attacker_dir) / ".kirocrew"
            fake_mc.mkdir()
            (fake_mc / "workspace").mkdir()
            (fake_mc / "workspace" / "stolen.md").write_text("secret data")
            # ALLOWED_ROOTS is already patched to tmp_tree only (autouse fixture)
            # so attacker_dir is NOT in ALLOWED_ROOTS.

            # Test 1: _safe_path blocks it (standard path)
            with pytest.raises(server.PathError) as exc_info:
                server._safe_path(str(fake_mc))
            assert exc_info.value.status == 403

            # Test 2: exercise the _h_tree special-case branch directly —
            # _expand + name==".kirocrew" + is_dir() all pass, but
            # ALLOWED_ROOTS gate must still block.
            expanded = server._expand(str(fake_mc))
            assert expanded.name == ".kirocrew"
            assert expanded.is_dir()
            # The handler's ALLOWED_ROOTS check:
            assert not any(
                server._is_within(expanded, root) for root in server.ALLOWED_ROOTS
            ), "attacker dir should NOT be within ALLOWED_ROOTS"
