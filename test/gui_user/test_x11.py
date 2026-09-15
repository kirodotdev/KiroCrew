"""Unit tests for the pure half of the X11 backend (no display, no xdotool)."""

from __future__ import annotations

from pathlib import Path

import pytest
from gui_user import x11


class TestGeometry:
    def test_scale_and_shot_height_follow_the_real_aspect_ratio(self) -> None:
        geo = x11.Geometry(1600, 1000, 1280)
        assert geo.scale == pytest.approx(1.25)
        assert geo.shot_h == 800

    def test_to_real_scales_and_rounds(self) -> None:
        geo = x11.Geometry(1600, 1000, 1280)
        assert geo.to_real(640, 400) == (800, 500)
        assert geo.to_real(0, 0) == (0, 0)
        assert geo.to_real(1279, 799) == (1599, 999)

    def test_to_real_clamps_off_screen_estimates(self) -> None:
        # The model's estimate can overshoot; land on the edge, never raise.
        geo = x11.Geometry(1600, 1000, 1280)
        assert geo.to_real(-5, 5000) == (0, 999)
        assert geo.to_real(99999, -1) == (1599, 0)

    def test_identity_when_shot_width_equals_real(self) -> None:
        geo = x11.Geometry(1280, 800, 1280)
        assert geo.scale == 1
        assert geo.to_real(10, 20) == (10, 20)

    def test_never_upscales(self) -> None:
        with pytest.raises(ValueError):
            x11.Geometry(1024, 768, 1280)

    @pytest.mark.parametrize(
        "spec,expected",
        [("1600x1000", (1600, 1000)), ("1600x1000x24", (1600, 1000)), (" 800 x 600 ", (800, 600))],
    )
    def test_parse_screen(self, spec: str, expected: tuple[int, int]) -> None:
        assert x11.parse_screen(spec) == expected

    def test_parse_screen_rejects_garbage(self) -> None:
        with pytest.raises(ValueError):
            x11.parse_screen("wide")


class TestDisplayGuard:
    @pytest.mark.parametrize(
        "display", [":0", ":1", ":0.0", "localhost:0", "host.example:1.0", "", "  "]
    )
    def test_refuses_real_or_empty_displays(self, display: str) -> None:
        with pytest.raises(x11.X11Error):
            x11.refuse_real_display(display)

    @pytest.mark.parametrize("display", [":99", ":10", ":2", "localhost:99.0", "unix:99"])
    def test_accepts_private_virtual_displays(self, display: str) -> None:
        x11.refuse_real_display(display)

    @pytest.mark.parametrize("display", ["otherhost:99", "10.0.0.5:2", "ninety-nine", ":x"])
    def test_refuses_remote_or_malformed_displays(self, display: str) -> None:
        with pytest.raises(x11.X11Error):
            x11.refuse_real_display(display)

    def test_display_number(self) -> None:
        assert x11.display_number(":99") == 99
        assert x11.display_number("localhost:12.0") == 12

    def test_server_process_name_reads_lock_then_comm(self, tmp_path: Path) -> None:
        lock_dir = tmp_path / "tmp"
        proc = tmp_path / "proc"
        lock_dir.mkdir()
        (proc / "4242").mkdir(parents=True)
        (lock_dir / ".X99-lock").write_text("      4242\n", encoding="ascii")
        (proc / "4242" / "comm").write_text("Xvfb\n", encoding="utf-8")
        assert x11.server_process_name(":99", lock_dir=lock_dir, proc=proc) == "Xvfb"
        with pytest.raises(x11.X11Error, match="lock file"):
            x11.server_process_name(":98", lock_dir=lock_dir, proc=proc)
        (lock_dir / ".X97-lock").write_text("31337\n", encoding="ascii")
        with pytest.raises(x11.X11Error, match="no readable process"):
            x11.server_process_name(":97", lock_dir=lock_dir, proc=proc)

    @pytest.mark.parametrize("server", sorted(x11.VIRTUAL_X_SERVERS))
    def test_verify_accepts_memory_only_servers(self, server: str) -> None:
        assert x11.verify_virtual_display(":99", server_name=lambda _d: server) == server

    @pytest.mark.parametrize("server", ["Xorg", "X", "Xwayland", "gnome-shell", ""])
    def test_verify_refuses_real_servers_whatever_the_number(self, server: str) -> None:
        with pytest.raises(x11.X11Error, match="refusing to drive a real screen"):
            x11.verify_virtual_display(":2", server_name=lambda _d: server)

    def test_verify_runs_the_number_check_first(self) -> None:
        calls: list[str] = []

        def resolver(d: str) -> str:
            calls.append(d)
            return "Xvfb"

        with pytest.raises(x11.X11Error):
            x11.verify_virtual_display(":0", server_name=resolver)
        assert calls == []


class TestNormalizeKey:
    @pytest.mark.parametrize(
        "given,expected",
        [
            ("Enter", "Return"),
            ("enter", "Return"),
            ("esc", "Escape"),
            ("Tab", "Tab"),
            ("backspace", "BackSpace"),
            ("Page Down", "Page_Down"),
            ("pagedown", "Page_Down"),
            ("ctrl+l", "ctrl+l"),
            ("Ctrl+A", "ctrl+A"),
            ("cmd+a", "super+a"),
            ("shift+Tab", "shift+Tab"),
            ("F5", "F5"),
            ("f12", "F12"),
            ("Return", "Return"),
            ("a", "a"),
            ("+", "plus"),
            ("ArrowDown", "Down"),
        ],
    )
    def test_aliases_map_to_xdotool_keysyms(self, given: str, expected: str) -> None:
        assert x11.normalize_key(given) == expected

    def test_empty_key_is_an_error(self) -> None:
        with pytest.raises(x11.X11Error):
            x11.normalize_key("   ")


class TestKeyAllowlist:
    @pytest.mark.parametrize(
        "chord,expected",
        [
            ("Return", "Return"),
            ("a", "a"),
            ("Escape", "Escape"),
            ("Page_Down", "Page_Down"),
            ("ctrl+a", "ctrl+a"),
            ("Ctrl+L", "ctrl+l"),
            ("shift+Tab", "shift+Tab"),
            ("ctrl+enter", "ctrl+Return"),
            ("F5", "F5"),
        ],
    )
    def test_plain_keys_and_editing_chords_pass(self, chord: str, expected: str) -> None:
        assert x11.check_key_allowed(chord) == expected

    @pytest.mark.parametrize(
        "chord",
        [
            "ctrl+o",  # file picker
            "ctrl+s",  # save page
            "ctrl+u",  # view source
            "ctrl+shift+i",  # devtools
            "ctrl+shift+j",
            "F12",
            "F11",
            "F1",
            "ctrl+n",  # new window
            "ctrl+t",
            "ctrl+w",
            "ctrl+shift+n",
            "alt+F4",
            "alt+Tab",
            "super",
            "super+a",
            "ctrl+alt+Delete",
            "ctrl+alt+t",
            "ctrl+p",  # print
            "ctrl+h",  # history
            "ctrl+j",  # downloads
            "ctrl+shift+Delete",
            "shift+ctrl+a",  # modifier order is canonicalised before the lookup
        ],
    )
    def test_chords_that_leave_the_page_are_refused(self, chord: str) -> None:
        with pytest.raises(x11.X11Error, match="not on the allowlist|not allowed|exactly one"):
            x11.check_key_allowed(chord)

    def test_build_argv_key_goes_through_the_allowlist(self) -> None:
        with pytest.raises(x11.X11Error, match="not on the allowlist"):
            x11.build_argv("key", {"text": "ctrl+o"}, GEO)
        assert x11.build_argv("key", {"text": "ctrl+l"}, GEO) == [
            ["xdotool", "key", "--", "ctrl+l"]
        ]


GEO = x11.Geometry(1600, 1000, 1280)


class TestBuildArgv:
    def test_left_click_moves_then_clicks_button_one_in_real_pixels(self) -> None:
        argv = x11.build_argv("left_click", {"coordinate": [640, 400]}, GEO)
        assert argv == [["xdotool", "mousemove", "--sync", "800", "500", "click", "1"]]

    def test_right_and_middle_click_buttons(self) -> None:
        assert x11.build_argv("right_click", {"coordinate": [0, 0]}, GEO)[0][-1] == "3"
        assert x11.build_argv("middle_click", {"coordinate": [0, 0]}, GEO)[0][-1] == "2"

    def test_double_and_triple_click_repeat(self) -> None:
        dbl = x11.build_argv("double_click", {"coordinate": [8, 8]}, GEO)[0]
        assert dbl[-5:] == ["--repeat", "2", "--delay", "80", "1"]
        trp = x11.build_argv("triple_click", {"coordinate": [8, 8]}, GEO)[0]
        assert "3" in trp[trp.index("--repeat") + 1]

    def test_drag_is_down_move_up(self) -> None:
        argv = x11.build_argv(
            "left_click_drag", {"start_coordinate": [8, 8], "coordinate": [80, 80]}, GEO
        )
        assert argv == [
            ["xdotool", "mousemove", "--sync", "10", "10", "mousedown", "1"],
            ["xdotool", "mousemove", "--sync", "100", "100", "mouseup", "1"],
        ]

    def test_type_passes_text_after_double_dash(self) -> None:
        argv = x11.build_argv("type", {"text": "--hello"}, GEO)
        assert argv == [["xdotool", "type", "--delay", "20", "--", "--hello"]]

    def test_type_caps_length_and_rejects_empty(self) -> None:
        with pytest.raises(x11.X11Error):
            x11.build_argv("type", {"text": "x" * (x11.MAX_TYPE_CHARS + 1)}, GEO)
        with pytest.raises(x11.X11Error):
            x11.build_argv("type", {"text": ""}, GEO)

    def test_key_normalizes(self) -> None:
        assert x11.build_argv("key", {"text": "enter"}, GEO) == [["xdotool", "key", "--", "Return"]]
        # The native tool spells it `text`; a `key` spelling is accepted as well.
        assert x11.build_argv("key", {"key": "ctrl+l"}, GEO) == [["xdotool", "key", "--", "ctrl+l"]]

    def test_scroll_direction_maps_to_wheel_buttons_and_caps_amount(self) -> None:
        down = x11.build_argv(
            "scroll",
            {"coordinate": [640, 400], "scroll_direction": "down", "scroll_amount": 5},
            GEO,
        )[0]
        assert down[-1] == "5" and down[down.index("--repeat") + 1] == "5"
        up = x11.build_argv("scroll", {"coordinate": [0, 0], "scroll_direction": "up"}, GEO)[0]
        assert up[-1] == "4" and up[up.index("--repeat") + 1] == "3"  # default 3 clicks
        huge = x11.build_argv(
            "scroll", {"coordinate": [0, 0], "scroll_direction": "left", "scroll_amount": 999}, GEO
        )[0]
        assert huge[-1] == "6" and huge[huge.index("--repeat") + 1] == str(x11.MAX_SCROLL_CLICKS)

    def test_scroll_rejects_unknown_direction(self) -> None:
        with pytest.raises(x11.X11Error):
            x11.build_argv("scroll", {"coordinate": [0, 0], "scroll_direction": "sideways"}, GEO)

    def test_screenshot_and_wait_produce_no_commands(self) -> None:
        assert x11.build_argv("screenshot", {}, GEO) == []
        assert x11.build_argv("wait", {"duration": 2}, GEO) == []

    @pytest.mark.parametrize("action", sorted(x11.UNSUPPORTED_ACTIONS))
    def test_unsupported_native_actions_are_structured_errors(self, action: str) -> None:
        with pytest.raises(x11.X11Error, match="not supported"):
            x11.build_argv(action, {}, GEO)

    def test_unknown_action_is_an_error(self) -> None:
        with pytest.raises(x11.X11Error, match="unknown action"):
            x11.build_argv("rm_rf", {}, GEO)

    def test_bad_coordinates_are_errors_not_crashes(self) -> None:
        for bad in ({}, {"coordinate": [1]}, {"coordinate": "12,13"}, {"coordinate": ["a", "b"]}):
            with pytest.raises(x11.X11Error):
                x11.build_argv("left_click", bad, GEO)

    def test_vocabulary_is_exhaustive(self) -> None:
        # Every supported action either builds argv or is one of the two
        # backend-handled ones; nothing silently falls through.
        for action in x11.ACTIONS:
            params = {
                "coordinate": [1, 1],
                "start_coordinate": [0, 0],
                "text": "x",
                "scroll_direction": "down",
            }
            x11.build_argv(action, params, GEO)


def test_wait_seconds_clamps() -> None:
    assert x11.wait_seconds({"duration": 99}) == x11.MAX_WAIT_SECONDS
    assert x11.wait_seconds({"duration": -1}) == 0.0
    assert x11.wait_seconds({}) == 1.0
    assert x11.wait_seconds({"duration": "soon"}) == 1.0


class _FakeImage:
    def __init__(self, size: tuple[int, int]) -> None:
        self.size = size

    def resize(self, size: tuple[int, int]) -> "_FakeImage":
        return _FakeImage(size)

    def convert(self, _mode: str) -> "_FakeImage":
        return self

    def save(self, buf, format: str, optimize: bool) -> None:  # noqa: A002 - PIL's signature
        assert format == "PNG" and optimize
        buf.write(b"\x89PNG-fake-" + f"{self.size[0]}x{self.size[1]}".encode())


class TestDisplay:
    def test_perform_runs_argv_with_the_private_display(self, tmp_path: Path) -> None:
        calls: list[tuple[list[str], str]] = []

        def runner(argv, env):
            calls.append((list(argv), env["DISPLAY"]))

        d = x11.Display(
            ":99",
            GEO,
            tmp_path,
            runner=runner,
            grabber=lambda _d: _FakeImage((1600, 1000)),
            settle_seconds=0,
            server_name=lambda _d: "Xvfb",
        )
        assert d.perform("left_click", {"coordinate": [640, 400]}) == "left_click at [640, 400]"
        assert calls == [(["xdotool", "mousemove", "--sync", "800", "500", "click", "1"], ":99")]

    def test_refuses_a_real_display_at_construction(self, tmp_path: Path) -> None:
        with pytest.raises(x11.X11Error):
            x11.Display(
                ":0", GEO, tmp_path, runner=lambda a, e: None, grabber=lambda _d: _FakeImage((1, 1))
            )

    def test_screenshot_downscales_archives_and_numbers(self, tmp_path: Path) -> None:
        d = x11.Display(
            ":99",
            GEO,
            tmp_path,
            runner=lambda a, e: None,
            grabber=lambda _d: _FakeImage((1600, 1000)),
            server_name=lambda _d: "Xvfb",
        )
        png, path = d.screenshot("Members page!")
        assert png.endswith(b"1280x800")
        assert path.name == "01-Members-page.png"
        assert path.read_bytes() == png
        _, path2 = d.screenshot("second")
        assert path2.name == "02-second.png"

    def test_screenshot_adopts_the_real_server_size(self, tmp_path: Path) -> None:
        # Xvfb came up at a different size than --screen said: trust the pixels.
        d = x11.Display(
            ":99",
            GEO,
            tmp_path,
            runner=lambda a, e: None,
            grabber=lambda _d: _FakeImage((1280, 720)),
            server_name=lambda _d: "Xvfb",
        )
        d.screenshot("s")
        assert d.geo == x11.Geometry(1280, 720, 1280)

    def test_xdotool_failure_surfaces_as_x11error(self, tmp_path: Path) -> None:
        def runner(argv, env):
            raise x11.X11Error("xdotool failed (1): no such window")

        d = x11.Display(
            ":99",
            GEO,
            tmp_path,
            runner=runner,
            grabber=lambda _d: _FakeImage((1600, 1000)),
            server_name=lambda _d: "Xvfb",
        )
        with pytest.raises(x11.X11Error, match="no such window"):
            d.perform("key", {"text": "Return"})
