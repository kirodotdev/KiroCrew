import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

/** State mirrored back from the main process's per-panel browser manager. */
export interface NativeBrowserState {
  open: boolean
  visible: boolean
  url: string
  bounds: { x: number; y: number; width: number; height: number } | null
  overlayActive?: boolean
  refused?: boolean
}

interface NativeBrowserBridge {
  open: (panelId: string, url: string) => Promise<NativeBrowserState | null>
  navigate: (panelId: string, url: string) => Promise<NativeBrowserState | null>
  setBounds: (
    panelId: string,
    rect: { x: number; y: number; width: number; height: number },
    viewport: { width: number; height: number },
  ) => Promise<NativeBrowserState | null>
  setOverlayActive: (panelId: string, active: boolean) => Promise<NativeBrowserState | null>
  setInactive: (panelId: string, value: boolean) => Promise<NativeBrowserState | null>
  close: (panelId: string) => Promise<NativeBrowserState | null>
  getState: (panelId: string) => Promise<NativeBrowserState | null>
  setAgentAct: (panelId: string, enabled: boolean) => Promise<{ ok: boolean } | null>
  setControlOwner: (panelId: string, owner: string) => Promise<unknown>
  onAgentOpened: (cb: (p: { panelId?: string; url?: string }) => void) => () => void
  onDidNavigate: (cb: (p: { panelId?: string; url: string; title: string }) => void) => () => void
  onTitleUpdated: (cb: (p: { panelId?: string; url: string; title: string }) => void) => () => void
}

function bridge(): NativeBrowserBridge | null {
  const w = window as unknown as { browserAPI?: NativeBrowserBridge }
  return w.browserAPI ?? null
}

/** Agent control owners, mirroring browser-control.js. */
export const CONTROL_OWNER = { NONE: 'none', LIGHT: 'light', PLAYWRIGHT: 'playwright' } as const

/** Selector for SPA chrome that would be occluded by the native layer. */
const OVERLAY_SELECTOR = '[role="dialog"],[role="alertdialog"],[data-native-overlay]'

/**
 * Drive one dashboard Browser panel's NATIVE browser view.
 *
 * The Browser panel can be backed by two different transports:
 *
 *   • **native** (this hook) — a real Chromium `WebContentsView` owned by the
 *     Electron main process, composited over the panel's rectangle. Native
 *     paint, real events, downloads, video.
 *   • **mirror** (`useBrowserFrame`) — streamed JPEG frames from a remote
 *     browser. Still required when the browser does not live in this process.
 *
 * Because the native view is NOT in the React tree, this hook carries the two
 * responsibilities the renderer cannot delegate:
 *
 *   1. **Rect reporting.** React owns layout, so it must tell the main process
 *      where the panel ended up. The rect is sent in CSS px along with the
 *      viewport size; the main process derives the zoom scale from the ratio and
 *      converts to DIP (see browser-view.js `deriveScale`).
 *   2. **Overlay reporting.** The native layer paints ABOVE the SPA and cannot be
 *      layered by CSS, so anything the dashboard draws over the panel rect (a
 *      modal, a dropdown) would be hidden behind it. Open overlays are detected
 *      and the native view is hidden for their duration.
 *
 * Everything is scoped by `panelId`: the dashboard renders one Browser panel per
 * chat session, and each owns a separate view, control plane and agent-act
 * authorization in the main process.
 *
 * `available` is false in a plain browser (no preload bridge), which is the
 * signal to fall back to the mirror transport.
 */
export function useNativeBrowser(
  panelId: string,
  enabled: boolean,
  opts: { agentActEnabled?: boolean } = {},
) {
  const { agentActEnabled = false } = opts
  const api = useMemo(bridge, [])
  const available = !!api && !!panelId
  const hostRef = useRef<HTMLDivElement | null>(null)
  const [state, setState] = useState<NativeBrowserState | null>(null)

  const apply = useCallback((next: NativeBrowserState | null) => {
    if (next) setState(next)
  }, [])

  // Mirror this panel's agent-act authorization into the main process, which
  // evaluates the control gate itself against it — it never trusts an
  // authorization asserted per-request.
  useEffect(() => {
    if (!api || !panelId) return
    void api.setAgentAct(panelId, agentActEnabled)
  }, [api, panelId, agentActEnabled])

  // ── Control handoff: who drives the page over CDP ──
  // The native view is driven IN-PROCESS over CDP, so the agent owner for a
  // chat-opened page is LIGHT (never PLAYWRIGHT — that owner is for a SEPARATE
  // external browser process, which is only relevant when NO native view is
  // available and this hook is inert).
  //
  // LIGHT is requested whenever a native page EXISTS, not only when the Globe is
  // on. Ownership is "who holds the debugger", which the agent needs even to
  // READ the page (snapshot / screenshot); authorization for what it may DO with
  // it is enforced per op CLASS in the main process (see agentOpClass). Tying
  // ownership to the Globe instead meant Globe-off actively parked the owner at
  // NONE, so a plain "open this page" could never be served natively and
  // silently fell back to the mirror transport.
  //
  // The main process still RELEASES on revoke, to kill an operate-class op that
  // is already in flight — but that is a one-shot detach, not a standing state:
  // each op in `dispatch` re-acquires LIGHT under its own class gate, so
  // view-class reads keep working with the Globe off. This effect deliberately
  // does not re-assert NONE on top of that, which would park ownership and
  // re-break the native view.
  //
  // Deliberately NOT gated on `enabled`: a hidden-but-alive view the agent is
  // mid-task on must keep its owner (visibility via setInactive is orthogonal to
  // who controls the page). `state?.open` is a dependency so control is
  // (re)acquired once the page actually exists — the gate refuses LIGHT while
  // the view is not yet open, so the first request right after `open()` would
  // otherwise be dropped.
  useEffect(() => {
    if (!api || !panelId) return
    void api.setControlOwner(
      panelId,
      state?.open ? CONTROL_OWNER.LIGHT : CONTROL_OWNER.NONE,
    )
  }, [api, panelId, state?.open])

  /** Measure the host element and report it. Cheap and idempotent — the main
   *  process drops reports identical to the applied bounds. */
  const report = useCallback(() => {
    if (!api || !panelId || !enabled) return
    const el = hostRef.current
    if (!el) return
    const r = el.getBoundingClientRect()
    void api
      .setBounds(
        panelId,
        { x: r.x, y: r.y, width: r.width, height: r.height },
        { width: window.innerWidth, height: window.innerHeight },
      )
      .then(apply)
  }, [api, panelId, enabled, apply])

  // Keep the reported rect in step with layout. ResizeObserver catches the panel
  // being resized or the drawer animating; scroll/resize catch the rest. The
  // window's own resize is also handled in the main process, but the panel's
  // offset within the window can change without the window changing size.
  //
  // `state?.open` is a dependency because the measured host div only RENDERS once
  // the view is open. On the first open, `hostRef.current` is still null when this
  // effect first runs (and the `report()` inside `open()` fires before React has
  // mounted it), so without re-running here nothing would ever observe or measure
  // the host: no bounds would be sent and the native view would stay invisible.
  useEffect(() => {
    if (!api || !panelId || !enabled) return
    const el = hostRef.current
    if (!el) return
    report()
    const ro = new ResizeObserver(() => report())
    ro.observe(el)
    window.addEventListener('resize', report)
    window.addEventListener('scroll', report, true)
    return () => {
      ro.disconnect()
      window.removeEventListener('resize', report)
      window.removeEventListener('scroll', report, true)
    }
  }, [api, panelId, enabled, report, state?.open])

  // Hide the native view whenever SPA chrome covers the panel rect.
  useEffect(() => {
    if (!api || !panelId || !enabled) return
    let last: boolean | null = null
    const check = () => {
      const active = document.querySelector(OVERLAY_SELECTOR) !== null
      if (active === last) return
      last = active
      void api.setOverlayActive(panelId, active).then(apply)
    }
    check()
    const mo = new MutationObserver(check)
    mo.observe(document.body, {
      childList: true,
      subtree: true,
      attributes: true,
      attributeFilter: ['role', 'data-native-overlay'],
    })
    return () => {
      mo.disconnect()
      // Never leave the view hidden because an overlay was up at unmount.
      void api.setOverlayActive(panelId, false)
    }
  }, [api, panelId, enabled, apply])

  // Reflect navigation the view performs on its own (link clicks, redirects, or
  // an agent driving it over CDP) so the panel's URL/title stay honest. Events
  // are broadcast to the window, so ignore other panels'.
  useEffect(() => {
    if (!api || !panelId || !enabled) return
    const mine = (p: { panelId?: string }) => !p?.panelId || p.panelId === panelId
    const refresh = (p: { panelId?: string }) => {
      if (mine(p)) void api.getState(panelId).then(apply)
    }
    const offNav = api.onDidNavigate(refresh)
    const offTitle = api.onTitleUpdated(refresh)
    return () => {
      offNav()
      offTitle()
    }
  }, [api, panelId, enabled, apply])

  // Going inactive (the panel's tab is no longer the visible one, or the
  // transport switched) HIDES the view; it must not destroy it. Closing here
  // would drop the WebContents, so switching side-panel tabs away and back would
  // lose unsaved form input, scroll position and history — a browser should not
  // forget your page because you glanced at another tab.
  useEffect(() => {
    if (!api || !panelId) return
    void api.setInactive(panelId, !enabled).then(apply)
  }, [api, panelId, enabled, apply])

  // The view IS released when this panel unmounts, or is re-keyed to a different
  // session: past that point nothing can surface it again, so a live browser
  // would just leak.
  useEffect(() => {
    if (!api || !panelId) return
    return () => {
      void api.close(panelId)
    }
  }, [api, panelId])

  useEffect(() => {
    if (!api || !panelId || !enabled) return
    void api.getState(panelId).then(apply)
  }, [api, panelId, enabled, apply])

  const open = useCallback(
    (url: string) => {
      if (!api || !panelId) return
      void api.open(panelId, url).then((s) => {
        apply(s)
        report()
      })
    },
    [api, panelId, apply, report],
  )

  const navigate = useCallback(
    (url: string) => {
      if (!api || !panelId) return
      void api.navigate(panelId, url).then(apply)
    },
    [api, panelId, apply],
  )

  const close = useCallback(() => {
    if (!api || !panelId) return
    void api.close(panelId).then(apply)
  }, [api, panelId, apply])

  return { available, hostRef, state, open, navigate, close, report }
}
