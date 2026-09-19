# Dashboard iframe hosts — which one to use

The dashboard embeds third-party content in **four** different iframe hosts. They look interchangeable and are not: each one picked a different `sandbox` for a different trust level, and two of them make *opposite* assumptions about whether the frame is allowed to talk back.

Pick a host from this table. **Do not widen an existing host's sandbox to make it fit a new use** — that is the failure mode this document exists to prevent.

| Host | `sandbox` | Content it is for | Frame → host messages |
|---|---|---|---|
| `WebPreviewPanel.tsx` (the **Browser** tab) | `allow-scripts allow-same-origin allow-forms allow-popups allow-modals allow-downloads` | A URL selected in the preview panel | none |
| `WidgetFrame.tsx` (`<mcwidget>`, artifacts) | `allow-scripts allow-popups allow-popups-to-escape-sandbox` | **LLM-emitted HTML** | **Defended against.** A malicious emitted `<script>` can `postMessage`, so the host treats inbound messages as hostile |
| `McpAppFrame.tsx` (the **App** tab) | `allow-scripts allow-forms` | **MCP-server-supplied HTML** (`srcDoc`) | **Required and trusted-by-capability.** SEP-1865 JSON-RPC bridge; the host holds a `callback_secret` |
| `InstancesViewport.tsx` | (remote Kiro Crew instances) | another instance's dashboard | n/a |

## The two axes

Trust level and connectedness are **independent**, which is why no host can be reused for another's job:

- `WebPreviewPanel` is the **most** privileged (`allow-same-origin` — it can reach dashboard origin) because it loads a URL selected in the preview panel. Never put server-supplied or model-supplied HTML in it.
- `McpAppFrame` is the **least** privileged (no `allow-same-origin`, no popups) *and* the **most** connected. It is the only host with a live bidirectional bridge, and it is null-origin precisely because it must be. Note this is a deliberate divergence from SEP-1865, which mandates a two-frame *sandbox proxy* whose outer frame carries `allow-same-origin`; the trade and what it costs an app are documented in [MCP Apps](../../src/kiro_crew/docs/mcp-apps.md#deviations-from-sep-1865). Do not "fix" the sandbox attribute to match the spec without reading that section.
- `WidgetFrame` is closest to `McpAppFrame` on content, but is built on the opposite bridge assumption. Reusing it for an MCP App would mean adopting a host that is designed to distrust exactly the messages the App protocol needs.

## The rule that constrains all of them: an iframe cannot be moved

Per WHATWG HTML, removing an `iframe` from a document **destroys its child navigable** (and the loaded document); inserting it creates a fresh one and re-runs the load steps. `appendChild`/`insertBefore` into a *different parent* is defined as an atomic remove-then-insert, so **moving an iframe node reloads it**. There is no cross-browser way around this.

Two consequences that have already cost real debugging time here:

- **A React portal does not help.** `createPortal` changes the frame's *real* DOM parent, so switching portal targets is a reparent, and therefore a reload. See `McpAppFrame.tsx` — the fullscreen overlay is promoted **in place** (`position: fixed` on the same never-moved wrapper) rather than portaled, with the reason stated in-comment.
- **Unmount-to-hide loses state.** For a null-origin frame with no storage there is nothing to restore from, so an unmount discards the user's work. `InstancesViewport.tsx` and `SidePanel.tsx` both solve this the same way: keep the frame mounted and toggle `display`. Follow that precedent.

If a feature needs content to *appear* somewhere else, restyle the stable container — do not move the node.

## The other rule: a refused frame is indistinguishable from a healthy one

A cross-origin frame whose document was blocked by `X-Frame-Options` or CSP `frame-ancestors` is, from the embedding page, **not detectable**. It fires `load` exactly as a healthy frame does, and fires it *sooner*. Measured with the parent cross-origin to the target, which is the real configuration here (dashboard on one port, dev server on another):

| target | Chromium `load` | Firefox `load` | `error` | readable `contentDocument` | resource timing |
|---|---|---|---|---|---|
| healthy page | 7 ms | 21 ms | never | no | status 0 |
| `X-Frame-Options: DENY` | 4 ms | 127 ms | never | no | status 0 |
| `X-Frame-Options: SAMEORIGIN` | 4 ms | 55 ms | never | no | status 0 |
| CSP `frame-ancestors 'none'` | 4 ms | 56 ms | never | no | status 0 |
| healthy but slow server (3 s) | 3008 ms | 3012 ms | never | no | status 0 |

Comparing every field between a refused frame and a healthy one yields **no discriminating field, in either engine**. Two consequences:

- **A load-timeout heuristic is inverted, not merely unreliable.** A refusal reports `load` in 4 ms against 3008 ms for a healthy cold dev server, so any threshold that catches the refusal also fires on a slow start. The false positive is worse than the blank frame it was meant to explain.
- **`useSilentLoadWatch` cannot cover it.** Its verdict is "`load` never arrived", and here `load` always arrives.

The only signal that separates the two is a console CSP violation, which a page cannot read, and which `X-Frame-Options` does not emit at all. A server-side frameability probe would answer it exactly, at the cost of an outbound-fetch endpoint and its SSRF surface.

So `WebPreviewPanel.tsx` does not detect this state. It carries a standing note above every frame instead, and the note is worded and marked as a note rather than as a status, because it is shown over healthy previews too. Do not replace it with a timer.

## Adding a new panel tab kind

Tab kinds live in `website/src/hooks/usePanelTabs.ts` (`ViewKind` / `TabKind`), and the body is dispatched in `website/src/pages/chat/SidePanel.tsx`. Two things to know before adding one:

- **Tab switch is safe only for non-category bodies.** Terminal, document, artifact, folder, and App bodies are kept mounted and hidden via `display` in `SidePanel.tsx`, so switching those tabs does not tear down a frame. Category views are unmounted when inactive.
- **Category views unmount on switch** (`if (!isActive) return null`), so a stateful frame must not be registered as a category view.

Auto-opening a tab is a solved pattern: dispatch `openActivityPanel()` then call `tabsCtlRef.current.openView(<kind>)` — see the web-preview path in `website/src/pages/ChatPage.tsx`.
