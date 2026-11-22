"""The ``widgets`` feature preview: what an agent may learn about phone widgets.

The Kiro Mobile app's home-screen widgets are not served yet, so nothing an agent
reads may teach it to write them while the preview is off. Behind
``dashboard.feature_previews.widgets``, default off:

* off, ``dashboard_save`` does not advertise ``widgets``, the tool refuses one without
  calling the gateway, and the save route refuses one with ``widgets_preview_off``,
  storing nothing. A save that leaves ``widgets`` out still keeps the widgets a stored
  package already holds, on or off;
* on, the schema offers ``widgets`` with :data:`GUIDE` (the example and the block
  table) in its description, and the route takes it.

:data:`GUIDE` lives here rather than in the dashboard skill because a built-in skill
is copied to disk whole, where any agent can read it; text kept out of that copy is
the only text an off preview can keep from an agent.
"""

from __future__ import annotations

from typing import Final

#: The preview's name under ``dashboard.feature_previews``.
WIDGETS_PREVIEW: Final[str] = "widgets"
#: The refusal code of a sent ``widgets`` while the preview is off.
PREVIEW_OFF_CODE: Final[str] = "widgets_preview_off"
#: Its message, shared by the HTTP route and the MCP reply.
PREVIEW_OFF_MESSAGE: Final[str] = (
    "widgets is not taken: phone widgets are not enabled on this Crew (the 'Phone widgets' "
    "feature preview is off). Save the page without widgets."
)

#: The tool whose ``widgets`` argument the preview gates.
GATED_TOOL: Final[str] = "dashboard_save"

#: How an agent writes widgets, appended to the ``widgets`` description while on.
GUIDE: Final[str] = """\
## Phone widgets

A page can also carry `widgets`: up to four home-screen widgets for the Kiro
Mobile app, saved with the page by passing `widgets` to `dashboard_save`.
Leave it out and the stored widgets stay as they are; `[]` removes them.
Every Bot also gets a built-in `status` widget, so that id is taken. In this
version only the Bot's owner sees widgets.

A widget is layout, never values or HTML. Each block names one field of your
`model.types`, and the gateway fills it from the same source the page reads.
A block cannot show a field the model does not declare, nor one read from a
tree-keyed fold such as `worktree`. The phone draws every string as plain
text, so markup in a value shows as typed.

```json
{
  "model": {"types": {
    "pipeline_state": {"type": "enum", "choices": ["green", "amber", "red"], "source": {"agentic": true}},
    "open_prs":       {"type": "number", "source": {"agentic": true}},
    "waiting_items":  {"type": "array", "source": {"fold": "waiting", "path": "items"}}
  }},
  "widgets": [{
    "id": "build-health",
    "title": "Build health",
    "sizes": ["small", "medium", "large"],
    "blocks": [
      {"type": "status", "label": "Pipeline", "field": "pipeline_state",
       "tones": {"green": "ok", "amber": "warn", "red": "bad"}},
      {"type": "metric", "label": "Open PRs", "field": "open_prs", "warn_at": 10, "bad_at": 20},
      {"type": "list", "label": "Waiting on you", "field": "waiting_items", "item_text": "text", "max_items": 3}
    ],
    "tap": {"open": "bot"}
  }]
}
```

| Block `type` | Keys (required first) | Field type |
|---|---|---|
| `title` | `text` or `field`, exactly one | text, enum |
| `metric` | `label`, `field`; `format` (`integer`, `decimal`, `percent`, `duration_s`, `text`), `unit`, `warn_at`, `bad_at`, `higher_is_worse` (default true) | number, text |
| `list` | `label`, `field`, `item_text`; `item_detail`, `max_items` (1 to 5) | array |
| `progress` | `label`, `field`, and `total` or `total_field` | number |
| `sparkline` | `label`, `field`; `point`, `max_points` (2 to 48) | array |
| `status` | `label`, `field`; `tones` (value to `ok`, `warn`, `bad`, `idle`, at most 8) | enum, bool, text |

Limits: 1 to 6 blocks a widget; `id` of lowercase letters, digits and `-`,
starting with a letter, at most 32 characters and unique; `title` and `label` 1 to 40 characters, `unit`
at most 8; `sizes` a subset of `small`, `medium`, `large`; `tap.open` is
`bot` only; the stored `widgets` at most 16 KiB. A small widget draws the first
2 blocks, a medium one 4, a large one all 6, so put the most important first.
A refusal names the key path and the reason: fix that one and save again.
"""


def widgets_enabled() -> bool:
    """Whether the preview is on. ``False`` on any read failure. Blocking: reads config."""
    try:
        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.edition_locks import feature_previews

        previews = feature_previews(KiroCrewConfig.load().dashboard.feature_previews)
    except Exception:  # noqa: BLE001 -- an unreadable config reads as off
        return False
    return isinstance(previews, dict) and previews.get(WIDGETS_PREVIEW) is True


def mcp_refusal(args: dict) -> str | None:
    """The MCP reply refusing *args*' ``widgets`` while the preview is off, else ``None``."""
    if args.get("widgets") is None or widgets_enabled():
        return None
    return f"Error: {PREVIEW_OFF_MESSAGE} (code `{PREVIEW_OFF_CODE}`); nothing was saved."


def with_widgets_schema(tools: list[dict], enabled: bool) -> list[dict]:
    """*tools* with ``widgets`` on ``dashboard_save`` carrying :data:`GUIDE` when *enabled*,
    else with ``widgets`` dropped from that schema."""
    out = []
    for tool in tools:
        props = (tool.get("inputSchema") or {}).get("properties") or {}
        if tool.get("name") == GATED_TOOL and "widgets" in props:
            schema = dict(tool["inputSchema"])
            if enabled:
                prop = dict(props["widgets"])
                prop["description"] = f"{prop.get('description', '')}\n\n{GUIDE}"
                schema["properties"] = {**props, "widgets": prop}
            else:
                schema["properties"] = {k: v for k, v in props.items() if k != "widgets"}
            tool = {**tool, "inputSchema": schema}
        out.append(tool)
    return out
