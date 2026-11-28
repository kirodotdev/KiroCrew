"""The helper allowlist the gateway side checks before it sends a helper call.

``supervisor.HELPERS`` is this table; ``helper_worker._ALLOWLIST`` is the copy the
worker holds, and a test pins the two equal. Its own module so a helper added here
does not grow ``supervisor.py``.
"""

from __future__ import annotations

from collections.abc import Mapping

#: The helpers :meth:`HarnessSupervisor.call` will forward, with the keyword
#: arguments each accepts. Mirrors ``helper_worker._ALLOWLIST``; ``js`` runs the
#: gateway's own accessibility walker (``browser_harness/snapshot_walker.js``) and,
#: with ``cdp`` and ``http_get``, the agent's ops of those names
#: (``browser_harness.raw_ops``); ``js_isolated`` runs the walker's screenshot-guard
#: counts in an isolated world.
HELPERS: Mapping[str, tuple[str, ...]] = {
    "goto": ("url",),
    "click": ("x", "y", "button", "clicks"),
    "mouse_move": ("x", "y"),
    "drag": ("x", "y", "to_x", "to_y"),
    "type_text": ("text",),
    "fill_input": ("selector", "text", "clear_first"),
    "dispatch_key": ("selector", "key", "event"),
    "press_key": ("key", "modifiers"),
    "scroll": ("x", "y", "dy", "dx"),
    "screenshot": ("path", "full", "max_dim"),
    "screenshot_clip": ("path", "x", "y", "width", "height", "max_dim", "magnify"),
    "page_info": (),
    "dialog": ("action", "text"),
    "wait_for_load": ("timeout",),
    "wait_for_element": ("selector", "timeout", "visible"),
    "wait_for_network_idle": ("timeout", "idle_ms"),
    "new_tab": ("url",),
    "switch_tab": ("target", "activate"),
    "list_tabs": ("include_chrome",),
    "close_tab": ("target",),
    "current_tab": (),
    "js": ("expression", "target_id"),
    "cdp": ("method", "params"),
    "http_get": ("url", "headers", "timeout"),
    "js_isolated": ("expression",),
    "frame_owner_count": (),
    "closed_secret_count": (),
    "page_clock": ("state",),
    "set_files": ("expression", "files"),
    "chooser_files": ("x", "y", "files"),
    "drop_files": ("x", "y", "files"),
}
