"""``computer_use.windows_ffi`` stays the curated surface it was before its split into owners.

``windows_ffi`` is a :mod:`kiro_crew._facade_mirror` facade: every name its owners define is
read and patched as ``windows_ffi.<name>``. Two parts of its surface are not derived from the
owners and are pinned here: the hand-written ``__all__``, which keeps ``from windows_ffi import *``
exporting exactly what it exported before the split rather than every name the owners bind,
and the logger name the whole family logs under, which operators filter on.
"""

from __future__ import annotations

from kiro_crew.computer_use import windows_ffi

#: ``windows_ffi.__all__`` as it stood before the split, frozen.
_BASE_ALL = frozenset(
    {
        "CACHED_WALK_PROPERTIES",
        "E_ACCESSDENIED",
        "ExpandCollapseState_Collapsed",
        "ExpandCollapseState_Expanded",
        "ExpandCollapseState_LeafNode",
        "MAX_WALK_SECS",
        "ScrollAmount_LargeDecrement",
        "ScrollAmount_LargeIncrement",
        "ScrollAmount_NoAmount",
        "UIA_IsPasswordPropertyId",
        "VARIANT",
        "WalkResult",
        "WindowInfo",
        "add_ref",
        "automation",
        "available",
        "bstr_value",
        "cached_prop",
        "create_cache_request",
        "do_default_action",
        "dpi_awareness_scope",
        "element_from_hwnd",
        "element_has_focus",
        "expand_collapse_state",
        "expand_element",
        "invoke_element",
        "is_secure_element",
        "libraries",
        "owned",
        "pattern",
        "pattern_action",
        "prop_value_ex",
        "release",
        "release_all",
        "release_all_clients",
        "reset_libraries",
        "reset_thread_state",
        "root_window_at_point",
        "scroll_element",
        "select_element",
        "set_element_focus",
        "set_element_value",
        "toggle_element",
        "variant_value",
        "walk_bounded",
        "window_bounds",
        "window_class",
        "window_is_live",
        "window_is_minimized",
        "window_is_on_screen",
        "window_list",
        "window_pid",
        "window_render_scale",
        "window_root",
        "window_text",
    }
)


def test_all_is_the_curated_list_it_was_before_the_split() -> None:
    assert len(windows_ffi.__all__) == len(set(windows_ffi.__all__))
    assert set(windows_ffi.__all__) == _BASE_ALL


def test_every_name_in_all_resolves_on_the_facade() -> None:
    assert [name for name in windows_ffi.__all__ if not hasattr(windows_ffi, name)] == []


def test_the_family_logs_under_the_facade_name() -> None:
    assert windows_ffi.logger.name == "kiro_crew.computer_use.windows_ffi"
