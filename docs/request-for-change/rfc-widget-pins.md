---
title: Widget pins — keep live widgets on screen beside the chat, visible to the session's agent
status: draft
author: EllaRed
created: 2026-11-10
last-audited: 2026-11-12
audited-at: d9839eee5
doc-pr: 35914
implementation-prs: []
tracking-issues: [35628]
supersedes: []
superseded-by: []
---

# RFC: Widget pins

**Status:** draft. Nothing is built on main. A working version runs on a 0.10 install and a 0.12 install, and the
numbers below come from it.

## Summary

A person can pin a widget from its toolbar. The widget then stays on screen in a panel over the chat pane, live,
while the conversation scrolls on. The panel floats, docks beside the chat, or fills the pane, and it resizes from
any edge. Pins belong to a session, so they come back after a reload and hide while another session is on screen.
The agent in that session sees which widgets are pinned and can pin, move or unpin them, but only in its own
session. Pinned widgets are kept when old widgets are cleaned up.

## Motivation

### Current state (main at d9839eee5)

- The widget toolbar offers Download, Open in new tab and Expand
  (`website/src/components/WidgetFrame.tsx`). Expand fills the pane and hides the chat while it is open.
  Open in new tab takes the widget out of the dashboard.
- Message pins (`/api/chat/pins`, `src/kiro_crew/dashboard/routes/sessions.py`) keep a text preview of a
  message in the Pins tab, not the live widget.
- The agent has no way to know which widget the person is watching, so "update the chart I'm watching" has
  nothing to point at.
- The gateway keeps only the newest 200 unstarred widget artifacts (`MAX_AUTO_WIDGET_ARTIFACTS`,
  `src/kiro_crew/artifacts_disk.py`, applied by `prune_auto_widgets`, `src/kiro_crew/artifacts_scans.py`).
  A widget someone keeps watching can be cleaned up under them.
- An app cannot fill the gap. Apps have no contribution point on the widget toolbar, no surface that floats over
  the chat, and no way to add a line to the agent's turn context. The link-preview overlay opens only on a link
  hover.
- The dashboard MCP server, which holds `chat_session_pin`, is `opt_in` (`src/kiro_crew/agent.py`). The
  default agent mounts none of its tools; crewmate threads and conductors get it through their own grants. A tool
  placed there would not reach a default session.

### Problems

1. A widget that is meant to be watched (a status board, a chart, a checklist) scrolls away.
2. Keeping it in view means scrolling back or switching tabs.
3. The agent cannot see or arrange what the person is watching.

## Goals

- **G1.** Keep up to 8 live widgets per session on screen over or beside the chat. The chat column never moves.
- **G2.** Pins survive a reload and a session switch, and hide while another session is on screen.
- **G3.** The session's agent sees which widgets are pinned and how, and can pin, move (by position name) and
  unpin them in its own session only.
- **G4.** A pinned widget is never cleaned up while it is pinned.

## Non-goals

- Separate OS windows (#26375, #29874).
- Sharing pins between sessions.
- Buttons inside a pinned widget that post to the chat.
- Syncing panel sizes between browsers. Each browser keeps its own, so each monitor keeps its own layout.
- Pins in a crew window (a session on a connected install). Its widgets live on the other install, so the Pin
  button is hidden there.
- Pixel positions or sizes set by an agent.

## Design

### The panel

- **Pin** is a new button at the start of the widget toolbar, before Download. It turns into Unpin while the widget is pinned,
  and the panel's title bar has its own Unpin, Maximize and Restore buttons.
- The panel renders the same artifact in a new frame, with the same sandbox and CSP as the inline widget
  (`AGENT_PAGE_SANDBOX`, `WidgetFrame.tsx`). The inline frame is never moved, because moving a live iframe
  reloads it. An agent's edit to the artifact updates both.
- **Modes:** floating, docked left, docked right, and maximized (fills the pane).
- **Docking rule.** A docked panel fills the free margin beside the chat column when that margin is at least 240px
  wide. Otherwise it is a 320px overlay at that edge, covering only the edge of the chat. Panels docked to the same
  side stack. Docks start below the pane's top band (the title row and tab strip), so they never cover its
  controls. Docked panels re-measure when the pane resizes or the layout density changes.
- **Resize** from all eight edges and corners. Resizing a docked panel makes it float.
- **Keyboard** on the panel's title bar: `[` and `]` dock left and right, arrow keys move, Shift and an arrow key
  resize.

### Where state lives

| What | Where | Why |
|---|---|---|
| Which widgets are pinned, and each one's mode | Gateway, per session | Every browser and the agent see the same pins |
| A floating panel's position and size | Each browser's localStorage | Each monitor keeps its own layout |

A browser with no stored rectangle for a pin places it by the docking rule: in a margin when it fits.

### Gateway store and routes

- One file in the data home, `widget_pins.json`, mode 0600, written atomically.
- A record: `{slot_key, slug, title, mode, corner, corner_seq, pinned_at}`. `mode` is `float`, `left`, `right` or
  `max`. `corner` is the corner an agent last asked for (`top-left`, `top-right`, `bottom-left`, `bottom-right`) or
  null; `corner_seq` tells each browser to apply a new corner request once. Every field has a reader: the dashboard
  reads all of them, and the turn line reads `slug`, `mode` and `corner`.
- At most 8 pins per session. Only widget artifacts that exist can be pinned.
- Routes, with the same authentication and app-slot check as message pins:

| Route | Does | Answers |
|---|---|---|
| `GET /api/chat/widget-pins?slot=<slot_key>` | List the session's pins | `{pins: [...]}` |
| `POST /api/chat/widget-pins` `{slot_key, slug, title?, mode?, corner?}` | Pin (idempotent) | 201 new, 200 existing, 409 `widget_pins_full` |
| `PATCH /api/chat/widget-pins` `{slot_key, slug, mode, corner?}` | Change the mode | `{pin}` |
| `DELETE /api/chat/widget-pins?slot=<slot_key>&slug=<slug>` | Unpin | `{ok, pin}` |

- Errors use fixed codes: 400 for a bad slot key, slug, mode or corner (a corner needs `float`), 404
  `slot_not_found` and `widget_pin_not_found`, 413 for a body over 8 KiB, 415 for a body that is not JSON.
- A websocket event, `widget_pins_changed {slot_key}`, tells open dashboards to refetch.
- `prune_auto_widgets` skips every pinned slug. If the pin file cannot be read, it prunes nothing that round
  rather than guess.

### What the agent sees and does

- When a session has pins, each turn's rail (`rail_parts`, `src/kiro_crew/context_assembly/turn.py`) carries
  one line, for example `[WIDGET PINS] Pinned in this session: 9b59b3d507830225 (docked right)`. The line carries
  slugs and fixed words only, never titles: the rail is a trusted channel, and titles are agent-written prose.
- One `kirocrew-core` tool, in `src/kiro_crew/mcp_tools/` beside the artifact tools:
  `chat_widget_pin(slug, action=pin|move|unpin, position=dock-left|dock-right|fill|float, corner?)`. It lives on
  core because the turn line reaches every session, so the tool must too, and core is the surface every session
  carries. `chat_tag` is the precedent for a self-only `chat_*` tool there. There is no list tool, because the turn
  line already lists the pins and a core schema costs context in every session.
- **Scope.** A call acts only on the caller's own session. The tool calls the pin routes with the caller's
  `X-Session-Key`, as core tools do (`src/kiro_crew/mcp_tools/dashboard_client.py`). The route resolves it with
  `caller_slot_key` (`src/kiro_crew/dashboard/session_control_parts/caller_access.py`) and refuses any other
  slot with 404 and an audit record. Channel sessions cannot call it.

## Migration plan

Each phase is one pull request, shippable and abandonable on its own.

1. **Panels in the dashboard, browser-only.** Pin button, panel layer, modes, docking rule, resize, keyboard,
   localStorage per session, Pin hidden in crew windows. Exit criteria: pinning from the toolbar shows a live panel;
   a panel survives a reload and a session switch; a test asserts the chat column's geometry does not change when a
   panel docks; no Pin button renders in a crew window.
2. **Gateway store.** `widget_pins.json`, the four routes, the websocket event and the prune skip. The dashboard
   syncs modes through the gateway, uploads its browser-only pins once, and keeps Phase 1 behavior when the route
   answers a plain 404. Exit criteria: route tests for authentication, the cap, every error code and idempotent
   POST; a prune test that keeps a pinned widget past the 200 limit; a dashboard test for the 404 fallback.
3. **Agent awareness and control.** The turn line and `chat_widget_pin`. Exit criteria: the line appears only when
   pins exist and carries no title; a default-agent session can call the tool; the tool refuses another session's
   slot, an unknown slug and a non-widget artifact; a channel session cannot call it.

## Backward compatibility

Compatible: every change is additive. No existing route, tool, record or setting changes shape. Removing the feature
later leaves an unread `widget_pins.json` and unread localStorage keys, which nothing else reads.

## Security considerations

- The routes use the same authentication and app-slot check as message pins; an agent reaches only its own
  session's pins.
- The turn line is built from slugs that match the artifact slug pattern and fixed words, and passes the existing
  injection screen.
- Request bodies are capped at 8 KiB and must be JSON.
- A panel frame has the same sandbox and CSP as an inline widget, so pinning grants a widget no new origin, network
  reach or storage.

## Alternatives considered

- **An app.** No toolbar contribution point, no floating surface over the chat, and no turn-context line.
- **A list of pinned widgets in the side panel.** It does not keep the widget in view while the person reads the
  chat.
- **A separate window or picture-in-picture.** Separate windows are covered by #26375 and #29874, and leave the
  dashboard.
- **Panel rectangles held by the gateway.** One layout everywhere, but a panel sized for one monitor is wrong on
  another. Per-browser rectangles keep each monitor's layout; this can be revisited.
- **Making room by moving or narrowing the chat.** The chat column stays fixed; panels use the free margin or
  overlay the edge.
- **The tool on the dashboard MCP server, beside `chat_session_pin`** (`src/kiro_crew/mcp_dashboard.py`). This
  is how the first version was built. It costs no context in sessions without the server, but the default agent
  does not mount the server, so a default session would see pins it cannot move.
- **Mounting the dashboard server on the default agent.** It would bring the server's session-control tools, which
  reach every live session, to get one tool that touches only the caller's own panels.

## Open questions

1. **UI placement.** Is the start of the widget toolbar the right place for Pin, and is a panel layer over the chat
   pane acceptable to the maintainers?
2. **Docked width.** Fixed by the docking rule (current), or draggable?
3. **Stale corner.** After a person drags a floating panel, the record still names the last corner an agent asked
   for, so the turn line can name a corner the panel has left. Drop the corner from the line, or clear it when the
   person drags?
4. **Deleted sessions.** Should deleting a session delete its pins, or should a sweep remove pins whose session is
   gone?
5. **Approval.** Sessions that auto-approve `kirocrew-core` would run `chat_widget_pin` without asking, as they run
   `chat_tag`. Is that right for a tool that changes only the caller's own panels?
