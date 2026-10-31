"""``computer_use.macos_ffi`` stays the curated surface it was before its split into owners.

``macos_ffi`` is a :mod:`kiro_crew._facade_mirror` facade: every name its owners define is
read and patched as ``macos_ffi.<name>``. Two parts of its surface are not derived from the
owners and are pinned here: the hand-written ``__all__``, which keeps ``from macos_ffi import *``
exporting exactly what it exported before the split rather than every name the owners bind,
and the logger name the whole family logs under, which operators filter on.
"""

from __future__ import annotations

from kiro_crew.computer_use import macos_ffi

#: ``macos_ffi.__all__`` as it stood before the split, frozen, less the AX and scroll
#: names removed when agent-desktop took over the tree.
_BASE_ALL = frozenset(
    {
        "CGPoint",
        "CGRect",
        "CGSize",
        "CG_WINDOW_LAYER_NORMAL",
        "CLICK_PAIR_DELAY_SECS",
        "DRAG_STEPS",
        "DRAG_STEP_DELAY_SECS",
        "K_CG_HID_EVENT_TAP",
        "K_CG_MOUSE_EVENT_CLICK_STATE",
        "Libs",
        "MOUSE_EVENT_TYPES",
        "TypeIds",
        "WindowInfo",
        "available",
        "ax_is_process_trusted",
        "capture_window_jpeg",
        "cf_array_items",
        "cf_is",
        "cf_number_int",
        "cf_string",
        "cf_string_value",
        "event_source",
        "executable_path",
        "frameworks",
        "jpeg_dimensions",
        "mouse_button_codes",
        "post_key",
        "post_mouse_click",
        "post_mouse_drag",
        "post_mouse_drag_global",
        "post_mouse_global",
        "post_text",
        "preflight_screen_capture",
        "reset_frameworks",
        "shots_dir_default",
        "type_ids",
        "window_list",
    }
)


def test_all_is_the_curated_list_it_was_before_the_split() -> None:
    assert len(macos_ffi.__all__) == len(set(macos_ffi.__all__))
    assert set(macos_ffi.__all__) == _BASE_ALL


def test_every_name_in_all_resolves_on_the_facade() -> None:
    assert [name for name in macos_ffi.__all__ if not hasattr(macos_ffi, name)] == []


def test_the_family_logs_under_the_facade_name() -> None:
    assert macos_ffi.logger.name == "kiro_crew.computer_use.macos_ffi"
