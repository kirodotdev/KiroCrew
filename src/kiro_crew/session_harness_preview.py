"""The ``session_harness`` feature preview: a harness picked per session at creation.

``session_create(backend=...)`` (and ``work_dispatch(backend=...)``, which passes it
to ``session_create``) lets a conductor start a worker on a harness of its choosing.
Until per-Bot harness selection settles how the choice is offered, that argument sits
behind ``dashboard.feature_previews.session_harness``, default off. Off, the tool
schemas do not advertise ``backend`` and the create route refuses one with
``session_harness_preview_off``, creating nothing. A Bot's own harness pin and
``spawn_run(backend=...)`` are not part of this preview and work either way.
"""

from __future__ import annotations

from typing import Final

#: The preview's name under ``dashboard.feature_previews``.
SESSION_HARNESS_PREVIEW: Final[str] = "session_harness"
#: The refusal code of an explicit ``backend`` while the preview is off.
PREVIEW_OFF_CODE: Final[str] = "session_harness_preview_off"
#: Its message, shared by the HTTP route and the MCP reply.
PREVIEW_OFF_MESSAGE: Final[str] = (
    "backend is not taken: the 'Agent Harness for worker chats' feature preview is off "
    "(Settings > Developer > Feature previews). Omit backend to run on the default harness."
)


def session_harness_enabled() -> bool:
    """Whether the preview is on. ``False`` on any read failure. Blocking: reads config."""
    try:
        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.edition_locks import feature_previews

        previews = feature_previews(KiroCrewConfig.load().dashboard.feature_previews)
    except Exception:  # noqa: BLE001 -- an unreadable config reads as off
        return False
    return isinstance(previews, dict) and previews.get(SESSION_HARNESS_PREVIEW) is True


def mcp_refusal(args: dict) -> str | None:
    """The MCP reply refusing *args*' ``backend`` while the preview is off, else ``None``."""
    if not args.get("backend") or session_harness_enabled():
        return None
    return f"Error: {PREVIEW_OFF_MESSAGE} (code `{PREVIEW_OFF_CODE}`); nothing was created."


#: The tools whose ``backend`` argument the preview gates.
GATED_TOOLS: Final[frozenset[str]] = frozenset({"session_create", "work_dispatch"})


def with_backend_schema(tools: list[dict], enabled: bool) -> list[dict]:
    """*tools* unchanged when *enabled*, else with ``backend`` dropped from the gated schemas."""
    if enabled:
        return tools
    out = []
    for tool in tools:
        props = (tool.get("inputSchema") or {}).get("properties") or {}
        if tool.get("name") in GATED_TOOLS and "backend" in props:
            schema = dict(tool["inputSchema"])
            schema["properties"] = {k: v for k, v in props.items() if k != "backend"}
            tool = {**tool, "inputSchema": schema}
        out.append(tool)
    return out
