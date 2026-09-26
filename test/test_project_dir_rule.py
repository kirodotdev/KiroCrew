"""The one project-directory rule (``kiro_crew.project_dir``) and its two callers.

A chat folder and a cron job each name the repository their sessions run in.
The absolute/realpath/sensitive/isdir rule is one body; each surface keeps
only its wording, its audit attribution, and (for the cron store) its caps.
These tests pin the core directly and pin that both surfaces reach it, so a
future edit to the rule cannot land on one surface and not the other.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from kiro_crew.project_dir import ProjectDirRefused, resolve_project_dir

_UNC_LIKE = re.compile(r"^[\\/]{2}")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    d = tmp_path / "repo"
    d.mkdir()
    return d


def _call(raw: str, **over):
    kw = {"label": "project_dir", "audit_operation": "test.project_dir", "audit_caller": "t"}
    kw.update(over)
    return resolve_project_dir(raw, **kw)


class TestResolveProjectDir:
    def test_empty_means_unset(self):
        assert _call("") == ""

    def test_returns_the_realpath(self, repo: Path, tmp_path: Path):
        link = tmp_path / "link"
        os.symlink(repo, link, target_is_directory=True)
        if os.name == "nt":
            # A reparse point is never resolved on Windows (its target may be a
            # share): the linked spelling is refused, the real path canonical.
            with pytest.raises(ProjectDirRefused, match="reparse point"):
                _call(str(link))
            assert _call(str(repo)) == os.path.realpath(repo)
            return
        assert _call(str(link)) == os.path.realpath(repo)

    def test_tilde_expands(self, repo: Path, monkeypatch):
        # expanduser reads HOME on POSIX and USERPROFILE on Windows.
        monkeypatch.setenv("HOME", str(repo.parent))
        monkeypatch.setenv("USERPROFILE", str(repo.parent))
        assert _call("~/repo") == os.path.realpath(repo)

    def test_relative_refused_with_the_surfaces_label(self):
        with pytest.raises(ProjectDirRefused, match="^Project directory must be an absolute path$"):
            _call("relative/path", label="Project directory")

    def test_missing_directory_refused(self, tmp_path: Path):
        with pytest.raises(ProjectDirRefused, match="must be an existing directory"):
            _call(str(tmp_path / "gone"))

    def test_sensitive_refused_and_audited_for_the_surface(self, repo: Path, monkeypatch):
        monkeypatch.setattr("kiro_crew.security.is_sensitive_canonical_path", lambda p: True)
        fake_sel = MagicMock()
        monkeypatch.setattr("kiro_crew.sel.sel", lambda: fake_sel)
        with pytest.raises(ProjectDirRefused, match="sensitive"):
            _call(str(repo), audit_operation="chat.folder_project_dir", audit_caller="dashboard")
        fake_sel.log_api_access.assert_called_once()
        kw = fake_sel.log_api_access.call_args.kwargs
        assert kw["operation"] == "chat.folder_project_dir"
        assert kw["caller"] == "dashboard"
        assert kw["outcome"] == "denied"

    def test_uses_the_canonical_gate_not_the_bounded_resolver(self, repo: Path, monkeypatch):
        def _bounded_must_not_run(*a, **k):
            raise AssertionError("pool-bounded is_sensitive_path consulted on a canonical path")

        monkeypatch.setattr("kiro_crew.security.is_sensitive_path", _bounded_must_not_run)
        monkeypatch.setattr("kiro_crew.security.is_sensitive_canonical_path", lambda p: False)
        assert _call(str(repo)) == os.path.realpath(repo)

    def test_audit_failure_never_masks_the_refusal(self, repo: Path, monkeypatch):
        monkeypatch.setattr("kiro_crew.security.is_sensitive_canonical_path", lambda p: True)

        def _boom():
            raise RuntimeError("sel down")

        monkeypatch.setattr("kiro_crew.sel.sel", _boom)
        with pytest.raises(ProjectDirRefused, match="sensitive"):
            _call(str(repo))


class TestRefusedBeforeCanonicalisation:
    """The two refusals that must run BEFORE ``realpath`` can reach a host.

    ``realpath`` is what would open a UNC target or resolve a reparse point
    through to one -- an outbound SMB/NTLM authentication carrying the
    gateway's credentials -- so each test also pins that ``realpath`` was
    never reached.
    """

    @pytest.fixture
    def realpath_forbidden(self, monkeypatch):
        """Trip if ``realpath`` is handed the CANDIDATE path.

        Scoped to the inputs these tests submit (``/srv/...``, ``/link/...``
        and any UNC spelling): the sensitive-path gate legitimately realpaths
        the operator's own home directories, which is not the untrusted string.
        """
        real = os.path.realpath

        def _guard(p, *a, **k):
            text = os.fspath(p)
            if text.startswith(("/srv", "/link")) or _UNC_LIKE.match(text):
                raise AssertionError(f"realpath reached for the candidate path {text!r}")
            return real(p, *a, **k)

        monkeypatch.setattr(os.path, "realpath", _guard)

    @pytest.mark.parametrize(
        "raw",
        [
            r"\\evil\share\repo",
            "//evil/share/repo",
            r"\\/evil/share",
            r"\\?\UNC\evil\share",
            r"\\?\C:\repo",
        ],
    )
    def test_unc_spellings_refused_for_every_surface(self, raw, realpath_forbidden):
        with pytest.raises(ProjectDirRefused, match="project_dir must be a local path"):
            _call(raw)
        with pytest.raises(ProjectDirRefused, match="Project directory must be a local path"):
            _call(raw, label="Project directory")

    @pytest.fixture
    def pinned_walk(self, monkeypatch):
        """Drive the Windows branch off Windows against faked pin/final-path helpers.

        ``pins`` records every prefix opened, in order; ``closed`` every fd
        released. ``reparse_at`` names the prefix that is a reparse point (the
        pin refuses it with ``NotADirectoryError``, as ``pin_directory`` does
        for a junction or symlink); ``missing_at`` one that does not exist.
        ``final`` is what the leaf handle's real path reads as.
        """
        import kiro_crew.project_dir as pd
        from kiro_crew import pinned_fs, platform_compat

        monkeypatch.setattr(pd, "_WINDOWS", True)
        state = {"pins": [], "closed": [], "reparse_at": None, "missing_at": None, "final": None}

        def _pin(prefix):
            state["pins"].append(prefix)
            if prefix == state["reparse_at"]:
                raise NotADirectoryError(20, "not a real directory", prefix)
            if prefix == state["missing_at"]:
                raise FileNotFoundError(2, "no such file", prefix)
            return 1000 + len(state["pins"])

        def _final(fd):
            state["leaf_fd"] = fd
            return state["final"]

        monkeypatch.setattr(platform_compat, "pin_directory", _pin)
        monkeypatch.setattr(pinned_fs, "fd_real_path", _final)
        # The volume is local unless a test says otherwise.
        monkeypatch.setattr(platform_compat, "path_volume_is_remote", lambda p: False)
        # The message-only diagnostic: the refused name IS the reparse point.
        monkeypatch.setattr(pd, "_is_reparse_point", lambda p: p == state["reparse_at"])
        monkeypatch.setattr(
            platform_compat, "release_directory_chain", lambda fds: state["closed"].extend(fds)
        )
        # isdir by name would re-resolve the string; the branch must not use it.
        monkeypatch.setattr(os.path, "isdir", lambda _p: pytest.fail("isdir by name on Windows"))
        return state

    @pytest.fixture
    def denials(self, monkeypatch):
        events: list[dict] = []
        fake_sel = MagicMock()
        fake_sel.log_api_access = lambda **kw: events.append(kw)
        monkeypatch.setattr("kiro_crew.sel.sel", lambda: fake_sel)
        return events

    def test_a_unc_spelling_is_a_security_denial(self, denials, realpath_forbidden):
        # Same class of decision as the sensitive-path refusal: audited under
        # the surface's operation, for its caller, with the path redacted.
        with pytest.raises(ProjectDirRefused, match="UNC/network"):
            _call(r"\\evil\share", audit_operation="op.x", audit_caller="who")
        assert [(e["operation"], e["caller"], e["outcome"], e["error"]) for e in denials] == [
            ("op.x", "who", "denied", "network path")
        ]

    def test_a_tilde_that_expands_to_a_unc_profile_is_refused(
        self, denials, realpath_forbidden, monkeypatch
    ):
        # ``~`` is admitted past the raw check and expands from the profile
        # variables, so the guard is asked AGAIN on the expansion before any
        # filesystem access: a UNC-rooted profile never reaches the walk.
        import kiro_crew.project_dir as pd

        monkeypatch.setattr(pd, "_WINDOWS", True)
        monkeypatch.setattr(
            "kiro_crew.platform_compat.pin_directory",
            lambda p: pytest.fail("walk reached for a UNC expansion"),
        )
        monkeypatch.setattr(os.path, "expanduser", lambda p: r"\\fileserver\profiles\u" + p[1:])
        with pytest.raises(ProjectDirRefused, match="UNC/network"):
            _call("~/repo")
        assert [e["error"] for e in denials] == ["network path"]

    def test_a_tilde_that_stays_relative_after_expansion_is_refused(
        self, monkeypatch, realpath_forbidden
    ):
        # ``~`` is admitted past the raw check on the promise that expansion
        # makes it absolute. An unknown ``~user`` (or a home-less process)
        # leaves the string as it was, and a relative path here would resolve
        # against the gateway's own cwd rather than anything the caller named.
        monkeypatch.setattr(os.path, "expanduser", lambda p: p)
        with pytest.raises(ProjectDirRefused, match="must be an absolute path") as info:
            _call("~nobody/repo")
        assert info.value.reason == "relative"

    @pytest.mark.parametrize("verdict", [True, None], ids=["remote", "unknown"])
    def test_a_mapped_or_unclassifiable_volume_is_refused_before_anything_opens(
        self, pinned_walk, realpath_forbidden, monkeypatch, verdict
    ):
        # ``Z:\repo`` bound to a share spells like a local path and passes the
        # UNC guard; the volume root's drive type is asked first and anything
        # but an explicit "local" is refused, closed, as a network denial.
        from kiro_crew import platform_compat

        events: list[dict] = []
        fake_sel = MagicMock()
        fake_sel.log_api_access = lambda **kw: events.append(kw)
        monkeypatch.setattr("kiro_crew.sel.sel", lambda: fake_sel)
        monkeypatch.setattr(platform_compat, "path_volume_is_remote", lambda p: verdict)
        with pytest.raises(ProjectDirRefused, match="mapped drive") as info:
            _call("/srv/u/repo")
        assert info.value.reason == "network"
        assert [e["error"] for e in events] == ["network path"]
        assert pinned_walk["pins"] == []

    def test_a_letter_without_a_local_volume_identity_is_a_network_denial(
        self, pinned_walk, realpath_forbidden, monkeypatch
    ):
        # The drive-type pre-check said "local", but by the time the chain binds
        # the letter has no local identity (rebound to a share, or released):
        # the chain refuses before opening anything, and the rule reports it as
        # the same audited network denial.
        from kiro_crew import platform_compat

        events: list[dict] = []
        fake_sel = MagicMock()
        fake_sel.log_api_access = lambda **kw: events.append(kw)
        monkeypatch.setattr("kiro_crew.sel.sel", lambda: fake_sel)

        def _no_identity(path):
            raise platform_compat.NotALocalVolume(19, "drive is not a local volume", "Z:")

        monkeypatch.setattr(platform_compat, "pin_directory_chain", _no_identity)
        with pytest.raises(ProjectDirRefused, match="mapped drive") as info:
            _call("/srv/u/repo")
        assert info.value.reason == "network"
        assert [e["error"] for e in events] == ["network path"]

    def test_a_reparse_point_component_is_refused_on_windows(
        self, pinned_walk, realpath_forbidden, monkeypatch
    ):
        # A drive-less absolute path is absolute on every platform, so the
        # same input drives the walk here and on a real Windows runner.
        u = os.sep + "srv"
        pinned_walk["reparse_at"] = u + os.sep + "u" + os.sep + "link"
        events: list[dict] = []
        fake_sel = MagicMock()
        fake_sel.log_api_access = lambda **kw: events.append(kw)
        monkeypatch.setattr("kiro_crew.sel.sel", lambda: fake_sel)
        with pytest.raises(ProjectDirRefused, match="must not pass through a symlink, junction"):
            _call("/srv/u/link/repo", audit_operation="op.y", audit_caller="c")
        # A security decision, so it is audited like the sensitive refusal.
        assert [(e["operation"], e["caller"], e["outcome"], e["error"]) for e in events] == [
            ("op.y", "c", "denied", "reparse point")
        ]
        # Every prefix up to and including the point was opened, in order, and
        # nothing past it: the point was refused in the open that would have
        # traversed it. The ancestors opened before it were all released.
        assert pinned_walk["pins"] == [u, u + os.sep + "u", pinned_walk["reparse_at"]]
        assert sorted(pinned_walk["closed"]) == [1001, 1002]

    def test_a_dotdot_after_a_reparse_point_never_walks_through_it(
        self, pinned_walk, realpath_forbidden
    ):
        pinned_walk["reparse_at"] = os.sep + "link"
        with pytest.raises(ProjectDirRefused, match="reparse point"):
            _call("/link/../repo")
        assert pinned_walk["pins"] == [os.sep + "link"]

    def test_the_canonical_path_comes_from_the_leaf_handle(self, pinned_walk, realpath_forbidden):
        # Not from realpath (forbidden here) and not from the string: the
        # object actually opened answers.
        pinned_walk["final"] = "C:" + os.sep + "Real" + os.sep + "repo"
        assert _call("/srv/u/repo") == pinned_walk["final"]
        assert pinned_walk["pins"] == [
            os.sep + "srv",
            os.sep + "srv" + os.sep + "u",
            os.sep + "srv" + os.sep + "u" + os.sep + "repo",
        ]
        assert pinned_walk["leaf_fd"] == 1003  # the last prefix's handle
        # Every handle was held until the end, then released.
        assert sorted(pinned_walk["closed"]) == [1001, 1002, 1003]

    def test_an_unreadable_handle_path_fails_closed(self, pinned_walk, realpath_forbidden):
        pinned_walk["final"] = None
        with pytest.raises(
            ProjectDirRefused, match="could not be canonicalised through its handle"
        ):
            _call("/srv/u/repo")
        assert sorted(pinned_walk["closed"]) == [1001, 1002, 1003]

    def test_a_missing_component_reports_missing(self, pinned_walk, realpath_forbidden):
        pinned_walk["missing_at"] = os.sep + "srv" + os.sep + "absent"
        with pytest.raises(ProjectDirRefused, match="must be an existing directory"):
            _call("/srv/absent/repo")
        assert pinned_walk["closed"] == [1001]

    def test_a_sensitive_canonical_path_is_still_refused_on_windows(self, pinned_walk, monkeypatch):
        # The handle's answer is what the sensitive gate judges: a benign
        # spelling that opens to a protected location is refused.
        from kiro_crew import security

        pinned_walk["final"] = "C:" + os.sep + "srv" + os.sep + "u" + os.sep + ".ssh"
        seen = []
        monkeypatch.setattr(
            security,
            "is_sensitive_canonical_path",
            lambda p: seen.append(p) or p == pinned_walk["final"],
        )
        with pytest.raises(ProjectDirRefused, match="sensitive path"):
            _call("/srv/u/link-to-ssh")
        # Asked twice: first on the lexical spelling (benign here), then on the
        # handle's answer, which is what refuses.
        assert seen == [os.path.normpath("/srv/u/link-to-ssh"), pinned_walk["final"]]

    def test_a_file_at_the_name_reports_missing_directory(
        self, pinned_walk, realpath_forbidden, monkeypatch
    ):
        # ``pin_directory`` refuses a file and a reparse point alike; only the
        # latter earns the reparse wording.
        import kiro_crew.project_dir as pd

        pinned_walk["reparse_at"] = os.sep + "srv" + os.sep + "f.txt"
        monkeypatch.setattr(pd, "_is_reparse_point", lambda p: False)  # a plain file
        with pytest.raises(ProjectDirRefused, match="must be an existing directory"):
            _call("/srv/f.txt")

    def test_a_protected_spelling_is_refused_before_the_walk(self, pinned_walk, monkeypatch):
        # Sensitive outranks missing on Windows too: a protected location that
        # does not exist is refused as protected, and nothing is opened.
        from kiro_crew import security

        pinned_walk["missing_at"] = os.sep + "srv"
        monkeypatch.setattr(security, "is_sensitive_canonical_path", lambda p: True)
        with pytest.raises(ProjectDirRefused, match="sensitive path"):
            _call("/srv/u/.ssh")
        assert pinned_walk["pins"] == []

    def test_the_walk_is_not_consulted_off_windows(self, repo: Path, monkeypatch):
        import kiro_crew.project_dir as pd
        from kiro_crew import platform_compat

        monkeypatch.setattr(pd, "_WINDOWS", False)
        calls = MagicMock()
        monkeypatch.setattr(platform_compat, "pin_directory", calls)
        assert _call(str(repo)) == os.path.realpath(repo)
        calls.assert_not_called()


class TestPinnedWalkOnTheRealFilesystem:
    """The Windows branch end to end against real ``pin_directory``/``fd_real_path``.

    On Windows this IS the production path. Elsewhere the seam is forced so the
    same plumbing runs in its POSIX flavour (``O_NOFOLLOW``/``O_DIRECTORY``
    opens, ``/proc/self/fd`` or ``F_GETPATH`` for the handle's path), which
    exercises the handle bookkeeping and the error mapping for real.
    """

    @pytest.fixture(autouse=True)
    def _force_branch(self, monkeypatch):
        import kiro_crew.project_dir as pd
        from kiro_crew import platform_compat

        monkeypatch.setattr(pd, "_WINDOWS", True)
        # Off Windows the volume classifier answers "unknown" (no mount table
        # here); the real one on Windows answers for the drive root.
        monkeypatch.setattr(platform_compat, "path_volume_is_remote", lambda p: False)

    def test_a_real_directory_is_canonicalised_through_its_handle(self, repo: Path):
        assert _call(str(repo)) == os.path.realpath(repo)

    def test_a_symlink_component_is_refused(self, repo: Path, tmp_path: Path):
        link = tmp_path / "link"
        os.symlink(repo, link, target_is_directory=True)
        with pytest.raises(ProjectDirRefused, match="reparse point"):
            _call(str(link / "."))
        with pytest.raises(ProjectDirRefused, match="reparse point"):
            _call(str(link))

    def test_a_file_reports_missing_directory(self, tmp_path: Path):
        f = tmp_path / "f.txt"
        f.write_text("x")
        with pytest.raises(ProjectDirRefused, match="must be an existing directory"):
            _call(str(f))

    def test_a_missing_directory_reports_missing(self, tmp_path: Path):
        with pytest.raises(ProjectDirRefused, match="must be an existing directory"):
            _call(str(tmp_path / "absent" / "deeper"))

    def test_a_protected_location_is_refused_even_when_absent(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        with pytest.raises(ProjectDirRefused, match="sensitive path"):
            _call("~/.ssh")


class TestBothSurfacesCallTheCore:
    def test_the_chat_folder_validator_is_a_wrapper(self, repo: Path, monkeypatch):
        from kiro_crew.dashboard import chat_folders

        seen: list[dict] = []

        def _core(raw, **kw):
            seen.append(kw)
            return "/resolved"

        monkeypatch.setattr("kiro_crew.dashboard.chat_folders.resolve_project_dir", _core)
        assert chat_folders._validate_project_dir(str(repo)) == ("/resolved", None)
        assert seen == [
            {
                "label": "Project directory",
                "audit_operation": "chat.folder_project_dir",
                "audit_caller": "dashboard",
            }
        ]

    def test_the_chat_folder_validator_reports_the_refusal_as_an_error(self, tmp_path: Path):
        from kiro_crew.dashboard import chat_folders

        resolved, err = chat_folders._validate_project_dir(str(tmp_path / "gone"))
        assert resolved == ""
        assert err == "Project directory must be an existing directory"

    def test_the_cron_validator_is_a_wrapper(self, repo: Path, monkeypatch):
        from kiro_crew.cron import validate_cron_project_dir

        seen: list[dict] = []

        def _core(raw, **kw):
            seen.append(kw)
            return "/resolved"

        monkeypatch.setattr("kiro_crew.cron.resolve_project_dir", _core)
        assert validate_cron_project_dir(str(repo), audit_caller="cli") == "/resolved"
        assert seen == [
            {"label": "project_dir", "audit_operation": "cron.project_dir", "audit_caller": "cli"}
        ]

    def test_the_cron_validator_translates_the_refusal_to_value_error(self, tmp_path: Path):
        from kiro_crew.cron import validate_cron_project_dir

        with pytest.raises(ValueError, match="project_dir must be an existing directory"):
            validate_cron_project_dir(str(tmp_path / "gone"))

    def test_the_core_imports_its_collaborators_at_module_scope(self):
        # No function-local imports: the security, SEL and pinned-filesystem
        # modules are attributes of the module, patchable by name.
        import ast
        import inspect

        import kiro_crew.project_dir as pd

        tree = ast.parse(inspect.getsource(pd))
        local_imports = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom)) and node.col_offset > 0
        ]
        assert local_imports == []
        assert pd.security is not None and pd.sel is not None
        assert pd.pinned_fs is not None and pd.platform_compat is not None

    def test_no_surface_keeps_its_own_copy_of_the_rule(self):
        # The rule's checks appear in ONE module. A copy that returned to
        # any wrapper -- the cron store, the chat-folder validator or the
        # slot-project endpoint -- would be exactly the drift this core removed. (The
        # steering-dirs validator beside the folder one is a different rule --
        # memory-silo and hardlink checks -- and is not in scope here.)
        import inspect

        from kiro_crew.cron import validate_cron_project_dir
        from kiro_crew.dashboard.chat_folders import _validate_project_dir
        from kiro_crew.dashboard.chat_handlers import api_chat_slot_project

        for fn in (validate_cron_project_dir, _validate_project_dir, api_chat_slot_project):
            src = inspect.getsource(fn)
            assert "refers to a sensitive path" not in src, fn.__qualname__
            assert "must be an existing directory" not in src, fn.__qualname__
            assert "realpath(os.path.expanduser" not in src, fn.__qualname__
            assert "is_sensitive_path(" not in src, fn.__qualname__
            assert "resolve_project_dir" in src, fn.__qualname__
