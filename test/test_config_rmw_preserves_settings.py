"""A failed config read must never silently reset the user's settings.

Every read-modify-write of ``config.json`` used to fall back to ``data = {}``
on a read failure and then write that empty dict back, so one unreadable or
mid-write file turned "flip one toggle" into "erase every setting". These tests
pin the fail-closed contract of ``read_config_for_update``: an unreadable
existing config raises, and a genuinely absent one still starts from ``{}``.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew import platform_compat
from kiro_crew.config.loader import ConfigReadError, read_config_for_update

_REAL_SETTINGS = {
    "agent": {"approval_mode": "interactive", "max_subagents": 8},
    "dashboard": {"theme_mode": "dark", "theme_color": "monokai", "language": "zh-CN"},
    "session": {"timeout_secs": 7200},
    "timezone": "Asia/Shanghai",
    "auto_update": True,
}


class TestReadConfigForUpdate:
    def test_absent_config_returns_empty_dict(self, tmp_path):
        """A never-created config is a legitimate empty starting point."""
        assert read_config_for_update(tmp_path / "config.json") == {}

    def test_valid_config_round_trips(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text(json.dumps(_REAL_SETTINGS), encoding="utf-8")
        assert read_config_for_update(path) == _REAL_SETTINGS

    def test_truncated_config_raises_instead_of_returning_empty(self, tmp_path):
        """A torn/mid-write file must abort the update, not reset the config.

        This is the regression: returning ``{}`` here is what let a caller
        write ``{"auto_update": false}`` over a fully populated config.
        """
        path = tmp_path / "config.json"
        path.write_text(json.dumps(_REAL_SETTINGS, indent=2)[:-20], encoding="utf-8")
        with pytest.raises(ConfigReadError):
            read_config_for_update(path)

    def test_non_object_config_raises(self, tmp_path):
        """A JSON array/scalar is not a config; refuse rather than reset."""
        path = tmp_path / "config.json"
        path.write_text("[1, 2, 3]", encoding="utf-8")
        with pytest.raises(ConfigReadError):
            read_config_for_update(path)

    def test_invalid_utf8_config_raises(self, tmp_path):
        """Invalid UTF-8 must not escape the controlled path.

        UnicodeDecodeError is a ValueError, not an OSError, so it has to be
        named explicitly — otherwise a torn write that splits a multi-byte
        sequence crashes the caller instead of returning the clean refusal.
        """
        path = tmp_path / "config.json"
        path.write_bytes(b'{"agent": "\xff\xfe not utf8"}')
        with pytest.raises(ConfigReadError):
            read_config_for_update(path)

    def test_unreadable_config_raises(self, tmp_path):
        """An OSError on read must not be mistaken for an empty config."""
        path = tmp_path / "config.json"
        path.write_text(json.dumps(_REAL_SETTINGS), encoding="utf-8")
        # A directory at the config path reliably raises OSError on read_text
        # across platforms, without depending on chmod semantics (Windows).
        path.unlink()
        path.mkdir()
        with pytest.raises(ConfigReadError):
            read_config_for_update(path)


class TestNoFailOpenConfigWriters:
    """Guard the whole class of bug, not just the sites fixed by hand.

    The original sweep used a variable-name-specific pattern and missed the
    ``mc_cfg`` site on the agent-config PUT path. This walks the AST instead:
    any ``except`` handler that binds ``{}`` to a name which is later written
    back to a config path is the same data-loss shape.
    """

    def test_no_except_handler_defaults_config_to_empty_dict(self):
        import ast
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[1] / "src" / "kiro_crew"
        # The one documented exception: interactive `config set --local`
        # deliberately overwrites a corrupt overlay (see config.md).
        allowed = {("cli_config.py", "d")}
        writers = ("write_text", "atomic_write", "write_config_atomically")

        def _handler_is_benign(handler: ast.ExceptHandler) -> bool:
            """``FileNotFoundError -> {}`` is correct: an ABSENT config is a
            genuine empty starting point. Only a corrupt/unreadable one must
            fail closed."""
            names = set()
            exc = handler.type
            for node in ast.walk(exc) if exc is not None else []:
                if isinstance(node, ast.Name):
                    names.add(node.id)
            return bool(names) and names <= {"FileNotFoundError", "OSError"}

        offenders: list[str] = []
        for path in root.rglob("*.py"):
            if "_vendor" in path.parts:
                continue
            src = path.read_text(encoding="utf-8", errors="replace")
            if "config_path()" not in src:
                continue
            try:
                tree = ast.parse(src)
            except SyntaxError:
                continue
            # Scope per function: the same local name (`data`) is reused across
            # unrelated handlers, so a file-wide match reports false positives.
            funcs = [
                n
                for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            ]
            for func in funcs:
                fail_open: dict[str, int] = {}
                for node in ast.walk(func):
                    if not isinstance(node, ast.ExceptHandler) or _handler_is_benign(node):
                        continue
                    for stmt in ast.walk(node):
                        if (
                            isinstance(stmt, ast.Assign)
                            and isinstance(stmt.value, ast.Dict)
                            and not stmt.value.keys
                        ):
                            for tgt in stmt.targets:
                                if isinstance(tgt, ast.Name):
                                    fail_open[tgt.id] = stmt.lineno
                if not fail_open:
                    continue
                for node in ast.walk(func):
                    if not isinstance(node, ast.Call):
                        continue
                    fn = node.func
                    name = getattr(fn, "attr", None) or getattr(fn, "id", None)
                    if name not in writers:
                        continue
                    for arg in node.args:
                        payload = ast.dump(arg)
                        for var, lineno in fail_open.items():
                            if f"id='{var}'" in payload and (path.name, var) not in allowed:
                                offenders.append(f"{path.name}:{lineno} ({var}) -> {name}()")

        assert not offenders, (
            "A failed config read must not default to {} and then be written back — "
            "that replaces the user's whole config with a near-empty one. Use "
            "read_config_for_update() + write_config_atomically().\n  "
            + "\n  ".join(sorted(set(offenders)))
        )


class TestNoModeWideningConfigWriters:
    """config.json / config.local.json must only be written mode-preservingly.

    ``atomic_write`` creates a NEW inode, so writing the config through it
    directly resets an operator's tightened 0600 to the umask default — and the
    file can hold inline credentials. ``write_config_atomically`` carries the
    mode over; every config writer must go through it.
    """

    def test_config_writes_go_through_the_mode_preserving_helper(self):
        import ast
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[1] / "src" / "kiro_crew"
        # loader.py IS the implementation; cli_commands.py hand-rolls the same
        # contract (explicit mode= + restrict_to_owner) and predates the helper.
        allowed_files = {"loader.py", "cli_commands.py"}
        raw_writers = {"write_text", "atomic_write"}

        offenders: list[str] = []
        for path in root.rglob("*.py"):
            if "_vendor" in path.parts or path.name in allowed_files:
                continue
            src = path.read_text(encoding="utf-8", errors="replace")
            if "config_path()" not in src and "config_local_path()" not in src:
                continue
            try:
                tree = ast.parse(src)
            except SyntaxError:
                continue
            for func in [
                n
                for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            ]:
                # Names bound to a config path in this function.
                cfg_names = set()
                for node in ast.walk(func):
                    if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                        fn = node.value.func
                        called = getattr(fn, "attr", None) or getattr(fn, "id", None)
                        if called in ("config_path", "config_local_path"):
                            for tgt in node.targets:
                                if isinstance(tgt, ast.Name):
                                    cfg_names.add(tgt.id)
                if not cfg_names:
                    continue
                for node in ast.walk(func):
                    if not isinstance(node, ast.Call):
                        continue
                    fn = node.func
                    name = getattr(fn, "attr", None) or getattr(fn, "id", None)
                    if name not in raw_writers:
                        continue
                    # A method call on the config path, or the path passed first.
                    target = getattr(fn, "value", None)
                    hit = isinstance(target, ast.Name) and target.id in cfg_names
                    if not hit and node.args:
                        a0 = node.args[0]
                        hit = isinstance(a0, ast.Name) and a0.id in cfg_names
                    if hit:
                        offenders.append(f"{path.name}:{node.lineno} -> {name}()")

        assert not offenders, (
            "config.json must be written via write_config_atomically() so an "
            "operator's 0600 is not widened to the umask default by tmp+rename.\n  "
            + "\n  ".join(sorted(set(offenders)))
        )


class TestWriteConfigAtomically:
    @pytest.mark.skipif(
        not platform_compat.IS_POSIX,
        reason=(
            "POSIX mode bits only: atomic_write applies `mode` via fchmod_safe, "
            "which is a documented no-op on Windows (access there is carried by "
            "the DACL, and applying one would mean an icacls subprocess — which "
            "this function must not run, see write_config_atomically)."
        ),
    )
    def test_preserves_existing_mode(self, tmp_path):
        """tmp+rename creates a new inode — an operator's 0600 must survive.

        config.json can hold inline credentials, so a settings write must never
        widen who can read it to the umask default.
        """
        import os
        import stat

        from kiro_crew.config.loader import write_config_atomically

        path = tmp_path / "config.json"
        path.write_text(json.dumps({"slack": {"bot_token": "xoxb-secret"}}), encoding="utf-8")
        os.chmod(path, 0o600)

        write_config_atomically(path, {"slack": {"bot_token": "xoxb-secret"}, "auto_update": False})

        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o600, f"mode widened to {oct(mode)}"
        assert not mode & 0o077, "group/other must not gain access"

    @pytest.mark.skipif(
        not platform_compat.IS_POSIX,
        reason="POSIX mode bits only (fchmod_safe is a no-op on Windows)",
    )
    def test_new_file_is_owner_only(self, tmp_path):
        import stat

        from kiro_crew.config.loader import write_config_atomically

        path = tmp_path / "config.json"
        write_config_atomically(path, {"auto_update": True})
        assert not stat.S_IMODE(path.stat().st_mode) & 0o077

    def test_does_not_spawn_a_subprocess_on_the_event_loop(self, tmp_path, monkeypatch):
        """Must not call restrict_to_owner: it shells out to icacls on Windows.

        This function runs inside async request handlers and KiroCrewConfig.save(),
        so a blocking subprocess here would freeze the gateway's event loop —
        the `no-blocking-call-on-event-loop` AUTOSDE rule. Pinned because the
        obvious "harden the file" reflex reintroduces it.
        """
        import subprocess

        from kiro_crew import platform_compat
        from kiro_crew.config.loader import write_config_atomically

        def _fail(*a, **k):  # pragma: no cover - must never run
            raise AssertionError("write_config_atomically must not spawn a subprocess")

        monkeypatch.setattr(subprocess, "run", _fail)
        monkeypatch.setattr(platform_compat, "restrict_to_owner", _fail)
        write_config_atomically(tmp_path / "config.json", {"auto_update": True})

    @pytest.mark.skipif(
        not platform_compat.IS_POSIX,
        reason="symlink creation needs elevation on Windows",
    )
    def test_follows_a_symlinked_config_instead_of_replacing_it(self, tmp_path):
        """os.replace would rename over the link, orphaning its target.

        Symlinking config.json into a dotfiles repo is a normal setup, and the
        write_text this replaced followed the link. Preserve that.
        """
        from kiro_crew.config.loader import write_config_atomically

        target = tmp_path / "real-config.json"
        link = tmp_path / "config.json"
        target.write_text(json.dumps({"timezone": "Asia/Shanghai"}), encoding="utf-8")
        link.symlink_to(target)

        write_config_atomically(link, {"timezone": "Asia/Shanghai", "auto_update": False})

        assert link.is_symlink(), "the symlink was replaced by a regular file"
        assert json.loads(target.read_text(encoding="utf-8"))["auto_update"] is False

    def test_leaves_no_temp_files_behind(self, tmp_path):
        from kiro_crew.config.loader import write_config_atomically

        path = tmp_path / "config.json"
        write_config_atomically(path, {"auto_update": True})
        assert [p.name for p in tmp_path.iterdir()] == ["config.json"]


class TestAutoUpdateToggleKeepsSettings:
    """The narrowest end-to-end proof, on the endpoint that first showed it."""

    @pytest.mark.asyncio
    async def test_toggle_preserves_all_other_settings(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.handlers import updates

        path = tmp_path / "config.json"
        path.write_text(json.dumps(_REAL_SETTINGS, indent=2), encoding="utf-8")
        monkeypatch.setattr(updates, "config_path", lambda: path)

        class _Req:
            async def json(self):
                return {"enabled": False}

        resp = await updates.api_update_auto(_Req())
        assert resp.status == 200

        after = json.loads(path.read_text(encoding="utf-8"))
        assert after["auto_update"] is False
        for key, value in _REAL_SETTINGS.items():
            if key != "auto_update":
                assert after[key] == value, f"{key} was lost by the toggle"

    @pytest.mark.asyncio
    async def test_unreadable_config_fails_loudly_and_changes_nothing(
        self, tmp_path, monkeypatch
    ):
        from kiro_crew.dashboard.handlers import updates

        path = tmp_path / "config.json"
        torn = json.dumps(_REAL_SETTINGS, indent=2)[:-20]
        path.write_text(torn, encoding="utf-8")
        monkeypatch.setattr(updates, "config_path", lambda: path)

        class _Req:
            async def json(self):
                return {"enabled": False}

        resp = await updates.api_update_auto(_Req())
        assert resp.status == 500
        # The unreadable file is left exactly as it was — not replaced by a
        # one-key config that silently drops every real setting.
        assert path.read_text(encoding="utf-8") == torn
