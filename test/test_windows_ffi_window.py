"""``computer_use.windows_ffi_window`` — the per-window reads and the hit test, on every platform.

Each read reaches ``user32``, ``kernel32`` and ``psapi`` only through
``windows_ffi._libraries``, so a fake library patched on the facade drives the real
body on a Linux or macOS shard. These pin what each read returns when the API succeeds
and for each failure it reports: an empty answer, never a raised error, and never a
handle left open. :func:`window_list` builds a ``WINFUNCTYPE`` callback and is covered
on Windows by ``test_computer_use_windows_ffi.py``.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from kiro_crew.computer_use import windows_ffi as ffi


class _Lib:
    """A fake DLL whose attributes are the functions it was built with."""

    def __init__(self, **functions: object) -> None:
        self.__dict__.update(functions)


def _install(monkeypatch: pytest.MonkeyPatch, **libs: _Lib) -> None:
    monkeypatch.setattr(ffi, "_libraries", lambda: libs)


class TestWindowText:
    def test_a_titled_window_returns_its_title(self, monkeypatch) -> None:
        seen: dict[str, int] = {}

        def get_text(handle, buf, size):
            seen["handle"], seen["size"] = handle.value, size
            buf.value = "Notes"
            return 5

        _install(
            monkeypatch, user32=_Lib(GetWindowTextLengthW=lambda h: 5, GetWindowTextW=get_text)
        )
        assert ffi.window_text(0x1234) == "Notes"
        # The buffer holds the title AND its terminator, or the last character is cut.
        assert seen == {"handle": 0x1234, "size": 6}

    def test_an_untitled_window_is_empty_and_its_text_is_never_read(self, monkeypatch) -> None:
        def get_text(*args):
            raise AssertionError("GetWindowTextW must not run for a zero-length title")

        _install(
            monkeypatch, user32=_Lib(GetWindowTextLengthW=lambda h: 0, GetWindowTextW=get_text)
        )
        assert ffi.window_text(7) == ""


class TestWindowClass:
    def test_the_class_name_is_returned(self, monkeypatch) -> None:
        def get_class(handle, buf, size):
            buf.value = "Chrome_WidgetWin_1"
            return len(buf.value)

        _install(monkeypatch, user32=_Lib(GetClassNameW=get_class))
        assert ffi.window_class(9) == "Chrome_WidgetWin_1"

    def test_a_failed_read_is_empty(self, monkeypatch) -> None:
        _install(monkeypatch, user32=_Lib(GetClassNameW=lambda handle, buf, size: 0))
        assert ffi.window_class(9) == ""


class TestWindowPidAndRoot:
    def test_the_pid_comes_from_the_window(self, monkeypatch) -> None:
        def thread_process_id(handle, pid_ref):
            pid_ref._obj.value = 4242
            return 1

        _install(monkeypatch, user32=_Lib(GetWindowThreadProcessId=thread_process_id))
        assert ffi.window_pid(11) == 4242

    def test_the_root_is_the_ancestor_the_api_names(self, monkeypatch) -> None:
        seen: list[int] = []

        def ancestor(handle, flags):
            seen.append(flags)
            return 0x77

        _install(monkeypatch, user32=_Lib(GetAncestor=ancestor))
        assert ffi.window_root(0x10) == 0x77
        assert seen == [ffi.GA_ROOT]

    def test_a_window_with_no_ancestor_is_its_own_root(self, monkeypatch) -> None:
        _install(monkeypatch, user32=_Lib(GetAncestor=lambda handle, flags: 0))
        assert ffi.window_root(0x10) == 0x10


class TestWindowBounds:
    def test_the_rect_becomes_left_top_width_height(self, monkeypatch) -> None:
        def window_rect(handle, rect_ref):
            rect = rect_ref._obj
            rect.left, rect.top, rect.right, rect.bottom = 10, 20, 310, 420
            return 1

        _install(monkeypatch, user32=_Lib(GetWindowRect=window_rect))
        assert ffi.window_bounds(5) == (10.0, 20.0, 300.0, 400.0)

    def test_a_failed_read_is_none_rather_than_a_rect_at_the_origin(self, monkeypatch) -> None:
        _install(monkeypatch, user32=_Lib(GetWindowRect=lambda handle, rect_ref: 0))
        assert ffi.window_bounds(5) is None


class TestExeName:
    def _libs(self, monkeypatch, *, handle: int, path: str, written: int) -> list[str]:
        calls: list[str] = []

        def open_process(access, inherit, pid):
            calls.append(f"open:{access}:{inherit}:{pid}")
            return handle

        def module_file_name(process, module, buf, size):
            calls.append(f"name:{process}:{size}")
            buf.value = path
            return written

        def close_handle(process):
            calls.append(f"close:{process}")
            return 1

        _install(
            monkeypatch,
            kernel32=_Lib(OpenProcess=open_process, CloseHandle=close_handle),
            psapi=_Lib(GetModuleFileNameExW=module_file_name),
        )
        return calls

    def test_the_image_name_is_the_last_path_component(self, monkeypatch) -> None:
        calls = self._libs(
            monkeypatch, handle=0x50, path="C:\\Program Files\\App\\app.exe", written=29
        )
        assert ffi._exe_name(1234) == "app.exe"
        access = ffi.PROCESS_QUERY_LIMITED_INFORMATION
        assert calls == [f"open:{access}:False:1234", "name:80:512", "close:80"]

    def test_a_forward_slash_path_splits_the_same_way(self, monkeypatch) -> None:
        self._libs(monkeypatch, handle=0x50, path="C:/tools/bin/tool.exe", written=21)
        assert ffi._exe_name(1) == "tool.exe"

    def test_a_process_that_cannot_be_opened_is_empty(self, monkeypatch) -> None:
        calls = self._libs(monkeypatch, handle=0, path="unused", written=6)
        assert ffi._exe_name(1) == ""
        assert [c for c in calls if not c.startswith("open:")] == []

    def test_a_failed_name_read_is_empty_and_still_closes_the_handle(self, monkeypatch) -> None:
        calls = self._libs(monkeypatch, handle=0x50, path="", written=0)
        assert ffi._exe_name(1) == ""
        assert calls[-1] == "close:80"


class TestRootWindowAtPoint:
    def _libs(self, monkeypatch, *, under: int, root: int) -> list[object]:
        seen: list[object] = []

        @contextmanager
        def scope():
            seen.append("enter")
            yield
            seen.append("exit")

        def window_from_point(point):
            seen.append((point.x, point.y))
            return under

        def ancestor(handle, flags):
            seen.append("ancestor")
            return root

        monkeypatch.setattr(ffi, "dpi_awareness_scope", scope)
        _install(monkeypatch, user32=_Lib(WindowFromPoint=window_from_point, GetAncestor=ancestor))
        return seen

    def test_the_point_is_floored_and_read_inside_the_dpi_scope(self, monkeypatch) -> None:
        seen = self._libs(monkeypatch, under=0x10, root=0x20)
        assert ffi.root_window_at_point(-100.5, 20.9) == 0x20
        # Read inside ONE awareness scope, at the SAME pixel the click normalizes:
        # floor, never truncation toward zero.
        assert seen == ["enter", (-101, 20), "ancestor", "exit"]

    def test_nothing_under_the_point_is_zero(self, monkeypatch) -> None:
        self._libs(monkeypatch, under=0, root=0x20)
        assert ffi.root_window_at_point(5.0, 5.0) == 0

    def test_a_point_on_a_top_level_window_is_that_window(self, monkeypatch) -> None:
        self._libs(monkeypatch, under=0x10, root=0)
        assert ffi.root_window_at_point(5.0, 5.0) == 0x10


class TestDpiScopeEdges:
    def test_a_host_without_the_setter_runs_the_block_unchanged(self, monkeypatch) -> None:
        _install(monkeypatch, user32=_Lib())
        ran: list[int] = []
        with ffi.dpi_awareness_scope():
            ran.append(1)
        assert ran == [1]

    def test_a_failed_restore_is_swallowed_after_the_block_ran(self, monkeypatch) -> None:
        calls: list[object] = []

        def setter(context):
            calls.append(context)
            if len(calls) > 1:
                raise OSError("restore failed")
            return 0x99

        _install(monkeypatch, user32=_Lib(SetThreadDpiAwarenessContext=setter))
        monkeypatch.setattr(ffi._ct, "set_last_error", lambda code: None, raising=False)
        ran: list[int] = []
        with ffi.dpi_awareness_scope():
            ran.append(1)
        assert ran == [1]
        assert calls[-1] == 0x99
