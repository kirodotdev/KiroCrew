"""Coverage tests for ``kiro_crew.hooks``.

This family's focus is the parts of the module the existing hook suites never reach: the
descriptor-pinned file helpers (``safe_*``), the internal-read allowlist and its
SEL-audit gate, the boot-time builtin-app registries, the script-hook store's
persistence/rollback contract, and script-hook dispatch (registration ordering,
matcher filtering, and failure isolation).

Everything here is hermetic: no real network, no real subprocess, and every
filesystem write lands under ``tmp_path``.

This module pins config parsing and the path validation with the descriptor-pinned
reads, writes and copies. The audited internal reads live in
``test_hooks_coverage_internal_reads.py``; the builtin-app registries, the script hooks
(store, governance, dispatch), the gate's governance helpers, message transforms and the
read-only auto-approve branch in ``test_hooks_coverage_hook_manager.py``; the file
helpers' remaining branches in ``test_hooks_coverage_file_branches.py``; the shared
helpers in ``hooks_coverage_helpers.py``.
"""

from __future__ import annotations

import json
import ntpath
import os
import stat as _stat
from pathlib import Path

import pytest
from hooks_coverage_helpers import (
    _IS_WINDOWS,
    _same,
    _staged_sibling,
    _write,
)

from kiro_crew import hooks as hooks_mod
from kiro_crew import security
from kiro_crew.hooks import (
    FileTooLargeError,
    HooksConfig,
    UserDeniedPattern,
    _coerce_bool,
    _fd_real_path,
    _is_access_control_xattr,
    _should_carry_xattr,
    effective_denied_regexes_from_config,
    hooks_config_from_config_dict,
    load_denied_commands_state,
    resolve_denied_notes,
    safe_copy_file_nolink,
    safe_read_file,
    safe_read_file_bytes,
    safe_read_file_bytes_nolink,
    safe_read_file_bytes_with_identity,
    safe_read_prefix,
    safe_write_file_nolink,
    stat_identity,
    validate_file_path,
)


def _identity(path: Path) -> tuple[int, int]:
    st = os.stat(path)
    return (st.st_dev, st.st_ino)


def _try_hardlink(src: Path, dst: Path) -> None:
    """Hardlink *src* to *dst*, skipping the test when the platform refuses."""
    try:
        os.link(src, dst)
    except (OSError, NotImplementedError, AttributeError) as exc:  # pragma: no cover
        pytest.skip(f"hardlinks unavailable here: {exc}")


# ── config parsing ──


class TestCoerceBool:
    @pytest.mark.parametrize("raw", ["true", "TRUE", " 1 ", "yes", "on"])
    def test_truthy_spellings(self, raw):
        assert _coerce_bool(raw, default=False) is True

    @pytest.mark.parametrize("raw", ["false", "FALSE", "0", "no", "off"])
    def test_falsey_spellings(self, raw):
        # Plain bool("false") is True -- the trap this helper exists to close.
        assert _coerce_bool(raw, default=True) is False

    @pytest.mark.parametrize("raw", [None, 3, [], {}, "maybe"])
    def test_unrecognised_falls_back_to_default(self, raw):
        assert _coerce_bool(raw, default=True) is True
        assert _coerce_bool(raw, default=False) is False

    def test_real_bool_passes_through(self):
        assert _coerce_bool(True, default=False) is True
        assert _coerce_bool(False, default=True) is False


class TestUserDeniedPattern:
    def test_missing_id_gets_generated(self):
        p = UserDeniedPattern.from_dict({"pattern": "rm .*"})
        assert p.id and len(p.id) == 12
        assert p.pattern == "rm .*"
        assert p.enabled is True
        assert p.note == ""

    def test_malformed_enabled_stays_on(self):
        # Fail safe: an ambiguous value must keep a deny rule enforcing.
        assert UserDeniedPattern.from_dict({"pattern": "x", "enabled": "junk"}).enabled is True
        assert UserDeniedPattern.from_dict({"pattern": "x", "enabled": "off"}).enabled is False

    def test_malformed_note_degrades_to_blank(self):
        assert UserDeniedPattern.from_dict({"pattern": "x", "note": None}).note == ""
        assert UserDeniedPattern.from_dict({"pattern": "x", "note": 7}).note == "7"

    def test_round_trip(self):
        p = UserDeniedPattern(id="abc", pattern="p", enabled=False, note="n")
        assert p.to_dict() == {"id": "abc", "pattern": "p", "enabled": False, "note": "n"}


class TestHooksConfigFromDict:
    def test_non_dict_input_degrades(self):
        cfg = HooksConfig.from_dict("not a dict")  # type: ignore[arg-type]
        assert cfg.auto_replies == []
        assert cfg.context_rules == []

    def test_scalar_where_list_expected_degrades(self):
        cfg = HooksConfig.from_dict(
            {
                "auto_replies": 1,
                "transforms": "x",
                "context_rules": None,
                "auto_approve_sources": 5,
                "auto_deny_tools": {"a": 1},
            }
        )
        assert cfg.auto_replies == []
        assert cfg.transforms == []
        assert cfg.context_rules == []
        assert cfg.auto_approve_sources == []
        assert cfg.auto_deny_tools == []

    def test_non_dict_items_in_lists_are_dropped(self):
        cfg = HooksConfig.from_dict({"auto_replies": ["nope", {"pattern": "p", "reply": "r"}, 3]})
        assert len(cfg.auto_replies) == 1
        assert cfg.auto_replies[0].pattern == "p"

    def test_non_string_auto_approve_entries_dropped_and_bundled_merged(self):
        cfg = HooksConfig.from_dict({"auto_approve_tools": ["mine", 4, None]})
        assert "mine" in cfg.auto_approve_tools
        for bundled in hooks_mod._BUNDLED_AUTO_APPROVE_TOOLS:
            assert bundled in cfg.auto_approve_tools
        # No duplicates even when the operator listed a bundled pattern too.
        assert len(cfg.auto_approve_tools) == len(set(cfg.auto_approve_tools))

    def test_subagent_flags_fail_safe_on_junk(self):
        cfg = HooksConfig.from_dict(
            {
                "auto_approve_subagent_spawn": "false",
                "auto_approve_subagent_tools": "nonsense",
            }
        )
        assert cfg.auto_approve_subagent_spawn is False
        assert cfg.auto_approve_subagent_tools is False

    def test_denied_commands_junk_degrades_to_no_optout(self):
        cfg = HooksConfig.from_dict(
            {"denied_commands": {"user_added": 1, "disabled_ids": "x", "disable_all": "false"}}
        )
        assert cfg.denied_commands_user_added == []
        assert cfg.denied_commands_disabled_ids == []
        assert cfg.denied_commands_disable_all is False

    def test_denied_commands_non_dict_degrades(self):
        cfg = HooksConfig.from_dict({"denied_commands": ["nope"]})
        assert cfg.denied_commands_state() == {
            "disabled_ids": [],
            "disable_all": False,
            "user_added": [],
        }

    def test_blank_user_patterns_are_dropped(self):
        cfg = HooksConfig.from_dict(
            {
                "denied_commands": {
                    "user_added": [{"pattern": "  "}, {"pattern": "keep"}, "junk"],
                    "disabled_ids": ["a", 2, ""],
                }
            }
        )
        assert [p.pattern for p in cfg.denied_commands_user_added] == ["keep"]
        assert cfg.denied_commands_disabled_ids == ["a"]

    def test_to_dict_omits_bundled_and_nests_denied(self):
        cfg = HooksConfig.from_dict({"auto_approve_tools": ["mine"]})
        out = cfg.to_dict()
        assert out["auto_approve_tools"] == ["mine"]
        assert out["denied_commands"] == {
            "disabled_ids": [],
            "disable_all": False,
            "user_added": [],
        }


class TestDeniedCommandsState:
    def test_load_state_missing_file_is_no_optout(self):
        assert load_denied_commands_state() == {}

    def test_load_state_reads_keystone(self, tmp_path, monkeypatch):
        from kiro_crew.config import loader

        keystone = tmp_path / "denied_commands.json"
        _write(keystone, json.dumps({"disable_all": True}))
        monkeypatch.setattr(loader, "denied_commands_path", lambda: keystone)
        assert load_denied_commands_state() == {"disable_all": True}

    def test_load_state_non_object_degrades(self, tmp_path, monkeypatch):
        from kiro_crew.config import loader

        keystone = _write(tmp_path / "denied_commands.json", "[1, 2]")
        monkeypatch.setattr(loader, "denied_commands_path", lambda: keystone)
        assert load_denied_commands_state() == {}

    def test_load_state_corrupt_json_degrades(self, tmp_path, monkeypatch):
        from kiro_crew.config import loader

        keystone = _write(tmp_path / "denied_commands.json", "{not json")
        monkeypatch.setattr(loader, "denied_commands_path", lambda: keystone)
        assert load_denied_commands_state() == {}

    def test_boot_path_ignores_config_json_denied_section(self, monkeypatch):
        # config.json's own hooks.denied_commands must be discarded: the
        # keystone file is the sole source for the deny ceiling.
        monkeypatch.setattr(hooks_mod, "load_denied_commands_state", lambda: {})
        cfg = hooks_config_from_config_dict(
            {"denied_commands": {"disable_all": True}, "auto_deny_tools": ["x"]}
        )
        assert cfg.denied_commands_disable_all is False
        assert cfg.auto_deny_tools == ["x"]

    def test_boot_path_tolerates_non_dict_section(self, monkeypatch):
        monkeypatch.setattr(hooks_mod, "load_denied_commands_state", lambda: {})
        assert hooks_config_from_config_dict(None).auto_deny_tools == []  # type: ignore[arg-type]

    def test_effective_set_fails_closed_when_load_raises(self, monkeypatch):
        def _boom():
            raise RuntimeError("keystone unreadable")

        monkeypatch.setattr(hooks_mod, "load_denied_commands_state", _boom)
        result = effective_denied_regexes_from_config()
        expected = security.compute_effective_denied(
            security.BUILTIN_DENIED_RULES, (), False, (), ()
        )
        assert result == expected

    def test_effective_set_from_disk_includes_user_pattern(self, monkeypatch):
        monkeypatch.setattr(
            hooks_mod,
            "load_denied_commands_state",
            lambda: {"user_added": [{"pattern": "my-own-rule", "enabled": True}]},
        )
        assert "my-own-rule" in effective_denied_regexes_from_config()


class TestResolveDeniedNotes:
    def test_only_annotated_enabled_patterns_appear(self):
        cfg = HooksConfig(
            denied_commands_user_added=[
                UserDeniedPattern(id="1", pattern="a", enabled=True, note=" use rg "),
                UserDeniedPattern(id="2", pattern="b", enabled=True, note="   "),
                UserDeniedPattern(id="3", pattern="c", enabled=False, note="hidden"),
                UserDeniedPattern(id="4", pattern="", enabled=True, note="no pattern"),
            ]
        )
        assert resolve_denied_notes(cfg) == {"a": "use rg"}

    def test_note_that_forges_a_refusal_line_is_dropped(self):
        forged = f"{security.DENY_REASON_MATCH_PREFIX} fabricated"
        cfg = HooksConfig(
            denied_commands_user_added=[
                UserDeniedPattern(id="1", pattern="a", enabled=True, note=forged)
            ]
        )
        # Fail-safe direction: lose the note, keep the pattern.
        assert resolve_denied_notes(cfg) == {}


# ── path validation and reads ──


class TestValidateFilePath:
    def test_blank_is_rejected(self):
        assert validate_file_path("") is None

    def test_sensitive_is_rejected(self):
        assert validate_file_path(str(Path.home() / ".aws" / "credentials")) is None

    def test_ordinary_path_is_canonicalized(self, tmp_path):
        f = _write(tmp_path / "ok.txt", "x")
        assert _same(validate_file_path(str(f)) or "", str(f))

    @pytest.mark.parametrize(
        "raw,expected",
        [
            (r"\\?\C:\Users\me\.aws\creds", r"C:\Users\me\.aws\creds"),
            (r"\\?\c:\x", r"c:\x"),
            ("\\\\?\\C:/x", "C:/x"),
            (r"\\?\UNC\host\share", r"\\?\UNC\host\share"),
            (r"\\?\GLOBALROOT\Device\Mup\host\share", r"\\?\GLOBALROOT\Device\Mup\host\share"),
            (r"C:\plain", r"C:\plain"),
            ("//host/share", "//host/share"),
            ("", ""),
        ],
        ids=[
            "drive-local-folds",
            "lowercase-drive",
            "forward-slash-remainder",
            "unc-longform-untouched",
            "globalroot-untouched",
            "plain-drive-untouched",
            "posix-doubled-slash-untouched",
            "empty",
        ],
    )
    def test_fold_extended_length_local(self, raw, expected):
        r"""Only a drive-absolute ``\\?\`` remainder folds to a plain
        local path; ``\\?\UNC\`` and every other extended namespace are left
        intact so they stay UNC-shaped and fail closed."""
        from kiro_crew import hooks as hooks_mod

        assert hooks_mod._fold_extended_length_local(raw) == expected

    def test_extended_length_local_secret_path_is_refused(self, monkeypatch):
        r"""An extended-length credential path is folded at the INPUT and
        refused as sensitive.

        ``is_unc_shape`` reports ``\\?\C:\`` as non-UNC (it names a local drive,
        not a share), so the UNC gate does not fire on it. The prefix is folded
        at the input instead, so the sensitive-path fence sees the plain ``C:\``
        path and refuses a read of ``\\?\C:\Users\<user>\<secret-dir>\<leaf>``.
        The probe asserts the fence never sees the ``\\?\`` prefix -- the
        property that makes the credential leaf recognisable."""
        from kiro_crew import hooks as hooks_mod
        from kiro_crew import platform_compat

        seen: list[str] = []
        secret_dir = "." + "aws"

        def _probe(p):
            seen.append(p)
            return secret_dir in p.lower()

        self._windows(monkeypatch)
        monkeypatch.setattr(platform_compat, "first_linked_ancestor", lambda _p: None)
        monkeypatch.setattr(platform_compat, "is_link_or_junction", lambda _p: False)
        monkeypatch.setattr(hooks_mod, "is_sensitive_path", _probe)
        assert validate_file_path("\\\\?\\C:\\Users\\me\\" + secret_dir + "\\creds") is None
        assert seen, "the sensitive-path fence was never consulted"
        assert all(not p.startswith("\\\\?\\") for p in seen), seen

    def test_extended_length_local_persona_still_resolves(self, monkeypatch):
        r"""Non-vacuity: a benign extended-length local path
        (``\\?\C:\...\persona.md``) must still read -- the fold makes
        ``is_unc_shape`` see a plain (non-UNC) ``C:\`` path, and the fence
        passes a non-secret file. Proves the refusal above is the fence firing,
        not a blanket ``\\?\`` ban."""
        from kiro_crew import hooks as hooks_mod
        from kiro_crew import platform_compat

        self._windows(monkeypatch)
        monkeypatch.setattr(platform_compat, "first_linked_ancestor", lambda _p: None)
        monkeypatch.setattr(platform_compat, "is_link_or_junction", lambda _p: False)
        monkeypatch.setattr(hooks_mod, "is_sensitive_path", lambda _p: False)
        assert validate_file_path(r"\\?\C:\Users\me\project\persona.md") is not None

    @pytest.mark.parametrize(
        "raw",
        [
            r"\\?\UNC\evil-host\share\doc.txt",
            r"\\?\GLOBALROOT\Device\Mup\evil-host\share\doc.txt",
            r"\\.\PhysicalDrive0",
        ],
        ids=["unc-longform", "globalroot", "physicaldrive"],
    )
    def test_extended_namespace_input_is_refused_before_resolution(self, monkeypatch, raw):
        r"""A raw ``\\?\UNC\...``, ``\\?\GLOBALROOT\...`` or ``\\.\device`` input
        is NOT folded to a local path: it stays UNC-shaped and the UNC
        trusted-root gate refuses it before any resolution (``realpath`` wired
        to explode proves the gate returned first)."""
        from kiro_crew import platform_compat

        def _boom(_p):  # pragma: no cover
            raise AssertionError("resolution ran on an extended-namespace input")

        self._windows(monkeypatch, realpath=_boom)
        monkeypatch.setattr(platform_compat, "first_linked_ancestor", lambda _p: None)
        assert validate_file_path(raw) is None

    def _windows(self, monkeypatch, realpath=os.path.realpath, path_module=os.path):
        """Simulate the Windows gates without patching the global os.name
        (which would make pathlib dispatch WindowsPath on a POSIX host).
        NOTE: candidate strings stay host-native under this simulation; the
        Windows CI shard exercises real backslash shapes via tmp_path. The
        namespace carries every os attribute the validate_file_path call
        graph can reach (unc_probe_allowed folds with normcase/normpath and
        joins with sep) so a stub miss cannot masquerade as a product bug."""
        import types

        from kiro_crew import hooks as hooks_mod

        monkeypatch.setattr(
            hooks_mod,
            "os",
            types.SimpleNamespace(
                name="nt",
                sep=path_module.sep,
                # unc_probe_allowed and main's _unc_agents_root memo key read
                # the environment through this namespace; carry the real
                # mapping so a stub miss cannot masquerade as a product bug.
                environ=os.environ,
                fspath=os.fspath,
                readlink=os.readlink,
                path=types.SimpleNamespace(
                    expanduser=path_module.expanduser,
                    abspath=path_module.abspath,
                    realpath=realpath,
                    normcase=path_module.normcase,
                    normpath=path_module.normpath,
                    isabs=path_module.isabs,
                    join=path_module.join,
                    dirname=path_module.dirname,
                    relpath=path_module.relpath,
                ),
            ),
        )

    def test_local_junction_ancestor_is_rewritten_before_realpath(self, monkeypatch):
        """The exact reported path stays clickable when ``Tasks`` is a local
        junction. The screen replaces the linked prefix before ``realpath``
        while preserving the descendant path."""
        from kiro_crew import hooks as hooks_mod
        from kiro_crew import platform_compat

        raw = (
            r"C:\wbr\workflow-takeover\Tasks"
            r"\TASK-2028 - Series Episode Table Revamp\cr-comments.md"
        )
        linked = r"C:\wbr\workflow-takeover\Tasks"
        destination = r"\\?\C:\wbr\workflow-takeover-tasks"
        expected = (
            r"C:\wbr\workflow-takeover-tasks"
            r"\TASK-2028 - Series Episode Table Revamp\cr-comments.md"
        )
        seen: list[str] = []

        def _realpath(path):
            seen.append(path)
            return path

        self._windows(monkeypatch, realpath=_realpath, path_module=ntpath)
        monkeypatch.setattr(
            platform_compat,
            "first_linked_ancestor",
            lambda path: linked if ntpath.normcase(path) == ntpath.normcase(raw) else None,
        )
        monkeypatch.setattr(platform_compat, "is_link_or_junction", lambda _p: False)
        monkeypatch.setattr(hooks_mod.os, "readlink", lambda _p: destination)
        monkeypatch.setattr(hooks_mod, "is_sensitive_path", lambda _p: False)

        assert validate_file_path(raw) == expected
        assert seen == [expected]

    def test_junction_ancestor_aimed_at_unc_is_refused_before_realpath(self, monkeypatch):
        """A local-looking junction aimed at an SMB share still refuses before
        ``realpath`` can start an outbound connection."""
        from kiro_crew import hooks as hooks_mod
        from kiro_crew import platform_compat

        raw = r"C:\wbr\workflow-takeover\Tasks\TASK-2028\cr-comments.md"
        linked = r"C:\wbr\workflow-takeover\Tasks"

        def _boom(_p):  # pragma: no cover
            raise AssertionError("realpath ran before the ancestor target screen")

        self._windows(monkeypatch, realpath=_boom, path_module=ntpath)
        monkeypatch.setattr(
            platform_compat,
            "first_linked_ancestor",
            lambda path: linked if ntpath.normcase(path) == ntpath.normcase(raw) else None,
        )
        monkeypatch.setattr(platform_compat, "is_link_or_junction", lambda _p: False)
        monkeypatch.setattr(
            hooks_mod.os, "readlink", lambda _p: r"\\evil-host\share", raising=False
        )

        assert validate_file_path(raw) is None

    def test_plain_path_still_validates_without_links(self, tmp_path, monkeypatch):
        """A normal local path still validates when the screen finds no links."""
        from kiro_crew import platform_compat

        f = _write(tmp_path / "ok.txt", "x")
        self._windows(monkeypatch)
        monkeypatch.setattr(platform_compat, "first_linked_ancestor", lambda _p: None)
        assert _same(validate_file_path(str(f)) or "", str(f))

    @pytest.mark.skipif(os.name != "nt", reason="junctions exist only on Windows")
    def test_a_real_local_junction_ancestor_resolves_to_its_target(self, tmp_path):
        r"""No stubs: a junction ``_winapi.CreateJunction`` made on this host.

        ``os.readlink`` on a real junction answers in the ``\\?\`` extended
        form, so this is the shape the screen must fold, not the one the
        simulated tests hand it. The file below the junction validates and the
        result is the TARGET's path, proving the linked prefix was rewritten
        rather than refused or resolved through.
        """
        import _winapi

        target = tmp_path / "workflow-takeover-tasks"
        (target / "TASK-2028 - Series Episode Table Revamp").mkdir(parents=True)
        doc = _write(target / "TASK-2028 - Series Episode Table Revamp" / "cr-comments.md", "x")
        junction = tmp_path / "Tasks"
        _winapi.CreateJunction(str(target), str(junction))
        raw = str(junction / "TASK-2028 - Series Episode Table Revamp" / "cr-comments.md")

        got = validate_file_path(raw)
        assert got is not None
        assert _same(got, str(doc))

    def _stub_ancestor(self, monkeypatch, raw, linked, destination):
        from kiro_crew import hooks as hooks_mod
        from kiro_crew import platform_compat

        self._windows(monkeypatch, path_module=ntpath)
        monkeypatch.setattr(
            platform_compat,
            "first_linked_ancestor",
            lambda path: linked if ntpath.normcase(path) == ntpath.normcase(raw) else None,
        )
        monkeypatch.setattr(hooks_mod.os, "readlink", lambda _p: destination)

    def test_shared_ancestor_screen_rewrites_a_local_junction_and_keeps_the_leaf(self, monkeypatch):
        """``screen_windows_linked_ancestors`` rewrites the linked prefix and
        does NOT follow the leaf, even when the leaf is itself a link: the
        caller's own leaf rule judges that."""
        from kiro_crew import platform_compat
        from kiro_crew.hooks import screen_windows_linked_ancestors

        raw = r"C:\wbr\Tasks\shot.png"
        self._stub_ancestor(monkeypatch, raw, r"C:\wbr\Tasks", r"\\?\C:\wbr\tasks-real")
        monkeypatch.setattr(platform_compat, "is_link_or_junction", lambda _p: True)

        got = screen_windows_linked_ancestors(raw)

        assert got is not None
        assert str(got) == r"C:\wbr\tasks-real\shot.png"

    @pytest.mark.parametrize(
        "destination",
        [
            r"\\evil-host\share",
            r"\\?\UNC\evil-host\share",
            r"\\?\GLOBALROOT\Device\HarddiskVolume1",
            r"D:relative",
            r"\rooted-no-drive",
        ],
    )
    def test_shared_ancestor_screen_refuses_untrusted_targets(self, monkeypatch, destination):
        """UNC share, device namespace, drive-relative and root-relative targets
        are refused by the shared screen, so every caller refuses them."""
        from kiro_crew.hooks import screen_windows_linked_ancestors

        raw = r"C:\wbr\Tasks\shot.png"
        self._stub_ancestor(monkeypatch, raw, r"C:\wbr\Tasks", destination)

        assert screen_windows_linked_ancestors(raw) is None

    def test_shared_ancestor_screen_refuses_a_suffix_climbing_out_of_the_link(self, monkeypatch):
        """A child suffix that climbs back above the link with ``..`` is refused,
        so the rewritten path can never land above the link's target."""
        from kiro_crew.hooks import screen_windows_linked_ancestors

        raw = r"C:\wbr\Tasks\..\secret.png"
        self._stub_ancestor(monkeypatch, raw, r"C:\wbr\Tasks", r"\\?\C:\wbr\tasks-real")

        assert screen_windows_linked_ancestors(raw) is None

    def test_a_leaf_link_aimed_at_unc_is_refused_before_realpath(self, tmp_path, monkeypatch):
        """A leaf FILE symlink is part of this function's contract (it
        resolves and re-checks), so the leaf is not blanket-refused -- but a
        leaf link aimed straight at an untrusted UNC share must be refused
        before the realpath that would probe it. readlink is a local
        metadata read, wired here to prove no traversal happened."""
        from kiro_crew import hooks as hooks_mod
        from kiro_crew import platform_compat

        def _boom(_p):  # pragma: no cover
            raise AssertionError("realpath ran before the leaf target screen")

        self._windows(monkeypatch, realpath=_boom)
        monkeypatch.setattr(platform_compat, "first_linked_ancestor", lambda _p: None)
        monkeypatch.setattr(platform_compat, "is_link_or_junction", lambda _p: True)
        monkeypatch.setattr(
            hooks_mod.os, "readlink", lambda _p: r"\\evil-host\share\doc.txt", raising=False
        )
        assert validate_file_path(str(tmp_path / "doc.txt")) is None

    def test_a_benign_leaf_link_still_resolves_on_windows(self, tmp_path, monkeypatch):
        """The documented contract resolves benign leaf symlinks (CI pins it
        end-to-end with a real link in test_hooks_shell_command.py test_allows_benign_symlink);
        the Windows leaf screen must not blanket-refuse them -- only an
        untrusted-UNC target refuses. Under this simulation no real link
        exists, so the pin is that validation SUCCEEDS (reaches realpath)
        rather than returning None."""
        from kiro_crew import hooks as hooks_mod
        from kiro_crew import platform_compat

        alias = tmp_path / "alias.txt"
        self._windows(monkeypatch)
        monkeypatch.setattr(platform_compat, "first_linked_ancestor", lambda _p: None)
        monkeypatch.setattr(platform_compat, "is_link_or_junction", lambda p: str(p) == str(alias))
        # A drive-absolute target: the shape a real Windows link carries.
        monkeypatch.setattr(
            hooks_mod.os, "readlink", lambda _p: r"C:\Users\me\real.txt", raising=False
        )
        assert validate_file_path(str(alias)) is not None

    @pytest.mark.parametrize("where", ["ancestor", "leaf"])
    def test_a_link_whose_target_cannot_be_read_is_unchecked(self, tmp_path, monkeypatch, where):
        """A Windows link whose own target ``os.readlink`` cannot read is declined
        as UNCHECKED: the screen did not finish, so a caller that keeps state per
        path retries it. ``validate_file_path`` still answers ``None``."""
        from kiro_crew import hooks as hooks_mod
        from kiro_crew import platform_compat

        def _boom(_p):  # pragma: no cover
            raise AssertionError("realpath ran past a link the screen could not read")

        def _unreadable(_p):
            raise PermissionError("reparse data unreadable")

        raw = str(tmp_path / "doc.txt")
        self._windows(monkeypatch, realpath=_boom)
        monkeypatch.setattr(
            platform_compat,
            "first_linked_ancestor",
            (lambda _p: str(tmp_path)) if where == "ancestor" else (lambda _p: None),
        )
        monkeypatch.setattr(platform_compat, "is_link_or_junction", lambda _p: where == "leaf")
        monkeypatch.setattr(hooks_mod.os, "readlink", _unreadable, raising=False)

        assert hooks_mod.screen_file_path(raw) is hooks_mod.PathDecline.UNCHECKED
        assert validate_file_path(raw) is None

    def test_a_link_target_the_screen_does_not_accept_is_refused(self, tmp_path, monkeypatch):
        """A link target the screen does not accept is a lasting decline."""
        from kiro_crew import hooks as hooks_mod
        from kiro_crew import platform_compat

        self._windows(monkeypatch)
        monkeypatch.setattr(platform_compat, "first_linked_ancestor", lambda _p: None)
        monkeypatch.setattr(platform_compat, "is_link_or_junction", lambda _p: True)
        monkeypatch.setattr(
            hooks_mod.os, "readlink", lambda _p: r"\\other-host\share\doc.txt", raising=False
        )

        raw = str(tmp_path / "doc.txt")
        assert hooks_mod.screen_file_path(raw) is hooks_mod.PathDecline.REFUSED

    @pytest.mark.parametrize(
        ("second_reading", "expected"),
        [
            ("Blocked: access to sensitive path: {p}", "REFUSED"),
            ("{stall}. Path: {p!r}", "UNCHECKED"),
            (None, "UNCHECKED"),
        ],
        ids=["match", "stall", "clean-on-second-reading"],
    )
    def test_the_decline_kind_follows_the_refusal_wording(
        self, tmp_path, monkeypatch, second_reading, expected
    ):
        from kiro_crew import hooks as hooks_mod
        from kiro_crew.security import UNVERIFIABLE_PATH_PREFIX

        def refusal(p, base_dir=None):
            if second_reading is None:
                return None
            return second_reading.format(p=p, stall=UNVERIFIABLE_PATH_PREFIX)

        target = _write(tmp_path / "doc.txt", "x")
        monkeypatch.setattr(hooks_mod, "is_sensitive_path", lambda _p: True)
        monkeypatch.setattr(hooks_mod, "sensitive_path_refusal", refusal)

        assert hooks_mod.screen_file_path(str(target)) is getattr(hooks_mod.PathDecline, expected)

    def test_validate_file_path_reads_the_sensitive_list_once(self, tmp_path, monkeypatch):
        from kiro_crew import hooks as hooks_mod

        asked: list[str] = []
        target = _write(tmp_path / "doc.txt", "x")
        monkeypatch.setattr(hooks_mod, "is_sensitive_path", lambda _p: True)
        monkeypatch.setattr(
            hooks_mod, "sensitive_path_refusal", lambda p, base_dir=None: asked.append(p)
        )

        assert validate_file_path(str(target)) is None
        assert asked == []

    def test_an_accepted_path_is_the_validated_path(self, tmp_path):
        from kiro_crew import hooks as hooks_mod

        target = _write(tmp_path / "doc.txt", "x")

        assert hooks_mod.screen_file_path(str(target)) == validate_file_path(str(target))

    def test_a_multi_hop_chain_landing_on_unc_is_refused(self, tmp_path, monkeypatch):
        r"""A leaf link -> LOCAL link -> UNC chain must be refused hop by hop:
        screening only the first target would launder the probe through the
        intermediate local link."""
        from kiro_crew import hooks as hooks_mod
        from kiro_crew import platform_compat

        alias = tmp_path / "alias.txt"
        mid_t = r"C:\Users\me\mid.txt"

        def _boom(_p):  # pragma: no cover
            raise AssertionError("realpath ran before the chain walk refused")

        self._windows(monkeypatch, realpath=_boom)
        monkeypatch.setattr(platform_compat, "first_linked_ancestor", lambda _p: None)
        monkeypatch.setattr(
            platform_compat,
            "is_link_or_junction",
            lambda p: str(p) in (str(alias), mid_t),
        )
        targets = {str(alias): mid_t, mid_t: r"\\evil-host\share\doc.txt"}
        monkeypatch.setattr(hooks_mod.os, "readlink", lambda p: targets[str(p)], raising=False)
        assert validate_file_path(str(alias)) is None

    def test_an_overlong_leaf_chain_is_refused_not_probed(self, tmp_path, monkeypatch):
        """A chain longer than the walk's bound refuses rather than probes --
        the same fail-closed posture as the kernels' own ELOOP ceiling."""
        from kiro_crew import hooks as hooks_mod
        from kiro_crew import platform_compat

        alias = tmp_path / "alias.txt"

        def _boom(_p):  # pragma: no cover
            raise AssertionError("realpath ran on an over-long chain")

        self._windows(monkeypatch, realpath=_boom)
        monkeypatch.setattr(platform_compat, "first_linked_ancestor", lambda _p: None)
        monkeypatch.setattr(platform_compat, "is_link_or_junction", lambda _p: True)
        monkeypatch.setattr(hooks_mod.os, "readlink", lambda _p: r"C:\loop\self.lnk", raising=False)
        assert validate_file_path(str(alias)) is None

    @pytest.mark.parametrize(
        "exotic",
        [r"\pivot\file.txt", r"D:pivot\file.txt"],
        ids=["root-relative", "drive-relative"],
    )
    def test_root_and_drive_relative_targets_are_refused(self, tmp_path, monkeypatch, exotic):
        r"""Root-relative (\pivot -> the CURRENT drive's root) and
        drive-relative (D:pivot -> D:'s per-drive CWD) targets resolve
        against ambient state the walk cannot see, so the screened string
        and the resolved string could diverge by drive. Refused fail-closed
        before realpath."""
        from kiro_crew import hooks as hooks_mod
        from kiro_crew import platform_compat

        alias = tmp_path / "alias.txt"

        def _boom(_p):  # pragma: no cover
            raise AssertionError("realpath ran on an ambient-state target")

        self._windows(monkeypatch, realpath=_boom)
        monkeypatch.setattr(platform_compat, "first_linked_ancestor", lambda _p: None)
        monkeypatch.setattr(platform_compat, "is_link_or_junction", lambda p: str(p) == str(alias))
        monkeypatch.setattr(hooks_mod.os, "readlink", lambda _p: exotic, raising=False)
        assert validate_file_path(str(alias)) is None

    def test_an_adversarially_deep_path_is_refused_before_the_walk(self, monkeypatch):
        """The screen is one lstat per component, so the walk's cost is
        bounded BEFORE it starts: a path with thousands of components would
        stall the event loop inside the guard itself."""
        from kiro_crew import platform_compat

        def _boom(_p):  # pragma: no cover
            raise AssertionError("ancestor walk started on an over-deep path")

        self._windows(monkeypatch)
        monkeypatch.setattr(platform_compat, "first_linked_ancestor", _boom)
        deep = "/" + "/".join("a" * 1 for _ in range(300)) + "/doc.txt"
        assert validate_file_path(deep) is None

    def test_an_unreadable_leaf_link_fails_closed(self, tmp_path, monkeypatch):
        from kiro_crew import hooks as hooks_mod
        from kiro_crew import platform_compat

        def _raise(_p):
            raise OSError("unreadable reparse point")

        self._windows(monkeypatch)
        monkeypatch.setattr(platform_compat, "first_linked_ancestor", lambda _p: None)
        monkeypatch.setattr(platform_compat, "is_link_or_junction", lambda _p: True)
        monkeypatch.setattr(hooks_mod.os, "readlink", _raise, raising=False)
        assert validate_file_path(str(tmp_path / "doc.txt")) is None

    def test_a_longform_unc_leaf_target_is_still_refused(self, tmp_path, monkeypatch):
        r"""The \\?\UNC\host\share long-path spelling folds into the screened
        UNC shape rather than slipping past as a non-UNC-looking string."""
        from kiro_crew import hooks as hooks_mod
        from kiro_crew import platform_compat

        self._windows(monkeypatch)
        monkeypatch.setattr(platform_compat, "first_linked_ancestor", lambda _p: None)
        monkeypatch.setattr(platform_compat, "is_link_or_junction", lambda _p: True)
        monkeypatch.setattr(
            hooks_mod.os,
            "readlink",
            lambda _p: "\\\\?\\UNC\\evil-host\\share\\doc.txt",
            raising=False,
        )
        assert validate_file_path(str(tmp_path / "doc.txt")) is None

    @pytest.mark.parametrize(
        "spelling",
        [
            "\\\\?\\unc\\evil-host\\share\\doc.txt",
            "\\\\?\\Unc\\evil-host\\share\\doc.txt",
            "\\\\?\\uNC\\evil-host\\share\\doc.txt",
        ],
        ids=["lowercase", "titlecase", "mixed"],
    )
    def test_a_mixed_case_longform_unc_target_is_still_refused(
        self, tmp_path, monkeypatch, spelling
    ):
        r"""The OS resolves \\?\unc\... case-insensitively, so the fold must
        match the UNC component case-insensitively too: a case-sensitive
        match would drop a lowercase spelling into the plain \\?\ branch,
        strip four characters, and launder the share into a relative-looking
        string that realpath would then probe over SMB."""
        from kiro_crew import hooks as hooks_mod
        from kiro_crew import platform_compat

        def _boom(_p):  # pragma: no cover
            raise AssertionError("realpath ran on a mixed-case UNC target")

        self._windows(monkeypatch, realpath=_boom)
        monkeypatch.setattr(platform_compat, "first_linked_ancestor", lambda _p: None)
        monkeypatch.setattr(platform_compat, "is_link_or_junction", lambda _p: True)
        monkeypatch.setattr(hooks_mod.os, "readlink", lambda _p: spelling, raising=False)
        assert validate_file_path(str(tmp_path / "doc.txt")) is None

    @pytest.mark.parametrize(
        "spelling",
        [
            "\\\\?\\GLOBALROOT\\Device\\Mup\\evil-host\\share\\doc.txt",
            "\\\\?\\Volume{deadbeef-0000-0000-0000-000000000000}\\doc.txt",
        ],
        ids=["globalroot-mup", "volume-guid"],
    )
    def test_a_non_drive_extended_namespace_target_is_refused(
        self, tmp_path, monkeypatch, spelling
    ):
        r"""A bare \\?\ prefix is only folded when the remainder is a
        drive-absolute local path. Any other extended namespace (GLOBALROOT
        device-namespace spelling of a UNC share, a Volume GUID) must be
        refused outright: stripping the prefix would leave a string with no
        leading separator and no drive, which the shape allowlist would then
        anchor as PLAIN RELATIVE while realpath follows the real link."""
        from kiro_crew import hooks as hooks_mod
        from kiro_crew import platform_compat

        def _boom(_p):  # pragma: no cover
            raise AssertionError("realpath ran on an extended-namespace target")

        self._windows(monkeypatch, realpath=_boom)
        monkeypatch.setattr(platform_compat, "first_linked_ancestor", lambda _p: None)
        monkeypatch.setattr(platform_compat, "is_link_or_junction", lambda _p: True)
        monkeypatch.setattr(hooks_mod.os, "readlink", lambda _p: spelling, raising=False)
        assert validate_file_path(str(tmp_path / "doc.txt")) is None

    @pytest.mark.skipif(os.name == "nt", reason="pins POSIX resolve-through-symlink semantics")
    def test_posix_dotdot_still_resolves_through_the_symlink(self, tmp_path):
        """A `..` crossing a symlinked component must keep resolving THROUGH
        the link (realpath order), not be collapsed lexically first -- the
        Windows-only abspath anchoring must never leak into the POSIX path."""
        real = tmp_path / "srv"
        (real / "sub").mkdir(parents=True)
        x = _write(real / "x.txt", "payload")
        pub = tmp_path / "pub"
        pub.mkdir()
        link = pub / "link"
        link.symlink_to(real / "sub", target_is_directory=True)

        got = validate_file_path(str(link / ".." / "x.txt"))

        # realpath: link -> srv/sub, then `..` -> srv, then x.txt. A lexical
        # collapse would instead yield pub/x.txt, which does not exist.
        assert got is not None
        assert _same(got, str(x))

    def test_the_ancestor_walk_is_not_consulted_on_posix(self, tmp_path, monkeypatch):
        """On POSIX the real guard is is_sensitive_path on the RESOLVED path;
        an unconditional walk would refuse a symlinked /home."""
        if os.name == "nt":
            pytest.skip("gate is active on Windows by design")
        from kiro_crew import platform_compat

        def _boom(_p):  # pragma: no cover
            raise AssertionError("ancestor walk ran on POSIX")

        monkeypatch.setattr(platform_compat, "first_linked_ancestor", _boom)
        f = _write(tmp_path / "ok.txt", "x")
        assert _same(validate_file_path(str(f)) or "", str(f))

    def test_unc_shaped_anchored_form_is_refused_before_the_walk(self, monkeypatch):
        """A `~` expanding to a roaming-profile UNC home surfaces a UNC shape
        the raw text did not have. The ANCHORED form (the exact string walked
        and resolved) must be screened before the ancestor walk -- the walk is
        an lstat per component, so on an untrusted UNC path the walk itself
        would be the outbound SMB probe. abspath never strips UNC-ness, so
        this single screen covers the expanded form too."""
        import types

        from kiro_crew import hooks as hooks_mod
        from kiro_crew import platform_compat

        def _boom(_p):  # pragma: no cover
            raise AssertionError("ancestor walk ran on a UNC-shaped expansion")

        monkeypatch.setattr(platform_compat, "first_linked_ancestor", _boom)
        monkeypatch.setattr(
            hooks_mod,
            "os",
            types.SimpleNamespace(
                name="nt",
                sep=os.sep,
                environ=os.environ,
                path=types.SimpleNamespace(
                    expanduser=lambda _raw: "//evil-host/share/doc.txt",
                    abspath=os.path.abspath,
                    realpath=os.path.realpath,
                    # unc_probe_allowed's lexical folding runs on this
                    # namespace too once the expanded form is UNC-shaped.
                    normcase=os.path.normcase,
                    normpath=os.path.normpath,
                ),
            ),
        )
        assert validate_file_path("~/doc.txt") is None

    def test_unc_home_sessions_transcript_is_refused(self, monkeypatch):
        """A kiro-cli session transcript under a Windows roaming-profile
        (UNC) home is refused by the UNC trusted-root gate, because the sessions
        dir is not one of unc_probe_allowed's admitted roots. This is the exact
        refusal the usage page counts as ``refused_transcripts`` instead of
        rendering a confident zero. Exercised as DATA -- a ``\\\\server\\share``
        string through validate_file_path -- so it runs on a POSIX host; the
        Windows CI shard confirms the native backslash form.

        This refusal is NOT relaxed: admitting the sessions
        dir to the gate is a separate trust decision. This
        test therefore pins that the transcript STAYS refused.
        """
        from kiro_crew import platform_compat

        # No linked ancestor: isolate the pure UNC-shape screen.
        monkeypatch.setattr(platform_compat, "first_linked_ancestor", lambda _p: None)
        self._windows(monkeypatch)
        unc_transcript = "//roaming-server/profiles/alice/.kiro/sessions/cli/s1.jsonl"
        assert validate_file_path(unc_transcript) is None

    def test_unc_path_under_data_home_still_validates(self, monkeypatch, tmp_path):
        """Control for the test above: a UNC path UNDER an admitted root (the
        data home) is NOT refused by the UNC gate -- so the refusal there is
        attributable to the sessions dir being outside the trusted roots, not
        to a blanket UNC ban. Uses a UNC-shaped data_home so unc_probe_allowed
        has a UNC root to match against."""
        import kiro_crew.hooks as hooks_mod

        unc_home = "//roaming-server/profiles/alice/.kiro/crew"
        monkeypatch.setattr(hooks_mod._config_paths, "peek_data_home", lambda: Path(unc_home))
        self._windows(monkeypatch)
        candidate = unc_home + "/ledger/state.json"
        # The UNC gate admits it (unc_probe_allowed returns True); the value may
        # still be canonicalized downstream, but it is NOT refused by the gate.
        assert hooks_mod.unc_probe_allowed(candidate) is True

    def test_root_resolution_performs_no_maintenance_io(self, monkeypatch, tmp_path):
        """The trusted-root memo resolves WHERE the data home is without
        creating it or refreshing the recovery breadcrumb.

        ``_unc_data_home_root()`` is primed at import time, so if it delegated
        to ``data_home()`` a first resolution would run ``config_dir()``'s
        maintenance -- ``mkdir(parents=True)`` plus the breadcrumb write --
        as a side effect of importing this module. It resolves through
        ``peek_data_home()`` instead: same override predicate, no filesystem
        writes.
        """
        import kiro_crew.config.paths as paths_mod
        import kiro_crew.hooks as hooks_mod

        home = tmp_path / "unmade" / ".kiro" / "crew"
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        # Fresh resolution state: nothing memoized from other tests.
        monkeypatch.setattr(paths_mod, "_resolved_home", None)
        monkeypatch.setattr(hooks_mod, "_unc_data_home_root_cache", None)

        root = hooks_mod._unc_data_home_root()

        assert root == home.resolve() or root == home
        # The whole point: resolution did NOT create the home...
        assert not home.exists()
        # ...and did not run breadcrumb maintenance anywhere under tmp_path.
        assert not list(tmp_path.rglob("*.breadcrumb"))

    def test_root_memo_invalidates_on_peek_accessor_swap(self, monkeypatch):
        """The memo key carries the identity of the accessor the root is
        resolved through (``peek_data_home``), so a monkeypatched accessor --
        how every test above steers the gate -- invalidates the memo instead
        of serving a stale root past it.
        """
        import kiro_crew.hooks as hooks_mod

        monkeypatch.setattr(hooks_mod, "_unc_data_home_root_cache", None)
        first = hooks_mod._unc_data_home_root()
        assert first is not None

        swapped = Path("//other-server/profiles/bob/.kiro/crew")
        monkeypatch.setattr(hooks_mod._config_paths, "peek_data_home", lambda: swapped)
        assert hooks_mod._unc_data_home_root() == swapped


class TestSafeReadFile:
    def test_reads_text(self, tmp_path):
        f = _write(tmp_path / "a.txt", "hello\n")
        assert safe_read_file(str(f)) == "hello\n"

    def test_sensitive_path_refused(self):
        with pytest.raises(PermissionError, match="sensitive path"):
            safe_read_file(str(Path.home() / ".ssh" / "id_rsa"))

    def test_missing_file_propagates(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            safe_read_file(str(tmp_path / "nope.txt"))

    def test_sensitive_refusal_message_escapes_the_path(self, monkeypatch):
        """The refused path is caller/attacker influenced and the message reaches
        log records via ``exc_info``; a raw newline in it would forge a second
        record (the log-forgery class).
        """
        forged = "/tmp/pods/wt-evil\nWARNING forged: reclaim authorized"
        # The message carries the RESOLVED path (drive-lettered and
        # backslashed on Windows), so compute the expectation the same way
        # production does.
        resolved = os.path.realpath(os.path.expanduser(forged))
        monkeypatch.setattr(
            hooks_mod,
            "sensitive_path_refusal",
            lambda p, *a, **k: f"Blocked: access to sensitive path: {p}",
        )
        with pytest.raises(PermissionError, match="sensitive path") as excinfo:
            safe_read_file(forged)
        message = str(excinfo.value)
        assert "\n" not in message
        assert repr(resolved) in message

    def test_symlink_refusal_message_escapes_the_path(self, tmp_path, monkeypatch):
        """The ELOOP arm keeps the pre-race resolved path — including any
        newline-bearing directory name — so its message must escape it too.
        """
        import errno as _errno

        from kiro_crew import platform_compat as _pc

        f = tmp_path / "map.json"
        f.write_text("{}", encoding="utf-8")
        resolved = os.path.realpath(str(f))

        real_no_reparse = _pc.open_file_no_reparse

        # Patch the open the code actually performs, not one platform's
        # implementation of it: the Windows arm of open_file_no_reparse reaches
        # CreateFileW, so a patch on os.open would simulate the race on POSIX only
        # and the assertion would pass for the wrong reason on the Windows shard.
        def _eloop(path, *args, **kwargs):
            if str(path) == resolved:
                raise OSError(_errno.ELOOP, "symlink swapped in")
            return real_no_reparse(path, *args, **kwargs)

        monkeypatch.setattr(_pc, "open_file_no_reparse", _eloop)
        with pytest.raises(PermissionError, match="refusing to follow symlink") as excinfo:
            safe_read_file(str(f))
        message = str(excinfo.value)
        assert "\n" not in message
        assert repr(resolved) in message


class TestSafeReadFileBytes:
    def test_reads_bytes(self, tmp_path):
        f = _write(tmp_path / "a.bin", "abc")
        assert safe_read_file_bytes(str(f)) == b"abc"

    def test_rejected_path_returns_none(self):
        assert safe_read_file_bytes("") is None
        assert safe_read_file_bytes(str(Path.home() / ".aws" / "config")) is None

    def test_missing_file_returns_none(self, tmp_path):
        assert safe_read_file_bytes(str(tmp_path / "gone")) is None

    def test_directory_returns_none(self, tmp_path):
        d = tmp_path / "adir"
        d.mkdir()
        # A directory is either EISDIR on open or unreadable on read; both -> None.
        assert safe_read_file_bytes(str(d)) is None

    def test_oversize_raises(self, tmp_path, monkeypatch):
        monkeypatch.setattr(hooks_mod, "MAX_FILE_BYTES", 4)
        f = _write(tmp_path / "big.bin", "0123456789")
        with pytest.raises(FileTooLargeError):
            safe_read_file_bytes(str(f))


class TestSafeReadFileBytesWithIdentity:
    def test_allowlisted_inode_is_read(self, tmp_path):
        f = _write(tmp_path / "a.txt", "payload")
        assert safe_read_file_bytes_with_identity(str(f), {_identity(f)}) == b"payload"

    def test_unlisted_inode_is_refused(self, tmp_path):
        f = _write(tmp_path / "a.txt", "payload")
        with pytest.raises(PermissionError, match="not in the authorized set"):
            safe_read_file_bytes_with_identity(str(f), set())

    def test_rejected_path_returns_none(self, tmp_path):
        assert safe_read_file_bytes_with_identity("", {(1, 2)}) is None
        assert safe_read_file_bytes_with_identity(str(tmp_path / "gone"), {(1, 2)}) is None

    def test_symlink_swap_at_final_component_is_refused(self, tmp_path, monkeypatch):
        # validate_file_path resolves symlinks, so the refusal is reached by
        # making the post-validation open report ELOOP -- the TOCTOU shape the
        # final-component guard exists for. Patching open_file_no_reparse rather
        # than os.open keeps the simulation faithful on Windows, whose arm of that
        # helper reaches CreateFileW instead.
        f = _write(tmp_path / "a.txt", "payload")
        import errno as _errno

        from kiro_crew import platform_compat as _pc

        real_no_reparse = _pc.open_file_no_reparse

        def _eloop(path, *args, **kwargs):
            if _same(str(path), str(f)):
                raise OSError(_errno.ELOOP, "symlink swapped in")
            return real_no_reparse(path, *args, **kwargs)

        monkeypatch.setattr(_pc, "open_file_no_reparse", _eloop)
        with pytest.raises(PermissionError, match="refusing to follow symlink"):
            safe_read_file_bytes_with_identity(str(f), {_identity(f)})

    def test_oversize_raises(self, tmp_path, monkeypatch):
        monkeypatch.setattr(hooks_mod, "MAX_FILE_BYTES", 2)
        f = _write(tmp_path / "a.txt", "0123456789")
        with pytest.raises(FileTooLargeError):
            safe_read_file_bytes_with_identity(str(f), {_identity(f)})


class TestStatIdentity:
    def test_returns_dev_ino(self, tmp_path):
        f = _write(tmp_path / "a.txt", "x")
        assert stat_identity(str(f)) == _identity(f)

    def test_missing_or_rejected_returns_none(self, tmp_path):
        assert stat_identity(str(tmp_path / "gone")) is None
        assert stat_identity("") is None
        assert stat_identity(str(Path.home() / ".aws")) is None


class TestFdRealPath:
    def test_resolves_an_open_descriptor(self, tmp_path):
        f = _write(tmp_path / "a.txt", "x")
        fd = os.open(str(f), os.O_RDONLY)
        try:
            got = _fd_real_path(fd)
        finally:
            os.close(fd)
        # Windows is a supported descriptor-containment platform. Other
        # platforms without a supported mechanism still fail closed (None).
        if os.name == "nt":
            assert got is not None
            assert _same(got, str(f))
        else:
            assert got is None or _same(got, str(f))


class TestSafeReadPrefix:
    def test_non_positive_n_short_circuits(self, tmp_path):
        f = _write(tmp_path / "a.txt", "abcdef")
        assert safe_read_prefix(str(f), 0) == b""
        assert safe_read_prefix(str(f), -1) == b""

    def test_reads_only_the_prefix(self, tmp_path):
        f = _write(tmp_path / "a.txt", "abcdef")
        assert safe_read_prefix(str(f), 3) == b"abc"

    def test_rejected_or_missing_returns_none(self, tmp_path):
        assert safe_read_prefix("", 4) is None
        assert safe_read_prefix(str(tmp_path / "gone"), 4) is None
        assert safe_read_prefix(str(Path.home() / ".aws" / "config"), 4) is None


class TestIsAccessControlXattr:
    @pytest.mark.parametrize("attr", ["system.posix_acl_access", "system.posix_acl_default"])
    def test_access_control_attrs(self, attr):
        assert _is_access_control_xattr(attr) is True

    @pytest.mark.parametrize("attr", ["user.comment", "trusted.thing", ""])
    def test_informational_attrs(self, attr):
        assert _is_access_control_xattr(attr) is False

    @pytest.mark.parametrize(
        "attr",
        ["security.capability", "security.ima", "security.evm", "security.selinux"],
    )
    def test_a_privileged_attr_is_neither_carried_nor_fail_closed(self, attr):
        """These are outside the carry entirely, so also outside the refusal.

        The carry replays attributes onto an inode holding CALLER-supplied
        content, so a privilege- or integrity-bearing name must never be
        reproduced (see ``atomic_write._CARRIED_ACCESS_CONTROL_XATTRS``). Since it
        is never collected, no ``setxattr`` is attempted for it and it cannot
        refuse a save either -- which also stops ``security.selinux`` from failing
        every write on an enforcing host that denies ``relabelto``.
        """
        assert _should_carry_xattr(attr) is False
        assert _is_access_control_xattr(attr) is False

    @pytest.mark.parametrize(
        "attr", ["system.posix_acl_access", "system.posix_acl_default", "user.comment"]
    )
    def test_the_carried_set(self, attr):
        assert _should_carry_xattr(attr) is True


class TestSafeReadFileBytesNolink:
    def test_reads_a_plain_file(self, tmp_path):
        f = _write(tmp_path / "a.txt", "body")
        assert safe_read_file_bytes_nolink(str(f)) == b"body"

    def test_negative_max_bytes_is_a_programming_error(self, tmp_path):
        f = _write(tmp_path / "a.txt", "body")
        with pytest.raises(ValueError, match="non-negative"):
            safe_read_file_bytes_nolink(str(f), max_bytes=-1)

    def test_hardlinked_inode_refused(self, tmp_path):
        f = _write(tmp_path / "a.txt", "body")
        _try_hardlink(f, tmp_path / "b.txt")
        assert safe_read_file_bytes_nolink(str(f)) is None

    def test_a_hardlink_is_admitted_only_by_the_callback_on_its_bytes(self, tmp_path):
        """``admit_hardlinked`` is opt-in and judges the bytes actually read."""
        f = _write(tmp_path / "a.txt", "body")
        _try_hardlink(f, tmp_path / "b.txt")
        seen: list[tuple[str, bytes]] = []

        def admit(path: str, data: bytes) -> bool:
            seen.append((path, data))
            return data == b"body"

        assert safe_read_file_bytes_nolink(str(f), admit_hardlinked=admit) == b"body"
        assert [data for _path, data in seen] == [b"body"]
        assert os.path.samefile(seen[0][0], f)
        assert safe_read_file_bytes_nolink(str(f), admit_hardlinked=lambda p, d: False) is None

    def test_the_callback_is_not_consulted_for_a_single_link(self, tmp_path):
        f = _write(tmp_path / "a.txt", "body")
        calls: list[str] = []
        assert (
            safe_read_file_bytes_nolink(str(f), admit_hardlinked=lambda p, d: bool(calls.append(p)))
            == b"body"
        )
        assert calls == []

    def test_an_admitted_hardlink_still_passes_containment(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        outside = _write(tmp_path / "outside.txt", "out")
        _try_hardlink(outside, tmp_path / "alias.txt")
        assert (
            safe_read_file_bytes_nolink(
                str(outside), within_root=str(root), admit_hardlinked=lambda p, d: True
            )
            is None
        )

    def test_an_admitted_hardlink_is_never_truncated(self, tmp_path):
        f = _write(tmp_path / "a.txt", "0123456789")
        _try_hardlink(f, tmp_path / "b.txt")
        with pytest.raises(FileTooLargeError):
            safe_read_file_bytes_nolink(
                str(f), max_bytes=4, allow_truncate=True, admit_hardlinked=lambda p, d: True
            )

    @staticmethod
    def _kernel_names(monkeypatch, name: Path) -> None:
        """Make the descriptor's kernel name *name*, as macOS ``F_GETPATH`` does
        for a hardlinked inode about one read in a hundred."""
        spelled = os.path.realpath(name)
        monkeypatch.setattr(hooks_mod, "_fd_real_path", lambda fd: spelled)

    @pytest.mark.skipif(
        not hooks_mod.pinned_fs.supports_pinned_walk(), reason="needs the pinned witness walk"
    )
    def test_a_hardlink_the_kernel_names_by_its_sibling_is_still_read(self, tmp_path, monkeypatch):
        root = tmp_path / "root"
        root.mkdir()
        f = _write(root / "a.txt", "body")
        _try_hardlink(f, tmp_path / "sibling.txt")  # outside the root, like site-packages
        self._kernel_names(monkeypatch, tmp_path / "sibling.txt")
        admit = lambda p, d: True  # noqa: E731
        assert safe_read_file_bytes_nolink(str(f), admit_hardlinked=admit) == b"body"
        assert (
            safe_read_file_bytes_nolink(str(f), within_root=str(root), admit_hardlinked=admit)
            == b"body"
        )
        with pytest.raises(FileTooLargeError):
            safe_read_file_bytes_nolink(
                str(f), max_bytes=2, allow_truncate=True, admit_hardlinked=admit
            )

    def test_a_single_link_file_the_kernel_names_elsewhere_is_refused(self, tmp_path, monkeypatch):
        f = _write(tmp_path / "a.txt", "body")
        _write(tmp_path / "other.txt", "body")  # a different inode: a swap, not a link
        self._kernel_names(monkeypatch, tmp_path / "other.txt")
        assert safe_read_file_bytes_nolink(str(f)) is None

    def test_a_hardlink_whose_sibling_is_sensitive_is_refused(self, tmp_path, monkeypatch):
        root = tmp_path / "root"
        root.mkdir()
        f = _write(root / "a.txt", "body")
        sibling = tmp_path / "secret.txt"
        _try_hardlink(f, sibling)
        self._kernel_names(monkeypatch, sibling)
        flagged = os.path.realpath(sibling)
        real_sensitive = hooks_mod.is_sensitive_path
        monkeypatch.setattr(
            hooks_mod, "is_sensitive_path", lambda p: p == flagged or real_sensitive(p)
        )
        assert (
            safe_read_file_bytes_nolink(
                str(f), within_root=str(root), admit_hardlinked=lambda p, d: True
            )
            is None
        )

    def test_a_sensitive_sibling_named_only_at_the_containment_read_is_refused(
        self, tmp_path, monkeypatch
    ):
        # The kernel answers per call: the identity check can see the opened
        # name and the containment read the sensitive sibling. The containment
        # branch must refuse on its own.
        root = tmp_path / "root"
        root.mkdir()
        f = _write(root / "a.txt", "body")
        sibling = tmp_path / "secret.txt"
        _try_hardlink(f, sibling)
        answers = iter([os.path.realpath(f)])
        flagged = os.path.realpath(sibling)
        monkeypatch.setattr(hooks_mod, "_fd_real_path", lambda fd: next(answers, flagged))
        real_sensitive = hooks_mod.is_sensitive_path
        monkeypatch.setattr(
            hooks_mod, "is_sensitive_path", lambda p: p == flagged or real_sensitive(p)
        )
        assert (
            safe_read_file_bytes_nolink(
                str(f), within_root=str(root), admit_hardlinked=lambda p, d: True
            )
            is None
        )

    def test_non_regular_refused(self, tmp_path):
        d = tmp_path / "adir"
        d.mkdir()
        assert safe_read_file_bytes_nolink(str(d)) is None

    def test_rejected_and_missing_paths(self, tmp_path):
        assert safe_read_file_bytes_nolink("") is None
        assert safe_read_file_bytes_nolink(str(tmp_path / "gone")) is None

    def test_within_root_accepts_a_contained_file(self, tmp_path):
        root = tmp_path / "root"
        (root / "sub").mkdir(parents=True)
        f = _write(root / "sub" / "a.txt", "in")
        assert safe_read_file_bytes_nolink(str(f), within_root=str(root)) == b"in"

    def test_within_root_refuses_an_escaping_file(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        outside = _write(tmp_path / "outside.txt", "out")
        assert safe_read_file_bytes_nolink(str(outside), within_root=str(root)) is None

    def test_within_root_fails_closed_when_fd_path_unknown(self, tmp_path, monkeypatch):
        root = tmp_path / "root"
        root.mkdir()
        f = _write(root / "a.txt", "in")
        monkeypatch.setattr(hooks_mod, "_fd_real_path", lambda fd: None)
        assert safe_read_file_bytes_nolink(str(f), within_root=str(root)) is None

    def test_oversize_refused_by_default(self, tmp_path):
        f = _write(tmp_path / "a.txt", "0123456789")
        with pytest.raises(FileTooLargeError):
            safe_read_file_bytes_nolink(str(f), max_bytes=4)

    def test_oversize_truncated_when_caller_opts_in(self, tmp_path):
        f = _write(tmp_path / "a.txt", "0123456789")
        got = safe_read_file_bytes_nolink(str(f), max_bytes=4, allow_truncate=True)
        assert got == b"0123"


class TestSafeWriteFileNolink:
    def test_overwrites_an_existing_file(self, tmp_path):
        f = _write(tmp_path / "a.txt", "old")
        assert safe_write_file_nolink(str(f), "new body") is True
        assert f.read_text(encoding="utf-8") == "new body"

    def test_preserves_the_target_mode(self, tmp_path):
        if _IS_WINDOWS:
            pytest.skip("POSIX mode bits are not meaningful on Windows")
        f = _write(tmp_path / "a.txt", "old")
        os.chmod(f, 0o644)
        assert safe_write_file_nolink(str(f), "new") is True
        assert _stat.S_IMODE(os.stat(f).st_mode) == 0o644

    def test_refuses_to_create_a_missing_file(self, tmp_path):
        target = tmp_path / "gone.txt"
        assert safe_write_file_nolink(str(target), "x") is False
        assert not target.exists()

    def test_refuses_a_blank_or_sensitive_path(self):
        assert safe_write_file_nolink("", "x") is False
        assert safe_write_file_nolink(str(Path.home() / ".aws" / "credentials"), "x") is False

    def test_refuses_a_hardlinked_target(self, tmp_path):
        f = _write(tmp_path / "a.txt", "old")
        _try_hardlink(f, tmp_path / "b.txt")
        assert safe_write_file_nolink(str(f), "new") is False
        assert f.read_text(encoding="utf-8") == "old"

    def test_refuses_a_directory(self, tmp_path):
        d = tmp_path / "adir"
        d.mkdir()
        assert safe_write_file_nolink(str(d), "new") is False

    def test_within_root_accepts_a_contained_file(self, tmp_path):
        root = tmp_path / "root"
        (root / "sub").mkdir(parents=True)
        f = _write(root / "sub" / "a.txt", "old")
        assert safe_write_file_nolink(str(f), "new", within_root=str(root)) is True
        assert f.read_text(encoding="utf-8") == "new"

    def test_within_root_refuses_an_escaping_file(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        outside = _write(tmp_path / "outside.txt", "old")
        assert safe_write_file_nolink(str(outside), "new", within_root=str(root)) is False
        assert outside.read_text(encoding="utf-8") == "old"

    def test_within_root_fails_closed_when_fd_path_unknown(self, tmp_path, monkeypatch):
        root = tmp_path / "root"
        root.mkdir()
        f = _write(root / "a.txt", "old")
        monkeypatch.setattr(hooks_mod, "_fd_real_path", lambda fd: None)
        assert safe_write_file_nolink(str(f), "new", within_root=str(root)) is False
        assert f.read_text(encoding="utf-8") == "old"

    def test_no_staging_file_is_left_behind(self, tmp_path):
        f = _write(tmp_path / "a.txt", "old")
        assert safe_write_file_nolink(str(f), "new") is True
        assert sorted(p.name for p in tmp_path.iterdir()) == ["a.txt"]

    def test_a_target_swapped_after_validation_is_not_clobbered(self, tmp_path, monkeypatch):
        """A NEW inode at the target name after validation must refuse, not overwrite.

        Both re-checks in the write path (the pinned-parent re-resolve and the
        last-moment pre-rename stat) compare ``(st_dev, st_ino)`` against the
        validated identity, so reporting a foreign inode from the first stat of
        the target exercises the refusal on every platform -- the pinned branch
        where a directory fd is available, the pre-rename branch where it is not.
        """
        f = _write(tmp_path / "a.txt", "old")
        real_stat = os.stat
        state = {"fired": False}

        def _swap_after_staging(path, *args, **kwargs):
            st = real_stat(path, *args, **kwargs)
            if isinstance(path, int):
                return st
            try:
                name = os.fspath(path)
            except TypeError:  # pragma: no cover - defensive
                return st
            # Keyed on the staged sibling EXISTING rather than on a call count:
            # the identity re-checks are the only stats of the target that
            # happen after staging, whatever screening ran before it.
            if not isinstance(name, str) or os.path.basename(name) != f.name:
                return st
            if not _staged_sibling(tmp_path, f.name):
                return st
            state["fired"] = True

            class _Other:
                st_dev = st.st_dev
                st_ino = st.st_ino + 100000
                st_mode = st.st_mode
                st_nlink = 1

            return _Other()

        monkeypatch.setattr(os, "stat", _swap_after_staging)
        try:
            assert safe_write_file_nolink(str(f), "new") is False
        finally:
            monkeypatch.undo()
        assert state["fired"] is True
        assert f.read_text(encoding="utf-8") == "old"
        # The staged sibling is cleaned up even on the refusal path.
        assert sorted(p.name for p in tmp_path.iterdir()) == ["a.txt"]


class TestSafeCopyFileNolink:
    def test_copies_into_the_destination_dir(self, tmp_path):
        src = _write(tmp_path / "src.png", "bytes-here")
        dest = tmp_path / "dest"
        dest.mkdir()
        copied = safe_copy_file_nolink(str(src), str(dest))
        assert copied is not None
        assert Path(copied).read_text(encoding="utf-8") == "bytes-here"
        assert Path(copied).parent == dest
        assert Path(copied).suffix == ".png"

    def test_binary_payload_is_copied_byte_for_byte(self, tmp_path):
        """A media file must survive the copy exactly, 0x1A and CRLF included.

        This function exists to hand a large binary to a subprocess BY PATH, so
        byte fidelity is its whole contract. Two Windows-specific hazards can break
        it while every text-content test still passes: a CRT descriptor in text mode
        translates CRLF, and it reports end-of-file at the first 0x1A. The copy loop
        reads with a raw ``os.read``, which honours that mode, so the descriptor has
        to be opened in binary — a payload of ASCII would not detect either fault.
        """
        payload = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) + b"\r\ntail\x1amore\x00\xff"
        src = tmp_path / "clip.mp4"
        src.write_bytes(payload)
        dest = tmp_path / "dest"
        dest.mkdir()

        copied = safe_copy_file_nolink(str(src), str(dest))

        assert copied is not None
        assert Path(copied).read_bytes() == payload

    def test_copy_is_private(self, tmp_path):
        if _IS_WINDOWS:
            pytest.skip("POSIX mode bits are not meaningful on Windows")
        src = _write(tmp_path / "src.bin", "x")
        dest = tmp_path / "dest"
        dest.mkdir()
        copied = safe_copy_file_nolink(str(src), str(dest))
        assert copied is not None
        assert _stat.S_IMODE(os.stat(copied).st_mode) == 0o600

    def test_refuses_a_hardlinked_source(self, tmp_path):
        src = _write(tmp_path / "src.bin", "x")
        _try_hardlink(src, tmp_path / "link.bin")
        dest = tmp_path / "dest"
        dest.mkdir()
        assert safe_copy_file_nolink(str(src), str(dest)) is None
        assert list(dest.iterdir()) == []

    def test_refuses_a_directory_source(self, tmp_path):
        d = tmp_path / "adir"
        d.mkdir()
        dest = tmp_path / "dest"
        dest.mkdir()
        assert safe_copy_file_nolink(str(d), str(dest)) is None

    def test_missing_or_rejected_source(self, tmp_path):
        dest = tmp_path / "dest"
        dest.mkdir()
        assert safe_copy_file_nolink("", str(dest)) is None
        assert safe_copy_file_nolink(str(tmp_path / "gone"), str(dest)) is None

    def test_fails_closed_when_fd_path_unknown(self, tmp_path, monkeypatch):
        src = _write(tmp_path / "src.bin", "x")
        dest = tmp_path / "dest"
        dest.mkdir()
        monkeypatch.setattr(hooks_mod, "_fd_real_path", lambda fd: None)
        assert safe_copy_file_nolink(str(src), str(dest)) is None

    def test_unwritable_destination_returns_none(self, tmp_path):
        src = _write(tmp_path / "src.bin", "x")
        assert safe_copy_file_nolink(str(src), str(tmp_path / "no-such-dir")) is None
