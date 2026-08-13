"""The Cursor Motion overlay — supervisor lifecycle and the AppKit renderer.

Two modules under test, exercised with NO AppKit and NO real child process:

* ``computer_use.overlay`` — the gateway-side supervisor. Its child process is a
  fake ``asyncio.subprocess.Process`` stand-in, so the spawn/write/reap logic runs
  for real while nothing is spawned. This is where the two absolute properties of
  a cosmetic subsystem are pinned: **it never raises into a caller** and **it is a
  clean no-op when disabled or off macOS**.
* ``computer_use.overlay_proc`` — the renderer. Its ObjC runtime is faked exactly
  the way ``test_computer_use_ffi.py`` fakes CoreFoundation: a stand-in exposing
  the same three primitives (``cls`` / ``sel`` / ``msg``) that RECORDS every
  message send, so the real window-construction body runs and the load-bearing
  selectors become assertions instead of hoping somebody re-reads the file.

Why each claim is worth a test:

* **No-op off macOS.** The overlay is an AppKit window with no cross-platform
  equivalent, so every non-macOS platform must degrade to "no visual cursor" —
  never to a spawn attempt, and never to a raised exception. Asserted by flipping
  ``platform_compat.IS_MACOS`` rather than by needing another OS.
* **Default OFF.** A feature that draws on the user's screen must not start
  because a config field was missing. The enable read is deliberately
  ``getattr``-defensive, so the test covers the missing-field case too.
* **``setSharingType: 0`` and ``setIgnoresMouseEvents: True`` are SENT.** The
  first keeps the agent's own fake cursor out of the screenshots the agent takes
  (otherwise the decoration feeds back into the model's observations); the second
  makes the window click-through, without which a purely decorative window would
  swallow the user's real clicks. Both are one-line regressions with no other
  visible symptom, which is exactly why they are pinned to a recorded selector.
* **The spawn is bounded and the child is REAPED.** A cosmetic subsystem that can
  leak processes — or spin respawning against a broken AppKit — is worse than one
  that draws nothing. The readiness wait is bounded, repeated failures give up,
  and every abandonment path closes stdin then force-kills through
  ``platform_compat``.
* **EOF exits.** The renderer's stdin-EOF exit is THE guarantee that a crashed
  gateway cannot leave an orphan cursor parked on the user's screen.
* **Never raises.** Parametrized over every public method with a child that
  raises from every I/O call.
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import threading

import pytest

from kiro_crew import platform_compat
from kiro_crew.computer_use import overlay as overlay_mod
from kiro_crew.computer_use import overlay_proc as proc_mod
from kiro_crew.computer_use.cursor_motion import plan_motion
from kiro_crew.computer_use.overlay import (
    CursorOverlay,
    cursor_motion_enabled,
    get_shared_overlay,
    reset_shared_overlay,
)
from kiro_crew.computer_use.types import (
    CLICK_PULSE_DEPTH,
    CLICK_PULSE_MS,
    CURSOR_GLYPH_HEIGHT,
    CURSOR_GLYPH_WIDTH,
    FALLBACK_SCREEN_HEIGHT,
    FALLBACK_SCREEN_WIDTH,
    MAX_CLICK_COUNT,
    MAX_MOVE_DURATION_MS,
    MIN_MOVE_DURATION_MS,
    NS_ACTIVATION_POLICY_ACCESSORY,
    NS_COLLECTION_BEHAVIOR,
    NS_STATUS_WINDOW_LEVEL,
    NS_WINDOW_SHARING_NONE,
    OVERLAY_CMD_CLICK,
    OVERLAY_CMD_HIDE,
    OVERLAY_CMD_KEY,
    OVERLAY_CMD_MOVE,
    OVERLAY_CMD_QUIT,
    OVERLAY_KEY_COUNT,
    OVERLAY_KEY_MS,
    OVERLAY_KEY_POINTS,
    OVERLAY_KEY_X,
    OVERLAY_KEY_Y,
    OVERLAY_MAX_FAILURES,
    OVERLAY_MODULE,
    OVERLAY_READY_LINE,
)

_SCREEN_W = 3008.0
_SCREEN_H = 1692.0


# ──────────────────────────────────────────────────────────────────────────
# Fakes
# ──────────────────────────────────────────────────────────────────────────
class _FakeStdin:
    """An ``asyncio`` StreamWriter stand-in that records what was written."""

    def __init__(self, *, fail: bool = False) -> None:
        self.lines: list[str] = []
        self.closed = False
        self.fail = fail

    def write(self, data: bytes) -> None:
        if self.fail:
            raise BrokenPipeError("fake pipe is gone")
        self.lines.append(data.decode("utf-8").strip())

    async def drain(self) -> None:
        if self.fail:
            raise ConnectionResetError("fake pipe reset")

    def is_closing(self) -> bool:
        return self.closed

    def close(self) -> None:
        self.closed = True

    @property
    def commands(self) -> list[dict]:
        return [json.loads(line) for line in self.lines]


class _FakeStdout:
    """Serves one readiness line, then blocks forever (like a live child)."""

    def __init__(self, line: "bytes | None" = None, *, hang: bool = False) -> None:
        self._line = line if line is not None else f"{OVERLAY_READY_LINE} 1\n".encode()
        self._hang = hang
        self._served = False

    async def readline(self) -> bytes:
        if self._hang:
            await asyncio.sleep(3600)
        if self._served:
            await asyncio.sleep(3600)
        self._served = True
        return self._line


class _FakeProc:
    """A stand-in for ``asyncio.subprocess.Process``.

    Exposes only what the supervisor touches, so a change that starts using more
    of the real API fails loudly here rather than passing against a permissive
    mock.
    """

    def __init__(
        self,
        *,
        ready: "bytes | None" = None,
        hang: bool = False,
        write_fails: bool = False,
        ignore_eof: bool = False,
        pid: int = 424242,
    ) -> None:
        self.stdin = _FakeStdin(fail=write_fails)
        self.stdout = _FakeStdout(ready, hang=hang)
        self.pid = pid
        self.returncode: "int | None" = None
        self._ignore_eof = ignore_eof
        self.waits = 0
        self.killed = False

    async def wait(self) -> int:
        self.waits += 1
        if self._ignore_eof and not self.killed:
            await asyncio.sleep(3600)
        self.returncode = 0
        return 0


class _FakeRuntime:
    """A fake ObjC runtime: records every send, mimics the values AppKit returns.

    Same harness idiom as ``test_computer_use_ffi.py``'s fake CoreFoundation — the
    REAL window-construction body runs against this, so the selectors it sends
    become assertable facts.
    """

    def __init__(self, *, glyph: bool = True, fail_on: "set[str] | None" = None) -> None:
        self.sends: list[tuple[str, tuple]] = []
        self._glyph = glyph
        self._fail_on = fail_on or set()
        self._handles: dict[str, int] = {}
        self._next = 0x1000

    # ── the three primitives the window code uses ──

    def cls(self, name: str) -> int:
        return self._handle(f"class:{name}")

    def sel(self, name: str) -> str:
        # Returning the NAME (not an opaque int) is what makes ``sends`` readable
        # and lets a test assert on a selector rather than on a pointer value.
        return name

    def msg(self, receiver, selector, restype, argtypes, *args):
        self.sends.append((selector, args))
        if selector in self._fail_on:
            raise OSError(f"fake AppKit refused {selector}")
        if selector == "arrowCursor":
            return self._handle("cursor") if self._glyph else None
        if selector == "image":
            return self._handle("image") if self._glyph else None
        if selector == "size":
            return proc_mod.CGSize(CURSOR_GLYPH_WIDTH, CURSOR_GLYPH_HEIGHT)
        if selector == "hotSpot":
            return proc_mod.CGPoint(5.0, 5.0)
        if selector in ("alloc", "sharedApplication", "clearColor", "contentView"):
            return self._handle(f"obj:{selector}")
        if selector.startswith("initWith"):
            return self._handle(f"obj:{selector}")
        if selector in ("currentRunLoop", "dateWithTimeIntervalSinceNow:", "stringWithUTF8String:"):
            return self._handle(f"obj:{selector}")
        return None

    def screen_size(self) -> tuple[float, float]:
        return (_SCREEN_W, _SCREEN_H)

    # ── helpers ──

    def _handle(self, key: str) -> int:
        if key not in self._handles:
            self._next += 0x10
            self._handles[key] = self._next
        return self._handles[key]

    def selectors(self) -> list[str]:
        return [name for name, _ in self.sends]

    def args_for(self, selector: str) -> list[tuple]:
        return [args for name, args in self.sends if name == selector]

    def origins(self) -> list[tuple[float, float]]:
        return [(a[0].x, a[0].y) for a in self.args_for("setFrameOrigin:")]

    def alphas(self) -> list[float]:
        return [a[0] for a in self.args_for("setAlphaValue:")]


@pytest.fixture
def enabled(monkeypatch):
    """Pin the process to macOS with ``cursor_motion`` ON."""
    monkeypatch.setattr(platform_compat, "IS_MACOS", True)
    monkeypatch.setattr(overlay_mod, "cursor_motion_enabled", lambda: True)
    reset_shared_overlay()
    yield
    reset_shared_overlay()


@pytest.fixture
def spawned(monkeypatch):
    """Replace the real spawn with a recording fake; yields the spawn log."""
    created: list[_FakeProc] = []
    kwargs_log: list[dict] = []
    argv_log: list[tuple] = []

    def _factory(**proc_kwargs):
        async def _spawn(*argv, **kwargs):
            argv_log.append(argv)
            kwargs_log.append(kwargs)
            proc = _FakeProc(**proc_kwargs)
            created.append(proc)
            return proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)

    _factory()
    return {"procs": created, "kwargs": kwargs_log, "argv": argv_log, "configure": _factory}


# ──────────────────────────────────────────────────────────────────────────
# Enable gate
# ──────────────────────────────────────────────────────────────────────────
class TestEnableGate:
    def test_disabled_off_macos_even_when_config_says_yes(self, monkeypatch):
        """No cross-platform equivalent exists, so the answer is no, not a try."""

        class _Cfg:
            computer_use = type("S", (), {"cursor_motion": True})()

        monkeypatch.setattr(platform_compat, "IS_MACOS", False)
        monkeypatch.setattr(
            "kiro_crew.config.loader.KiroCrewConfig.load", classmethod(lambda cls: _Cfg())
        )
        assert cursor_motion_enabled() is False

    def test_default_off_when_the_config_field_is_absent(self, monkeypatch):
        """A build whose dataclass predates the field must not start drawing.

        The field is owned by ``config/loader.py``, so this module reads it through
        ``getattr``; a missing field can only ever mean "no decoration".
        """

        class _Cfg:
            computer_use = object()

        monkeypatch.setattr(platform_compat, "IS_MACOS", True)
        monkeypatch.setattr(
            "kiro_crew.config.loader.KiroCrewConfig.load", classmethod(lambda cls: _Cfg())
        )
        assert cursor_motion_enabled() is False

    def test_enabled_only_for_an_exact_true(self, monkeypatch):
        monkeypatch.setattr(platform_compat, "IS_MACOS", True)
        for value, expected in ((True, True), (False, False), ("yes", False), (1, False)):

            class _Cfg:
                computer_use = type("S", (), {"cursor_motion": value})()

            monkeypatch.setattr(
                "kiro_crew.config.loader.KiroCrewConfig.load", classmethod(lambda cls: _Cfg())
            )
            assert cursor_motion_enabled() is expected, value

    def test_config_failure_disables_rather_than_raising(self, monkeypatch):
        def _boom(cls):
            raise RuntimeError("config is unreadable")

        monkeypatch.setattr(platform_compat, "IS_MACOS", True)
        monkeypatch.setattr("kiro_crew.config.loader.KiroCrewConfig.load", classmethod(_boom))
        assert cursor_motion_enabled() is False


# ──────────────────────────────────────────────────────────────────────────
# Supervisor: the no-op contract
# ──────────────────────────────────────────────────────────────────────────
class TestNoOpWhenDisabled:
    @pytest.mark.asyncio
    async def test_nothing_is_spawned_off_macos(self, monkeypatch):
        """The whole point of the Linux CI assertion: no child, no exception."""
        monkeypatch.setattr(platform_compat, "IS_MACOS", False)
        spawns: list[tuple] = []

        async def _spawn(*argv, **kwargs):
            spawns.append(argv)
            raise AssertionError("must not spawn off macOS")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)
        overlay = CursorOverlay()
        assert await overlay.move_to(100.0, 100.0) is False
        assert await overlay.pulse_click(100.0, 100.0) is False
        assert await overlay.hide() is False
        await overlay.stop()
        assert spawns == []
        assert overlay.running is False

    @pytest.mark.asyncio
    async def test_nothing_is_spawned_when_the_flag_is_off(self, monkeypatch):
        monkeypatch.setattr(platform_compat, "IS_MACOS", True)
        monkeypatch.setattr(overlay_mod, "cursor_motion_enabled", lambda: False)

        async def _spawn(*argv, **kwargs):
            raise AssertionError("must not spawn while disabled")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)
        overlay = CursorOverlay()
        assert await overlay.move_to(10.0, 10.0) is False
        assert await overlay.pulse_click(10.0, 10.0, 2) is False

    @pytest.mark.asyncio
    async def test_stop_is_safe_before_anything_ran(self, monkeypatch):
        monkeypatch.setattr(platform_compat, "IS_MACOS", False)
        overlay = CursorOverlay()
        await overlay.stop()
        await overlay.stop()  # idempotent


# ──────────────────────────────────────────────────────────────────────────
# Supervisor: spawn / write / reap
# ──────────────────────────────────────────────────────────────────────────
class TestSupervisorLifecycle:
    @pytest.mark.asyncio
    async def test_first_move_spawns_once_and_ships_the_sampled_path(self, enabled, spawned):
        overlay = CursorOverlay()
        assert await overlay.move_to(1200.0, 700.0) is True
        assert len(spawned["procs"]) == 1
        command = spawned["procs"][0].stdin.commands[0]
        assert command[OVERLAY_CMD_KEY] == OVERLAY_CMD_MOVE
        # The path is PRE-SAMPLED by the gateway: the renderer has no Bezier in it,
        # so every shape decision stays in the unit-tested pure module.
        expected = plan_motion((1200.0, 700.0), (1200.0, 700.0))
        assert len(command[OVERLAY_KEY_POINTS]) == len(expected.points)
        assert command[OVERLAY_KEY_POINTS][-1] == [1200.0, 700.0]
        assert command[OVERLAY_KEY_MS] == expected.duration_ms

    @pytest.mark.asyncio
    async def test_second_move_reuses_the_child(self, enabled, spawned):
        overlay = CursorOverlay()
        await overlay.move_to(100.0, 100.0)
        await overlay.move_to(900.0, 500.0)
        assert len(spawned["procs"]) == 1
        assert len(spawned["procs"][0].stdin.commands) == 2

    @pytest.mark.asyncio
    async def test_a_move_starts_from_the_last_drawn_point(self, enabled, spawned):
        """Otherwise every move would teleport from a fixed corner first."""
        overlay = CursorOverlay()
        await overlay.move_to(200.0, 200.0)
        await overlay.move_to(1400.0, 800.0)
        second = spawned["procs"][0].stdin.commands[1]
        assert second[OVERLAY_KEY_POINTS][0] == [200.0, 200.0]
        assert second[OVERLAY_KEY_POINTS][-1] == [1400.0, 800.0]

    @pytest.mark.asyncio
    async def test_spawn_argv_is_fixed_and_module_based(self, enabled, spawned):
        """Nothing agent-supplied may enter the argv.

        The only agent-influenced values in this subsystem are numeric coordinates,
        and they travel as JSON on stdin — never as a command-line argument.
        """
        overlay = CursorOverlay()
        await overlay.move_to(1.0, 1.0)
        argv = spawned["argv"][0]
        assert argv[1:] == ("-m", OVERLAY_MODULE)
        assert "kiro_crew.computer_use" in argv[2]

    @pytest.mark.asyncio
    async def test_spawn_uses_the_platform_compat_isolation_flags(self, enabled, spawned):
        """Per the repo contract: the two kwargs passed EXPLICITLY, not unpacked."""
        overlay = CursorOverlay()
        await overlay.move_to(1.0, 1.0)
        kwargs = spawned["kwargs"][0]
        assert kwargs["start_new_session"] is platform_compat.IS_POSIX
        assert kwargs["creationflags"] == platform_compat.CREATE_NEW_PROCESS_GROUP
        assert kwargs["stdin"] is asyncio.subprocess.PIPE
        # stderr is discarded: a renderer's debug chatter must not fill the
        # gateway's pipe buffer and block a cosmetic subsystem's child.
        assert kwargs["stderr"] is asyncio.subprocess.DEVNULL

    @pytest.mark.asyncio
    async def test_click_command_carries_a_clamped_count(self, enabled, spawned):
        overlay = CursorOverlay()
        assert await overlay.pulse_click(400.0, 300.0, 99) is True
        command = spawned["procs"][0].stdin.commands[0]
        assert command[OVERLAY_CMD_KEY] == OVERLAY_CMD_CLICK
        assert command[OVERLAY_KEY_X] == 400.0
        assert command[OVERLAY_KEY_Y] == 300.0
        assert command[OVERLAY_KEY_COUNT] == MAX_CLICK_COUNT

    @pytest.mark.asyncio
    async def test_hide_never_spawns(self, enabled, spawned):
        """Starting a process in order to hide nothing would be absurd."""
        overlay = CursorOverlay()
        assert await overlay.hide() is False
        assert spawned["procs"] == []
        await overlay.move_to(5.0, 5.0)
        assert await overlay.hide() is True
        assert spawned["procs"][0].stdin.commands[-1][OVERLAY_CMD_KEY] == OVERLAY_CMD_HIDE

    @pytest.mark.asyncio
    async def test_readiness_wait_is_bounded(self, enabled, spawned, monkeypatch):
        """A child that wedges before printing must not hang a tool-call path."""
        monkeypatch.setattr(overlay_mod, "OVERLAY_SPAWN_TIMEOUT_SECS", 0.05)
        spawned["configure"](hang=True)
        overlay = CursorOverlay()
        assert await overlay.move_to(1.0, 1.0) is False
        # The wedged child is abandoned, not leaked: stdin closed and reaped.
        assert spawned["procs"][0].stdin.closed is True
        assert spawned["procs"][0].waits >= 1
        assert overlay.running is False

    @pytest.mark.asyncio
    async def test_a_ready_zero_line_is_treated_as_a_failure(self, enabled, spawned):
        """The child started but could not build a window — do not ship commands."""
        spawned["configure"](ready=f"{OVERLAY_READY_LINE} 0\n".encode())
        overlay = CursorOverlay()
        assert await overlay.move_to(1.0, 1.0) is False
        assert spawned["procs"][0].stdin.closed is True

    @pytest.mark.asyncio
    async def test_garbage_instead_of_a_ready_line_is_a_failure(self, enabled, spawned):
        spawned["configure"](ready=b"Traceback (most recent call last):\n")
        overlay = CursorOverlay()
        assert await overlay.move_to(1.0, 1.0) is False

    @pytest.mark.asyncio
    async def test_a_dead_child_is_replaced_not_written_to(self, enabled, spawned):
        overlay = CursorOverlay()
        await overlay.move_to(1.0, 1.0)
        spawned["procs"][0].returncode = 1  # crashed between commands
        await overlay.move_to(2.0, 2.0)
        assert len(spawned["procs"]) == 2
        assert len(spawned["procs"][1].stdin.commands) == 1

    @pytest.mark.asyncio
    async def test_repeated_failures_stop_retrying(self, enabled, monkeypatch):
        """A cursor that cannot be drawn is cosmetic; a respawn loop is a leak."""
        attempts = []

        async def _spawn(*argv, **kwargs):
            attempts.append(argv)
            raise OSError("no fork for you")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)
        overlay = CursorOverlay()
        for _ in range(10):
            assert await overlay.move_to(1.0, 1.0) is False
        assert len(attempts) == OVERLAY_MAX_FAILURES

    @pytest.mark.asyncio
    async def test_a_write_failure_drops_and_reaps_the_child(self, enabled, spawned):
        spawned["configure"](write_fails=True)
        overlay = CursorOverlay()
        assert await overlay.move_to(1.0, 1.0) is False
        assert spawned["procs"][0].stdin.closed is True
        assert spawned["procs"][0].waits >= 1
        assert overlay.running is False


class TestSupervisorStop:
    @pytest.mark.asyncio
    async def test_stop_quits_closes_stdin_and_reaps(self, enabled, spawned):
        overlay = CursorOverlay()
        await overlay.move_to(1.0, 1.0)
        proc = spawned["procs"][0]
        await overlay.stop()
        assert proc.stdin.commands[-1][OVERLAY_CMD_KEY] == OVERLAY_CMD_QUIT
        # EOF is the child's primary exit path — and the only one that also covers
        # a gateway crash, where the quit command never got sent.
        assert proc.stdin.closed is True
        assert proc.waits >= 1
        assert overlay.running is False

    @pytest.mark.asyncio
    async def test_stop_force_kills_a_child_that_ignores_eof(self, enabled, spawned, monkeypatch):
        killed: list[tuple[int, int]] = []

        def _kill(pid, sig):
            killed.append((pid, sig))
            for proc in spawned["procs"]:
                proc.killed = True
            return True

        monkeypatch.setattr(overlay_mod.platform_compat, "kill_process_tree", _kill)
        monkeypatch.setattr(overlay_mod, "OVERLAY_STOP_TIMEOUT_SECS", 0.05)
        spawned["configure"](ignore_eof=True)
        overlay = CursorOverlay()
        await overlay.move_to(1.0, 1.0)
        await overlay.stop()
        # Routed through platform_compat, never a raw os.killpg: os.kill(pid, 0)
        # TERMINATES on Windows and killpg does not exist there at all.
        assert killed == [(spawned["procs"][0].pid, platform_compat.SIGKILL)]

    @pytest.mark.asyncio
    async def test_stop_is_idempotent_and_forgets_the_child(self, enabled, spawned):
        overlay = CursorOverlay()
        await overlay.move_to(1.0, 1.0)
        await overlay.stop()
        await overlay.stop()
        assert len(spawned["procs"][0].stdin.commands) == 2  # move + one quit


class TestNeverRaises:
    """The hard contract: a failed overlay must never fail a tool call."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("method,args", [("move_to", (1.0, 2.0)), ("pulse_click", (1.0, 2.0))])
    async def test_a_hostile_child_cannot_raise_into_a_caller(
        self, enabled, monkeypatch, method, args
    ):
        class _Hostile:
            pid = 7
            returncode = None

            class stdin:
                @staticmethod
                def is_closing():
                    raise RuntimeError("even the probe explodes")

            class stdout:
                @staticmethod
                async def readline():
                    return f"{OVERLAY_READY_LINE} 1\n".encode()

            @staticmethod
            async def wait():
                raise RuntimeError("cannot wait")

        async def _spawn(*argv, **kwargs):
            return _Hostile()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)
        overlay = CursorOverlay()
        assert await getattr(overlay, method)(*args) is False

    @pytest.mark.asyncio
    async def test_a_non_serializable_command_is_dropped_not_raised(self, enabled, spawned):
        """``allow_nan=False`` means a NaN coordinate fails json.dumps.

        A NaN must not reach the renderer (an un-placeable window) and must not
        reach the caller (a failed tool call) — it is dropped.
        """
        overlay = CursorOverlay()
        proc = await overlay._spawn()
        assert proc is not None
        assert await overlay._write_line(proc, {"x": float("nan")}) is False

    @pytest.mark.asyncio
    async def test_a_broken_planner_cannot_raise_into_a_caller(self, enabled, monkeypatch):
        def _boom(*a, **k):
            raise ZeroDivisionError("planner bug")

        monkeypatch.setattr(overlay_mod, "plan_motion", _boom)
        assert await CursorOverlay().move_to(1.0, 1.0) is False


class TestSharedSingleton:
    def test_shared_overlay_is_one_instance(self):
        reset_shared_overlay()
        try:
            # Two supervisors would spawn two children and two fake cursors would
            # fight over the same screen.
            assert get_shared_overlay() is get_shared_overlay()
        finally:
            reset_shared_overlay()

    def test_reset_yields_a_fresh_instance(self):
        first = get_shared_overlay()
        reset_shared_overlay()
        assert get_shared_overlay() is not first
        reset_shared_overlay()


class TestShowPointerMotion:
    """The SYNC seam the blocking dispatcher calls (reviewer finding).

    The dispatcher runs on a worker thread, so it cannot await the animation. This
    schedules it onto the gateway loop and returns immediately, and must never
    raise — a decoration that could fail a click would be worse than no animation.
    """

    @pytest.fixture(autouse=True)
    def _clean(self):
        reset_shared_overlay()
        overlay_mod.bind_gateway_loop(None)
        yield
        reset_shared_overlay()
        overlay_mod.bind_gateway_loop(None)

    @pytest.fixture
    def enabled(self, monkeypatch):
        monkeypatch.setattr(overlay_mod, "cursor_motion_enabled", lambda: True)

    def test_it_is_a_no_op_while_the_setting_is_off(self, monkeypatch):
        """Default OFF: nothing may be scheduled, and nothing may be spawned."""
        monkeypatch.setattr(overlay_mod, "cursor_motion_enabled", lambda: False)
        calls: list = []
        monkeypatch.setattr(overlay_mod, "get_shared_overlay", lambda: calls.append(1))
        overlay_mod.show_pointer_motion(1.0, 2.0)
        assert calls == []

    def test_it_is_a_no_op_with_no_bound_loop(self, enabled, monkeypatch):
        """A coroutine scheduled onto a loop nobody runs would never execute."""
        calls: list = []
        monkeypatch.setattr(overlay_mod, "get_shared_overlay", lambda: calls.append(1))
        overlay_mod.show_pointer_motion(1.0, 2.0)
        assert calls == []

    @pytest.mark.asyncio
    async def test_it_schedules_a_move_then_a_click_pulse(self, enabled):
        seen: list = []

        class _Fake:
            async def move_to(self, x, y, **kw):
                seen.append(("move", x, y))
                return True

            async def pulse_click(self, x, y, count=1):
                seen.append(("click", x, y, count))
                return True

        overlay_mod._shared_overlay = _Fake()
        overlay_mod.bind_gateway_loop()
        overlay_mod.show_pointer_motion(12.0, 34.0, 2)
        # The call itself does NOT await: yield once so the scheduled task runs.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert seen == [("move", 12.0, 34.0), ("click", 12.0, 34.0, 2)]

    @pytest.mark.asyncio
    async def test_it_never_raises_when_the_overlay_explodes(self, enabled):
        class _Boom:
            async def move_to(self, x, y, **kw):
                raise RuntimeError("appkit is on fire")

            async def pulse_click(self, x, y, count=1):
                raise RuntimeError("still on fire")

        overlay_mod._shared_overlay = _Boom()
        overlay_mod.bind_gateway_loop()
        # No exception here, and none surfacing from the loop either.
        overlay_mod.show_pointer_motion(1.0, 2.0)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    def test_a_closed_loop_is_not_scheduled_onto(self, enabled, monkeypatch):
        """A gateway restart must not leave a stale reference we write into.

        ``monkeypatch``, not a bare assignment + ``del``: ``del
        overlay_mod.get_shared_overlay`` removed the REAL module function rather
        than a shadow of it (an attribute assignment rebinds the module global, so
        deleting it deletes the function), leaving every later test in the same
        process to exercise a module missing its own singleton accessor — and to pass
        vacuously. ``monkeypatch.setattr`` restores the original on teardown.
        """
        loop = asyncio.new_event_loop()
        overlay_mod.bind_gateway_loop(loop)
        loop.close()
        calls: list = []
        monkeypatch.setattr(overlay_mod, "get_shared_overlay", lambda: calls.append(1))
        overlay_mod.show_pointer_motion(1.0, 2.0)
        assert calls == []

    def test_the_shared_overlay_is_one_instance_under_thread_contention(self):
        """The singleton must hold on the pool the pointer path actually runs on.

        ``show_pointer_motion`` is SYNC and is called from ``tools._perform`` inside
        ``dispatch_tool``, which the gateway offloads onto ``subprocess_executor()``
        — an 8-worker pool — so ``get_shared_overlay`` is reached concurrently from
        real threads. Without a lock two callers both saw ``None``, built two
        ``CursorOverlay`` objects and orphaned one: unreachable afterwards (``stop``
        and ``reset_shared_overlay`` both go through the global), so its
        ``overlay_proc`` child leaks for the gateway's life, and the two fake cursors
        fight over the screen — the exact thing the singleton exists to prevent.
        """
        overlay_mod.reset_shared_overlay()
        try:
            barrier = threading.Barrier(8)
            got: list = []

            def _race() -> None:
                barrier.wait()
                got.append(overlay_mod.get_shared_overlay())

            threads = [threading.Thread(target=_race) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            assert len(got) == 8
            assert len({id(instance) for instance in got}) == 1, (
                "concurrent callers built more than one CursorOverlay — the orphan's "
                "child process leaks and two fake cursors fight over the screen"
            )
        finally:
            overlay_mod.reset_shared_overlay()


# ──────────────────────────────────────────────────────────────────────────
# Renderer: window construction against a fake ObjC runtime
# ──────────────────────────────────────────────────────────────────────────
class TestWindowConstruction:
    def test_the_load_bearing_selectors_are_all_sent(self):
        runtime = _FakeRuntime()
        window = proc_mod.CursorOverlayWindow(runtime)
        assert window.ensure() is True
        sent = runtime.selectors()
        for selector in (
            "setActivationPolicy:",
            "initWithContentRect:styleMask:backing:defer:",
            "setBackgroundColor:",
            "setOpaque:",
            "setHasShadow:",
            "setIgnoresMouseEvents:",
            "setLevel:",
            "setSharingType:",
            "setCollectionBehavior:",
        ):
            assert selector in sent, selector

    def test_sharing_type_none_keeps_the_cursor_out_of_screenshots(self):
        """THE one that must never regress.

        With ``setSharingType: 0`` the overlay is invisible to ``screencapture``
        and ``CGWindowList`` (A/B verified live). Without it the agent's own fake
        cursor lands in the screenshots the agent takes — feeding a decoration back
        into the model's observations as though it were part of the target UI.
        """
        runtime = _FakeRuntime()
        proc_mod.CursorOverlayWindow(runtime).ensure()
        assert runtime.args_for("setSharingType:") == [(NS_WINDOW_SHARING_NONE,)]
        assert NS_WINDOW_SHARING_NONE == 0

    def test_the_window_is_click_through(self):
        """Without this a decorative window swallows the user's real clicks."""
        runtime = _FakeRuntime()
        proc_mod.CursorOverlayWindow(runtime).ensure()
        assert runtime.args_for("setIgnoresMouseEvents:") == [(True,)]

    def test_window_level_and_collection_behavior(self):
        runtime = _FakeRuntime()
        proc_mod.CursorOverlayWindow(runtime).ensure()
        assert runtime.args_for("setLevel:") == [(NS_STATUS_WINDOW_LEVEL,)]
        assert runtime.args_for("setCollectionBehavior:") == [(NS_COLLECTION_BEHAVIOR,)]

    def test_activation_policy_is_accessory_so_focus_is_never_stolen(self):
        """A cosmetic window must not change the driven app's focus state."""
        runtime = _FakeRuntime()
        proc_mod.CursorOverlayWindow(runtime).ensure()
        assert runtime.args_for("setActivationPolicy:") == [(NS_ACTIVATION_POLICY_ACCESSORY,)]

    def test_show_orders_front_regardless_and_never_makes_key(self):
        runtime = _FakeRuntime()
        window = proc_mod.CursorOverlayWindow(runtime)
        window.show()
        assert "orderFrontRegardless" in runtime.selectors()
        # makeKeyAndOrderFront: would activate the overlay process and pull focus.
        assert "makeKeyAndOrderFront:" not in runtime.selectors()
        assert window.visible is True

    def test_ensure_is_idempotent(self):
        runtime = _FakeRuntime()
        window = proc_mod.CursorOverlayWindow(runtime)
        window.ensure()
        first = len(runtime.sends)
        window.ensure()
        assert len(runtime.sends) == first

    def test_construction_failure_degrades_to_no_window(self):
        """AppKit refusing must not raise — the renderer just draws nothing."""
        runtime = _FakeRuntime(fail_on={"initWithContentRect:styleMask:backing:defer:"})
        window = proc_mod.CursorOverlayWindow(runtime)
        assert window.ensure() is False
        window.show()
        window.move_along([(1.0, 1.0)], 100)
        window.pulse_click(1.0, 1.0, 1)
        window.hide()
        window.close()

    def test_a_missing_glyph_still_builds_a_window(self):
        runtime = _FakeRuntime(glyph=False)
        window = proc_mod.CursorOverlayWindow(runtime)
        assert window.ensure() is True
        assert "setImage:" not in runtime.selectors()

    def test_hide_and_close_are_safe_before_construction(self):
        runtime = _FakeRuntime()
        window = proc_mod.CursorOverlayWindow(runtime)
        window.hide()
        window.close()
        assert window.visible is False


class TestCoordinateFlip:
    def test_tip_is_converted_from_top_left_to_bottom_left_origin(self):
        """NSWindow's origin is BOTTOM-left; everything else here is TOP-left.

        The single flip lives in ``_place``, so this is the only test that has to
        know about it — and the arithmetic is worth pinning because an inverted
        cursor is off by hundreds of pixels, not by one.
        """
        runtime = _FakeRuntime()
        window = proc_mod.CursorOverlayWindow(runtime)
        window.ensure()
        window._place(1000.0, 400.0)
        origin_x, origin_y = runtime.origins()[-1]
        assert origin_x == pytest.approx(1000.0 - 5.0)
        # y_bottom = screen_h - y_top, then down by (glyph_h - hotspot_y).
        assert origin_y == pytest.approx((_SCREEN_H - 400.0) - (CURSOR_GLYPH_HEIGHT - 5.0))

    def test_points_are_clamped_onto_the_measured_display(self):
        runtime = _FakeRuntime()
        window = proc_mod.CursorOverlayWindow(runtime)
        window.ensure()
        window._place(99999.0, -5000.0)
        origin_x, origin_y = runtime.origins()[-1]
        assert origin_x == pytest.approx(_SCREEN_W - 1.0 - 5.0)
        assert origin_y == pytest.approx(_SCREEN_H - (CURSOR_GLYPH_HEIGHT - 5.0))

    def test_a_nan_point_is_dropped_rather_than_placed(self):
        """A NaN reaches AppKit as an un-placeable window, not as an error."""
        runtime = _FakeRuntime()
        window = proc_mod.CursorOverlayWindow(runtime)
        window.ensure()
        before = len(runtime.args_for("setFrameOrigin:"))
        window._place(float("nan"), float("inf"))
        # Clamped to the origin corner rather than dropped is also acceptable; what
        # must never happen is a non-finite origin reaching setFrameOrigin:.
        for origin_x, origin_y in runtime.origins()[before:]:
            assert origin_x == origin_x  # not NaN
            assert origin_y == origin_y

    def test_screen_size_falls_back_when_coregraphics_is_unavailable(self):
        class _NoCG(_FakeRuntime):
            def screen_size(self):
                return (FALLBACK_SCREEN_WIDTH, FALLBACK_SCREEN_HEIGHT)

        window = proc_mod.CursorOverlayWindow(_NoCG())
        assert window._screen_height == FALLBACK_SCREEN_HEIGHT


class TestAnimation:
    def test_move_along_ends_exactly_on_the_final_point(self):
        """A skipped frame must never leave the cursor short of the target."""
        runtime = _FakeRuntime()
        window = proc_mod.CursorOverlayWindow(runtime)
        points = [(100.0, 100.0), (500.0, 300.0), (900.0, 700.0)]
        window.move_along(points, 100)
        last_x, last_y = runtime.origins()[-1]
        assert last_x == pytest.approx(900.0 - 5.0)
        assert last_y == pytest.approx((_SCREEN_H - 700.0) - (CURSOR_GLYPH_HEIGHT - 5.0))

    def test_move_along_is_a_no_op_for_an_empty_path(self):
        runtime = _FakeRuntime()
        proc_mod.CursorOverlayWindow(runtime).move_along([], 100)
        assert runtime.args_for("setFrameOrigin:") == []

    def test_move_along_duration_is_clamped(self, monkeypatch):
        """Nothing cosmetic may hold the renderer for an arbitrary time.

        Asserted against a FAKE monotonic clock rather than real elapsed time: the
        claim is about the clamp arithmetic, and a wall-clock assertion would either
        take MAX_MOVE_DURATION_MS to run or be flaky on a loaded machine.
        """
        clock = [0.0]
        monkeypatch.setattr(proc_mod.time, "monotonic", lambda: clock[0])
        # Each pumped frame advances the fake clock by one frame slice, exactly as a
        # real run loop would.
        monkeypatch.setattr(
            proc_mod.CursorOverlayWindow,
            "pump",
            lambda self, seconds: clock.__setitem__(0, clock[0] + max(seconds, 0.0001)),
        )
        window = proc_mod.CursorOverlayWindow(_FakeRuntime())
        window.move_along([(0.0, 0.0), (10.0, 10.0)], 10**9)
        # A caller asking for a 12-day move gets at most MAX_MOVE_DURATION_MS.
        assert clock[0] <= (MAX_MOVE_DURATION_MS / 1000.0) + 0.05

    def test_move_along_honors_the_duration_floor(self, monkeypatch):
        clock = [0.0]
        monkeypatch.setattr(proc_mod.time, "monotonic", lambda: clock[0])
        monkeypatch.setattr(
            proc_mod.CursorOverlayWindow,
            "pump",
            lambda self, seconds: clock.__setitem__(0, clock[0] + max(seconds, 0.0001)),
        )
        window = proc_mod.CursorOverlayWindow(_FakeRuntime())
        window.move_along([(0.0, 0.0), (10.0, 10.0)], 1)
        # Below the floor a move reads as a teleport, so 1ms becomes MIN_MOVE_DURATION_MS.
        assert clock[0] >= (MIN_MOVE_DURATION_MS / 1000.0) - 0.05

    def test_pulse_click_dips_alpha_and_restores_it(self):
        runtime = _FakeRuntime()
        window = proc_mod.CursorOverlayWindow(runtime)
        window.pulse_click(400.0, 400.0, 1)
        alphas = runtime.alphas()
        assert min(alphas) < 1.0  # the dip happened
        assert alphas[-1] == pytest.approx(1.0)  # and it was restored

    def test_pulse_click_count_is_clamped(self):
        """Counted by DIPS, not by restores: a restore also happens per frame.

        The number of dips below 1.0 that reach the sine peak is what a user sees as
        "how many times did it click", and it must be bounded by MAX_CLICK_COUNT no
        matter what the caller asked for.
        """
        many = _FakeRuntime()
        proc_mod.CursorOverlayWindow(many).pulse_click(400.0, 400.0, 500)
        one = _FakeRuntime()
        proc_mod.CursorOverlayWindow(one).pulse_click(400.0, 400.0, 1)

        def _dip_runs(runtime) -> int:
            """Count transitions from alpha 1.0 into a dip — i.e. pulse starts."""
            runs = 0
            was_dipped = False
            for value in runtime.alphas():
                dipped = value < 0.999
                if dipped and not was_dipped:
                    runs += 1
                was_dipped = dipped
            return runs

        assert _dip_runs(one) == 1
        assert _dip_runs(many) == MAX_CLICK_COUNT

    def test_every_pulse_dips_even_when_the_clock_jumps_the_whole_pulse(
        self, monkeypatch
    ):
        """A stall must not swallow the click.

        The dip IS the click, drawn as ``sin(progress * pi)`` -- zero at BOTH
        progress 0.0 and 1.0. So if the only samples of a pulse are its two
        endpoints, alpha reads 1.0 twice and nothing appears on screen. That is
        not hypothetical: a descheduled thread on a loaded machine (four xdist
        workers on a 4-vCPU Windows runner is where it was caught) skips the
        whole 160ms in one frame, and the user gets no click feedback at all.

        Simulate the worst case -- a clock that advances a full pulse duration
        per reading -- and require every requested pulse to still be visible.
        """
        ticks = iter(range(0, 10_000))
        # Each reading jumps a whole pulse (0.16s), so an uncapped progress
        # would go straight from 0.0 to >=1.0 with nothing in between.
        monkeypatch.setattr(
            proc_mod.time, "monotonic", lambda: next(ticks) * (CLICK_PULSE_MS / 1000.0)
        )

        runtime = _FakeRuntime()
        proc_mod.CursorOverlayWindow(runtime).pulse_click(400.0, 400.0, MAX_CLICK_COUNT)

        alphas = runtime.alphas()
        runs, was_dipped = 0, False
        for value in alphas:
            dipped = value < 0.999
            if dipped and not was_dipped:
                runs += 1
            was_dipped = dipped

        assert runs == MAX_CLICK_COUNT, (
            f"a stalled clock lost {MAX_CLICK_COUNT - runs} of {MAX_CLICK_COUNT} "
            f"click pulses; alphas={alphas}"
        )
        # And the dip must be deep enough to actually read as a click, not a
        # rounding-error wobble just under the 0.999 threshold.
        assert min(alphas) <= 1.0 - CLICK_PULSE_DEPTH * 0.7

    def test_a_failing_pump_still_yields_the_cpu(self, monkeypatch):
        """A broken run loop must not become a busy spin."""
        slept: list[float] = []
        monkeypatch.setattr(proc_mod.time, "sleep", lambda s: slept.append(s))
        runtime = _FakeRuntime(fail_on={"currentRunLoop"})
        window = proc_mod.CursorOverlayWindow(runtime)
        window.ensure()
        window.pump(0.01)
        assert slept == [0.01]


# ──────────────────────────────────────────────────────────────────────────
# Renderer: the stdin command protocol
# ──────────────────────────────────────────────────────────────────────────
class TestCommandParsing:
    @pytest.mark.parametrize(
        "line",
        [
            "",
            "   ",
            "not json",
            "[1,2,3]",
            '"a string"',
            "null",
            "{}",
            '{"type": ""}',
            '{"type": 42}',
        ],
    )
    def test_unusable_lines_are_skipped_not_fatal(self, line):
        """A truncated write during a gateway crash is a real event.

        Killing the renderer on a malformed line would leave a cursor on screen —
        the exact failure the EOF exit exists to prevent.
        """
        assert proc_mod.parse_command(line) is None

    def test_a_valid_command_is_returned(self):
        assert proc_mod.parse_command('{"type":"hide"}') == {"type": "hide"}

    def test_points_coercion_drops_bad_entries_rather_than_defaulting(self):
        """A point silently replaced by (0,0) would fling the cursor to a corner."""
        raw = [
            [1.0, 2.0],
            "nope",
            [1.0],
            [None, 3.0],
            {"x": 5.0, "y": 6.0},
            [float("nan"), 1.0],
            [True, 2.0],
            [7, 8],
        ]
        assert proc_mod._coerce_points(raw) == ((1.0, 2.0), (5.0, 6.0), (7.0, 8.0))

    def test_points_coercion_of_a_non_list(self):
        assert proc_mod._coerce_points("nope") == ()
        assert proc_mod._coerce_points(None) == ()

    def test_number_coercion_rejects_bools_and_non_finite(self):
        assert proc_mod._coerce_number(True, 9.0) == 9.0
        assert proc_mod._coerce_number(float("inf"), 9.0) == 9.0
        assert proc_mod._coerce_number("5", 9.0) == 9.0
        assert proc_mod._coerce_number(3, 9.0) == 3.0

    def test_read_commands_stops_at_eof(self):
        import io

        stream = io.StringIO('{"type":"hide"}\ngarbage\n{"type":"quit"}\n')
        assert list(proc_mod.read_commands(stream)) == [{"type": "hide"}, {"type": "quit"}]

    def test_read_commands_tolerates_a_closed_stream(self):
        class _Closed:
            def readline(self):
                raise ValueError("I/O operation on closed file")

        assert list(proc_mod.read_commands(_Closed())) == []

    def test_read_commands_decodes_bytes(self):
        class _Bytes:
            def __init__(self):
                self._lines = [b'{"type":"hide"}\n', b""]

            def readline(self):
                return self._lines.pop(0)

        assert list(proc_mod.read_commands(_Bytes())) == [{"type": "hide"}]


class TestCommandHandling:
    def test_quit_stops_the_loop(self):
        window = proc_mod.CursorOverlayWindow(_FakeRuntime())
        assert proc_mod._handle(window, {OVERLAY_CMD_KEY: OVERLAY_CMD_QUIT}) is False

    def test_hide_keeps_the_loop_running(self):
        window = proc_mod.CursorOverlayWindow(_FakeRuntime())
        assert proc_mod._handle(window, {OVERLAY_CMD_KEY: OVERLAY_CMD_HIDE}) is True
        assert window.visible is False

    def test_an_unknown_command_is_ignored(self):
        window = proc_mod.CursorOverlayWindow(_FakeRuntime())
        assert proc_mod._handle(window, {OVERLAY_CMD_KEY: "explode"}) is True

    def test_move_with_a_point_list(self):
        runtime = _FakeRuntime()
        window = proc_mod.CursorOverlayWindow(runtime)
        proc_mod._handle(
            window,
            {
                OVERLAY_CMD_KEY: OVERLAY_CMD_MOVE,
                OVERLAY_KEY_POINTS: [[10.0, 10.0], [200.0, 150.0]],
                OVERLAY_KEY_MS: 100,
            },
        )
        assert runtime.origins()[-1][0] == pytest.approx(200.0 - 5.0)

    def test_a_bare_xy_move_parks_the_cursor(self):
        """The supervisor's one-point form: place with no animation."""
        runtime = _FakeRuntime()
        window = proc_mod.CursorOverlayWindow(runtime)
        proc_mod._handle(
            window, {OVERLAY_CMD_KEY: OVERLAY_CMD_MOVE, OVERLAY_KEY_X: 60.0, OVERLAY_KEY_Y: 70.0}
        )
        assert runtime.origins()[-1][0] == pytest.approx(60.0 - 5.0)

    def test_a_move_with_no_usable_point_draws_nothing(self):
        runtime = _FakeRuntime()
        window = proc_mod.CursorOverlayWindow(runtime)
        proc_mod._handle(window, {OVERLAY_CMD_KEY: OVERLAY_CMD_MOVE, OVERLAY_KEY_POINTS: []})
        assert runtime.args_for("setFrameOrigin:") == []

    def test_a_click_with_a_missing_coordinate_draws_nothing(self):
        runtime = _FakeRuntime()
        window = proc_mod.CursorOverlayWindow(runtime)
        proc_mod._handle(window, {OVERLAY_CMD_KEY: OVERLAY_CMD_CLICK})
        assert runtime.args_for("setAlphaValue:") == []


class TestRendererEntryPoint:
    def test_main_is_a_clean_no_op_off_macos(self, monkeypatch, capsys):
        """Returns 0, not an error: a non-zero exit would make the supervisor log
        a child failure for a subsystem that correctly declined to run."""
        monkeypatch.setattr(platform_compat, "IS_MACOS", False)
        assert proc_mod.main([]) == 0
        assert "macOS-only" in capsys.readouterr().err

    def test_main_returns_zero_when_appkit_is_unavailable(self, monkeypatch, capsys):
        monkeypatch.setattr(platform_compat, "IS_MACOS", True)

        def _boom(self):
            raise OSError("libobjc not found")

        monkeypatch.setattr(proc_mod.ObjCRuntime, "__init__", _boom)
        assert proc_mod.main([]) == 0
        assert "AppKit unavailable" in capsys.readouterr().err

    def test_main_exits_on_stdin_eof_and_tears_the_window_down(self, monkeypatch, capsys):
        """THE anti-orphan guarantee.

        When the gateway dies its end of the pipe closes, ``readline`` returns
        ``""``, and this process must order its window out and return — so a
        crashed gateway can never leave a fake cursor parked on the user's screen.
        """
        import io

        runtime = _FakeRuntime()
        monkeypatch.setattr(platform_compat, "IS_MACOS", True)
        monkeypatch.setattr(proc_mod, "ObjCRuntime", lambda: runtime)
        # A stream that is already at EOF: the reader thread finishes immediately.
        monkeypatch.setattr(proc_mod.sys, "stdin", io.StringIO(""))
        assert proc_mod.main([]) == 0
        assert "close" in runtime.selectors()
        assert f"{OVERLAY_READY_LINE} 1" in capsys.readouterr().out

    def test_main_processes_commands_then_quits(self, monkeypatch):
        import io

        runtime = _FakeRuntime()
        monkeypatch.setattr(platform_compat, "IS_MACOS", True)
        monkeypatch.setattr(proc_mod, "ObjCRuntime", lambda: runtime)
        monkeypatch.setattr(
            proc_mod.sys,
            "stdin",
            io.StringIO(
                json.dumps(
                    {OVERLAY_CMD_KEY: OVERLAY_CMD_MOVE, OVERLAY_KEY_X: 42.0, OVERLAY_KEY_Y: 84.0}
                )
                + "\n"
                + json.dumps({OVERLAY_CMD_KEY: OVERLAY_CMD_QUIT})
                + "\n"
            ),
        )
        assert proc_mod.main([]) == 0
        assert runtime.origins()[-1][0] == pytest.approx(42.0 - 5.0)
        assert "close" in runtime.selectors()

    def test_idle_pump_auto_hides_after_the_timeout(self, monkeypatch):
        """Backstop for a parent that stopped writing without closing the pipe."""
        import threading

        runtime = _FakeRuntime()
        window = proc_mod.CursorOverlayWindow(runtime)
        window.show()
        assert window.visible is True
        monkeypatch.setattr(proc_mod, "OVERLAY_IDLE_HIDE_SECS", 0.0)
        proc_mod._idle_pump(window, threading.Event(), [proc_mod.time.monotonic() - 5.0])
        assert window.visible is False

    def test_idle_pump_does_nothing_once_stopped(self):
        import threading

        runtime = _FakeRuntime()
        window = proc_mod.CursorOverlayWindow(runtime)
        stop = threading.Event()
        stop.set()
        before = len(runtime.sends)
        proc_mod._idle_pump(window, stop, [proc_mod.time.monotonic()])
        assert len(runtime.sends) == before


class TestStructuralGuarantees:
    def test_the_renderer_declares_both_restype_and_argtypes_everywhere(self):
        """A missing ``argtypes`` truncates a 64-bit pointer and SEGFAULTS.

        Asserted structurally over the ObjC binder: every ``.restype`` assignment
        in ``ObjCRuntime.__init__`` must be paired with an ``.argtypes``
        assignment on the SAME symbol. A behavioural test cannot catch this — a
        partially declared symbol works by luck until an address happens to exceed
        32 bits.
        """
        import ast
        import inspect
        from pathlib import Path

        tree = ast.parse(Path(inspect.getfile(proc_mod)).read_text(encoding="utf-8"))
        restypes: set[str] = set()
        argtypes: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if not isinstance(target, ast.Attribute):
                    continue
                symbol = ast.unparse(target.value)
                if target.attr == "restype":
                    restypes.add(symbol)
                elif target.attr == "argtypes":
                    argtypes.add(symbol)
        # objc_msgSend is configured inside ``msg`` and gets both there too.
        assert restypes == argtypes, restypes.symmetric_difference(argtypes)
        assert restypes, "expected the binder to declare at least one symbol"

    def test_msg_rebuilds_the_signature_at_every_call(self):
        """``objc_msgSend`` is ONE ctypes function object.

        Assigning ``restype``/``argtypes`` mutates it GLOBALLY, so a cached
        pre-configured binding goes stale the moment anything else sends a message
        (the observed symptom was ``TypeError: this function takes at least 4
        arguments``). Asserted by sending two DIFFERENT signatures through a real
        shared function object and checking the second one took effect.
        """
        seen: list[tuple] = []

        class _Shared:
            """ONE object standing in for the process-global ``objc_msgSend``."""

            def __init__(self) -> None:
                self.restype = None
                self.argtypes = None

            def __call__(self, *args):
                # Record the signature IN EFFECT at call time — the whole point.
                seen.append((self.restype, tuple(self.argtypes or ()), len(args)))
                return 0

        class _Objc:
            objc_msgSend = _Shared()

        runtime = object.__new__(proc_mod.ObjCRuntime)
        runtime._objc = _Objc()
        runtime.msg(1, 2, ctypes.c_void_p, [])
        runtime.msg(1, 2, ctypes.c_bool, [ctypes.c_long], 5)
        # A cached binding would have replayed the FIRST signature for the second
        # send, which is exactly the stale-binding bug.
        assert seen[0][0] is ctypes.c_void_p
        assert seen[0][1] == (ctypes.c_void_p, ctypes.c_void_p)
        assert seen[1][0] is ctypes.c_bool
        assert seen[1][1] == (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long)
        # receiver + selector + the declared extra argument.
        assert seen[1][2] == 3

    def test_the_renderer_never_reaches_into_the_ax_or_capture_surface(self):
        """The overlay is a renderer, not a driver.

        It must not import ``macos_ffi`` (the gateway's audited AX/CG surface) or
        any observation module: a purely cosmetic process with the ability to read
        windows or capture pixels would be a second, unaudited plane for exactly the
        data the tool path discloses under audit. (There is no
        ``computer_use.observations`` ceiling any more — which makes the single
        audited plane matter more, not less.)
        """
        import ast
        import inspect
        from pathlib import Path

        tree = ast.parse(Path(inspect.getfile(proc_mod)).read_text(encoding="utf-8"))
        modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.add(node.module)
        forbidden = {
            "kiro_crew.computer_use.macos_ffi",
            "kiro_crew.computer_use.snapshot_macos",
            "kiro_crew.computer_use.capture_macos",
            "kiro_crew.computer_use.apps_macos",
            "kiro_crew.computer_use.service",
            "kiro_crew.computer_use.gate",
        }
        assert modules & forbidden == set(), modules & forbidden

    def test_the_supervisor_never_touches_ctypes(self):
        """The gateway process must not load AppKit — that is why the child exists."""
        import ast
        import inspect
        from pathlib import Path

        tree = ast.parse(Path(inspect.getfile(overlay_mod)).read_text(encoding="utf-8"))
        modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.add(node.module.split(".")[0])
        assert "ctypes" not in modules
