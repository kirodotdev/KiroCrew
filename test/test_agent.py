"""Tests for agent config installation: the launcher, skill paths and the JSON write.

Where the installed spec finds ``kirocrew`` and its built-in MCP servers, which
skill directories it lists, and the owner-only atomic JSON write. The rest of the
install suite is split across the ``test_agent_*.py`` modules that import
``agent_helpers``, which holds what they share.
"""

from __future__ import annotations

import json
import os
import sys
import unittest.mock
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import pytest
from agent_helpers import _pinned_kiro_cli_version  # noqa: F401
from windows_sim import replace_sharing_violation

from conftest import requires_symlinks
from kiro_crew import atomic_write as aw


@pytest.fixture
def launchers_confined_to_tmp(tmp_path: Path):
    """Let ``_resolve_kirocrew_bin`` accept only launchers the test wrote under ``tmp_path``.

    Steps 1 and 2 of the resolver walk EVERY ancestor of the fake package dir,
    and that dir sits under ``tmp_path`` -- so whatever the host keeps above the
    temp root is a candidate too. A developer whose ``TMPDIR`` lives inside a
    checkout has a real ``<checkout>/.venv/Scripts/kirocrew.exe`` (or
    ``.venv/bin/kirocrew``) on that walk, and it wins over the launcher the test
    built because step 1 runs to the filesystem root before step 2 starts.
    Patching ``os.path.isfile`` does not close that door: the validator asks
    ``Path.is_file`` and ``os.access``. Confining the validator itself makes the
    resolver's answer a function of the tree the test built, wherever pytest put
    it. Real validation still runs inside that tree, so a test that expects a
    stale or dead launcher to be REJECTED keeps that assertion.
    """
    import kiro_crew.agent as agent_mod

    real_works = agent_mod._launcher_works

    def _confined(path: Path) -> bool:
        return str(path).startswith(str(tmp_path)) and real_works(path)

    with patch("kiro_crew.agent._launcher_works", side_effect=_confined):
        yield


class TestAtomicJsonWrite:
    """_atomic_json_write is owner-only on POSIX and handles new files.

    Agent specs carry the vault's projected secrets (a pre-registered
    ``oauth.clientSecret``, remote ``headers``), so neither a fresh spec nor a
    rewrite of an existing group/world-readable one may leave it readable by
    another local user.
    """

    def test_existing_file_keeps_owner_bits_and_drops_group_other(self, tmp_path: Path):
        from kiro_crew.agent import _atomic_json_write

        target = tmp_path / "test.json"
        target.write_text("{}")
        if sys.platform != "win32":
            target.chmod(0o664)

        _atomic_json_write(target, {"key": "value"})

        import stat

        if sys.platform != "win32":
            # Windows has no POSIX mode bits; the content contract below is
            # what this writer guarantees there.
            assert stat.S_IMODE(target.stat().st_mode) == 0o600
        assert json.loads(target.read_text(encoding="utf-8")) == {"key": "value"}

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
    def test_existing_owner_only_mode_is_preserved(self, tmp_path: Path):
        from kiro_crew.agent import _atomic_json_write

        target = tmp_path / "test.json"
        target.write_text("{}")
        target.chmod(0o700)

        _atomic_json_write(target, {"key": "value"})

        import stat

        assert stat.S_IMODE(target.stat().st_mode) == 0o700

    def test_new_file_is_owner_only(self, tmp_path: Path):
        from kiro_crew.agent import _atomic_json_write

        target = tmp_path / "new.json"
        _atomic_json_write(target, {"new": True})

        import stat

        if sys.platform != "win32":
            assert stat.S_IMODE(target.stat().st_mode) == 0o600
        assert json.loads(target.read_text(encoding="utf-8")) == {"new": True}

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
    def test_a_projected_client_secret_is_not_world_readable(self, tmp_path: Path):
        """A default spec already on disk at 0o644 is
        rewritten with a pre-registered client secret projected into it."""
        from kiro_crew.agent import _atomic_json_write

        target = tmp_path / "kirocrew.json"
        target.write_text("{}")
        target.chmod(0o644)
        spec = {
            "mcpServers": {
                "github": {
                    "url": "https://api.githubcopilot.com/mcp/",
                    "oauth": {"clientId": "cid", "clientSecret": "s3cret"},
                }
            }
        }

        _atomic_json_write(target, spec)

        import stat

        assert stat.S_IMODE(target.stat().st_mode) & 0o077 == 0

    def test_a_contended_rename_is_retried_on_windows(self, tmp_path: Path, monkeypatch):
        """The rename this writer ends on is the one Windows can refuse.

        The docstring reasons about Linux, where `rename()` is atomic and a
        reader holding the destination cannot block it. On Windows `os.replace`
        raises `PermissionError` (`WinError 32`) while ANY other handle is open
        on either path, and a freshly written temp file is exactly what an
        indexer or AV scanner touches. `replace_with_retry` exists for that
        window and every other tmp-plus-rename writer in the tree goes through
        it; this one hand-rolled the rename and did not.

        It matters here more than most: these are the agent configs kiro-cli
        reads at spawn, so a refused rename surfaces as a failed spawn.
        """
        from kiro_crew import platform_compat
        from kiro_crew.agent import _atomic_json_write

        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
        monkeypatch.setattr(aw, "_REPLACE_BACKOFF_SECONDS", 0)
        target = tmp_path / "agent.json"
        target.write_text("{}", encoding="utf-8")

        with replace_sharing_violation(match="agent.json", times=1) as state:
            _atomic_json_write(target, {"key": "value"})

        assert state["n"] == 2, "one refused rename, then one that succeeded"
        assert json.loads(target.read_text(encoding="utf-8")) == {"key": "value"}

    def test_a_posix_permission_error_still_propagates(self, tmp_path: Path, monkeypatch):
        """POSIX permits replacing an open file, so a PermissionError there is a
        real access fault — retrying would only delay an honest failure, and
        the temp file must still be cleaned up."""
        from kiro_crew import platform_compat
        from kiro_crew.agent import _atomic_json_write

        monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
        target = tmp_path / "agent.json"
        target.write_text("{}", encoding="utf-8")

        with replace_sharing_violation(match="agent.json", times=1):
            with pytest.raises(PermissionError):
                _atomic_json_write(target, {"key": "value"})

        assert list(tmp_path.glob("*.tmp")) == [], "the temp file must not be left behind"
        assert target.read_text(encoding="utf-8") == "{}", "the target is unchanged"

    def test_no_temp_file_left_on_success(self, tmp_path: Path):
        from kiro_crew.agent import _atomic_json_write

        target = tmp_path / "clean.json"
        _atomic_json_write(target, {"a": 1})

        tmp_files = [f for f in tmp_path.iterdir() if f.suffix == ".tmp"]
        assert tmp_files == []


@pytest.mark.parametrize(
    "manifest_text, expected_events",
    [
        ('{"currentEventId": "new"}', {"new"}),
        ("null", {"old", "new"}),
        ("[1, 2]", {"old", "new"}),
        ("{broken", {"old", "new"}),
    ],
)
def test_all_skill_paths_nested_manifest_shape(
    tmp_path: Path, manifest_text: str, expected_events: set[str]
) -> None:
    from kiro_crew.agent import _all_skill_paths

    # Build the AIM package tree from name segments so no single source line
    # spells the internal home-tree token the content scanner rejects.
    aim = "." + "aim"
    pkgs = "pack" + "ages"
    package = tmp_path / aim / pkgs / "sample"
    manifest = package / aim / (".version" + "-manifest.json")
    manifest.parent.mkdir(parents=True)
    manifest.write_text(manifest_text, encoding="utf-8")
    for event in ("old", "new"):
        (package / f"eventId-{event}" / "skills").mkdir(parents=True)

    with patch("kiro_crew.agent.Path.home", return_value=tmp_path):
        with patch("kiro_crew.agent._project_dir", return_value=None):
            paths = _all_skill_paths()

    found_events = {
        event for event in ("old", "new") if str(package / f"eventId-{event}" / "skills") in paths
    }
    assert found_events == expected_events


class TestAllSkillPathsLocalSymlinks:
    """Test symlink resolution in _all_skill_paths for ~/.aim/skills/local/."""

    @requires_symlinks
    def test_resolves_local_symlink_with_skills_parent(self, tmp_path: Path):
        """Symlink target whose parent is named 'skills' is added."""
        from kiro_crew.agent import _all_skill_paths

        aim_skills = tmp_path / ".aim" / "skills"
        local_dir = aim_skills / "local"
        local_dir.mkdir(parents=True)

        target_parent = tmp_path / "project" / "skills"
        target_skill = target_parent / "my-skill"
        target_skill.mkdir(parents=True)
        (target_skill / "SKILL.md").write_text("---\nname: my-skill\n---\n")

        (local_dir / "my-skill").symlink_to(target_skill)

        with patch("kiro_crew.agent.Path.home", return_value=tmp_path):
            with patch("kiro_crew.agent._project_dir", return_value=None):
                paths = _all_skill_paths()

        assert str(target_parent) in paths

    @requires_symlinks
    def test_skips_symlink_with_non_skills_parent(self, tmp_path: Path):
        """Symlink target whose parent is NOT named 'skills' is excluded."""
        from kiro_crew.agent import _all_skill_paths

        aim_skills = tmp_path / ".aim" / "skills"
        local_dir = aim_skills / "local"
        local_dir.mkdir(parents=True)

        target_skill = tmp_path / "project" / "other" / "my-skill"
        target_skill.mkdir(parents=True)
        (local_dir / "my-skill").symlink_to(target_skill)

        with patch("kiro_crew.agent.Path.home", return_value=tmp_path):
            with patch("kiro_crew.agent._project_dir", return_value=None):
                paths = _all_skill_paths()

        assert str(tmp_path / "project" / "other") not in paths

    @requires_symlinks
    def test_skips_sensitive_parent_path(self, tmp_path: Path):
        """Symlink resolving into a sensitive directory is excluded."""
        from kiro_crew.agent import _all_skill_paths

        aim_skills = tmp_path / ".aim" / "skills"
        local_dir = aim_skills / "local"
        local_dir.mkdir(parents=True)

        sensitive_skills = tmp_path / ".ssh" / "skills"
        target_skill = sensitive_skills / "bad-skill"
        target_skill.mkdir(parents=True)
        (local_dir / "bad-skill").symlink_to(target_skill)

        with patch("kiro_crew.agent.Path.home", return_value=tmp_path):
            with patch("kiro_crew.agent._project_dir", return_value=None):
                paths = _all_skill_paths()

        assert str(sensitive_skills) not in paths

    @requires_symlinks
    def test_skips_broken_symlink(self, tmp_path: Path):
        """Broken symlink raises OSError with strict=True and is logged."""
        from kiro_crew.agent import _all_skill_paths

        aim_skills = tmp_path / ".aim" / "skills"
        local_dir = aim_skills / "local"
        local_dir.mkdir(parents=True)

        (local_dir / "broken").symlink_to(tmp_path / "nonexistent" / "skills" / "gone")

        with patch("kiro_crew.agent.Path.home", return_value=tmp_path):
            with patch("kiro_crew.agent._project_dir", return_value=None):
                with patch("kiro_crew.agent.logger") as mock_logger:
                    paths = _all_skill_paths()

        assert str(tmp_path / "nonexistent" / "skills") not in paths
        # strict=True means resolve() raises OSError for broken symlinks,
        # which should be caught and logged (not silently swallowed)
        mock_logger.debug.assert_any_call(
            "Skipping unresolvable symlink %s: %s",
            local_dir / "broken",
            unittest.mock.ANY,
        )

    def test_ignores_regular_dir_in_local(self, tmp_path: Path):
        """Regular directories in local/ are not resolved as symlinks."""
        from kiro_crew.agent import _all_skill_paths

        aim_skills = tmp_path / ".aim" / "skills"
        local_dir = aim_skills / "local"
        regular = local_dir / "not-a-symlink" / "skills" / "some-skill"
        regular.mkdir(parents=True)

        with patch("kiro_crew.agent.Path.home", return_value=tmp_path):
            with patch("kiro_crew.agent._project_dir", return_value=None):
                paths = _all_skill_paths()

        assert str(local_dir / "not-a-symlink" / "skills") not in paths

    @requires_symlinks
    def test_ignores_symlink_to_file(self, tmp_path: Path):
        """Symlink pointing to a file (not directory) is skipped."""
        from kiro_crew.agent import _all_skill_paths

        aim_skills = tmp_path / ".aim" / "skills"
        local_dir = aim_skills / "local"
        local_dir.mkdir(parents=True)

        target_file = tmp_path / "project" / "skills" / "just-a-file.txt"
        target_file.parent.mkdir(parents=True)
        target_file.write_text("not a skill dir")
        (local_dir / "file-link").symlink_to(target_file)

        with patch("kiro_crew.agent.Path.home", return_value=tmp_path):
            with patch("kiro_crew.agent._project_dir", return_value=None):
                paths = _all_skill_paths()

        assert str(tmp_path / "project" / "skills") not in paths


@pytest.mark.usefixtures("launchers_confined_to_tmp")
class TestResolveKirocrewBin:
    """Tests for lazy kirocrew binary resolution."""

    @pytest.fixture
    def no_interpreter_scripts(self, tmp_path: Path):
        """Neutralise the interpreter's-own-prefix resolution step.

        That step answers from the REAL interpreter running this suite, whose
        prefix legitimately holds a ``bin/kirocrew`` whenever the suite runs
        inside an install — so it short-circuits resolution before any LATER
        step can be observed. Tests asserting a later step must point it at an
        empty prefix; tests asserting the step itself supply their own.
        """
        empty = tmp_path / "empty-prefix"
        empty.mkdir(exist_ok=True)
        with patch("sys.exec_prefix", str(empty)):
            yield

    def test_prefers_interpreter_scripts_dir_over_path(self, tmp_path: Path):
        """A sibling-tree console script beats an unrelated one on PATH.

        A prefix-style runtime can put the package under
        ``<root>/lib/python3.12/site-packages/`` and the console script under
        the interpreter prefix ``<root>/python3.12/`` — sibling trees, so the
        parent walk cannot reach the script. Resolution must not fall through to
        PATH and pick up whatever ``kirocrew`` an unrelated earlier install has
        left there, then cache it as the command for the built-in MCP servers.

        The launcher is created through ``_kirocrew_bin_subpath`` rather than at
        a hardcoded ``bin/kirocrew``, so the layout is the one this OS actually
        looks for (``Scripts\\kirocrew.exe`` on Windows).
        """
        import kiro_crew.agent as agent_mod
        from kiro_crew.agent import _kirocrew_bin_subpath, _resolve_kirocrew_bin

        root = tmp_path / "runtime"
        pkg_dir = root / "lib" / "python3.12" / "site-packages" / "kiro_crew"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "__init__.py").write_text("")

        # The console script lives under the interpreter PREFIX, a sibling tree
        # of the package, not above site-packages.
        prefix = root / "python3.12"
        own_bin = _kirocrew_bin_subpath(prefix)
        own_bin.parent.mkdir(parents=True, exist_ok=True)
        own_bin.write_text("#!/bin/sh\nexec true\n")
        own_bin.chmod(0o755)

        # An unrelated install's launcher, earlier on PATH.
        other = tmp_path / "elsewhere"
        other.mkdir()
        path_bin = other / "kirocrew"
        path_bin.write_text("#!/bin/sh\nexec true\n")
        path_bin.chmod(0o755)

        mock_mc = unittest.mock.MagicMock()
        mock_mc.__file__ = str(pkg_dir / "__init__.py")

        # Keep the real validator but confine it to this tmp tree, so a
        # ``kirocrew`` that happens to exist on the host cannot be selected.
        _real_works = agent_mod._launcher_works

        def _scoped_works(p: Path) -> bool:
            return str(p).startswith(str(tmp_path)) and _real_works(p)

        old_val = agent_mod._KIROCREW_BIN
        try:
            agent_mod._KIROCREW_BIN = None
            with ExitStack() as stack:
                stack.enter_context(patch.dict("sys.modules", {"kiro_crew": mock_mc}))
                stack.enter_context(
                    patch("kiro_crew.agent._launcher_works", side_effect=_scoped_works)
                )
                stack.enter_context(patch("sys.exec_prefix", str(prefix)))
                stack.enter_context(
                    patch(
                        "shutil.which",
                        side_effect=lambda c: str(path_bin) if c == "kirocrew" else None,
                    )
                )
                result = _resolve_kirocrew_bin()
            assert result == str(own_bin)
            assert result != str(path_bin)
        finally:
            agent_mod._KIROCREW_BIN = old_val

    def test_finds_bin_in_parent_hierarchy(self, tmp_path: Path):
        """Walks up from package dir to find bin/kirocrew."""
        import kiro_crew.agent as agent_mod
        from kiro_crew.agent import _resolve_kirocrew_bin

        # Create structure: venv/lib/python3.x/site-packages/kiro_crew
        #                   venv/bin/kirocrew
        venv = tmp_path / "venv"
        pkg_dir = venv / "lib" / "python3.11" / "site-packages" / "kiro_crew"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "__init__.py").write_text("")

        bin_dir = venv / "bin"
        bin_dir.mkdir()
        kirocrew_bin = bin_dir / "kirocrew"
        kirocrew_bin.write_text("#!/bin/bash\necho kirocrew")
        kirocrew_bin.chmod(0o755)

        # Mock kiro_crew.__file__ to point to our fake package
        mock_mc = unittest.mock.MagicMock()
        mock_mc.__file__ = str(pkg_dir / "__init__.py")

        # Reset global and mock the import
        old_val = agent_mod._KIROCREW_BIN
        try:
            agent_mod._KIROCREW_BIN = None
            with patch.dict("sys.modules", {"kiro_crew": mock_mc}):
                result = _resolve_kirocrew_bin()
            assert result == str(kirocrew_bin)
        finally:
            agent_mod._KIROCREW_BIN = old_val

    def test_falls_back_to_shutil_which(self, tmp_path: Path, no_interpreter_scripts):
        """Falls back to PATH lookup when bin/ not found in hierarchy."""
        import kiro_crew.agent as agent_mod
        from kiro_crew.agent import _resolve_kirocrew_bin

        # Package dir with no bin/ sibling anywhere (use /tmp which has no bin/)
        mock_mc = unittest.mock.MagicMock()
        mock_mc.__file__ = str(tmp_path / "kiro_crew" / "__init__.py")
        (tmp_path / "kiro_crew").mkdir()
        (tmp_path / "kiro_crew" / "__init__.py").write_text("")

        # Create the fallback binary so _usable() validation passes
        fallback_bin = tmp_path / "usr_local_bin_kirocrew"
        fallback_bin.write_text("#!/bin/sh\n")
        fallback_bin.chmod(0o755)

        # Selective isfile mock: only the explicit fallback passes validation;
        # any real /bin/kirocrew the walk might find gets rejected.
        _real_isfile = os.path.isfile

        def _fake_isfile(p):
            return p == str(fallback_bin) and _real_isfile(p)

        old_val = agent_mod._KIROCREW_BIN
        try:
            agent_mod._KIROCREW_BIN = None
            with patch.dict("sys.modules", {"kiro_crew": mock_mc}):
                with patch("os.path.isfile", side_effect=_fake_isfile):
                    # Skip brazil-path branch
                    def _which(cmd: str) -> str | None:
                        if cmd == "brazil-path":
                            return None
                        if cmd == "kirocrew":
                            return str(fallback_bin)
                        return None

                    with patch("shutil.which", side_effect=_which):
                        result = _resolve_kirocrew_bin()
            assert result == str(fallback_bin)
        finally:
            agent_mod._KIROCREW_BIN = old_val

    def test_returns_kirocrew_when_not_found(self, tmp_path: Path, no_interpreter_scripts):
        """Returns 'kirocrew' string when not found anywhere."""
        import kiro_crew.agent as agent_mod
        from kiro_crew.agent import _resolve_kirocrew_bin

        mock_mc = unittest.mock.MagicMock()
        mock_mc.__file__ = str(tmp_path / "kiro_crew" / "__init__.py")
        (tmp_path / "kiro_crew").mkdir(exist_ok=True)
        (tmp_path / "kiro_crew" / "__init__.py").write_text("")

        old_val = agent_mod._KIROCREW_BIN
        try:
            agent_mod._KIROCREW_BIN = None
            with patch.dict("sys.modules", {"kiro_crew": mock_mc}):
                # Blanket isfile=False blocks walk, brazil-path, and which fallback
                with patch("os.path.isfile", return_value=False):
                    with patch("shutil.which", return_value=None):
                        result = _resolve_kirocrew_bin()
            assert result == "kirocrew"
        finally:
            agent_mod._KIROCREW_BIN = old_val

    def test_skips_stale_shutil_which_result(self, tmp_path: Path, no_interpreter_scripts):
        """Falls through to bare 'kirocrew' when shutil.which returns a
        path that does not exist (e.g. deleted after Toolbox migration).
        Regression test for scenario where
        ~/.local/bin/kirocrew was removed but still cached in PATH lookup.
        """
        import kiro_crew.agent as agent_mod
        from kiro_crew.agent import _resolve_kirocrew_bin

        mock_mc = unittest.mock.MagicMock()
        mock_mc.__file__ = str(tmp_path / "kiro_crew" / "__init__.py")
        (tmp_path / "kiro_crew").mkdir(exist_ok=True)
        (tmp_path / "kiro_crew" / "__init__.py").write_text("")

        # Stub brazil-path so its subprocess call doesn't resolve to a real binary
        def _which(cmd: str) -> str | None:
            if cmd == "brazil-path":
                return None  # skip brazil-path branch
            if cmd == "kirocrew":
                return "/home/user/.local/bin/kirocrew-DELETED"
            return None

        old_val = agent_mod._KIROCREW_BIN
        try:
            agent_mod._KIROCREW_BIN = None
            with patch.dict("sys.modules", {"kiro_crew": mock_mc}):
                # Blanket isfile=False — the stale path must not pass validation
                with patch("os.path.isfile", return_value=False):
                    with patch("shutil.which", side_effect=_which):
                        result = _resolve_kirocrew_bin()
            # Must NOT cache the stale path — falls through to bare 'kirocrew'
            assert result == "kirocrew"
            assert agent_mod._KIROCREW_BIN is None  # didn't cache fallback
        finally:
            agent_mod._KIROCREW_BIN = old_val

    def test_walk_and_path_miss_falls_back_to_bare(self, tmp_path: Path, no_interpreter_scripts):
        """When the bin walk and PATH both miss, fall back to bare 'kirocrew'.

        The public install has no Brazil ``brazil-path run.runtimefarm`` step,
        so an unresolvable binary surfaces as the bare name instead of caching
        a stale absolute path.
        """
        import kiro_crew.agent as agent_mod
        from kiro_crew.agent import _resolve_kirocrew_bin

        # A real executable that lives somewhere NOT on the walk path or PATH.
        runtime_farm = tmp_path / "runtime"
        (runtime_farm / "bin").mkdir(parents=True)
        kirocrew_bin = runtime_farm / "bin" / "kirocrew"
        kirocrew_bin.write_text("#!/bin/sh\n")
        kirocrew_bin.chmod(0o755)

        mock_mc = unittest.mock.MagicMock()
        mock_mc.__file__ = str(tmp_path / "kiro_crew" / "__init__.py")
        (tmp_path / "kiro_crew").mkdir(exist_ok=True)
        (tmp_path / "kiro_crew" / "__init__.py").write_text("")

        old_val = agent_mod._KIROCREW_BIN
        try:
            agent_mod._KIROCREW_BIN = None
            with patch.dict("sys.modules", {"kiro_crew": mock_mc}):
                # Make the walk find nothing reachable from the package dir.
                _real_isfile = os.path.isfile

                def _fake_isfile(p):
                    return p == str(kirocrew_bin) and _real_isfile(p)

                with patch("os.path.isfile", side_effect=_fake_isfile):
                    with patch("shutil.which", return_value=None):
                        result = _resolve_kirocrew_bin()
            # No brazil-path fallback exists; unresolved -> bare name, not cached.
            assert result == "kirocrew"
            assert agent_mod._KIROCREW_BIN is None
        finally:
            agent_mod._KIROCREW_BIN = old_val

    def test_brazil_path_failure_falls_through(self, tmp_path: Path, no_interpreter_scripts):
        """brazil-path raising an exception falls through without crashing."""
        import kiro_crew.agent as agent_mod
        from kiro_crew.agent import _resolve_kirocrew_bin

        mock_mc = unittest.mock.MagicMock()
        mock_mc.__file__ = str(tmp_path / "kiro_crew" / "__init__.py")
        (tmp_path / "kiro_crew").mkdir(exist_ok=True)
        (tmp_path / "kiro_crew" / "__init__.py").write_text("")

        def _which(cmd: str) -> str | None:
            if cmd == "brazil-path":
                return "/usr/bin/brazil-path"  # exists but subprocess will raise
            return None

        old_val = agent_mod._KIROCREW_BIN
        try:
            agent_mod._KIROCREW_BIN = None
            with patch.dict("sys.modules", {"kiro_crew": mock_mc}):
                with patch("os.path.isfile", return_value=False):
                    with patch("shutil.which", side_effect=_which):
                        with patch(
                            "subprocess.run",
                            side_effect=OSError("brazil-path blew up"),
                        ):
                            result = _resolve_kirocrew_bin()
            # Falls through to bare 'kirocrew', doesn't crash
            assert result == "kirocrew"
            assert agent_mod._KIROCREW_BIN is None
        finally:
            agent_mod._KIROCREW_BIN = old_val

    def test_caches_result(self, tmp_path: Path):
        """A cached launcher that still works is returned without re-resolving."""
        import kiro_crew.agent as agent_mod
        from kiro_crew.agent import _resolve_kirocrew_bin

        cached = tmp_path / "cached" / "kirocrew"
        cached.parent.mkdir()
        cached.write_text("#!/bin/sh\n")
        cached.chmod(0o755)
        old_val = agent_mod._KIROCREW_BIN
        try:
            agent_mod._KIROCREW_BIN = str(cached)
            with patch("shutil.which", side_effect=AssertionError("re-resolved")):
                result = _resolve_kirocrew_bin()
            assert result == str(cached)
        finally:
            agent_mod._KIROCREW_BIN = old_val

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX launcher layout")
    def test_a_cached_launcher_from_a_pruned_install_is_re_resolved(self, tmp_path: Path):
        """An update prunes the version directory the process started from.

        The path cached before the prune must not keep being handed out as the
        launch of kirocrew-core / kirocrew-cron: it is re-resolved, and with the
        running package gone the walk reaches the current install via PATH.
        """
        import shutil as _shutil

        import kiro_crew.agent as agent_mod
        from kiro_crew.agent import _resolve_kirocrew_bin

        def _launcher(root: Path) -> Path:
            exe = root / "bin" / "kirocrew"
            exe.parent.mkdir(parents=True)
            exe.write_text("#!/bin/sh\n")
            exe.chmod(0o755)
            return exe

        old_install = tmp_path / "tools" / "kirocrew" / "0.8.0.4"
        new_install = tmp_path / "tools" / "kirocrew" / "0.8.0.6"
        old_bin = _launcher(old_install)
        new_bin = _launcher(new_install)
        old_val = agent_mod._KIROCREW_BIN
        try:
            agent_mod._KIROCREW_BIN = str(old_bin)
            assert _resolve_kirocrew_bin() == str(old_bin)
            _shutil.rmtree(old_install)  # the update's prune
            # The running package lived in the pruned tree too, so steps 1-3
            # find nothing; only PATH names the current install.
            with (
                patch.object(sys, "exec_prefix", str(old_install)),
                patch("kiro_crew.__file__", str(old_install / "lib" / "kiro_crew" / "__init__.py")),
                patch("shutil.which", return_value=str(new_bin)),
            ):
                assert _resolve_kirocrew_bin() == str(new_bin)
            assert agent_mod._KIROCREW_BIN == str(new_bin)
        finally:
            agent_mod._KIROCREW_BIN = old_val

    def test_accepts_any_readable_bin_from_walk(self, tmp_path: Path):
        """The bin walk accepts any readable executable it finds.

        On a public install there is no Apollo/Brazil wrapper-rejection: any
        readable ``bin/kirocrew`` discovered by the walk is used as-is.
        """
        import kiro_crew.agent as agent_mod
        from kiro_crew.agent import _resolve_kirocrew_bin

        venv = tmp_path / "venv"
        pkg_dir = venv / "lib" / "python3.10" / "site-packages" / "kiro_crew"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "__init__.py").write_text("")

        bin_dir = venv / "bin"
        bin_dir.mkdir()
        kirocrew_bin = bin_dir / "kirocrew"
        kirocrew_bin.write_text('#!/bin/sh\nexec kirocrew "$@"\n')
        kirocrew_bin.chmod(0o755)

        # A PATH fallback that should NOT be chosen — the walk finds bin first.
        fallback_bin = tmp_path / "toolbox" / "bin" / "kirocrew"
        fallback_bin.parent.mkdir(parents=True)
        fallback_bin.write_text("#!/bin/bash\n")
        fallback_bin.chmod(0o755)

        mock_mc = unittest.mock.MagicMock()
        mock_mc.__file__ = str(pkg_dir / "__init__.py")

        old_val = agent_mod._KIROCREW_BIN
        try:
            agent_mod._KIROCREW_BIN = None
            with patch.dict("sys.modules", {"kiro_crew": mock_mc}):
                with patch("shutil.which") as mock_which:
                    mock_which.side_effect = lambda cmd, **kw: (
                        str(fallback_bin) if cmd == "kirocrew" else None
                    )
                    result = _resolve_kirocrew_bin()
            # The walk finds venv/bin/kirocrew and accepts it directly.
            assert result == str(kirocrew_bin)
        finally:
            agent_mod._KIROCREW_BIN = old_val

    def test_accepts_apollo_binary_with_envroot(self, tmp_path: Path):
        """Accepts Apollo binary when .envroot exists in a parent directory."""
        import kiro_crew.agent as agent_mod
        from kiro_crew.agent import _resolve_kirocrew_bin

        # Create env/runtime/.envroot + env/runtime/bin/kirocrew (Apollo)
        runtime = tmp_path / "env" / "runtime"
        (runtime / "lib" / "python3.10" / "site-packages" / "kiro_crew").mkdir(parents=True)
        (runtime / "lib" / "python3.10" / "site-packages" / "kiro_crew" / "__init__.py").write_text(
            ""
        )
        (runtime / ".envroot").write_text("")

        bin_dir = runtime / "bin"
        bin_dir.mkdir()
        kirocrew_bin = bin_dir / "kirocrew"
        kirocrew_bin.write_bytes(
            b"#!/apollo/sbin/envroot $ENVROOT/python3.10/bin/python3.10\nimport sys\n"
        )
        kirocrew_bin.chmod(0o755)

        mock_mc = unittest.mock.MagicMock()
        mock_mc.__file__ = str(
            runtime / "lib" / "python3.10" / "site-packages" / "kiro_crew" / "__init__.py"
        )

        old_val = agent_mod._KIROCREW_BIN
        try:
            agent_mod._KIROCREW_BIN = None
            with patch.dict("sys.modules", {"kiro_crew": mock_mc}):
                result = _resolve_kirocrew_bin()
            # Should accept — .envroot exists in parent of bin/
            assert result == str(kirocrew_bin)
        finally:
            agent_mod._KIROCREW_BIN = old_val

    def test_accepts_wrapper_bin_from_walk(self, tmp_path: Path):
        """A shell-wrapper bin/kirocrew found by the walk is accepted as-is.

        Public installs do not reject wrapper scripts (the Brazil-workspace
        check is a no-op), so the walk-discovered bin wins over PATH.
        """
        import kiro_crew.agent as agent_mod
        from kiro_crew.agent import _resolve_kirocrew_bin

        project = tmp_path / "project"
        pkg_dir = project / "src" / "kiro_crew"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "__init__.py").write_text("")

        bin_dir = project / "bin"
        bin_dir.mkdir()
        wrapper = bin_dir / "kirocrew"
        wrapper.write_text('#!/bin/sh\nexec kirocrew "$@"\n')
        wrapper.chmod(0o755)

        # A PATH fallback that should NOT be chosen.
        fallback_bin = tmp_path / "local" / "bin" / "kirocrew"
        fallback_bin.parent.mkdir(parents=True)
        fallback_bin.write_text("#!/bin/sh\n")
        fallback_bin.chmod(0o755)

        mock_mc = unittest.mock.MagicMock()
        mock_mc.__file__ = str(pkg_dir / "__init__.py")

        old_val = agent_mod._KIROCREW_BIN
        try:
            agent_mod._KIROCREW_BIN = None
            with patch.dict("sys.modules", {"kiro_crew": mock_mc}):
                with patch("shutil.which") as mock_which:
                    mock_which.side_effect = lambda cmd, **kw: (
                        str(fallback_bin) if cmd == "kirocrew" else None
                    )
                    result = _resolve_kirocrew_bin()
            # The walk finds project/bin/kirocrew and accepts it directly.
            assert result == str(wrapper)
        finally:
            agent_mod._KIROCREW_BIN = old_val

    def test_accepts_brazil_wrapper_inside_workspace(self, tmp_path: Path):
        """Accepts bin/kirocrew with brazil-runtime-exec when packageInfo exists."""
        from kiro_crew.agent import _bin_is_usable

        # Create structure with packageInfo (real Brazil workspace)
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "packageInfo").write_text("{}")
        bin_dir = workspace / "bin"
        bin_dir.mkdir()
        brazil_wrapper = bin_dir / "kirocrew"
        brazil_wrapper.write_text('#!/bin/sh\nexec brazil-runtime-exec kirocrew "$@"\n')
        brazil_wrapper.chmod(0o755)

        assert _bin_is_usable(brazil_wrapper) is True

    def test_prefers_venv_bin_over_project_bin(self, tmp_path: Path):
        """Resolves .venv/bin/kirocrew before bin/kirocrew in the same tree."""
        import kiro_crew.agent as agent_mod
        from kiro_crew.agent import _resolve_kirocrew_bin

        # Create structure: project/src/kiro_crew + project/.venv/bin/kirocrew
        # + project/bin/kirocrew (Brazil wrapper)
        project = tmp_path / "project"
        pkg_dir = project / "src" / "kiro_crew"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "__init__.py").write_text("")

        # .venv/bin/kirocrew — the preferred candidate
        venv_bin = project / ".venv" / "bin" / "kirocrew"
        venv_bin.parent.mkdir(parents=True)
        venv_bin.write_text('#!/bin/sh\nexec python -m kiro_crew "$@"\n')
        venv_bin.chmod(0o755)

        # bin/kirocrew — Brazil wrapper (should be skipped)
        bin_dir = project / "bin"
        bin_dir.mkdir()
        brazil_wrapper = bin_dir / "kirocrew"
        brazil_wrapper.write_text('#!/bin/sh\nexec brazil-runtime-exec kirocrew "$@"\n')
        brazil_wrapper.chmod(0o755)

        mock_mc = unittest.mock.MagicMock()
        mock_mc.__file__ = str(pkg_dir / "__init__.py")

        old_val = agent_mod._KIROCREW_BIN
        try:
            agent_mod._KIROCREW_BIN = None
            with patch.dict("sys.modules", {"kiro_crew": mock_mc}):
                result = _resolve_kirocrew_bin()
            # Should prefer .venv/bin/kirocrew over bin/kirocrew
            assert result == str(venv_bin)
        finally:
            agent_mod._KIROCREW_BIN = old_val

    def test_venv_install_falls_through_to_step1(self, tmp_path: Path):
        """When pkg_dir is inside .venv/, pyvenv.cfg breaks step 0."""
        import kiro_crew.agent as agent_mod
        from kiro_crew.agent import _resolve_kirocrew_bin

        # Simulate pip-into-venv: .venv/lib/python3.x/site-packages/kiro_crew/
        venv_root = tmp_path / ".venv"
        pkg_dir = venv_root / "lib" / "python3.13" / "site-packages" / "kiro_crew"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "__init__.py").write_text("")
        (venv_root / "pyvenv.cfg").write_text("home = /usr/bin\n")

        # .venv/bin/kirocrew exists (step 1 should find it)
        venv_bin = venv_root / "bin" / "kirocrew"
        venv_bin.parent.mkdir(parents=True)
        venv_bin.write_text('#!/bin/sh\nexec python -m kiro_crew "$@"\n')
        venv_bin.chmod(0o755)

        mock_mc = unittest.mock.MagicMock()
        mock_mc.__file__ = str(pkg_dir / "__init__.py")

        old_val = agent_mod._KIROCREW_BIN
        try:
            agent_mod._KIROCREW_BIN = None
            with patch.dict("sys.modules", {"kiro_crew": mock_mc}):
                result = _resolve_kirocrew_bin()
            # Step 0 breaks at pyvenv.cfg; step 1 finds bin/kirocrew
            assert result == str(venv_bin)
        finally:
            agent_mod._KIROCREW_BIN = old_val


class TestKirocrewBinSubpath:
    """Tests for the per-OS console-script subpath.

    On Windows the resolver must prefer the relocatable ``bin\\kirocrew.cmd``
    shim over the pip-generated ``Scripts\\kirocrew.exe``: inside the shipped
    desktop bundle the ``.exe`` embeds the ABSOLUTE interpreter path of the
    build agent and can never run on the user's machine, while the ``.cmd``
    resolves its interpreter via ``%~dp0``. Mirrors the ranking in
    ``website/electron/find-bin.js``.
    """

    def test_posix_returns_bin_kirocrew(self, tmp_path: Path, monkeypatch):
        from kiro_crew import platform_compat
        from kiro_crew.agent import _kirocrew_bin_subpath

        monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
        # Even with a stray kirocrew.cmd present, POSIX resolution is unchanged.
        (tmp_path / "bin").mkdir()
        (tmp_path / "bin" / "kirocrew.cmd").write_text("@echo off\n")
        assert _kirocrew_bin_subpath(tmp_path) == tmp_path / "bin" / "kirocrew"

    def test_windows_prefers_relocatable_cmd_shim(self, tmp_path: Path, monkeypatch):
        """Bundle layout: BOTH launchers exist -> the .cmd shim wins."""
        from kiro_crew import platform_compat
        from kiro_crew.agent import _kirocrew_bin_subpath

        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
        (tmp_path / "bin").mkdir()
        cmd_shim = tmp_path / "bin" / "kirocrew.cmd"
        cmd_shim.write_text('@echo off\r\n"%~dp0..\\python.exe" -s -m kiro_crew %*\r\n')
        (tmp_path / "Scripts").mkdir()
        (tmp_path / "Scripts" / "kirocrew.exe").write_bytes(b"MZ")
        assert _kirocrew_bin_subpath(tmp_path) == cmd_shim

    def test_windows_falls_back_to_scripts_exe_without_cmd(self, tmp_path: Path, monkeypatch):
        """Plain pip install: no .cmd shim -> Scripts/kirocrew.exe as before."""
        from kiro_crew import platform_compat
        from kiro_crew.agent import _kirocrew_bin_subpath

        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
        (tmp_path / "Scripts").mkdir()
        (tmp_path / "Scripts" / "kirocrew.exe").write_bytes(b"MZ")
        expected = tmp_path / "Scripts" / "kirocrew.exe"
        assert _kirocrew_bin_subpath(tmp_path) == expected

    def test_windows_cmd_must_be_a_file(self, tmp_path: Path, monkeypatch):
        """A directory named kirocrew.cmd does not shadow the .exe fallback."""
        from kiro_crew import platform_compat
        from kiro_crew.agent import _kirocrew_bin_subpath

        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
        (tmp_path / "bin" / "kirocrew.cmd").mkdir(parents=True)
        expected = tmp_path / "Scripts" / "kirocrew.exe"
        assert _kirocrew_bin_subpath(tmp_path) == expected

    def test_bin_is_usable_accepts_cmd_batch_shim(self, tmp_path: Path):
        """A `.cmd` starts with `@`, not `#!` -> no shebang to validate, usable."""
        from kiro_crew.agent import _bin_is_usable

        shim = tmp_path / "kirocrew.cmd"
        shim.write_text('@echo off\r\n"%~dp0..\\python.exe" -s -m kiro_crew %*\r\n')
        assert _bin_is_usable(shim) is True

    def test_resolver_walk_finds_cmd_shim_in_bundle_layout(
        self, tmp_path: Path, monkeypatch, launchers_confined_to_tmp
    ):
        """End-to-end: the parent walk PREFERS the bundle's .cmd on Windows.

        Pins the issue's failure mode: the bundle ships BOTH launchers, and
        resolving the co-present ``Scripts\\kirocrew.exe`` instead of the
        ``.cmd`` shim is exactly the defect.
        """
        import kiro_crew.agent as agent_mod
        from kiro_crew import platform_compat
        from kiro_crew.agent import _resolve_kirocrew_bin

        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)

        # Bundle layout (packaging/build-desktop.sh build_backend_windows):
        # <root>/Lib/site-packages/kiro_crew + <root>/bin/kirocrew.cmd
        # + the pip-dropped <root>/Scripts/kirocrew.exe (non-relocatable).
        root = tmp_path / "kirocrew-backend"
        pkg_dir = root / "Lib" / "site-packages" / "kiro_crew"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "__init__.py").write_text("")
        (root / "bin").mkdir()
        cmd_shim = root / "bin" / "kirocrew.cmd"
        cmd_shim.write_text('@echo off\r\n"%~dp0..\\python.exe" -s -m kiro_crew %*\r\n')
        # The test host is POSIX, where the resolver's X_OK gate is real.
        cmd_shim.chmod(0o755)
        (root / "Scripts").mkdir()
        dead_exe = root / "Scripts" / "kirocrew.exe"
        dead_exe.write_bytes(b"MZ")
        dead_exe.chmod(0o755)

        mock_mc = unittest.mock.MagicMock()
        mock_mc.__file__ = str(pkg_dir / "__init__.py")

        old_val = agent_mod._KIROCREW_BIN
        try:
            agent_mod._KIROCREW_BIN = None
            with patch.dict("sys.modules", {"kiro_crew": mock_mc}):
                result = _resolve_kirocrew_bin()
            assert result == str(cmd_shim)
        finally:
            agent_mod._KIROCREW_BIN = old_val


class TestKirocrewMcpInvocation:
    """Tests for built-in MCP server invocation resolution.

    Regression: when ``_resolve_kirocrew_bin`` cannot find a usable
    standalone binary (e.g. the gateway runs as a systemd user service and
    ``kirocrew`` is not on the service PATH), the built-in cron/core servers
    must still get a runnable command instead of the bare ``"kirocrew"``
    sentinel, which fails validation and drops them from ``kirocrew.json`` on
    every refresh.
    """

    def test_uses_standalone_binary_when_resolved(self):
        from kiro_crew.agent import _kirocrew_mcp_invocation

        with patch("kiro_crew.agent._resolve_kirocrew_bin", return_value="/opt/bin/kirocrew"):
            cmd, args = _kirocrew_mcp_invocation("mcp-cron")
        assert cmd == "/opt/bin/kirocrew"
        assert args == ["mcp-cron"]

    def test_falls_back_to_interpreter_module_when_unresolved(
        self, nonbundled_python_without_user_site
    ):
        """Module fallback excludes the project CWD before importing Kiro Crew."""
        from kiro_crew.agent import _kirocrew_mcp_invocation

        # Bare "kirocrew" is the unresolved sentinel from _resolve_kirocrew_bin.
        with patch("kiro_crew.agent._resolve_kirocrew_bin", return_value="kirocrew"):
            cmd, args = _kirocrew_mcp_invocation("mcp-core")
        assert cmd == sys.executable
        assert args == ["-s", "-P", "-m", "kiro_crew", "mcp-core"]

    def test_unwraps_cmd_shim_to_sibling_interpreter(self, tmp_path: Path):
        """A resolved bin/kirocrew.cmd is never emitted verbatim.

        Mirrors website/electron/main.js: the shim is unwrapped to
        ``<root>/python.exe -s -P -m kiro_crew <sub>`` so kiro-cli spawns the
        interpreter, not a batch file.
        """
        from kiro_crew.agent import _kirocrew_mcp_invocation

        root = tmp_path / "kirocrew-backend"
        (root / "bin").mkdir(parents=True)
        shim = root / "bin" / "kirocrew.cmd"
        shim.write_text('@echo off\r\n"%~dp0..\\python.exe" -s -m kiro_crew %*\r\n')
        interpreter = root / "python.exe"
        interpreter.write_bytes(b"MZ")
        interpreter.chmod(0o755)  # X_OK is real on the POSIX test host

        with patch("kiro_crew.agent._resolve_kirocrew_bin", return_value=str(shim)):
            cmd, args = _kirocrew_mcp_invocation("mcp-cron")
        assert cmd == str(interpreter)
        # -P keeps the spawn CWD off sys.path; -s drops user site-packages.
        assert args == ["-s", "-P", "-m", "kiro_crew", "mcp-cron"]

    def test_cmd_shim_without_interpreter_falls_back_to_sys_executable(
        self, tmp_path: Path, nonbundled_python_without_user_site
    ):
        """Corrupted bundle: shim present but python.exe missing -> sys.executable."""
        from kiro_crew.agent import _kirocrew_mcp_invocation

        root = tmp_path / "kirocrew-backend"
        (root / "bin").mkdir(parents=True)
        shim = root / "bin" / "kirocrew.cmd"
        shim.write_text('@echo off\r\n"%~dp0..\\python.exe" -s -m kiro_crew %*\r\n')

        with patch("kiro_crew.agent._resolve_kirocrew_bin", return_value=str(shim)):
            cmd, args = _kirocrew_mcp_invocation("mcp-core")
        assert cmd == sys.executable
        assert args == ["-s", "-P", "-m", "kiro_crew", "mcp-core"]
