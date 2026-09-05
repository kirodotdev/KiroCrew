"""Coverage tests for ``kiro_crew.hooks``.

Focus is the parts of the module the existing hook suites never reach: the
descriptor-pinned file helpers (``safe_*``), the internal-read allowlist and its
SEL-audit gate, the boot-time builtin-app registries, the script-hook store's
persistence/rollback contract, and script-hook dispatch (registration ordering,
matcher filtering, and failure isolation).

Everything here is hermetic: no real network, no real subprocess, and every
filesystem write lands under ``tmp_path``.
"""

from __future__ import annotations

import json
import os
import platform
import stat as _stat
import uuid
from pathlib import Path

import pytest

from kiro_crew import hooks as hooks_mod
from kiro_crew import security, webhooks
from kiro_crew.hooks import (
    HOOK_EVENT_POST_TOOL_USE,
    HOOK_EVENT_PRE_TOOL_USE,
    HOOK_EVENT_STOP,
    HOOK_EVENT_USER_PROMPT_SUBMIT,
    HOOK_MODIFY,
    TOOL_ALLOW,
    TOOL_AUTO_APPROVE,
    FileTooLargeError,
    HookManager,
    HooksConfig,
    ScriptHook,
    ScriptHookResult,
    ScriptHookStore,
    TransformHook,
    UserDeniedPattern,
    _app_owns_mcp_server,
    _audit_governance,
    _builtin_app_for_agent,
    _coerce_bool,
    _cu_read_only_auto_approve,
    _emit_internal_read_audit,
    _fd_real_path,
    _governance_denial,
    _governance_pinned_command_ids,
    _is_access_control_xattr,
    _is_declared_builtin_mcp_server,
    _is_first_party_app,
    _script_hooks_capability_denied,
    _should_carry_xattr,
    effective_denied_regexes_from_config,
    emit_internal_read_audit,
    fire_tool_hooks,
    get_global_hook_store,
    hooks_config_from_config_dict,
    load_denied_commands_state,
    register_internal_read_path,
    resolve_denied_notes,
    run_script_hook,
    safe_copy_file_nolink,
    safe_read_file,
    safe_read_file_bytes,
    safe_read_file_bytes_nolink,
    safe_read_file_bytes_with_identity,
    safe_read_file_internal,
    safe_read_prefix,
    safe_write_file_nolink,
    set_builtin_app_agents,
    set_builtin_app_mcp_servers,
    set_builtin_app_names,
    set_global_hook_store,
    stat_identity,
    validate_file_path,
    verified_replace_file_nolink,
)

_IS_WINDOWS = platform.system() == "Windows"


# ── helpers ──


def _write(path: Path, text: str) -> Path:
    """Write *text* verbatim.

    ``newline="\\n"`` is explicit: the default translates ``\\n`` to ``\\r\\n``
    on Windows, which breaks the byte-exact assertions below.
    """
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def _same(a: str, b: str) -> bool:
    """Compare two paths after resolving BOTH sides.

    ``tempfile`` hands back the Windows 8.3 SHORT form while ``realpath``
    returns the long one, so a raw string compare passes on POSIX and fails
    only on Windows.
    """

    def _normalized(path: str) -> str:
        return os.path.normcase(os.path.normpath(os.path.realpath(path)))

    return _normalized(a) == _normalized(b)


def _identity(path: Path) -> tuple[int, int]:
    st = os.stat(path)
    return (st.st_dev, st.st_ino)


def _staged_sibling(directory: Path, base: str) -> bool:
    """True once ``safe_write_file_nolink`` has staged its temp file for *base*.

    A deterministic marker for "the payload is written and the identity
    re-checks are next", used instead of counting ``os.stat`` calls -- the
    screening before staging varies with what the process has already cached.
    ``Path.iterdir`` uses ``scandir``, so it does not re-enter a patched
    ``os.stat``.
    """
    prefix = f".{base}.kirocrew-"
    return any(p.name.startswith(prefix) for p in directory.iterdir())


def _try_hardlink(src: Path, dst: Path) -> None:
    """Hardlink *src* to *dst*, skipping the test when the platform refuses."""
    try:
        os.link(src, dst)
    except (OSError, NotImplementedError, AttributeError) as exc:  # pragma: no cover
        pytest.skip(f"hardlinks unavailable here: {exc}")


class _StubDecision:
    def __init__(self, permitted: bool, reason: str = "") -> None:
        self.permitted = permitted
        self.reason = reason


@pytest.fixture
def restore_builtin_registries():
    """Snapshot/restore the boot-warmed module globals the gate reads."""
    saved = (
        hooks_mod._BUILTIN_APP_NAMES,
        hooks_mod._BUILTIN_APP_MCP_SERVERS,
        dict(hooks_mod._BUILTIN_APP_AGENTS),
    )
    yield
    hooks_mod._BUILTIN_APP_NAMES = saved[0]
    hooks_mod._BUILTIN_APP_MCP_SERVERS = saved[1]
    hooks_mod._BUILTIN_APP_AGENTS = saved[2]


@pytest.fixture
def restore_internal_allowlist():
    """Snapshot/restore ``_INTERNAL_READ_ALLOWLIST`` around a registration."""
    saved = dict(hooks_mod._INTERNAL_READ_ALLOWLIST)
    yield
    hooks_mod._INTERNAL_READ_ALLOWLIST.clear()
    hooks_mod._INTERNAL_READ_ALLOWLIST.update(saved)


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

    def _windows(self, monkeypatch, realpath=os.path.realpath):
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
                sep=os.sep,
                # unc_probe_allowed and main's _unc_agents_root memo key read
                # the environment through this namespace; carry the real
                # mapping so a stub miss cannot masquerade as a product bug.
                environ=os.environ,
                path=types.SimpleNamespace(
                    expanduser=os.path.expanduser,
                    abspath=os.path.abspath,
                    realpath=realpath,
                    normcase=os.path.normcase,
                    normpath=os.path.normpath,
                    isabs=os.path.isabs,
                    join=os.path.join,
                    dirname=os.path.dirname,
                ),
            ),
        )

    def test_linked_ancestor_is_refused_before_realpath(self, tmp_path, monkeypatch):
        """realpath resolves the whole ancestor chain, so it IS the outbound
        SMB probe when an ancestor junction targets a UNC share (#5962).
        Wiring realpath to explode proves the walk returned first."""
        from kiro_crew import platform_compat

        def _boom(_p):  # pragma: no cover
            raise AssertionError("realpath ran before the ancestor walk")

        self._windows(monkeypatch, realpath=_boom)
        monkeypatch.setattr(platform_compat, "first_linked_ancestor", lambda _p: str(tmp_path))
        assert validate_file_path(str(tmp_path / "doc.txt")) is None

    def test_bypassing_the_ancestor_guard_restores_validation(self, tmp_path, monkeypatch):
        """Mutation check: with the walk reporting no link, the same path
        validates again -- the refusal above is attributable to the guard."""
        from kiro_crew import platform_compat

        f = _write(tmp_path / "ok.txt", "x")
        self._windows(monkeypatch)
        monkeypatch.setattr(platform_compat, "first_linked_ancestor", lambda _p: None)
        assert _same(validate_file_path(str(f)) or "", str(f))

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
        end-to-end with a real link in test_hooks.py test_allows_benign_symlink);
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
        record (refs #6371, the #6281/#6315 log-forgery class).
        """
        forged = "/tmp/pods/wt-evil\nWARNING forged: reclaim authorized"
        # The message carries the RESOLVED path (drive-lettered and
        # backslashed on Windows), so compute the expectation the same way
        # production does.
        resolved = os.path.realpath(os.path.expanduser(forged))
        monkeypatch.setattr(hooks_mod, "is_sensitive_path", lambda p: True)
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

        f = tmp_path / "map.json"
        f.write_text("{}", encoding="utf-8")
        resolved = os.path.realpath(str(f))

        real_open = os.open

        def _eloop(path, flags, *args, **kwargs):
            if str(path) == resolved:
                raise OSError(_errno.ELOOP, "symlink swapped in")
            return real_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(os, "open", _eloop)
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
        # O_NOFOLLOW guard exists for.
        f = _write(tmp_path / "a.txt", "payload")
        import errno as _errno

        real_open = os.open

        def _eloop(path, flags, *args, **kwargs):
            if _same(str(path), str(f)):
                raise OSError(_errno.ELOOP, "symlink swapped in")
            return real_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(os, "open", _eloop)
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


# ── internal (audited) reads of sensitive paths ──


class TestRegisterInternalReadPath:
    def test_blank_read_id_refused(self, restore_internal_allowlist):
        with pytest.raises(ValueError, match="non-empty string"):
            register_internal_read_path("", ".aws/x.json")
        with pytest.raises(ValueError, match="non-empty string"):
            register_internal_read_path(None, ".aws/x.json")  # type: ignore[arg-type]

    def test_repointing_an_existing_id_refused(self, restore_internal_allowlist):
        with pytest.raises(ValueError, match="refusing to repoint"):
            register_internal_read_path("kiro_usage_api.sso_token_cli", ".aws/other.json")

    def test_same_id_same_path_is_idempotent(self, restore_internal_allowlist):
        existing = hooks_mod._INTERNAL_READ_ALLOWLIST["kiro_usage_api.sso_token_cli"]
        register_internal_read_path("kiro_usage_api.sso_token_cli", existing)
        assert hooks_mod._INTERNAL_READ_ALLOWLIST["kiro_usage_api.sso_token_cli"] == existing

    @pytest.mark.parametrize(
        "rel",
        [
            "/etc/shadow",
            "../outside.json",
            ".aws/../../escape.json",
        ],
    )
    def test_non_relative_or_traversing_paths_refused(self, rel, restore_internal_allowlist):
        with pytest.raises(ValueError, match="must be relative"):
            register_internal_read_path("edition.probe", rel)

    def test_non_sensitive_target_refused(self, restore_internal_allowlist):
        with pytest.raises(ValueError, match="non-sensitive"):
            register_internal_read_path("edition.probe", "Documents/notes.txt")

    def test_valid_sensitive_registration_lands(self, restore_internal_allowlist):
        rel = ".aws/sso/cache/edition-probe.json"
        register_internal_read_path("edition.probe", rel)
        assert hooks_mod._INTERNAL_READ_ALLOWLIST["edition.probe"] == rel


class TestSafeReadFileInternal:
    def test_unregistered_read_id_is_denied(self):
        with pytest.raises(PermissionError, match="not in allowlist"):
            safe_read_file_internal("nope.not_registered")

    def test_allowlist_entry_that_is_no_longer_sensitive_is_denied(self, monkeypatch):
        # Defense in depth: the carve-out is only valid for a path the shared
        # file gate otherwise blocks.
        monkeypatch.setitem(hooks_mod._INTERNAL_READ_ALLOWLIST, "drifted", "Documents/plain.txt")
        with pytest.raises(PermissionError, match="non-sensitive"):
            safe_read_file_internal("drifted")

    def test_missing_file_returns_none_without_reading_anything(self, monkeypatch):
        rel = f".aws/sso/cache/absent-{uuid.uuid4().hex}.json"
        monkeypatch.setitem(hooks_mod._INTERNAL_READ_ALLOWLIST, "absent", rel)
        outcomes: list[tuple[str, str]] = []
        monkeypatch.setattr(
            hooks_mod,
            "_emit_internal_read_audit",
            lambda read_id, outcome: outcomes.append((read_id, outcome)) or True,
        )
        assert safe_read_file_internal("absent") is None
        assert outcomes == [("absent", "missing")]

    def test_unreadable_open_error_returns_none(self, monkeypatch):
        rel = f".aws/sso/cache/unreadable-{uuid.uuid4().hex}.json"
        monkeypatch.setitem(hooks_mod._INTERNAL_READ_ALLOWLIST, "unreadable", rel)
        outcomes: list[str] = []
        monkeypatch.setattr(
            hooks_mod,
            "_emit_internal_read_audit",
            lambda read_id, outcome: outcomes.append(outcome) or True,
        )
        real_open = os.open

        def _eacces(path, flags, *args, **kwargs):
            if str(path).endswith(os.path.basename(rel)):
                raise PermissionError("denied")
            return real_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(os, "open", _eacces)
        assert safe_read_file_internal("unreadable") is None
        assert outcomes == ["unreadable"]

    def test_unregistered_read_emits_an_audit_before_raising(self, monkeypatch):
        seen: list[tuple[str, str]] = []
        monkeypatch.setattr(
            hooks_mod,
            "_emit_internal_read_audit",
            lambda read_id, outcome: seen.append((read_id, outcome)) or True,
        )
        with pytest.raises(PermissionError):
            safe_read_file_internal("also.not_registered")
        assert seen == [("also.not_registered", "not_allowlisted")]


class TestInternalReadAudit:
    def test_success_is_reported_when_sel_records_it(self, monkeypatch):
        calls: list[dict] = []

        class _Sel:
            def log_tool_invocation(self, **kwargs):
                calls.append(kwargs)

        import kiro_crew.sel as sel_mod

        monkeypatch.setattr(sel_mod, "sel", lambda: _Sel())
        assert _emit_internal_read_audit("some.read", "success") is True
        assert calls[0]["outcome"] == "success"
        # A success gates the return of live credential bytes, so it must be
        # written synchronously.
        assert calls[0]["critical"] is True

    def test_non_success_outcome_is_not_critical(self, monkeypatch):
        calls: list[dict] = []

        class _Sel:
            def log_tool_invocation(self, **kwargs):
                calls.append(kwargs)

        import kiro_crew.sel as sel_mod

        monkeypatch.setattr(sel_mod, "sel", lambda: _Sel())
        assert _emit_internal_read_audit("some.read", "missing") is True
        assert calls[0]["critical"] is False

    def test_a_raising_sel_reports_failure(self, monkeypatch):
        class _Sel:
            def log_tool_invocation(self, **kwargs):
                raise RuntimeError("sel down")

        import kiro_crew.sel as sel_mod

        monkeypatch.setattr(sel_mod, "sel", lambda: _Sel())
        assert _emit_internal_read_audit("some.read", "success") is False

    def test_audit_only_wrapper_enforces_its_own_allowlist(self, monkeypatch):
        calls: list[str] = []

        class _Sel:
            def log_tool_invocation(self, **kwargs):
                calls.append(kwargs["outcome"])

        import kiro_crew.sel as sel_mod

        monkeypatch.setattr(sel_mod, "sel", lambda: _Sel())
        assert emit_internal_read_audit("not.registered", "success") is False
        assert calls == []
        registered = next(iter(hooks_mod._AUDIT_ONLY_READ_IDS))
        assert emit_internal_read_audit(registered, "success") is True
        assert calls == ["success"]


# ── boot-warmed builtin-app registries ──


class TestBuiltinAppRegistries:
    def test_mcp_server_set_is_casefolded_and_junk_dropped(self, restore_builtin_registries):
        set_builtin_app_mcp_servers(["Meetings:Srv", "", None, 5, "papyrus:tools"])
        assert _is_declared_builtin_mcp_server("meetings:srv") is True
        assert _is_declared_builtin_mcp_server("MEETINGS:SRV") is True
        assert _is_declared_builtin_mcp_server("papyrus:tools") is True
        assert _is_declared_builtin_mcp_server("meetings:evil") is False
        assert _is_declared_builtin_mcp_server("") is False

    def test_unwarmed_mcp_set_fails_closed(self, restore_builtin_registries):
        set_builtin_app_mcp_servers([])
        assert _is_declared_builtin_mcp_server("meetings:srv") is False

    def test_first_party_lookup_is_casefolded(self, restore_builtin_registries):
        set_builtin_app_names(["Meetings", "", 7])
        assert _is_first_party_app("meetings") is True
        assert _is_first_party_app("MEETINGS") is True
        assert _is_first_party_app("third-party") is False
        assert _is_first_party_app("") is False

    def test_agent_map_is_casefolded_and_junk_dropped(self, restore_builtin_registries):
        set_builtin_app_agents({"Mochi": "mochi", "": "x", "a": "", 3: "y"})
        assert _builtin_app_for_agent("mochi") == "mochi"
        assert _builtin_app_for_agent("MOCHI") == "mochi"
        assert _builtin_app_for_agent("unknown") == ""
        assert _builtin_app_for_agent("") == ""

    def test_agent_map_replacement_is_idempotent(self, restore_builtin_registries):
        set_builtin_app_agents({"a": "app-a"})
        set_builtin_app_agents({"b": "app-b"})
        assert _builtin_app_for_agent("a") == ""
        assert _builtin_app_for_agent("b") == "app-b"

    @pytest.mark.parametrize(
        ("server", "app", "expected"),
        [
            ("meetings:srv", "meetings", True),
            ("Meetings:srv", "MEETINGS", True),
            ("meetings:srv", "papyrus", False),
            ("kirocrew-cron", "meetings", False),
            ("", "meetings", False),
            ("meetings:srv", "", False),
        ],
    )
    def test_app_owns_mcp_server(self, server, app, expected):
        assert _app_owns_mcp_server(server, app) is expected


class TestComputerUseReadOnlyAutoApprove:
    def test_a_non_computer_use_title_never_auto_approves(self):
        assert _cu_read_only_auto_approve("execute_bash") is False
        assert _cu_read_only_auto_approve("") is False

    def test_enable_state_probe_failure_fails_closed(self, monkeypatch):
        monkeypatch.setattr(hooks_mod, "computer_use_action_from_title", lambda name: "get_state")
        monkeypatch.setattr(
            hooks_mod, "computer_use_action_classes", lambda action: (hooks_mod.CU_CLASS_OBSERVE,)
        )
        import kiro_crew.computer_use as cu

        class _Boom:
            @staticmethod
            def is_enabled():
                raise RuntimeError("keystone unreadable")

        monkeypatch.setattr(cu, "enable_state", _Boom, raising=False)
        assert _cu_read_only_auto_approve("mcp__kirocrew-computer__computer_get_state") is False

    def test_a_mutating_action_is_not_read_only(self, monkeypatch):
        monkeypatch.setattr(hooks_mod, "computer_use_action_from_title", lambda name: "click")
        monkeypatch.setattr(hooks_mod, "computer_use_action_classes", lambda action: ("mutate",))
        assert _cu_read_only_auto_approve("mcp__kirocrew-computer__computer_click") is False


# ── script hook dataclasses ──


class TestScriptHookDataclasses:
    def test_legacy_pattern_field_maps_to_matcher(self):
        hook = ScriptHook.from_dict({"pattern": "fs_*"})
        assert hook.matcher == "fs_*"

    def test_matcher_wins_over_legacy_pattern(self):
        hook = ScriptHook.from_dict({"pattern": "old", "matcher": "new"})
        assert hook.matcher == "new"

    def test_defaults_are_filled(self):
        hook = ScriptHook.from_dict({})
        assert hook.id and hook.event == HOOK_EVENT_USER_PROMPT_SUBMIT
        assert hook.timeout == 30 and hook.enabled is True

    def test_result_classification(self):
        blocked = ScriptHookResult(hook_id="a", hook_name="a", event="x", exit_code=2)
        ok = ScriptHookResult(hook_id="a", hook_name="a", event="x", exit_code=0)
        failed = ScriptHookResult(hook_id="a", hook_name="a", event="x", exit_code=1)
        assert blocked.blocked is True and blocked.succeeded is False
        assert ok.succeeded is True and ok.blocked is False
        assert failed.succeeded is False and failed.blocked is False


# ── script hook store persistence ──


class TestScriptHookStorePersistence:
    def test_foreign_top_level_keys_survive_a_mutation(self, tmp_path):
        path = tmp_path / "hooks.json"
        _write(path, json.dumps({"webhook-ctx-1": {"note": "resume me"}, "hooks": []}))
        store = ScriptHookStore(tmp_path)
        store.create({"name": "h1", "command": "true"})
        on_disk = json.loads(path.read_text(encoding="utf-8"))
        assert on_disk["webhook-ctx-1"] == {"note": "resume me"}
        assert len(on_disk["hooks"]) == 1

    def test_corrupt_file_is_not_overwritten(self, tmp_path):
        path = _write(tmp_path / "hooks.json", "{not json")
        store = ScriptHookStore(tmp_path)  # _load logs and continues
        assert store.list_all() == []
        with pytest.raises(webhooks.WebhookStoreUnreadable):
            store.create({"name": "h1", "command": "true"})
        # The unreadable file is left for an operator to repair.
        assert path.read_text(encoding="utf-8") == "{not json"

    def test_a_failed_persist_rolls_back_the_in_memory_set(self, tmp_path):
        store = ScriptHookStore(tmp_path)
        hook = store.create({"name": "h1", "command": "true"})
        _write(tmp_path / "hooks.json", "{not json")
        with pytest.raises(webhooks.WebhookStoreUnreadable):
            store.delete(hook.id)
        # The delete did not reach disk, so it must not be visible in memory.
        assert store.get(hook.id) is not None

    def test_toggle_rollback_restores_the_stored_object(self, tmp_path):
        store = ScriptHookStore(tmp_path)
        hook = store.create({"name": "h1", "command": "true", "enabled": True})
        _write(tmp_path / "hooks.json", "{not json")
        with pytest.raises(webhooks.WebhookStoreUnreadable):
            store.toggle(hook.id)
        # A shallow dict copy would share the ScriptHook and restore nothing.
        assert store.get(hook.id).enabled is True

    def test_load_reads_hooks_back(self, tmp_path):
        first = ScriptHookStore(tmp_path)
        first.create({"id": "keepme", "name": "h1", "command": "true"})
        second = ScriptHookStore(tmp_path)
        assert [h.id for h in second.list_all()] == ["keepme"]

    def test_update_rejects_an_unknown_event(self, tmp_path):
        store = ScriptHookStore(tmp_path)
        hook = store.create({"name": "h1", "command": "true"})
        with pytest.raises(ValueError, match="invalid event"):
            store.update(hook.id, {"event": "NotAnEvent"})

    @pytest.mark.parametrize("bad", [0, 301, -5, "30", 3.5, None])
    def test_update_rejects_an_out_of_range_timeout(self, tmp_path, bad):
        store = ScriptHookStore(tmp_path)
        hook = store.create({"name": "h1", "command": "true"})
        with pytest.raises(ValueError, match="timeout must be"):
            store.update(hook.id, {"timeout": bad})

    def test_update_accepts_the_range_bounds(self, tmp_path):
        store = ScriptHookStore(tmp_path)
        hook = store.create({"name": "h1", "command": "true"})
        assert store.update(hook.id, {"timeout": 1}).timeout == 1
        assert store.update(hook.id, {"timeout": 300}).timeout == 300

    def test_a_bool_timeout_is_rejected(self, tmp_path):
        # The deliberate tightening the old test anticipated (issue #5444):
        # ``bool`` is an ``int`` subclass, but ``True`` as a timeout is
        # meaningless, so the shared validator now rejects it at the update
        # boundary rather than silently landing a 1-second timeout.
        store = ScriptHookStore(tmp_path)
        hook = store.create({"name": "h1", "command": "true"})
        with pytest.raises(ValueError, match="timeout must be an integer"):
            store.update(hook.id, {"timeout": True})

    def test_update_applies_only_known_fields(self, tmp_path):
        store = ScriptHookStore(tmp_path)
        hook = store.create({"name": "h1", "command": "true"})
        updated = store.update(
            hook.id, {"name": "h2", "timeout": 12, "run_count": 999, "unknown": "x"}
        )
        assert updated is not None
        assert updated.name == "h2" and updated.timeout == 12
        assert updated.run_count == 0
        assert not hasattr(updated, "unknown")

    def test_mutations_on_a_missing_hook_are_no_ops(self, tmp_path):
        store = ScriptHookStore(tmp_path)
        assert store.update("nope", {"name": "x"}) is None
        assert store.delete("nope") is False
        assert store.toggle("nope") is None

    def test_toggle_flips_and_persists(self, tmp_path):
        store = ScriptHookStore(tmp_path)
        hook = store.create({"name": "h1", "command": "true", "enabled": True})
        assert store.toggle(hook.id).enabled is False
        assert ScriptHookStore(tmp_path).get(hook.id).enabled is False

    def test_delete_removes_from_disk(self, tmp_path):
        store = ScriptHookStore(tmp_path)
        hook = store.create({"name": "h1", "command": "true"})
        assert store.delete(hook.id) is True
        assert ScriptHookStore(tmp_path).list_all() == []

    def test_status_bookkeeping_never_raises_out_of_persist(self, tmp_path, caplog):
        store = ScriptHookStore(tmp_path)
        store.create({"name": "h1", "command": "true"})
        _write(tmp_path / "hooks.json", "{not json")
        # fire() is awaited from the PreToolUse path, so a corrupt file must not
        # turn every tool call into a rejection.
        store._persist_current()
        assert any("bookkeeping" in r.message for r in caplog.records)

    def test_save_snapshot_writes_the_given_list(self, tmp_path):
        store = ScriptHookStore(tmp_path)
        store._save_snapshot([{"id": "snap", "name": "s", "command": "true"}])
        on_disk = json.loads((tmp_path / "hooks.json").read_text(encoding="utf-8"))
        assert [h["id"] for h in on_disk["hooks"]] == ["snap"]


class TestGlobalHookStore:
    def test_set_and_get(self, tmp_path, monkeypatch):
        monkeypatch.setattr(hooks_mod, "_global_script_hook_store", None)
        assert get_global_hook_store() is None
        store = ScriptHookStore(tmp_path)
        set_global_hook_store(store)
        assert get_global_hook_store() is store


# ── script hook governance + dispatch ──


class TestScriptHookGovernance:
    def test_no_opinion_when_governance_permits(self, monkeypatch):
        import kiro_crew.platform.governance_profiles as gp

        monkeypatch.setattr(
            gp, "governance_permits", lambda *a, **k: _StubDecision(True), raising=False
        )
        assert _script_hooks_capability_denied("slot:1") is None

    def test_denial_reason_is_returned(self, monkeypatch):
        import kiro_crew.platform.governance_profiles as gp

        monkeypatch.setattr(
            gp,
            "governance_permits",
            lambda *a, **k: _StubDecision(False, "script hooks off"),
            raising=False,
        )
        assert _script_hooks_capability_denied() == "script hooks off"

    def test_a_transient_governance_error_degrades_to_no_opinion(self, monkeypatch):
        import kiro_crew.platform.governance_profiles as gp

        def _boom(*a, **k):
            raise RuntimeError("profile store glitch")

        monkeypatch.setattr(gp, "governance_permits", _boom, raising=False)
        monkeypatch.setattr(gp, "audit_governance_degraded", _boom, raising=False)
        # A glitch must not wedge every script hook.
        assert _script_hooks_capability_denied() is None

    def test_composition_error_fails_closed(self, monkeypatch):
        import kiro_crew.platform.governance_profiles as gp
        from kiro_crew.platform.context import PlatformCompositionError

        def _boom(*a, **k):
            raise PlatformCompositionError("cannot compose")

        monkeypatch.setattr(gp, "governance_permits", _boom, raising=False)
        with pytest.raises(PlatformCompositionError):
            _script_hooks_capability_denied()

    @pytest.mark.asyncio
    async def test_a_denied_hook_never_spawns_a_subprocess(self, monkeypatch):
        monkeypatch.setattr(
            hooks_mod, "_script_hooks_capability_denied", lambda sk: "capability disabled"
        )

        def _no_spawn(*a, **k):  # pragma: no cover - must not be reached
            raise AssertionError("run_script_hook spawned a subprocess despite the deny")

        monkeypatch.setattr(hooks_mod.asyncio, "create_subprocess_shell", _no_spawn)
        hook = ScriptHook(id="h1", name="blocked-hook", command="echo hi")
        result = await run_script_hook(hook, "ctx", {"parent_session_key": "slot:1"})
        assert result.exit_code == 2  # PreToolUse "block tool" convention
        assert "capability disabled" in result.error
        assert hook.last_status == "blocked"
        assert hook.run_count == 1

    @pytest.mark.asyncio
    async def test_the_deny_audit_never_breaks_the_caller(self, monkeypatch):
        monkeypatch.setattr(hooks_mod, "_script_hooks_capability_denied", lambda sk: "nope")
        import kiro_crew.sel as sel_mod

        class _Sel:
            def log_governance_decision(self, **kwargs):
                raise RuntimeError("sel down")

        monkeypatch.setattr(sel_mod, "sel", lambda: _Sel())
        result = await run_script_hook(ScriptHook(id="h1", command="echo hi"))
        assert result.exit_code == 2

    @pytest.mark.asyncio
    async def test_session_key_is_taken_from_the_event(self, monkeypatch):
        seen: list[str] = []
        monkeypatch.setattr(
            hooks_mod,
            "_script_hooks_capability_denied",
            lambda sk: seen.append(sk) or "denied",
        )
        await run_script_hook(ScriptHook(id="h1", command="x"), "", {"session_key": "slot:9"})
        await run_script_hook(
            ScriptHook(id="h2", command="x"), "", {"parent_session_key": "slot:7"}
        )
        await run_script_hook(ScriptHook(id="h3", command="x"))
        assert seen == ["slot:9", "slot:7", ""]


class TestFireDispatch:
    """Registration, ordering, matcher filtering, and failure isolation.

    ``run_script_hook`` is replaced so no real subprocess is spawned; the
    substitute records what it was handed, which is what these assertions are
    about.
    """

    @pytest.fixture
    def recorder(self, monkeypatch):
        calls: list[tuple[ScriptHook, str, dict]] = []

        async def _fake_run(hook, context="", hook_event=None):
            calls.append((hook, context, dict(hook_event or {})))
            # One registered hook failing must not stop the ones after it.
            exit_code = 1 if hook.name == "boom" else 0
            return ScriptHookResult(
                hook_id=hook.id,
                hook_name=hook.name,
                event=hook.event,
                exit_code=exit_code,
                stderr="failed" if exit_code else "",
            )

        monkeypatch.setattr(hooks_mod, "run_script_hook", _fake_run)
        return calls

    @pytest.mark.asyncio
    async def test_hooks_fire_in_registration_order(self, tmp_path, recorder):
        store = ScriptHookStore(tmp_path)
        for name in ("first", "second", "third"):
            store.create({"name": name, "command": "true", "event": HOOK_EVENT_STOP})
        results = await store.fire(HOOK_EVENT_STOP, context="done")
        assert [h.name for h, _c, _e in recorder] == ["first", "second", "third"]
        assert [r.hook_name for r in results] == ["first", "second", "third"]

    @pytest.mark.asyncio
    async def test_one_failing_hook_does_not_stop_the_rest(self, tmp_path, recorder):
        store = ScriptHookStore(tmp_path)
        for name in ("before", "boom", "after"):
            store.create({"name": name, "command": "true", "event": HOOK_EVENT_STOP})
        results = await store.fire(HOOK_EVENT_STOP, context="x")
        assert [r.hook_name for r in results] == ["before", "boom", "after"]
        assert [r.exit_code for r in results] == [0, 1, 0]

    @pytest.mark.asyncio
    async def test_disabled_and_other_event_hooks_are_skipped(self, tmp_path, recorder):
        store = ScriptHookStore(tmp_path)
        store.create({"name": "on", "command": "true", "event": HOOK_EVENT_STOP})
        store.create({"name": "off", "command": "true", "event": HOOK_EVENT_STOP, "enabled": False})
        store.create({"name": "other", "command": "true", "event": HOOK_EVENT_PRE_TOOL_USE})
        await store.fire(HOOK_EVENT_STOP, context="x")
        assert [h.name for h, _c, _e in recorder] == ["on"]

    @pytest.mark.asyncio
    async def test_tool_matcher_filters_by_tool_name(self, tmp_path, recorder):
        store = ScriptHookStore(tmp_path)
        store.create(
            {
                "name": "fs-only",
                "command": "true",
                "event": HOOK_EVENT_PRE_TOOL_USE,
                "matcher": "fs_*",
            }
        )
        store.create(
            {
                "name": "all-tools",
                "command": "true",
                "event": HOOK_EVENT_PRE_TOOL_USE,
                "matcher": "",
            }
        )
        await store.fire(HOOK_EVENT_PRE_TOOL_USE, tool_name="fs_write")
        assert sorted(h.name for h, _c, _e in recorder) == ["all-tools", "fs-only"]
        recorder.clear()
        await store.fire(HOOK_EVENT_PRE_TOOL_USE, tool_name="execute_bash")
        assert [h.name for h, _c, _e in recorder] == ["all-tools"]

    @pytest.mark.asyncio
    async def test_post_tool_use_uses_the_tool_matcher_too(self, tmp_path, recorder):
        store = ScriptHookStore(tmp_path)
        store.create(
            {
                "name": "post",
                "command": "true",
                "event": HOOK_EVENT_POST_TOOL_USE,
                "matcher": "fs_*",
            }
        )
        await store.fire(HOOK_EVENT_POST_TOOL_USE, tool_name="other")
        assert recorder == []
        await store.fire(HOOK_EVENT_POST_TOOL_USE, tool_name="fs_read", tool_response={"ok": True})
        assert [h.name for h, _c, _e in recorder] == ["post"]
        assert recorder[0][2]["tool_response"] == {"ok": True}

    @pytest.mark.asyncio
    async def test_non_tool_matcher_globs_the_context(self, tmp_path, recorder):
        store = ScriptHookStore(tmp_path)
        store.create(
            {
                "name": "prompt",
                "command": "true",
                "event": HOOK_EVENT_USER_PROMPT_SUBMIT,
                "matcher": "*deploy*",
            }
        )
        await store.fire(HOOK_EVENT_USER_PROMPT_SUBMIT, context="please DEPLOY now")
        assert [h.name for h, _c, _e in recorder] == ["prompt"]
        recorder.clear()
        await store.fire(HOOK_EVENT_USER_PROMPT_SUBMIT, context="nothing relevant")
        assert recorder == []

    @pytest.mark.asyncio
    async def test_prompt_event_carries_the_prompt(self, tmp_path, recorder):
        store = ScriptHookStore(tmp_path)
        store.create({"name": "p", "command": "true", "event": HOOK_EVENT_USER_PROMPT_SUBMIT})
        await store.fire(HOOK_EVENT_USER_PROMPT_SUBMIT, context="hi there")
        event = recorder[0][2]
        assert event["prompt"] == "hi there"
        assert event["hook_event_name"] == HOOK_EVENT_USER_PROMPT_SUBMIT
        assert "cwd" in event

    @pytest.mark.asyncio
    async def test_stop_event_always_carries_assistant_text(self, tmp_path, recorder):
        store = ScriptHookStore(tmp_path)
        store.create({"name": "s", "command": "true", "event": HOOK_EVENT_STOP})
        await store.fire(HOOK_EVENT_STOP, context="")
        # Unconditional, so a hook that always reads it never KeyErrors.
        assert recorder[0][2]["assistant_text"] == ""

    @pytest.mark.asyncio
    async def test_attribution_fields_are_forwarded(self, tmp_path, recorder):
        store = ScriptHookStore(tmp_path)
        store.create({"name": "t", "command": "true", "event": HOOK_EVENT_PRE_TOOL_USE})
        await store.fire(
            HOOK_EVENT_PRE_TOOL_USE,
            tool_name="fs_read",
            tool_input={"path": "/tmp/x"},
            subagent_id="sub-1",
            parent_session_key="slot:3",
            agent_role="reviewer",
        )
        event = recorder[0][2]
        assert event["tool_name"] == "fs_read"
        assert event["tool_input"] == {"path": "/tmp/x"}
        assert event["subagent_id"] == "sub-1"
        assert event["parent_session_key"] == "slot:3"
        assert event["agent_role"] == "reviewer"

    @pytest.mark.asyncio
    async def test_absent_attribution_fields_are_omitted(self, tmp_path, recorder):
        store = ScriptHookStore(tmp_path)
        store.create({"name": "t", "command": "true", "event": HOOK_EVENT_PRE_TOOL_USE})
        await store.fire(HOOK_EVENT_PRE_TOOL_USE)
        event = recorder[0][2]
        for key in ("tool_name", "tool_input", "subagent_id", "parent_session_key", "agent_role"):
            assert key not in event

    @pytest.mark.asyncio
    async def test_fire_persists_status_bookkeeping(self, tmp_path, recorder):
        store = ScriptHookStore(tmp_path)
        store.create({"id": "h1", "name": "s", "command": "true", "event": HOOK_EVENT_STOP})
        await store.fire(HOOK_EVENT_STOP, context="x")
        assert (tmp_path / "hooks.json").exists()


class TestFireToolHooks:
    @pytest.mark.asyncio
    async def test_a_missing_store_is_a_no_op(self):
        assert await fire_tool_hooks(None, "Running: ls") is None

    @pytest.mark.asyncio
    async def test_running_prefix_is_stripped_and_input_parsed(self, tmp_path, monkeypatch):
        seen: list[dict] = []

        class _Store:
            async def fire(self, event, **kwargs):
                seen.append({"event": event, **kwargs})
                return []

        await fire_tool_hooks(
            _Store(),  # type: ignore[arg-type]
            "Running: execute_bash",
            '{"command": "ls"}',
            subagent_id="sub-1",
            parent_session_key="slot:2",
            agent_role="worker",
        )
        assert seen[0]["event"] == HOOK_EVENT_PRE_TOOL_USE
        assert seen[0]["tool_name"] == "execute_bash"
        assert seen[0]["tool_input"] == {"command": "ls"}
        assert seen[0]["subagent_id"] == "sub-1"
        assert seen[0]["parent_session_key"] == "slot:2"
        assert seen[0]["agent_role"] == "worker"

    @pytest.mark.asyncio
    async def test_unparseable_tool_input_degrades_to_none(self):
        seen: list[dict] = []

        class _Store:
            async def fire(self, event, **kwargs):
                seen.append(kwargs)
                return []

        await fire_tool_hooks(_Store(), "", "{not json")  # type: ignore[arg-type]
        assert seen[0]["tool_input"] is None
        assert seen[0]["tool_name"] == ""

    @pytest.mark.asyncio
    async def test_a_raising_store_is_swallowed(self):
        class _Store:
            async def fire(self, event, **kwargs):
                raise RuntimeError("store on fire")

        # Informational hooks must never break the tool-call notification path.
        assert await fire_tool_hooks(_Store(), "Running: ls") is None  # type: ignore[arg-type]


# ── governance helper fail-soft / fail-closed discipline ──


class TestGovernancePinResolution:
    def test_a_glitch_degrades_to_no_pins(self, monkeypatch):
        def _boom():
            raise RuntimeError("policy store glitch")

        monkeypatch.setattr(security, "pinned_builtin_command_ids", _boom)
        # An empty set, not an exception: a glitch here must not wedge the gate.
        assert _governance_pinned_command_ids(None) == set()

    def test_composition_error_propagates(self, monkeypatch):
        from kiro_crew.platform.context import PlatformCompositionError

        def _boom():
            raise PlatformCompositionError("cannot compose")

        monkeypatch.setattr(security, "pinned_builtin_command_ids", _boom)
        with pytest.raises(PlatformCompositionError):
            _governance_pinned_command_ids(None)


class TestGovernanceDenial:
    def test_composition_error_propagates(self, monkeypatch):
        import kiro_crew.platform.governance_profiles as gp
        from kiro_crew.platform.context import PlatformCompositionError

        def _boom(*a, **k):
            raise PlatformCompositionError("cannot compose")

        monkeypatch.setattr(gp, "resolve_active_scope", _boom, raising=False)
        with pytest.raises(PlatformCompositionError):
            _governance_denial(object(), "some_tool", "", "", "")

    def test_a_glitch_degrades_to_no_opinion(self, monkeypatch):
        import kiro_crew.platform.governance_profiles as gp

        def _boom(*a, **k):
            raise RuntimeError("profile store glitch")

        monkeypatch.setattr(gp, "resolve_active_scope", _boom, raising=False)
        monkeypatch.setattr(gp, "audit_governance_degraded", _boom, raising=False)
        assert _governance_denial(object(), "some_tool", "", "", "") is None

    def test_ungoverned_host_is_a_fast_no_op(self, monkeypatch):
        import kiro_crew.platform.governance_profiles as gp

        monkeypatch.setattr(gp, "resolve_active_scope", lambda *a, **k: None, raising=False)

        class _Ctx:
            governance = None

        assert _governance_denial(_Ctx(), "some_tool", "", "", "") is None


class TestGovernanceAudit:
    def test_a_raising_sel_never_breaks_the_gate(self, monkeypatch):
        import kiro_crew.sel as sel_mod

        class _Sel:
            def log_governance_decision(self, **kwargs):
                raise RuntimeError("sel down")

        monkeypatch.setattr(sel_mod, "sel", lambda: _Sel())
        # Returns None rather than propagating -- the audit is best effort.
        assert _audit_governance("slot:1", "kirocrew", "some_tool", object()) is None


# ── extra branches in the file helpers ──


class TestSafeReadFileSymlinkRace:
    def test_eloop_after_canonicalization_is_refused(self, tmp_path, monkeypatch):
        import errno as _errno

        f = _write(tmp_path / "a.txt", "x")
        real_open = os.open

        def _eloop(path, flags, *args, **kwargs):
            if isinstance(path, str) and _same(path, str(f)):
                raise OSError(_errno.ELOOP, "swapped for a symlink")
            return real_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(os, "open", _eloop)
        with pytest.raises(PermissionError, match="refusing to follow symlink"):
            safe_read_file(str(f))


class TestFdPathSensitivityChecks:
    """The opened inode's real path is re-screened, not just the input name."""

    @staticmethod
    def _sensitive_on_second_call(monkeypatch):
        calls = {"n": 0}

        def _probe(path, *args, **kwargs):
            calls["n"] += 1
            # First call is validate_file_path's screening of the input name;
            # the next is the check against the OPENED descriptor's real path.
            return calls["n"] >= 2

        monkeypatch.setattr(hooks_mod, "is_sensitive_path", _probe)
        return calls

    def test_read_nolink_refuses_a_sensitive_opened_inode(self, tmp_path, monkeypatch):
        root = tmp_path / "root"
        root.mkdir()
        f = _write(root / "a.txt", "x")
        self._sensitive_on_second_call(monkeypatch)
        assert safe_read_file_bytes_nolink(str(f), within_root=str(root)) is None

    def test_copy_refuses_a_sensitive_opened_inode(self, tmp_path, monkeypatch):
        src = _write(tmp_path / "src.bin", "x")
        dest = tmp_path / "dest"
        dest.mkdir()
        self._sensitive_on_second_call(monkeypatch)
        assert safe_copy_file_nolink(str(src), str(dest)) is None
        assert list(dest.iterdir()) == []

    def test_write_refuses_a_sensitive_opened_inode(self, tmp_path, monkeypatch):
        root = tmp_path / "root"
        root.mkdir()
        f = _write(root / "a.txt", "old")
        self._sensitive_on_second_call(monkeypatch)
        assert safe_write_file_nolink(str(f), "new", within_root=str(root)) is False
        assert f.read_text(encoding="utf-8") == "old"


class TestSafeCopyFileNolinkStagingFailure:
    def test_a_failed_stage_write_cleans_up_and_returns_none(self, tmp_path, monkeypatch):
        import tempfile as _tempfile

        src = _write(tmp_path / "src.bin", "payload")
        dest = tmp_path / "dest"
        dest.mkdir()
        real_mkstemp = _tempfile.mkstemp
        staged: list[str] = []

        def _stale_fd(*args, **kwargs):
            fd, path = real_mkstemp(*args, **kwargs)
            staged.append(path)
            # A closed descriptor makes the very next os.write raise EBADF,
            # which is the OSError the cleanup branch exists for.
            os.close(fd)
            return fd, path

        monkeypatch.setattr(_tempfile, "mkstemp", _stale_fd)
        assert safe_copy_file_nolink(str(src), str(dest)) is None
        assert staged, "the staging file was never created"
        # The partial copy must not be left behind for a downstream reader.
        assert list(dest.iterdir()) == []


class TestSafeWriteFileNolinkXattrs:
    """Access controls must survive the atomic replace, or the write refuses."""

    def _require_xattrs(self):
        if not all(hasattr(os, a) for a in ("listxattr", "getxattr", "setxattr")):
            pytest.skip("this platform has no extended-attribute API")

    def test_an_unreadable_xattr_set_refuses_the_write(self, tmp_path, monkeypatch):
        self._require_xattrs()
        f = _write(tmp_path / "a.txt", "old")

        def _eperm(fd):
            raise OSError(1, "operation not permitted")

        monkeypatch.setattr(os, "listxattr", _eperm)
        # Not knowing what would be dropped is a refusal, not a best effort.
        assert safe_write_file_nolink(str(f), "new") is False
        assert f.read_text(encoding="utf-8") == "old"
        assert sorted(p.name for p in tmp_path.iterdir()) == ["a.txt"]

    def test_a_filesystem_without_xattrs_is_not_an_error(self, tmp_path, monkeypatch):
        self._require_xattrs()
        import errno as _errno

        f = _write(tmp_path / "a.txt", "old")

        def _unsupported(fd):
            raise OSError(_errno.ENOTSUP, "not supported")

        monkeypatch.setattr(os, "listxattr", _unsupported)
        # Nothing on the source to lose, so the write proceeds.
        assert safe_write_file_nolink(str(f), "new") is True
        assert f.read_text(encoding="utf-8") == "new"

    def test_an_uncopyable_access_control_attr_refuses_the_write(self, tmp_path, monkeypatch):
        self._require_xattrs()
        f = _write(tmp_path / "a.txt", "old")
        monkeypatch.setattr(os, "listxattr", lambda fd: ["system.posix_acl_access"])
        monkeypatch.setattr(os, "getxattr", lambda fd, attr: b"acl")

        def _refuse(fd, attr, value):
            raise OSError(1, "cannot set")

        monkeypatch.setattr(os, "setxattr", _refuse)
        assert safe_write_file_nolink(str(f), "new") is False
        assert f.read_text(encoding="utf-8") == "old"

    def test_a_privileged_attr_is_not_carried_and_does_not_refuse(self, tmp_path, monkeypatch):
        """File capabilities and integrity signatures must not reach the copy.

        ``safe_write_file_nolink`` also installs a fresh inode holding
        caller-supplied content, so it shares ``atomic_write``'s allowlist:
        replaying ``security.capability`` there would attach the old file's
        privileges to the new bytes, and ``security.ima``/``security.evm`` are
        signatures over bytes that no longer exist. The ACL beside them is still
        carried, so this is a filter and not a blanket stop.
        """
        self._require_xattrs()
        f = _write(tmp_path / "a.txt", "old")
        present = [
            "security.capability",
            "security.ima",
            "security.evm",
            "security.selinux",
            "system.posix_acl_access",
        ]
        monkeypatch.setattr(os, "listxattr", lambda fd: list(present))
        monkeypatch.setattr(os, "getxattr", lambda fd, attr: attr.encode())
        written: list[str] = []
        monkeypatch.setattr(os, "setxattr", lambda fd, attr, value: written.append(attr))

        assert safe_write_file_nolink(str(f), "new") is True
        assert f.read_text(encoding="utf-8") == "new"
        assert written == ["system.posix_acl_access"]

    def test_an_uncopyable_informational_attr_is_best_effort(self, tmp_path, monkeypatch):
        self._require_xattrs()
        f = _write(tmp_path / "a.txt", "old")
        monkeypatch.setattr(os, "listxattr", lambda fd: ["user.comment"])
        monkeypatch.setattr(os, "getxattr", lambda fd, attr: b"tag")

        def _refuse(fd, attr, value):
            raise OSError(1, "cannot set")

        monkeypatch.setattr(os, "setxattr", _refuse)
        # Losing a tag must not fail every save on a filesystem that cannot
        # store one.
        assert safe_write_file_nolink(str(f), "new") is True
        assert f.read_text(encoding="utf-8") == "new"


class TestSafeWriteFileNolinkWithoutDirFd:
    """The by-name replace branch taken where the POSIX dir-fd APIs are absent.

    Windows has no ``O_DIRECTORY`` / ``dir_fd`` support, so this is the branch it
    always uses. Clearing ``os.supports_dir_fd`` exercises the same code here.
    """

    @pytest.fixture(autouse=True)
    def _no_dir_fd(self, monkeypatch):
        monkeypatch.setattr(os, "supports_dir_fd", set())

    def test_the_staged_payload_is_still_renamed_over_the_target(self, tmp_path):
        f = _write(tmp_path / "a.txt", "old")
        assert safe_write_file_nolink(str(f), "new") is True
        assert f.read_text(encoding="utf-8") == "new"
        assert sorted(p.name for p in tmp_path.iterdir()) == ["a.txt"]

    def test_within_root_still_refuses_an_escaping_parent(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        outside = _write(tmp_path / "outside.txt", "old")
        assert safe_write_file_nolink(str(outside), "new", within_root=str(root)) is False
        assert outside.read_text(encoding="utf-8") == "old"

    def test_within_root_accepts_a_contained_parent(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        f = _write(root / "a.txt", "old")
        assert safe_write_file_nolink(str(f), "new", within_root=str(root)) is True
        assert f.read_text(encoding="utf-8") == "new"

    def test_a_vanished_target_refuses_before_the_rename(self, tmp_path, monkeypatch):
        f = _write(tmp_path / "a.txt", "old")
        real_stat = os.stat
        state = {"fired": False}

        def _vanish_on_the_recheck(path, *args, **kwargs):
            if (
                isinstance(path, str)
                and os.path.basename(path) == f.name
                and _staged_sibling(tmp_path, f.name)
            ):
                state["fired"] = True
                raise FileNotFoundError("target moved")
            return real_stat(path, *args, **kwargs)

        monkeypatch.setattr(os, "stat", _vanish_on_the_recheck)
        try:
            assert safe_write_file_nolink(str(f), "new") is False
        finally:
            monkeypatch.undo()
        assert state["fired"] is True
        assert f.read_text(encoding="utf-8") == "old"
        # The staged sibling is unlinked by name on the cleanup path.
        assert sorted(p.name for p in tmp_path.iterdir()) == ["a.txt"]


class TestSafeWriteFileNolinkDirFdChecks:
    def test_an_unverifiable_parent_fails_closed(self, tmp_path, monkeypatch):
        if not (
            getattr(os, "O_DIRECTORY", 0)
            and os.open in getattr(os, "supports_dir_fd", set())
            and os.rename in getattr(os, "supports_dir_fd", set())
        ):
            pytest.skip("this platform has no directory-fd pinning")
        root = tmp_path / "root"
        root.mkdir()
        f = _write(root / "a.txt", "old")
        real_fd_path = hooks_mod._fd_real_path

        def _files_only(fd):
            got = real_fd_path(fd)
            # The file's own check must still pass; only the DIRECTORY handle
            # becomes unverifiable.
            if got and os.path.isdir(got):
                return None
            return got

        monkeypatch.setattr(hooks_mod, "_fd_real_path", _files_only)
        assert safe_write_file_nolink(str(f), "new", within_root=str(root)) is False
        assert f.read_text(encoding="utf-8") == "old"

    def test_an_unstattable_pinned_target_refuses(self, tmp_path, monkeypatch):
        if not (
            getattr(os, "O_DIRECTORY", 0)
            and os.open in getattr(os, "supports_dir_fd", set())
            and os.rename in getattr(os, "supports_dir_fd", set())
        ):
            pytest.skip("this platform has no directory-fd pinning")
        f = _write(tmp_path / "a.txt", "old")
        real_stat = os.stat

        def _no_stat_through_dir_fd(path, *args, **kwargs):
            if "dir_fd" in kwargs and path == f.name:
                raise FileNotFoundError("gone from the pinned parent")
            return real_stat(path, *args, **kwargs)

        monkeypatch.setattr(os, "stat", _no_stat_through_dir_fd)
        try:
            assert safe_write_file_nolink(str(f), "new") is False
        finally:
            monkeypatch.undo()
        assert f.read_text(encoding="utf-8") == "old"


# ── internal read: the remaining outcomes ──


class TestSafeReadFileInternalOutcomes:
    """Exercise the read outcomes against a redirected home directory.

    ``HOME``/``USERPROFILE`` are repointed at ``tmp_path`` so a real file can
    live at an allowlisted, genuinely sensitive location without touching the
    operator's own ``~/.aws``.
    """

    @pytest.fixture
    def sensitive_home(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        (home / ".aws" / "sso" / "cache").mkdir(parents=True)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        rel = ".aws/sso/cache/probe.json"
        monkeypatch.setitem(hooks_mod._INTERNAL_READ_ALLOWLIST, "probe", rel)
        target = home / ".aws" / "sso" / "cache" / "probe.json"
        if not security.is_sensitive_path(str(target)):
            pytest.skip("home redirection did not take effect for the path gate")
        return target

    def test_a_successful_read_returns_the_bytes(self, sensitive_home, monkeypatch):
        _write(sensitive_home, "opaque-value")
        outcomes: list[str] = []
        monkeypatch.setattr(
            hooks_mod,
            "_emit_internal_read_audit",
            lambda read_id, outcome: outcomes.append(outcome) or True,
        )
        assert safe_read_file_internal("probe") == b"opaque-value"
        assert outcomes == ["success"]

    def test_a_read_whose_audit_cannot_be_recorded_is_denied(self, sensitive_home, monkeypatch):
        _write(sensitive_home, "opaque-value")
        monkeypatch.setattr(hooks_mod, "_emit_internal_read_audit", lambda read_id, outcome: False)
        # audit-or-deny: the carve-out's validity depends on the audit landing.
        assert safe_read_file_internal("probe") is None

    def test_a_non_regular_target_is_refused(self, sensitive_home, monkeypatch):
        sensitive_home.mkdir()
        outcomes: list[str] = []
        monkeypatch.setattr(
            hooks_mod,
            "_emit_internal_read_audit",
            lambda read_id, outcome: outcomes.append(outcome) or True,
        )
        assert safe_read_file_internal("probe") is None
        assert outcomes and outcomes[0] in ("not_regular", "unreadable")

    def test_an_oversized_target_is_refused(self, sensitive_home, monkeypatch):
        _write(sensitive_home, "0123456789")
        monkeypatch.setattr(hooks_mod, "MAX_FILE_BYTES", 4)
        outcomes: list[str] = []
        monkeypatch.setattr(
            hooks_mod,
            "_emit_internal_read_audit",
            lambda read_id, outcome: outcomes.append(outcome) or True,
        )
        assert safe_read_file_internal("probe") is None
        assert outcomes == ["too_large"]


# ── message transforms and the read-only auto-approve branch ──


class TestMessageTransformSuffix:
    def test_prefix_and_suffix_are_both_applied(self):
        cfg = HooksConfig(
            transforms=[TransformHook(pattern="deploy", prefix="[BEFORE]", suffix="[AFTER]")]
        )
        result = HookManager(cfg).on_message("please deploy")
        assert result.action == HOOK_MODIFY
        assert result.text == "[BEFORE]\nplease deploy\n[AFTER]"

    def test_suffix_only(self):
        cfg = HooksConfig(transforms=[TransformHook(pattern="deploy", suffix="[AFTER]")])
        assert HookManager(cfg).on_message("deploy").text == "deploy\n[AFTER]"


class TestReadOnlyKindAutoApprove:
    @pytest.mark.parametrize("kind", ["read", "fetch", "READ"])
    def test_a_read_only_kind_auto_approves(self, kind):
        got = HookManager().on_tool_call("some_tool", tool_kind=kind)
        assert got.action == TOOL_AUTO_APPROVE

    def test_a_computer_use_observation_never_reaches_its_own_gate(self, monkeypatch):
        """Documents that the computer-use observation branch is unreachable.

        ``on_tool_call`` returns ``auto_approve`` for every kind in
        ``_READ_ONLY_TOOL_KINDS`` one branch earlier, so the follow-up condition
        ``kind in _READ_ONLY_TOOL_KINDS and _cu_read_only_auto_approve(...)``
        can never be evaluated -- and with it the keystone computer-use
        enable-state check it carries. Asserted so the dead branch is visible
        rather than silently trusted.
        """
        consulted: list[str] = []
        monkeypatch.setattr(
            hooks_mod,
            "_cu_read_only_auto_approve",
            lambda name: consulted.append(name) or True,
        )
        got = HookManager().on_tool_call(
            "mcp__kirocrew-computer__computer_get_state", tool_kind="read"
        )
        assert got.action == TOOL_AUTO_APPROVE
        assert consulted == []

    def test_a_mutating_kind_falls_through_to_interactive_approval(self):
        assert HookManager().on_tool_call("some_tool", tool_kind="edit").action == TOOL_ALLOW
        assert HookManager().on_tool_call("some_tool", tool_kind="other").action == TOOL_ALLOW


class TestVerifiedReplaceFileNolink:
    """Compare-and-swap through one descriptor: verify and replace share a
    single name resolution, and every post-verify concurrency change answers
    conflict — the newer file wins, never the stale edit."""

    @staticmethod
    def _sha(text: str) -> str:
        import hashlib

        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def test_matching_base_replaces(self, tmp_path):
        f = _write(tmp_path / "a.txt", "BEFORE")
        assert (
            verified_replace_file_nolink(str(f), "AFTER", self._sha("BEFORE"), max_bytes=1_000_000)
            == "ok"
        )
        assert f.read_text(encoding="utf-8") == "AFTER"

    def test_stale_base_is_a_conflict_and_leaves_the_file(self, tmp_path):
        f = _write(tmp_path / "a.txt", "THEIRS")
        out = verified_replace_file_nolink(
            str(f), "MINE", self._sha("WHAT I SAW"), max_bytes=1_000_000
        )
        assert out == "conflict"
        assert f.read_text(encoding="utf-8") == "THEIRS"

    def test_file_grown_past_cap_is_too_large(self, tmp_path):
        f = _write(tmp_path / "a.txt", "x" * 100)
        out = verified_replace_file_nolink(str(f), "new", self._sha("x" * 100), max_bytes=50)
        assert out == "too_large"
        assert f.read_text(encoding="utf-8") == "x" * 100

    def test_missing_file_is_refused(self, tmp_path):
        out = verified_replace_file_nolink(
            str(tmp_path / "gone.txt"), "x", self._sha("anything"), max_bytes=1_000_000
        )
        assert out == "refused"
        assert not (tmp_path / "gone.txt").exists()

    def test_within_root_containment_still_applies(self, tmp_path):
        outside = _write(tmp_path / "outside.txt", "SECRET")
        root = tmp_path / "root"
        root.mkdir()
        out = verified_replace_file_nolink(
            str(outside), "clobber", self._sha("SECRET"), within_root=str(root), max_bytes=1_000_000
        )
        assert out == "refused"
        assert outside.read_text(encoding="utf-8") == "SECRET"

    def test_external_atomic_save_after_verify_is_a_conflict(self, tmp_path, monkeypatch):
        """THE finding class: an external editor completes its own atomic save
        (new inode) in the window between hash verification and the rename.
        The identity re-checks anchored to the verified descriptor detect the
        swap and answer conflict; the external editor's newer content survives.
        The injection point is the staging write (os.fsync), which sits
        strictly after verification and strictly before the rename."""
        f = _write(tmp_path / "a.txt", "BASE")
        real_fsync = os.fsync
        fired = {"done": False}

        def racing_fsync(fd):
            if not fired["done"]:
                fired["done"] = True
                # An editor's atomic save: stage + replace = NEW inode.
                side = tmp_path / ".editor-save.tmp"
                side.write_text("NEWER FROM OUTSIDE", encoding="utf-8")
                os.replace(side, f)
            return real_fsync(fd)

        monkeypatch.setattr(os, "fsync", racing_fsync)
        out = verified_replace_file_nolink(
            str(f), "STALE EDIT", self._sha("BASE"), max_bytes=1_000_000
        )
        monkeypatch.undo()
        assert fired["done"], "the race never ran — the test would be vacuous"
        assert out == "conflict"
        assert f.read_text(encoding="utf-8") == "NEWER FROM OUTSIDE"

    def test_external_in_place_write_after_verify_is_a_conflict(self, tmp_path, monkeypatch):
        """Same window, same-inode variant: an in-place rewrite keeps (dev,ino)
        so the identity check cannot see it — the mtime/size re-check does."""
        f = _write(tmp_path / "a.txt", "BASE")
        real_fsync = os.fsync
        fired = {"done": False}

        def racing_fsync(fd):
            if not fired["done"]:
                fired["done"] = True
                # In-place write through the SAME inode, different length.
                with open(f, "r+b") as fh:
                    fh.write(b"NEWER IN PLACE, LONGER THAN BASE")
                    fh.truncate()
            return real_fsync(fd)

        monkeypatch.setattr(os, "fsync", racing_fsync)
        out = verified_replace_file_nolink(
            str(f), "STALE EDIT", self._sha("BASE"), max_bytes=1_000_000
        )
        monkeypatch.undo()
        assert fired["done"], "the race never ran — the test would be vacuous"
        assert out == "conflict"
        assert f.read_text(encoding="utf-8") == "NEWER IN PLACE, LONGER THAN BASE"

    def test_same_length_in_place_rewrite_is_still_a_conflict(self, tmp_path, monkeypatch):
        """The mtime half of the freshness pair, exercised alone: a rewrite of
        the SAME length keeps st_size, so only st_mtime_ns can catch it. The
        mtime is advanced explicitly because a sub-timestamp-tick rewrite is
        stat-invisible — the documented residue this check cannot close."""
        f = _write(tmp_path / "a.txt", "BASE")
        real_fsync = os.fsync
        fired = {"done": False}

        def racing_fsync(fd):
            if not fired["done"]:
                fired["done"] = True
                with open(f, "r+b") as fh:
                    fh.write(b"EGAB")  # same length as BASE
                    fh.truncate()
                # An in-place rewrite landing within one filesystem timestamp
                # tick leaves NO stat-visible trace (same inode, size, and
                # mtime_ns) — that sub-tick case is the documented unclosable
                # residue, verified empirically on this filesystem. Advance
                # the mtime explicitly so this test pins the mtime half of
                # the comparison, which real editor saves (milliseconds to
                # minutes later) always trip.
                st = os.stat(f)
                os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
            return real_fsync(fd)

        monkeypatch.setattr(os, "fsync", racing_fsync)
        out = verified_replace_file_nolink(
            str(f), "STALE EDIT", self._sha("BASE"), max_bytes=1_000_000
        )
        monkeypatch.undo()
        assert fired["done"], "the race never ran — the test would be vacuous"
        assert out == "conflict"
        assert f.read_text(encoding="utf-8") == "EGAB"

    def test_mode_survives_a_verified_replace(self, tmp_path):
        if _IS_WINDOWS:
            pytest.skip("POSIX mode bits are not meaningful on Windows")
        f = _write(tmp_path / "a.txt", "BEFORE")
        os.chmod(f, 0o600)
        assert (
            verified_replace_file_nolink(str(f), "AFTER", self._sha("BEFORE"), max_bytes=1_000_000)
            == "ok"
        )
        assert _stat.S_IMODE(os.stat(f).st_mode) == 0o600

    def test_a_malformed_base_hash_is_refused_not_skipped(self, tmp_path):
        """Deny-by-default: an unverifiable base refuses — it must never skip
        verification and proceed (fail-open is the lost-update class itself)."""
        f = _write(tmp_path / "a.txt", "BASE")
        for bad in ("", "not-hex", "ABCDEF" + "0" * 58, None):
            out = verified_replace_file_nolink(
                str(f), "NEW", bad, max_bytes=1_000_000  # type: ignore[arg-type]
            )
            assert out == "refused", f"base_hash={bad!r}"
            assert f.read_text(encoding="utf-8") == "BASE"

    def test_wrapper_contract_unchanged(self, tmp_path):
        """safe_write_file_nolink is the engine's no-verify entry point: no
        base hash, no conflict vocabulary, bool contract preserved."""
        f = _write(tmp_path / "a.txt", "old")
        assert safe_write_file_nolink(str(f), "new") is True
        assert f.read_text(encoding="utf-8") == "new"
