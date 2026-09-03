"""A failed config read must never silently reset the user's settings.

Every read-modify-write of ``config.json`` used to fall back to ``data = {}``
on a read failure and then write that empty dict back, so one unreadable or
mid-write file turned "flip one toggle" into "erase every setting". These tests
pin the fail-closed contract of ``read_config_for_update``: an unreadable
existing config raises, and a genuinely absent one still starts from ``{}``.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from kiro_crew import platform_compat
from kiro_crew.config.loader import ConfigReadError, read_config_for_update


def _inline_on_the_loop(fn, /, *args, **kwargs):
    """Call *fn* from inside a running event loop, as an async handler does.

    ``write_config_atomically`` is synchronous and several dashboard handlers
    still reach it directly from a coroutine. That is the case its Windows volume
    gate exists for, so a test about the gate has to actually be on a loop --
    a plain test function is not, and would silently exercise the offloaded path
    instead.
    """

    async def _main():
        return fn(*args, **kwargs)

    return asyncio.run(_main())


def _offloaded(fn, /, *args, **kwargs):
    """Call *fn* in a worker thread from a running loop.

    The shape ``dashboard/chat_utils.run_config_write`` gives every config write
    it owns: the loop stays free and the blocking work happens where a wait costs
    nothing but the worker's own time.
    """

    async def _main():
        return await asyncio.to_thread(fn, *args, **kwargs)

    return asyncio.run(_main())


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
            "the DACL, which write_config_atomically applies on its Windows "
            "branch instead — see test_windows_applies_an_owner_only_dacl)."
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
        """No spawn, on either platform.

        This function runs inside async request handlers and KiroCrewConfig.save(),
        so a blocking subprocess here would freeze the gateway's event loop — the
        `no-blocking-call-on-event-loop` AUTOSDE rule. Pinned because the obvious
        "harden the file" reflex used to reintroduce it: the owner-only lockdown
        was an icacls subprocess, which is why this function used to skip it
        entirely. It now applies the DACL in-process, so the ban is on SPAWNING,
        not on hardening — hardening is asserted positively below.
        """
        import subprocess

        from kiro_crew.config.loader import write_config_atomically

        def _fail(*a, **k):  # pragma: no cover - must never run
            raise AssertionError("write_config_atomically must not spawn a subprocess")

        monkeypatch.setattr(subprocess, "run", _fail)
        write_config_atomically(tmp_path / "config.json", {"auto_update": True})

    @pytest.mark.skipif(
        platform_compat.IS_POSIX,
        reason="Windows DACL branch (POSIX carries access in the mode bits)",
    )
    def test_windows_applies_an_owner_only_dacl(self, tmp_path):
        """The Windows half of the guarantee the mode tests cover on POSIX.

        config.json can hold inline provider tokens, and on Windows the mode bits
        are inert — so without this the file lands under whatever DACL it inherits
        from its parent, readable by every other local account. No mode assertion
        can catch that (NTFS reports 0o666 regardless), so the descriptor itself
        is the observable.
        """
        from kiro_crew import windows_acl
        from kiro_crew.config.loader import write_config_atomically

        path = tmp_path / "config.json"
        write_config_atomically(path, {"slack": {"bot_token": "xoxb-secret"}})

        described = windows_acl.describe(path)
        expected = {"S-1-3-4", platform_compat.current_user_sid()}
        writers = {w.sid for w in described.writers}
        assert not described.null_dacl
        assert writers <= expected, f"unexpected writers: {sorted(writers - expected)}"

    def test_the_volume_is_classified_before_any_filesystem_work(self, tmp_path, monkeypatch):
        """Ordering IS the fix here, so it is asserted rather than the outcome alone.

        This case is a write running INLINE ON THE LOOP -- the one that cannot
        afford the unbounded SMB round-trip a DACL write to a UNC or mapped-drive
        path costs, and so the one the volume gate exists for. A check placed
        inside ``atomic_write`` -- where an earlier revision of this change put it
        -- is already too late: the ``stat`` and the ``parent.mkdir`` below, plus
        everything ``atomic_write`` does, each touch the target volume first, so the
        loop would have parked on the network before the verdict landed.

        The one thing that legitimately precedes the gate is the symlink resolve: a
        config symlinked into a dotfiles repo can point at a different volume than
        the link, so classifying before resolving would classify the wrong volume.
        That is asserted too, rather than left implied.

        The write is driven through :func:`_inline_on_the_loop` deliberately. A
        plain test function has no running loop, which is now the OFFLOADED case
        and skips the classification entirely -- so calling directly here would
        assert nothing about the gate.
        """
        import kiro_crew.config.loader as loader

        order: list[str] = []
        monkeypatch.setattr(platform_compat, "IS_POSIX", False)
        monkeypatch.setattr(
            loader.windows_acl,
            "volume_is_local",
            lambda _p: order.append("classify_volume") or False,
        )
        real_mkdir = loader.Path.mkdir

        def _tracking_mkdir(self, *a, **k):
            order.append("mkdir")
            return real_mkdir(self, *a, **k)

        monkeypatch.setattr(loader.Path, "mkdir", _tracking_mkdir)
        monkeypatch.setattr(
            platform_compat,
            "restrict_to_owner",
            lambda _p: order.append("lockdown"),  # pragma: no cover - must not run
        )

        path = tmp_path / "config.json"
        _inline_on_the_loop(
            loader.write_config_atomically, path, {"slack": {"bot_token": "xoxb-secret"}}
        )

        assert order[0] == "classify_volume", (
            "the volume must be classified before any filesystem work on it -- "
            f"got {order}, so the loop paid for work the gate exists to avoid"
        )
        assert "lockdown" not in order, "a non-local volume must skip the DACL entirely"
        # Skipping the lockdown must not lose the config write.
        assert json.loads(path.read_text())["slack"]["bot_token"] == "xoxb-secret"

    def test_a_local_volume_still_gets_the_lockdown(self, tmp_path, monkeypatch):
        # The other half: the gate must not become a blanket opt-out. On a local
        # volume the on-loop write is protected exactly as it is without the gate.
        import kiro_crew.config.loader as loader

        locked: list[str] = []
        monkeypatch.setattr(platform_compat, "IS_POSIX", False)
        monkeypatch.setattr(loader.windows_acl, "volume_is_local", lambda _p: True)
        monkeypatch.setattr(platform_compat, "restrict_to_owner", lambda p: locked.append(str(p)))

        path = tmp_path / "config.json"
        _inline_on_the_loop(
            loader.write_config_atomically, path, {"slack": {"bot_token": "xoxb-secret"}}
        )

        assert len(locked) == 1, f"the lockdown must run on a local volume: {locked}"
        assert locked[0].endswith(".tmp"), "the DACL must land on the TEMP, before the content"

    def test_an_unloadable_descriptor_api_skips_rather_than_crashing(self, tmp_path, monkeypatch):
        # A host where the security API cannot be loaded at all must still get its
        # config written: the lockdown would have failed there anyway, so the
        # classifier raising must degrade to "skip", never to a failed save.
        import kiro_crew.config.loader as loader

        def _boom(_p):
            raise RuntimeError("cannot load the Windows security API")

        monkeypatch.setattr(platform_compat, "IS_POSIX", False)
        monkeypatch.setattr(loader.windows_acl, "volume_is_local", _boom)

        path = tmp_path / "config.json"
        _inline_on_the_loop(loader.write_config_atomically, path, {"auto_update": True})
        assert json.loads(path.read_text())["auto_update"] is True

    def test_an_offloaded_write_gets_the_dacl_on_a_non_local_volume(self, tmp_path, monkeypatch):
        """The fix. A network-homed data home is protected once the caller offloads.

        The volume was never the thing that made the DACL unaffordable -- the
        event loop was. A write handed to a worker thread (what
        ``dashboard/chat_utils.run_config_write`` does for every config write it
        owns) blocks nothing but that worker, so an unbounded SMB round-trip is
        affordable and ``config.json`` gets the owner-only DACL even on a UNC or
        mapped-drive path.

        The volume must not even be classified here: its answer could only take
        protection away, so asking would be both pointless and a round-trip.
        """
        import kiro_crew.config.loader as loader

        locked: list[str] = []
        classified: list[str] = []
        monkeypatch.setattr(platform_compat, "IS_POSIX", False)
        monkeypatch.setattr(
            loader.windows_acl,
            "volume_is_local",
            lambda p: classified.append(str(p)) or False,  # a network-homed data home
        )
        monkeypatch.setattr(platform_compat, "restrict_to_owner", lambda p: locked.append(str(p)))

        path = tmp_path / "config.json"
        _offloaded(loader.write_config_atomically, path, {"slack": {"bot_token": "xoxb-secret"}})

        assert len(locked) == 1, (
            "an offloaded write blocks only its own worker, so the owner-only DACL "
            f"must be applied regardless of the volume: {locked}"
        )
        assert locked[0].endswith(".tmp"), (
            "the DACL must land on the TEMP file, before any content reaches it -- "
            "otherwise the inline token exists in a readable file first"
        )
        assert not classified, (
            "off the loop the volume must not be classified at all: its answer can "
            f"only weaken the outcome, so asking is a wasted round-trip: {classified}"
        )
        assert json.loads(path.read_text())["slack"]["bot_token"] == "xoxb-secret"

    def test_a_synchronous_caller_gets_the_dacl_on_a_non_local_volume(self, tmp_path, monkeypatch):
        # The CLI and startup paths (cli_setup, cli_chat, KiroCrewConfig.save from
        # boot) have no event loop at all, so they were skipping the DACL on a
        # network-homed data home for a reason that never applied to them.
        import kiro_crew.config.loader as loader

        locked: list[str] = []
        monkeypatch.setattr(platform_compat, "IS_POSIX", False)
        monkeypatch.setattr(loader.windows_acl, "volume_is_local", lambda _p: False)
        monkeypatch.setattr(platform_compat, "restrict_to_owner", lambda p: locked.append(str(p)))

        path = tmp_path / "config.json"
        loader.write_config_atomically(path, {"slack": {"bot_token": "xoxb-secret"}})

        assert len(locked) == 1, f"a caller with no loop has nothing to stall: {locked}"
        assert locked[0].endswith(".tmp")

    def test_an_offloaded_write_survives_a_lockdown_that_fails(self, tmp_path, monkeypatch):
        # restrict_on_error="warn", not "raise": config.json must not become
        # unwritable because a DACL could not be applied. Newly reachable on a
        # non-local volume, so it is pinned there rather than assumed.
        import kiro_crew.config.loader as loader

        def _boom(_p):
            raise OSError("the SMB share refused the descriptor write")

        monkeypatch.setattr(platform_compat, "IS_POSIX", False)
        monkeypatch.setattr(loader.windows_acl, "volume_is_local", lambda _p: False)
        monkeypatch.setattr(platform_compat, "restrict_to_owner", _boom)

        path = tmp_path / "config.json"
        _offloaded(loader.write_config_atomically, path, {"auto_update": True})

        assert json.loads(path.read_text())["auto_update"] is True, (
            "a DACL that cannot be applied must warn and continue -- losing the "
            "settings write would be strictly worse than an inherited ACL"
        )

    def test_the_predicate_answers_for_the_thread_that_actually_writes(self):
        """The link the fix hangs on, asserted directly rather than inferred.

        ``on_event_loop`` is asked from deep inside a synchronous call stack, so
        what matters is that it reports on the CALLING THREAD: True in a coroutine,
        False in the worker ``asyncio.to_thread`` hands the write to. If that ever
        inverted, every case above would still pass while the real behaviour
        flipped -- an on-loop write would take the SMB stall and an offloaded one
        would skip the DACL it can afford.
        """
        from kiro_crew.atomic_write import on_event_loop

        async def _both():
            return on_event_loop(), await asyncio.to_thread(on_event_loop)

        inline, offloaded = asyncio.run(_both())

        assert inline is True, "a coroutine runs on the loop it must not stall"
        assert offloaded is False, "asyncio.to_thread's worker has no loop of its own"
        assert on_event_loop() is False, "a plain synchronous caller has no loop either"

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


class TestEveryConfigWriterIsLocked:
    """No direct ``write_config_atomically(config_path())`` caller may reappear (#8032).

    ``update_config_locked`` holds an advisory lock on a ``<path>.lock`` sidecar
    for its whole read-modify-write. Its guarantee is only as strong as the set
    of writers that participate: a writer that renames ``config.json`` without
    taking that lock can land between a participant's read and write, and the
    second rename wins with a document that never saw the other's change. The
    loss is silent and the lost data is user configuration.

    That list was drained once by hand (the dashboard agents endpoint,
    ``security.py``, the apps manager, the CLI setup wizard). Without a ratchet
    it regrows: the write is one obvious line and nothing about it announces the
    lock it is missing. So this walks the AST rather than asserting on a
    hand-maintained list, in the shape ``TestNoFailOpenConfigWriters`` above
    established.

    **What it does NOT cover.** Only calls to ``write_config_atomically``. A
    second family of writers reaches ``config.json`` through
    ``kiro_crew.agent._atomic_json_write`` or :meth:`KiroCrewConfig.save`
    (``messaging.py``'s channel savers, ``core.py``'s STT and theme PUTs,
    ``mcp.py``'s gateway-enable, ``updates.py``'s log-level PUT, several
    ``agents.py`` CRUD endpoints) and still bypasses the lock. Green here does
    not mean every config writer is locked -- it means this class of them is.
    """

    #: The primitive itself writes through ``write_config_atomically`` by
    #: definition, and ``KiroCrewConfig.save`` is the head of the second family
    #: above. Both live here, so the module is exempt as a whole.
    _ALLOWED_FILES = {"loader.py"}

    #: Resolvers whose return value IS a config document path.
    _CONFIG_PATH_FUNCS = {"config_path", "config_local_path"}

    def test_no_unlocked_config_write_outside_the_primitive(self):
        import ast
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[1] / "src" / "kiro_crew"
        offenders: list[str] = []

        def _is_config_path_call(node: ast.AST) -> bool:
            if not isinstance(node, ast.Call):
                return False
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            return name in self._CONFIG_PATH_FUNCS

        for path in root.rglob("*.py"):
            if "_vendor" in path.parts or path.name in self._ALLOWED_FILES:
                continue
            src = path.read_text(encoding="utf-8", errors="replace")
            if "write_config_atomically" not in src:
                continue
            try:
                tree = ast.parse(src)
            except SyntaxError:
                continue
            # Scope per function: the same local name (``path``, ``cfg_file``) is
            # reused across unrelated functions, so a file-wide binding map
            # reports false positives -- and, worse, would let a genuine offender
            # hide behind an unrelated function's rebinding of the same name.
            funcs = [
                n
                for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            ]
            for func in funcs:
                config_names: set[str] = set()
                for node in ast.walk(func):
                    if not isinstance(node, ast.Assign) or not _is_config_path_call(node.value):
                        continue
                    for tgt in node.targets:
                        if isinstance(tgt, ast.Name):
                            config_names.add(tgt.id)
                for node in ast.walk(func):
                    if not isinstance(node, ast.Call):
                        continue
                    called = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
                    if called != "write_config_atomically" or not node.args:
                        continue
                    target = node.args[0]
                    hit = _is_config_path_call(target) or (
                        isinstance(target, ast.Name) and target.id in config_names
                    )
                    if hit:
                        offenders.append(f"{path.name}:{node.lineno} ({func.name})")

        assert not offenders, (
            "config.json / config.local.json must be written through "
            "update_config_locked(), which holds the <path>.lock sidecar across "
            "the whole read-modify-write. A direct write_config_atomically() "
            "here takes no advisory lock, so it can land between another "
            "writer's read and write and silently revert it (#8032). For an "
            "async handler, go through dashboard/chat_utils.run_config_write or "
            "the module's own shielded offload.\n  " + "\n  ".join(sorted(set(offenders)))
        )

    def test_the_ratchet_would_catch_a_reintroduced_writer(self, tmp_path):
        """The scan is not vacuous: the shape it forbids is actually detected.

        A ratchet asserting an empty list is indistinguishable from a ratchet
        whose matcher is broken, and this one has to see through a local variable
        to work at all. So the detector is exercised on both spellings a
        regression would take.
        """
        import ast

        source = (
            "def handler():\n"
            "    path = config_path()\n"
            "    data = read_config_for_update(path)\n"
            "    data['k'] = 1\n"
            "    write_config_atomically(path, data)\n"
            "\n"
            "def inline():\n"
            "    write_config_atomically(config_local_path(), {})\n"
            "\n"
            "def innocent(path):\n"
            "    write_config_atomically(path, {})\n"
        )
        tree = ast.parse(source)

        def _is_config_path_call(node):
            if not isinstance(node, ast.Call):
                return False
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            return name in self._CONFIG_PATH_FUNCS

        flagged: set[str] = set()
        for func in [
            n
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]:
            config_names = {
                tgt.id
                for node in ast.walk(func)
                if isinstance(node, ast.Assign) and _is_config_path_call(node.value)
                for tgt in node.targets
                if isinstance(tgt, ast.Name)
            }
            for node in ast.walk(func):
                if not isinstance(node, ast.Call) or not node.args:
                    continue
                called = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
                if called != "write_config_atomically":
                    continue
                target = node.args[0]
                if _is_config_path_call(target) or (
                    isinstance(target, ast.Name) and target.id in config_names
                ):
                    flagged.add(func.name)

        assert flagged == {"handler", "inline"}, (
            "the ratchet's matcher does not see the shape it exists to forbid "
            f"(flagged: {sorted(flagged)}). ``innocent`` writes a CALLER-SUPPLIED "
            "path -- the agent-spec write in agents.py is that shape -- and must "
            "not be flagged."
        )
