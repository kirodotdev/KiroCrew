import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Maximize2, Minimize2, Shrink, Expand } from 'lucide-react'
import { IconButton, IconButtonGroup } from './ui'
import { useDialogFocusTrap } from '../hooks/useDialogFocusTrap'
import { useTheme } from '../hooks/useTheme'
import { i18nT } from '../i18n/t'
import {
  buildMcpAppSrcdoc,
  buildAllowAttribute,
  type McpAppRenderPayload,
} from '../lib/mcpAppSrcdoc'
import { planReveal, prefersReducedMotion, hasRevealed, markRevealed } from './mcpAppReveal'

/** Inline height for a rendered MCP App before it reports its own size. */
const DEFAULT_HEIGHT = 480
/** Hard ceiling on app height — matches hostContext.containerDimensions.maxHeight
 * advertised to the app in the ui/initialize response. */
const MAX_HEIGHT = 1200

// MCP Apps UI-channel JSON-RPC method names (SEP-1865). The `ui/` namespace is
// disjoint from the MCP tools namespace, carried over postMessage between the
// host (this component) and the app iframe.
const PROTOCOL_VERSION = '2025-11-21'
const M_INITIALIZE = 'ui/initialize'
const M_TOOLS_CALL = 'tools/call'
const M_REQUEST_DISPLAY_MODE = 'ui/request-display-mode'
const M_OPEN_LINK = 'ui/open-link'
const N_INITIALIZED = 'ui/notifications/initialized'
const N_TOOL_INPUT = 'ui/notifications/tool-input'
/** Incomplete arguments, "may change" — the notification a streaming-aware app
 *  redraws from. Posted in ascending prefixes ahead of N_TOOL_INPUT; see
 *  `mcpAppReveal.ts` for why the pacing is the host's and not the model's. */
const N_TOOL_INPUT_PARTIAL = 'ui/notifications/tool-input-partial'
const N_TOOL_RESULT = 'ui/notifications/tool-result'
const N_SIZE_CHANGED = 'ui/notifications/size-changed'
const N_HOST_CONTEXT_CHANGED = 'ui/notifications/host-context-changed'
/** MCP logging notification (note: NOT under the `ui/` namespace). Apps send
 *  their own diagnostics here; dropping it makes an app opaque to debugging. */
const N_LOG_MESSAGE = 'notifications/message'

/** Capabilities this host actually implements. Declaring them matters even though
 *  the SDK's client-side gate is currently a no-op stub: a spec-conformant app is
 *  entitled to skip a feature we don't advertise, so an empty object here is a
 *  latent "the app silently stops trying" bug the moment that gate is enforced. */
const HOST_CAPABILITIES = {
  serverTools: {},
  openLinks: {},
} as const

/** The APP-FACING display mode (SEP-1865). Deliberately still the spec's two
 *  values: apps gate features on this string (excalidraw only mounts its
 *  editable canvas in `fullscreen`), so it is a protocol contract and not the
 *  place to express the host's own layout choices — those live in `Presentation`
 *  below and reach the app only as hostContext.containerDimensions. `pip` is in
 *  the spec's enum but not offered here. */
type DisplayMode = 'inline' | 'fullscreen'
const AVAILABLE_DISPLAY_MODES: DisplayMode[] = ['inline', 'fullscreen']

/** How the HOST is presenting the frame — three states behind the two protocol
 *  modes:
 *
 *  - `inline`  — in the transcript, at the chat column's width.
 *  - `wide`    — still in the transcript, but broken out of the column to the
 *                full width of the scroll area: grows left/right as well as down.
 *  - `overlay` — a viewport-filling sheet for real editing room.
 *
 *  `wide` and `overlay` both report `fullscreen` to the app and differ only in
 *  the dimensions granted, so an app needs no knowledge of this enum.
 *
 *  ESCALATION IS THE USER'S ALONE: an app requesting `fullscreen` is given
 *  `wide`, never `overlay`. These are conversational inline apps, and letting
 *  one throw a modal over the transcript on its own initiative is exactly the
 *  "pulled out of the conversation" failure the inline design exists to avoid.
 *  Only the header control promotes to `overlay`. */
type Presentation = 'inline' | 'wide' | 'overlay'

/** The protocol mode each presentation reports to the app. */
const PROTOCOL_MODE: Record<Presentation, DisplayMode> = {
  inline: 'inline',
  wide: 'fullscreen',
  overlay: 'fullscreen',
}

/** Fraction of the viewport each expanded presentation gets. `wide` stays short
 *  enough to keep the surrounding conversation visible; `overlay` is a sheet. */
const WIDE_VH = 0.8
const OVERLAY_VH = 0.92
const OVERLAY_VW = 0.95
/** Height reserved for the overlay's own header + borders, so the sheet as a
 *  whole still fits inside OVERLAY_VH of the viewport. */
const OVERLAY_CHROME_PX = 44
/** Left/right breathing room kept when `wide` breaks out of the chat column. */
const WIDE_GUTTER = 24

function viewportSize(): { w: number; h: number } {
  if (typeof window === 'undefined') return { w: 0, h: 0 }
  return { w: window.innerWidth, h: window.innerHeight }
}

/** Concrete content height granted to an expanded presentation. */
function expandedHeightPx(presentation: Presentation = 'wide'): number {
  const { h } = viewportSize()
  if (h <= 0) return MAX_HEIGHT
  if (presentation === 'overlay') {
    return Math.max(240, Math.round(h * OVERLAY_VH) - OVERLAY_CHROME_PX)
  }
  return Math.min(MAX_HEIGHT, Math.round(h * WIDE_VH))
}

/** Concrete content width granted to the overlay. */
function overlayWidthPx(): number {
  const { w } = viewportSize()
  return w > 0 ? Math.round(w * OVERLAY_VW) : 0
}

/**
 * hostContext.containerDimensions for a presentation.
 *
 * CRITICAL — an expanded presentation MUST carry a concrete `height`, not just
 * `maxHeight`. An app cannot lay out a fullscreen surface from a ceiling alone:
 * inside an iframe `position: fixed` gives the body no height, so a real app
 * (excalidraw) keys its fullscreen layout off `containerDimensions.height` and
 * renders into a ZERO-HEIGHT container when only `maxHeight` is supplied — the
 * mode flips, the editor mounts, and nothing visibly changes. `inline` keeps
 * advertising a ceiling because the app sizes itself there and reports back via
 * ui/notifications/size-changed.
 *
 * `width` is advertised for the expanded presentations because those are the
 * ones where the host changes it. excalidraw ignores it (it fills its own
 * document, which is the iframe), but a spec-conformant app is entitled to lay
 * out against it, and omitting it would make `wide` and `overlay`
 * indistinguishable to an app that does.
 */
function dimensionsFor(
  presentation: Presentation,
  wideWidth: number | null,
): Record<string, number> {
  if (presentation === 'inline') return { maxHeight: MAX_HEIGHT }
  const dims: Record<string, number> = { height: expandedHeightPx(presentation) }
  const width = presentation === 'overlay' ? overlayWidthPx() : wideWidth
  if (width && width > 0) dims.width = Math.round(width)
  return dims
}

/**
 * Gate for ui/open-link. The URL originates in SANDBOXED APP CONTENT, so it is
 * untrusted input to a host-side navigation: accept ONLY absolute `https:`.
 * That rejects `javascript:` (script execution in the host origin), `data:` and
 * `blob:` (arbitrary attacker-authored documents), `file:` (local disk reads),
 * and custom schemes that can hand off to native handlers. Callers must also
 * pass `noopener,noreferrer` so the opened tab gets no `window.opener` handle
 * back into the dashboard (reverse tabnabbing).
 */
function isOpenableUrl(raw: unknown): raw is string {
  if (typeof raw !== 'string' || !raw) return false
  try {
    return new URL(raw).protocol === 'https:'
  } catch {
    return false // not absolute / unparseable
  }
}

interface JsonRpcMessage {
  jsonrpc?: string
  id?: string | number | null
  method?: string
  params?: unknown
}

/**
 * Ref-counted scroll locks, keyed by the element being frozen.
 *
 * A per-instance save/restore is not enough. Two frames freezing the SAME
 * scroller would each snapshot its overflow independently, so whichever released
 * first would restore the scrollable value while the other sheet was still open
 * — re-arming the data loss below — and the later release could write a stale
 * `hidden` back, leaving the transcript permanently frozen. Counting holders
 * makes the FIRST lock authoritative and only the LAST release restore it.
 *
 * Two sheets at once is hard to reach today (the backdrop covers the viewport
 * and the focus trap confines Tab), but this protects against losing the user's
 * work, and "only one can be open" is a fragile assumption in a dashboard with
 * multi-pane session layouts.
 */
const scrollLocks = new Map<HTMLElement, {
  holders: number
  previous: string
  pinnedTop: number
  onScroll: () => void
}>()

/** Freeze `el`, returning its release. Safe to call for several holders.
 *
 *  Freezing has TWO halves, because they stop different things:
 *
 *  1. `overflowY: hidden` stops the user's wheel/trackpad. It writes `overflowY`
 *     specifically, NOT the `overflow` shorthand — the transcript scroller
 *     carries an inline `overflowY: 'auto'`, the shorthand would clobber it, and
 *     restoring the shorthand's previous (empty) value would leave the transcript
 *     permanently unscrollable, since React does not rewrite a style key whose
 *     value has not changed. Freezing one axis also leaves `overflowX` alone.
 *  2. A pinned `scrollTop` stops PROGRAMMATIC writes — the follow controller
 *     moving the transcript as new turns stream in. `overflow: hidden` does not
 *     prevent those, and the virtualizer derives its mounted window from scroll
 *     position, so an unattended write can unmount the originating row, destroy
 *     the iframe and reload the app with the user's work in it. While a sheet is
 *     open the transcript is already declared frozen, so holding the position is
 *     consistent with the intent rather than a new behaviour: follow-mode simply
 *     resumes on release.
 *
 *  The pin is inert when `el` is `document.body` (scroll events fire on the
 *  document, not the body) — acceptable, because that fallback is for hosts that
 *  scroll the document and have no virtualizer to unmount the row.
 */
function acquireScrollLock(el: HTMLElement): () => void {
  const existing = scrollLocks.get(el)
  if (existing) {
    existing.holders += 1
  } else {
    const pinnedTop = el.scrollTop
    const onScroll = () => {
      if (el.scrollTop !== pinnedTop) el.scrollTop = pinnedTop
    }
    el.addEventListener('scroll', onScroll)
    scrollLocks.set(el, { holders: 1, previous: el.style.overflowY, pinnedTop, onScroll })
    el.style.overflowY = 'hidden'
  }
  let released = false
  return () => {
    // A cleanup invoked twice (StrictMode double-effects, remount races) must not
    // under-count and free the lock while another holder still needs it.
    if (released) return
    released = true
    const entry = scrollLocks.get(el)
    if (!entry) return
    entry.holders -= 1
    if (entry.holders <= 0) {
      el.removeEventListener('scroll', entry.onScroll)
      el.style.overflowY = entry.previous
      scrollLocks.delete(el)
    }
  }
}

/**
 * Nearest ancestor that actually scrolls — the element that must be frozen while
 * the overlay is open.
 *
 * NOT `document.body`. In the dashboard the transcript scrolls its own
 * `.chat-container`, so freezing the body leaves the wheel free to scroll the
 * conversation behind the sheet.
 *
 * `.chat-container` is checked FIRST because it is a documented stable hook
 * (`website/docs/theming-contract.md`) and therefore a firmer contract than
 * reading computed overflow — which needs the CSS cascade to be resolvable and
 * is unreliable in the test DOM. The overflow walk stays as the general fallback
 * for hosts that scroll something else (embed panes), and `document.body` as the
 * last resort for hosts that scroll the document itself.
 */
function scrollLockTarget(from: HTMLElement | null): HTMLElement {
  const transcript = from?.closest<HTMLElement>('.chat-container')
  if (transcript) return transcript
  for (let el = from?.parentElement; el; el = el.parentElement) {
    const overflowY = getComputedStyle(el).overflowY
    if (overflowY === 'auto' || overflowY === 'scroll') return el
  }
  return document.body
}

/**
 * Overlay-only chrome: the dimming backdrop, the scroll lock, and modal
 * keyboard behaviour (focus in/out, Escape, Tab cycling).
 *
 * It deliberately does NOT contain the iframe. The frame is promoted in place by
 * toggling its own wrapper to `position: fixed`; moving that node into a portal
 * — which is what every ready-made modal in this codebase does — would unmount
 * and remount the iframe, reloading the app and destroying whatever the user has
 * drawn. So this renders as a SIBLING backdrop and points the focus trap at the
 * never-unmounted wrapper via `frameRef`. Mounting/unmounting this component is
 * what drives focus-in on open and focus-restore on close.
 */
function OverlayChrome({
  frameRef,
  onClose,
}: {
  frameRef: React.RefObject<HTMLElement | null>
  onClose: () => void
}) {
  useDialogFocusTrap(frameRef, onClose)

  // Freeze the scroll container for as long as the sheet is open.
  //
  // This is a DATA-LOSS guard, not a polish detail: the transcript is
  // virtualized (useVirtualChat bounds the mounted set to roughly window +
  // overscan), so scrolling the originating row out of that window unmounts this
  // whole component — destroying the iframe and reloading the sandboxed app with
  // the user's in-progress work. acquireScrollLock blocks BOTH routes to that:
  // the user's wheel (frozen overflow) and the follow controller's programmatic
  // scrollTop writes during streaming (pinned position).
  //
  // Ref-counted so overlapping holders cannot unfreeze each other. Returning the
  // release directly makes every exit path — dismiss, Escape, and
  // unmount-while-still-open — run the same teardown.
  useEffect(() => acquireScrollLock(scrollLockTarget(frameRef.current)), [frameRef])

  return (
    <button
      type="button"
      // A <button> rather than a div with a click handler, so the pointer
      // affordance carries interactive semantics instead of tripping the a11y
      // lint. It is hidden from assistive tech on purpose: it duplicates the
      // header's labelled Exit control and the Escape binding, and giving it the
      // same accessible name would announce one dismiss action twice.
      aria-hidden="true"
      tabIndex={-1}
      // One notch below the promoted frame (z-[90]). Both sit above the chat
      // content and the topbar; see the wrapper's comment for what they can NOT
      // out-stack.
      className="fixed inset-0 z-[89] bg-bg/70 backdrop-blur-sm"
      onClick={onClose}
    />
  )
}

/**
 * Renders an MCP App (SEP-1865) inline, below its originating tool-call row.
 *
 * SANDBOX: the iframe is `sandbox="allow-scripts allow-forms"` with a srcdoc
 * document — deliberately WITHOUT `allow-same-origin`. A srcdoc iframe inherits
 * the embedder's URL, so granting allow-same-origin would give the app document
 * the PARENT's origin — full access to the dashboard's cookies, storage, and
 * same-origin API endpoints. Withholding it forces a null (opaque) origin, so
 * the app is fully isolated. The trade-off (also intentional): with a null
 * origin the app cannot use its own cookies/localStorage — acceptable for M1
 * inline apps, which communicate solely over the postMessage AppBridge below.
 *
 * APPBRIDGE: a minimal host-side implementation of the MCP Apps UI JSON-RPC
 * channel. It answers `ui/initialize`, drives the tool-input / tool-result
 * notifications after the app signals `ui/notifications/initialized`, honors
 * `ui/notifications/size-changed`, negotiates `ui/request-display-mode` (granted
 * as an expanded bubble — see DisplayMode — and mirrored back to the app with
 * `ui/notifications/host-context-changed` when the host's own button toggles it),
 * and relays `tools/call` requests to the gateway via
 * `POST /api/mcp-apps/call` (the gateway enforces that only app-visible tools
 * are callable). Every other not-yet-supported request is rejected with a
 * JSON-RPC method-not-found error. Every inbound message is authenticated by
 * `event.source === iframe.contentWindow` before it is acted on.
 */
export default function McpAppFrame({ payload }: { payload: McpAppRenderPayload }) {
  const iframeRef = useRef<HTMLIFrameElement>(null)
  const wrapperRef = useRef<HTMLDivElement>(null)
  const [height, setHeight] = useState(DEFAULT_HEIGHT)
  // Host presentation (see Presentation). Driven from BOTH directions: the app
  // can request a protocol mode via ui/request-display-mode, and the host's own
  // header controls set it and notify the app. Everything else is derived from
  // this single value, so the frame's own size and the size the app was told can
  // never drift.
  const [presentation, setPresentation] = useState<Presentation>('inline')
  // Where the overlay returns to when dismissed — promoting from `wide` and
  // collapsing back to `inline` would silently discard the user's prior choice.
  const [restoreTo, setRestoreTo] = useState<Presentation>('inline')
  const expanded = presentation !== 'inline'
  const isOverlay = presentation === 'overlay'
  // Mirror for the stable message handler (which must not re-subscribe per mode).
  const presentationRef = useRef<Presentation>(presentation)
  presentationRef.current = presentation

  // The app styles itself from `hostContext.theme` alone: this host injects no
  // CSS into the srcdoc (see mcpAppSrcdoc.ts), so a hardcoded value left every
  // app dark regardless of the user's theme. `ResolvedMode` is already exactly
  // 'dark' | 'light', so it maps 1:1 onto the protocol field.
  //
  // Mirrored like `presentation` above because the bridge effect below closes
  // over `[]` — it must not re-subscribe when the theme changes.
  const { theme } = useTheme()
  const themeRef = useRef(theme)
  themeRef.current = theme

  // --- `wide`: breaking out of the chat column -------------------------------
  // The frame's width ceiling is not its own: the transcript row wrapper caps it
  // at `--mc-content-width` (default 900px, user-configurable), so on a wide
  // monitor an "expanded" app was still ~860px across. `wide` escapes that by
  // measuring the scroll area and shifting itself left with a negative margin.
  //
  // Measured rather than computed because the width cannot be derived: two of
  // the three content-width settings are PERCENTAGES ('84%' / '92%'), and the
  // usable width also moves with the nav rail and the side panel.
  //
  // `null` = no breakout (viewport too narrow to gain anything, or not `wide`),
  // in which case the frame keeps its normal in-column width.
  const [breakout, setBreakout] = useState<{ width: number; shift: number } | null>(null)
  const wideWidthRef = useRef<number | null>(null)
  wideWidthRef.current = presentation === 'wide' ? breakout?.width ?? null : null

  useEffect(() => {
    if (presentation !== 'wide') {
      setBreakout(null)
      return
    }
    const el = wrapperRef.current
    // The row wrapper that carries the width cap is an ancestor; the scroll area
    // is what we may expand into.
    const host = el?.parentElement
    const scroller = el?.closest('.chat-container') as HTMLElement | null
    if (!el || !host || !scroller) return

    const measure = () => {
      // `host` is measured, never `el`: a negative margin on a child does not
      // resize its parent, so the parent's box stays a stable reading of the
      // column even after we have applied a breakout. Measuring `el` would feed
      // its own widened width back in.
      const hostRect = host.getBoundingClientRect()
      // clientWidth already nets out the reserved scrollbar gutter.
      const width = Math.max(0, scroller.clientWidth - WIDE_GUTTER * 2)
      if (width <= hostRect.width + 1) {
        setBreakout(null) // narrow window: the column already fills the pane
        return
      }
      const shift = Math.round(hostRect.left - scroller.getBoundingClientRect().left - WIDE_GUTTER)
      setBreakout({ width: Math.round(width), shift })
    }

    measure()
    if (typeof ResizeObserver === 'undefined') return
    // Observe BOTH: the scroller catches window resizes, rail collapse and the
    // side panel; `host` catches a content-width setting change, which resizes
    // the column without resizing the pane.
    const ro = new ResizeObserver(measure)
    ro.observe(scroller)
    ro.observe(host)
    return () => ro.disconnect()
  }, [presentation])

  const srcDoc = useMemo(() => buildMcpAppSrcdoc(payload), [payload])
  const allow = useMemo(() => buildAllowAttribute(payload.permissions), [payload.permissions])

  // Keep the latest structured content available to the (stable) message
  // handler without re-subscribing the listener on every payload identity
  // change.
  const structuredRef = useRef<unknown>(payload.structured_content ?? null)
  structuredRef.current = payload.structured_content ?? null
  // Same pattern for the spool id — the capability token the tools/call
  // relay presents to the gateway.
  const spoolIdRef = useRef<string>(payload.spool_id)
  spoolIdRef.current = payload.spool_id
  // Originating tools/call inputs + result content (ui/notifications/*).
  const toolInputRef = useRef<unknown>(payload.tool_input ?? null)
  toolInputRef.current = payload.tool_input ?? null
  const resultContentRef = useRef<unknown>(payload.result_content ?? null)
  resultContentRef.current = payload.result_content ?? null
  // Session binding: the relay endpoint verifies the caller's session owns
  // the spool record, so the fetch must carry this session's key.
  const sessionKeyRef = useRef<string>(payload.session_key)
  sessionKeyRef.current = payload.session_key
  // Callback capability — delivered over the owner-WS render frame, relayed
  // on every tools/call. The gateway authorizes on THIS, not on the spool id.
  const callbackSecretRef = useRef<string>(payload.callback_secret ?? '')
  callbackSecretRef.current = payload.callback_secret ?? ''
  // Bound concurrent app→gateway callbacks: a hostile/buggy app document must
  // not be able to accumulate unbounded in-flight HTTP/socket/backend work.
  // Stable label for log lines emitted by the app (the message handler has []
  // deps, so it must read through a ref rather than close over `payload`).
  const labelRef = useRef<string>(`${payload.server}/${payload.tool}`)
  labelRef.current = `${payload.server}/${payload.tool}`
  const inFlightRef = useRef<number>(0)
  // Navigation guard: a sandboxed srcdoc iframe can self-navigate its own
  // document (CSP default-src/form-action 'none' do not block navigation), and
  // the contentWindow (WindowProxy) survives the swap — so the `event.source
  // === contentWindow` check alone would still pass for a REPLACEMENT document.
  // We count `load` events: the first is our srcdoc; any further load means the
  // app navigated away, so we invalidate the bridge and stop relaying (the
  // replacement document must never wield the host-held callback_secret).
  const loadCountRef = useRef<number>(0)
  const navigatedRef = useRef<boolean>(false)
  // Pending progressive-reveal step. Held in a ref so unmount (and app
  // navigation) can cancel a reveal mid-flight instead of posting into a torn
  // down frame.
  const revealTimerRef = useRef<number | null>(null)
  // Whether this app document has already completed its init handshake. A
  // SECOND `initialized` must not be serviced: the spool is already marked
  // revealed by then, so the duplicate would take the no-plan branch and post
  // the complete input + result immediately while the first reveal's timer is
  // still armed — the remaining partials would then land AFTER the result and
  // leave a partial-aware app rendering a truncated prefix permanently.
  const initializedRef = useRef<boolean>(false)

  useEffect(() => {
    const handler = (e: MessageEvent) => {
      const iframe = iframeRef.current
      const cw = iframe?.contentWindow
      // Authenticate the sender: only the app document we host may drive the
      // bridge. (A null-origin srcdoc iframe can't be identified by origin, so
      // we identify it by window reference.)
      if (!cw || e.source !== cw) return
      // Navigation-start signal from our injected bridge-guard bootstrap: it
      // fires on the ORIGINAL srcdoc's pagehide/beforeunload — i.e. BEFORE a
      // navigated-to document's <head> scripts parse and run. Kill the bridge
      // eagerly here so a replacement page cannot post a tools/call in the
      // window between navigation start and the (later) `load` event and have
      // the host relay it with the original callback capability. Forging this
      // signal can only make the bridge MORE restrictive (self-DoS), so it
      // needs no authenticating nonce.
      if (
        e.data &&
        typeof e.data === 'object' &&
        (e.data as { __kirocrew_nav__?: unknown }).__kirocrew_nav__
      ) {
        navigatedRef.current = true
        return
      }
      // Bridge is dead once the app navigates its document away from our
      // srcdoc — a replacement page keeps the same contentWindow but must not
      // drive tool calls with the host's callback capability. (The iframe
      // `load` handler is the backstop; the nav signal above is the primary,
      // pre-load trigger.)
      if (navigatedRef.current) return
      const msg = e.data as JsonRpcMessage | null
      if (!msg || typeof msg !== 'object' || typeof msg.method !== 'string') return

      const post = (out: object) => {
        // Null-origin target → we must post with '*'. The receiver is the
        // isolated app; no sensitive host data is ever placed in these frames.
        // '*' is REQUIRED here: the sandboxed srcdoc iframe has a null (opaque)
        // origin, which no non-wildcard targetOrigin can match. Confidentiality
        // holds because we post ONLY to this iframe's own contentWindow and the
        // frames carry no host secrets (see component doc block).
        // nosemgrep: javascript.browser.security.wildcard-postmessage-configuration.wildcard-postmessage-configuration
        try { cw.postMessage(out, '*') } catch { /* frame torn down */ }
      }
      // A request carries an id; a notification does not.
      const isRequest = msg.id !== undefined && msg.id !== null

      switch (msg.method) {
        case M_INITIALIZE:
          if (isRequest) {
            post({
              jsonrpc: '2.0',
              id: msg.id,
              result: {
                protocolVersion: PROTOCOL_VERSION,
                hostInfo: { name: 'kirocrew', version: '0.1' },
                hostCapabilities: HOST_CAPABILITIES,
                hostContext: {
                  theme: themeRef.current,
                  platform: 'web',
                  displayMode: PROTOCOL_MODE[presentationRef.current],
                  availableDisplayModes: AVAILABLE_DISPLAY_MODES,
                  containerDimensions: dimensionsFor(presentationRef.current, wideWidthRef.current),
                },
              },
            })
          }
          return

        case N_INITIALIZED: {
          // App finished its own init handshake → deliver the tool invocation
          // context: the ORIGINATING tools/call arguments and result content
          // captured by the gateway at interception time, so apps that
          // initialize from their inputs get real state.
          //
          // Exactly once per app document: a duplicate would race the in-flight
          // reveal and invert the notification ordering (see initializedRef).
          if (initializedRef.current) return
          initializedRef.current = true
          const args = toolInputRef.current ?? {}
          const postResult = () => {
            post({
              jsonrpc: '2.0',
              method: N_TOOL_RESULT,
              params: {
                content: Array.isArray(resultContentRef.current) ? resultContentRef.current : [],
                structuredContent: structuredRef.current ?? null,
              },
            })
          }
          const postComplete = () => {
            post({ jsonrpc: '2.0', method: N_TOOL_INPUT, params: { arguments: args } })
            // Ordering is a contract: the result may only land once the app has
            // been told the arguments are complete.
            postResult()
          }
          // Reveal the arguments progressively when they have a revealable
          // shape, the user has not asked for reduced motion, and this spool has
          // not already animated in this page session (a remounted transcript
          // frame must not replay history's draw-on).
          const plan =
            prefersReducedMotion() || hasRevealed(spoolIdRef.current)
              ? null
              : planReveal(args)
          if (!plan) {
            postComplete()
            return
          }
          markRevealed(spoolIdRef.current)
          const step = (i: number) => {
            revealTimerRef.current = null
            // The bridge dies with the app document; a reveal in flight when the
            // app navigates must stop rather than post into the replacement.
            if (navigatedRef.current) return
            if (i >= plan.frames.length) {
              postComplete()
              return
            }
            post({
              jsonrpc: '2.0',
              method: N_TOOL_INPUT_PARTIAL,
              params: { arguments: plan.frames[i] },
            })
            revealTimerRef.current = window.setTimeout(() => step(i + 1), plan.stepMs)
          }
          // First frame goes out synchronously, so the app paints immediately
          // and the reveal reads as drawing rather than as a delayed start.
          step(0)
          return
        }

        case N_SIZE_CHANGED: {
          const params = msg.params as { width?: number; height?: number } | undefined
          const h = params?.height
          if (typeof h === 'number' && isFinite(h) && h > 0) {
            setHeight(Math.min(Math.round(h), MAX_HEIGHT))
          }
          return
        }

        case M_TOOLS_CALL: {
          // App → gateway tool callback. The dashboard endpoint relays to
          // gatewayd, which re-verifies the spool capability token and only
          // permits tools whose _meta.ui.visibility includes "app".
          if (!isRequest) return
          const params = msg.params as { name?: string; arguments?: unknown } | undefined
          const name = params?.name
          if (typeof name !== 'string' || !name) {
            post({ jsonrpc: '2.0', id: msg.id, error: { code: -32602, message: 'missing tool name' } })
            return
          }
          // Per-frame in-flight cap: reject overflow instead of letting a
          // hostile app queue unbounded backend work.
          if (inFlightRef.current >= 16) {
            post({ jsonrpc: '2.0', id: msg.id, error: { code: -32000, message: 'too many concurrent calls' } })
            return
          }
          inFlightRef.current += 1
          void (async () => {
            try {
              const resp = await fetch('/api/mcp-apps/call', {
                method: 'POST',
                headers: {
                  'Content-Type': 'application/json',
                  // Session-ownership binding: the endpoint refuses to relay
                  // for a session other than the one the spool record names.
                  'X-Session-Key': sessionKeyRef.current,
                },
                body: JSON.stringify({
                  spool_id: spoolIdRef.current,
                  // The capability the gateway actually authorizes on.
                  callback_secret: callbackSecretRef.current,
                  tool: name,
                  arguments: params?.arguments ?? {},
                }),
              })
              const body = (await resp.json().catch(() => null)) as
                | { result?: unknown; error?: unknown }
                | null
              if (resp.ok && body && 'result' in body) {
                post({ jsonrpc: '2.0', id: msg.id, result: body.result })
              } else if (body && body.error && typeof body.error === 'object') {
                // Backend JSON-RPC error relayed verbatim.
                post({ jsonrpc: '2.0', id: msg.id, error: body.error })
              } else {
                const message = body && typeof body.error === 'string' ? body.error : `call failed (${resp.status})`
                post({ jsonrpc: '2.0', id: msg.id, error: { code: -32000, message } })
              }
            } catch {
              post({ jsonrpc: '2.0', id: msg.id, error: { code: -32000, message: 'network error' } })
            } finally {
              inFlightRef.current -= 1
            }
          })()
          return
        }

        case M_REQUEST_DISPLAY_MODE: {
          // App-initiated display-mode change (e.g. excalidraw's fullscreen
          // button, which is how it enters its EDITABLE canvas — in `inline` it
          // renders a static preview).
          //
          // `fullscreen` is granted as `wide` — a broken-out in-transcript frame
          // — and NEVER as `overlay`: promoting to a viewport sheet is the user's
          // call alone (see Presentation). The sheet is the user's choice in BOTH
          // directions, so an app that re-requests `fullscreen` while it is open
          // KEEPS the overlay rather than being demoted to `wide`; otherwise a
          // re-render could close a sheet the user deliberately opened.
          // `inline` still collapses from any presentation — that is the app's
          // own Escape path (excalidraw binds Esc to request `inline`), and
          // honouring it is what makes Esc work from inside the iframe.
          if (!isRequest) return
          const requested = (msg.params as { mode?: unknown } | undefined)?.mode
          const current = presentationRef.current
          const next: Presentation =
            requested === 'fullscreen' ? (current === 'overlay' ? 'overlay' : 'wide')
              : requested === 'inline' ? 'inline'
                : current
          presentationRef.current = next
          setPresentation(next)
          post({ jsonrpc: '2.0', id: msg.id, result: { mode: PROTOCOL_MODE[next] } })
          // The request-display-mode RESULT carries only `mode`, so the app has
          // no way to learn the height it must lay its fullscreen surface out
          // against. Follow the grant with the context update.
          post({
            jsonrpc: '2.0',
            method: N_HOST_CONTEXT_CHANGED,
            params: {
              displayMode: PROTOCOL_MODE[next],
              // A just-granted `wide` has not been measured yet (the effect runs
              // after this commit), so the width is omitted here and delivered by
              // the breakout re-notify below.
              containerDimensions: dimensionsFor(next, wideWidthRef.current),
            },
          })
          return
        }

        case M_OPEN_LINK: {
          // The app's "Open in Excalidraw" / menu links.
          if (!isRequest) return
          const url = (msg.params as { url?: unknown } | undefined)?.url
          if (!isOpenableUrl(url)) {
            // Report the refusal in-band (spec: isError) instead of throwing a
            // protocol error — the app can surface it to the user.
            post({ jsonrpc: '2.0', id: msg.id, result: { isError: true } })
            return
          }
          // noopener,noreferrer: the opened tab must not receive a window.opener
          // handle back into the dashboard origin.
          //
          // The return value is deliberately NOT inspected: per the HTML spec
          // `window.open` returns null whenever `noopener`/`noreferrer` is in the
          // feature string, so a successful open is indistinguishable from a
          // popup block. Treating null as failure would report `isError: true`
          // on every successful open.
          window.open(url, '_blank', 'noopener,noreferrer')
          post({ jsonrpc: '2.0', id: msg.id, result: { isError: false } })
          return
        }

        // NOTE: ui/update-model-context is deliberately NOT handled yet, so it
        // still returns -32601 below. The app uses it to tell the MODEL about
        // user edits, and there is no dashboard->session route to deliver that
        // today; answering with an EmptyResult would falsely tell the app the
        // context landed. Honest refusal until the backend route exists.

        case N_LOG_MESSAGE: {
          // App-emitted diagnostics. Dropping these (spec-legal for an unknown
          // notification) makes a misbehaving app impossible to debug from
          // outside: excalidraw routes its entire display-mode/editor-lifecycle
          // trace through app.sendLog, so the one signal that explains a stuck
          // app would be discarded. Forward to the console, tagged with the
          // server/tool so multiple apps stay separable.
          //
          // Treated as UNTRUSTED text: passed as a console argument (never
          // interpolated into a format string) and length-capped, so a hostile
          // app cannot flood or forge host log lines.
          if (isRequest) return // a log is a notification; ignore a malformed request form
          const p = msg.params as { level?: unknown; logger?: unknown; data?: unknown } | undefined
          const level = typeof p?.level === 'string' ? p.level : 'info'
          const logger = typeof p?.logger === 'string' ? p.logger.slice(0, 40) : '-'
          const data = typeof p?.data === 'string' ? p.data.slice(0, 2000) : p?.data
          // eslint-disable-next-line no-console
          console.debug(`[mcp-app ${labelRef.current}] ${level} ${logger}:`, data)
          return
        }

        default:
          // Unsupported REQUEST (e.g. tools/call) → JSON-RPC method-not-found.
          // Unsupported notifications (no id) are silently ignored per spec.
          if (isRequest) {
            post({ jsonrpc: '2.0', id: msg.id, error: { code: -32601, message: 'not supported yet' } })
          }
          return
      }
    }
    window.addEventListener('message', handler)
    return () => {
      window.removeEventListener('message', handler)
      if (revealTimerRef.current !== null) {
        window.clearTimeout(revealTimerRef.current)
        revealTimerRef.current = null
      }
    }
  }, [])

  // Push a partial hostContext update into the app. Mirrors the inbound bridge's
  // guards — never post into a document that navigated away.
  const notifyHostContext = useCallback((next: Presentation, wideWidth: number | null) => {
    const cw = iframeRef.current?.contentWindow
    if (!cw || navigatedRef.current) return
    try {
      // nosemgrep: javascript.browser.security.wildcard-postmessage-configuration.wildcard-postmessage-configuration
      cw.postMessage(
        {
          jsonrpc: '2.0',
          method: N_HOST_CONTEXT_CHANGED,
          // Partial context update — only the changed fields, per spec.
          params: {
            theme: themeRef.current,
            displayMode: PROTOCOL_MODE[next],
            containerDimensions: dimensionsFor(next, wideWidth),
          },
        },
        '*',
      )
    } catch { /* frame torn down */ }
  }, [])

  // A theme switch must reach an app that is ALREADY mounted — fixing only the
  // initialize reply above would leave the bug reachable by toggling the theme
  // with an app open.
  //
  // Routed through `notifyHostContext` rather than its own postMessage so the
  // frame keeps exactly one outbound post site: that one already carries the
  // navigated-away guard and the wildcard-origin review annotation, and adding a
  // second would mean re-auditing a null-origin sandboxed target.
  //
  // Compares against the last theme SENT rather than using a first-run flag:
  // StrictMode remounts effects, and a boolean would post a spurious update on
  // the second mount.
  const sentThemeRef = useRef(theme)
  useEffect(() => {
    if (sentThemeRef.current === theme) return
    sentThemeRef.current = theme
    notifyHostContext(presentationRef.current, wideWidthRef.current)
  }, [theme, notifyHostContext])

  // Host-initiated presentation change (the header controls).
  // The app MUST be told: it may gate an editable surface on the mode, and a
  // host that silently resizes leaves the two out of sync.
  //
  // `next` is derived from the ref rather than inside a setState updater: React
  // may invoke an updater twice (StrictMode), and the notify below is a side
  // effect — running it in the updater double-posted per click in dev.
  const goTo = useCallback((next: Presentation) => {
    const current = presentationRef.current
    if (next === current) return
    // Remember where a promotion came from, so dismissing the overlay restores
    // the user's previous choice instead of always collapsing to inline.
    if (next === 'overlay') setRestoreTo(current)
    presentationRef.current = next
    setPresentation(next)
    notifyHostContext(next, next === 'wide' ? wideWidthRef.current : null)
  }, [notifyHostContext])

  const toggleExpanded = useCallback(() => {
    goTo(presentationRef.current === 'inline' ? 'wide' : 'inline')
  }, [goTo])

  const toggleOverlay = useCallback(() => {
    goTo(presentationRef.current === 'overlay' ? restoreTo : 'overlay')
  }, [goTo, restoreTo])

  // While expanded, the granted height is derived from the viewport — so a window
  // resize invalidates it. The frame's own height recomputes on any re-render but
  // the height the APP was told is posted once, so the two silently diverge: the
  // app keeps laying out against the stale value (it hard-sets html/body height
  // from containerDimensions.height) and gets clipped when the window shrinks, or
  // leaves a dead band when it grows. Re-notify on resize, debounced, and only
  // while expanded (inline apps self-size via size-changed).
  const [resizeTick, setResizeTick] = useState(0)
  useEffect(() => {
    if (!expanded) return
    let t: ReturnType<typeof setTimeout> | undefined
    const onResize = () => {
      if (t) clearTimeout(t)
      t = setTimeout(() => {
        setResizeTick((n) => n + 1) // recompute the frame height
        // Both expanded presentations are viewport-derived, so both need this.
        notifyHostContext(presentationRef.current, wideWidthRef.current)
      }, 150)
    }
    window.addEventListener('resize', onResize)
    return () => {
      if (t) clearTimeout(t)
      window.removeEventListener('resize', onResize)
    }
  }, [expanded, notifyHostContext])

  // The `wide` width is measured AFTER the mode is granted, and can then change
  // again (rail collapse, side panel, content-width setting) without a window
  // resize firing. Push each settled measurement to the app, otherwise it lays
  // out against a width that is missing or stale.
  useEffect(() => {
    if (presentation !== 'wide' || !breakout) return
    notifyHostContext('wide', breakout.width)
  }, [presentation, breakout, notifyHostContext])

  // Must match the `height` granted in dimensionsFor — a frame shorter than the
  // advertised height would clip the app's own layout.
  // resizeTick is a deliberate recompute trigger, not a value.
  void resizeTick
  const displayHeight = expanded ? expandedHeightPx(presentation) : height

  // The overlay is promoted IN PLACE (position: fixed on this same wrapper)
  // rather than portaled, because portaling would move the iframe in the DOM and
  // reload the app — see OverlayChrome.
  //
  // z-[90] clears the chat content, the topbar and anchored menus. It cannot
  // out-stack the theme-experience layer (overlays z-45, mute button z-50,
  // consent modal z-120): that layer is mounted in main.tsx as a SIBLING of the
  // dashboard shell, and the shell is `relative z-[1]`, so it forms a stacking
  // context that confines every descendant regardless of value. Those elements
  // therefore still float above the sheet.
  const wrapperClass = isOverlay
    ? 'fixed left-1/2 top-1/2 z-[90] rounded-lg border border-border overflow-hidden bg-card shadow-2xl'
    : 'my-2 rounded-md border border-border overflow-hidden bg-card'
  const wrapperStyle: React.CSSProperties | undefined = isOverlay
    ? { width: overlayWidthPx(), transform: 'translate(-50%, -50%)' }
    : breakout
      ? { width: breakout.width, marginLeft: -breakout.shift }
      : undefined

  return (
    <>
      {isOverlay && <OverlayChrome frameRef={wrapperRef} onClose={toggleOverlay} />}
      {/* Hold the vacated space while the frame is promoted out of flow, so the
          transcript does not reflow and jump the user's scroll position. */}
      {isOverlay && <div style={{ height: displayHeight }} aria-hidden="true" />}
      <div
        ref={wrapperRef}
        className={wrapperClass}
        style={wrapperStyle}
        {...(isOverlay
          ? {
              role: 'dialog',
              'aria-modal': true,
              'aria-label': `${payload.server} / ${payload.tool}`,
              tabIndex: -1,
              // Contain keystrokes while the sheet is open, matching the dialog
              // convention already used elsewhere (see ConnectRepoModal). This is
              // a DATA-LOSS guard, not just polish: the global shortcut layer
              // binds Ctrl/Alt+digit to chat jumps, and switching session
              // unmounts this frame — reloading the sandboxed app and destroying
              // the user's in-progress work — from a keystroke aimed at a sheet
              // that is covering the whole viewport.
              //
              // Escape still works: useDialogFocusTrap listens in the CAPTURE
              // phase (documented there for exactly this reason), so it sees the
              // key before this bubble-phase guard stops it.
              onKeyDown: (e: React.KeyboardEvent) => e.stopPropagation(),
            }
          : {})}
      >
        <div className="flex items-center justify-between px-3 py-1.5 border-b border-border bg-bg-elevated">
          <span className="text-[12px] font-mono text-muted truncate">
            <span className="text-text">{payload.server}</span>
            <span className="mx-1 opacity-60">/</span>
            {payload.tool}
          </span>
          <IconButtonGroup>
            {/* Expand/collapse is meaningless while the sheet is open — the
                overlay is already larger than `wide` — so it is hidden there and
                the sheet offers only "restore". */}
            {!isOverlay && (
              <IconButton
                onClick={toggleExpanded}
                title={expanded
                  ? i18nT('components.mcpAppFrame.collapse')
                  : i18nT('components.mcpAppFrame.expand')}
                aria-label={expanded
                  ? i18nT('components.mcpAppFrame.collapse_app')
                  : i18nT('components.mcpAppFrame.expand_app')}
              >
                {expanded ? <Minimize2 size={12} /> : <Maximize2 size={12} />}
              </IconButton>
            )}
            <IconButton
              onClick={toggleOverlay}
              title={isOverlay
                ? i18nT('components.mcpAppFrame.exit_full_screen_esc')
                : i18nT('components.mcpAppFrame.full_screen')}
              aria-label={isOverlay
                ? i18nT('components.mcpAppFrame.exit_full_screen')
                : i18nT('components.mcpAppFrame.open_app_full_screen')}
            >
              {isOverlay ? <Shrink size={12} /> : <Expand size={12} />}
            </IconButton>
          </IconButtonGroup>
        </div>
        <iframe
          ref={iframeRef}
          onLoad={() => {
            // First load = our srcdoc; any subsequent load = the app navigated
            // its document, so the bridge is no longer talking to trusted HTML.
            loadCountRef.current += 1
            if (loadCountRef.current > 1) navigatedRef.current = true
          }}
          // NO allow-same-origin — see component doc comment (origin isolation).
          sandbox="allow-scripts allow-forms"
          srcDoc={srcDoc}
          // Explicit tab stop so the app itself is keyboard-reachable. Without
          // it the overlay's focus trap has nothing but the header button in its
          // focusable set — `useDialogFocusTrap`'s FOCUSABLE selector matches
          // `[tabindex]:not([tabindex="-1"])`, which a bare <iframe> does not
          // satisfy — so Tab cycled on that one control and a keyboard user
          // could never reach the canvas they had just opened full screen.
          tabIndex={0}
          allow={allow || undefined}
          className="w-full border-none bg-card block"
          style={{ height: displayHeight }}
          title={`${payload.server} / ${payload.tool}`}
        />
      </div>
    </>
  )
}
