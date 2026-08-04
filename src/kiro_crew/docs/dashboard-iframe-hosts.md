# Dashboard iframe hosts — which one to use

The dashboard embeds third-party content in **four** different iframe hosts. They
look interchangeable and are not: each one picked a different `sandbox` for a
different trust level, and two of them make *opposite* assumptions about whether
the frame is allowed to talk back.

Pick a host from this table. **Do not widen an existing host's sandbox to make it
fit a new use** — that is the failure mode this document exists to prevent.

| Host | `sandbox` | Content it is for | Frame → host messages |
|---|---|---|---|
| `WebPreviewPanel.tsx` (the **Browser** tab) | `allow-scripts allow-same-origin allow-forms allow-popups allow-modals allow-downloads` | A **URL you trust** — your own dev server, a page you deployed | none |
| `WidgetFrame.tsx` (`<mcwidget>`, artifacts) | `allow-scripts allow-popups allow-popups-to-escape-sandbox` | **LLM-emitted HTML** | **Defended against.** A malicious emitted `<script>` can `postMessage`, so the host treats inbound messages as hostile |
| `McpAppFrame.tsx` (the **App** tab) | `allow-scripts allow-forms` | **MCP-server-supplied HTML** (`srcDoc`) | **Required and trusted-by-capability.** SEP-1865 JSON-RPC bridge; the host holds a `callback_secret` |
| `InstancesViewport.tsx` | (remote Kiro Crew instances) | another instance's dashboard | n/a |

## The two axes

Trust level and connectedness are **independent**, which is why no host can be
reused for another's job:

- `WebPreviewPanel` is the **most** privileged (`allow-same-origin` — it can
  reach dashboard origin) because you chose the URL. Never put
  server-supplied or model-supplied HTML in it.
- `McpAppFrame` is the **least** privileged (no `allow-same-origin`, no popups)
  *and* the **most** connected. It is the only host with a live bidirectional
  bridge, and it is null-origin precisely because it must be.
- `WidgetFrame` is closest to `McpAppFrame` on content, but is built on the
  opposite bridge assumption. Reusing it for an MCP App would mean adopting a
  host that is designed to distrust exactly the messages the App protocol needs.

## The rule that constrains all of them: an iframe cannot be moved

Per WHATWG HTML, removing an `iframe` from a document **destroys its child
navigable** (and the loaded document); inserting it creates a fresh one and
re-runs the load steps. `appendChild`/`insertBefore` into a *different parent* is
defined as an atomic remove-then-insert, so **moving an iframe node reloads it**.
There is no cross-browser way around this.

Two consequences that have already cost real debugging time here:

- **A React portal does not help.** `createPortal` changes the frame's *real* DOM
  parent, so switching portal targets is a reparent, and therefore a reload.
  See `McpAppFrame.tsx` — the fullscreen overlay is promoted **in place**
  (`position: fixed` on the same never-moved wrapper) rather than portaled, with
  the reason stated in-comment.
- **Unmount-to-hide loses state.** For a null-origin frame with no storage there
  is nothing to restore from, so an unmount discards the user's work.
  `InstancesViewport.tsx` and `SidePanel.tsx` both solve this the same way:
  keep the frame mounted and toggle `display`. Follow that precedent.

If a feature needs content to *appear* somewhere else, restyle the stable
container — do not move the node.

## Adding a new panel tab kind

Tab kinds live in `website/src/hooks/usePanelTabs.ts` (`ViewKind` / `TabKind`),
and the body is dispatched in `website/src/pages/chat/SidePanel.tsx`. Two things
to know before adding one:

- **Tab switch is safe; panel close is not.** Non-category tab bodies are kept
  mounted and hidden via `display` in `SidePanel.tsx`, so switching tabs does not
  tear down a frame. But the whole `SidePanel` is gated behind `activityOpen` in
  `ChatPage.tsx`, so **closing the panel unmounts everything in it**. A tab
  hosting stateful content must account for that explicitly.
- **Category views unmount on switch** (`if (!isActive) return null`), so a
  stateful frame must not be registered as a category view.

Auto-opening a tab is a solved pattern: dispatch `openActivityPanel()` then call
`tabsCtl.openView(<kind>)` — see the web-preview path in `ChatPage.tsx`.
