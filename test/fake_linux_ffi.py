"""Per-test isolation of ``kiro_crew.computer_use.linux_ffi`` for the reading-layer tests.

The reading layer (``apps_linux`` / ``snapshot_linux`` / ``capture_linux``) is tested
against an in-memory AT-SPI world, never a real bus or X server. Every ffi function
those modules call is therefore replaced, FOR THE DURATION OF ONE TEST, with a stub
that raises ``AssertionError`` naming itself — so a test that forgets to wire a
function gets a loud failure rather than a silent wrong answer, and so no test can
reach native code with a fabricated handle (on a host with ``DISPLAY`` set that is a
segfault, not an exception). Individual tests then ``monkeypatch`` the functions they
exercise onto the same module.

This used to install a fake MODULE into ``sys.modules`` before the real ``linux_ffi``
existed. That is no longer possible: the swap leaked into every other test collected
on the same xdist worker (``test_computer_use_linux_ffi.py`` then saw a module with no
``reset_libraries``), and the real module carries every constant and function the
contract names, so patching its attributes is both sufficient and contained.
"""

from __future__ import annotations

import pytest

from kiro_crew.computer_use import linux_ffi

# Every function name the reading layer calls, defaulting to a loud raise.
_FUNCTIONS = (
    "available",
    "unavailable_reason",
    "display_name",
    "shutdown",
    "desktop",
    "child_count",
    "child_at",
    "parent",
    "name",
    "description",
    "role",
    "role_name",
    "pid",
    "toolkit",
    "states",
    "has_state",
    "extents",
    "actions",
    "do_action",
    "text",
    "value",
    "set_text_contents",
    "insert_text",
    "grab_focus",
    "unref",
    "generate_keyboard_event",
    "generate_mouse_event",
    "x_root",
    "x_screen_size",
    "x_client_list",
    "x_active_window",
    "x_window_pid",
    "x_window_title",
    "x_window_geometry",
    "x_gtk_frame_extents",
    "x_window_is_viewable",
    "x_window_at",
    "x_get_image_rgb",
    "encode_jpeg_rgb",
)


def _not_wired(name: str):
    def _raise(*_a, **_k):
        raise AssertionError(f"linux_ffi.{name} was called but not wired by the test")

    return _raise


def isolate_linux_ffi(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace every contract function on the REAL module with a loud stub, for one test.

    ``unref`` is called in ``finally`` blocks even on error paths, so it is a harmless
    no-op by default rather than a raise. Constants (``ATSPI_ROLE_*`` etc.) are left as
    the real module defines them — they are the pinned enum values the walkers compare
    against, and a test that needs a different one patches it explicitly.
    """
    for fn in _FUNCTIONS:
        assert hasattr(linux_ffi, fn), f"contract drift: linux_ffi has no {fn!r}"
        monkeypatch.setattr(linux_ffi, fn, _not_wired(fn))
    monkeypatch.setattr(linux_ffi, "unref", lambda handle: None)


@pytest.fixture(autouse=True)
def _isolated_linux_ffi(monkeypatch: pytest.MonkeyPatch) -> None:
    """Autouse in every module that imports this helper: see :func:`isolate_linux_ffi`."""
    isolate_linux_ffi(monkeypatch)
