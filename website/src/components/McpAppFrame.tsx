import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Maximize2, Minimize2 } from 'lucide-react'
import { IconButton, IconButtonGroup } from './ui'
import {
  buildMcpAppSrcdoc,
  buildAllowAttribute,
  type McpAppRenderPayload,
} from '../lib/mcpAppSrcdoc'

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

/** Display modes this host offers. `fullscreen` is honored as an EXPANDED
 *  BUBBLE (a taller inline frame), never as a viewport-covering overlay: these
 *  are conversational inline apps, and yanking the user out of the transcript
 *  to a modal defeats that. The mode name is the app-facing contract (apps
 *  commonly gate an editable/expanded surface on `fullscreen` — excalidraw only
 *  mounts its editor there); how much room the host actually grants is
 *  communicated separately via hostContext.containerDimensions. `pip` is in the
 *  spec's enum but not offered here. */
type DisplayMode = 'inline' | 'fullscreen'
const AVAILABLE_DISPLAY_MODES: DisplayMode[] = ['inline', 'fullscreen']

/** Fraction of the viewport an EXPANDED app gets. Big enough to cover the
 *  response bubble and be genuinely usable for editing, without becoming a
 *  viewport-covering modal. */
const EXPANDED_VH = 0.8

/** Concrete pixel height granted in `fullscreen`. */
function expandedHeightPx(): number {
  const vh = typeof window !== 'undefined' ? window.innerHeight : 0
  return vh > 0 ? Math.min(MAX_HEIGHT, Math.round(vh * EXPANDED_VH)) : MAX_HEIGHT
}

/**
 * hostContext.containerDimensions for a mode.
 *
 * CRITICAL — `fullscreen` MUST carry a concrete `height`, not just `maxHeight`.
 * An app cannot lay out a fullscreen surface from a ceiling alone: inside an
 * iframe `position: fixed` gives the body no height, so a real app (excalidraw)
 * keys its fullscreen layout off `containerDimensions.height` and renders into a
 * ZERO-HEIGHT container when only `maxHeight` is supplied — the mode flips, the
 * editor mounts, and nothing visibly changes. `inline` keeps advertising a
 * ceiling because the app sizes itself there and reports back via
 * ui/notifications/size-changed.
 */
function dimensionsFor(mode: DisplayMode): Record<string, number> {
  return mode === 'fullscreen' ? { height: expandedHeightPx() } : { maxHeight: MAX_HEIGHT }
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
  const [height, setHeight] = useState(DEFAULT_HEIGHT)
  // Negotiated display mode (SEP-1865). Driven from BOTH directions: the app can
  // request a change via ui/request-display-mode, and the host's own
  // expand/collapse button sets it and notifies the app. `expanded` is derived
  // so there is exactly one source of truth (the two used to be able to drift:
  // the header button changed only the local height and the app was never told).
  const [displayMode, setDisplayMode] = useState<DisplayMode>('inline')
  const expanded = displayMode === 'fullscreen'
  // Mirror for the stable message handler (which must not re-subscribe per mode).
  const displayModeRef = useRef<DisplayMode>(displayMode)
  displayModeRef.current = displayMode

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
  // #418 callback capability — delivered over the owner-WS render frame, relayed
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
                  theme: 'dark',
                  platform: 'web',
                  displayMode: displayModeRef.current,
                  availableDisplayModes: AVAILABLE_DISPLAY_MODES,
                  containerDimensions: dimensionsFor(displayModeRef.current),
                },
              },
            })
          }
          return

        case N_INITIALIZED:
          // App finished its own init handshake → deliver the tool invocation
          // context: the ORIGINATING tools/call arguments and result content
          // captured by the gateway at interception time, so apps that
          // initialize from their inputs get real state.
          post({
            jsonrpc: '2.0',
            method: N_TOOL_INPUT,
            params: { arguments: toolInputRef.current ?? {} },
          })
          post({
            jsonrpc: '2.0',
            method: N_TOOL_RESULT,
            params: {
              content: Array.isArray(resultContentRef.current) ? resultContentRef.current : [],
              structuredContent: structuredRef.current ?? null,
            },
          })
          return

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
                  // #418: the capability the gateway actually authorizes on.
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
          // renders a static preview). Previously this fell through to the
          // method-not-found default, so the app's request rejected and it was
          // stuck in a non-interactive mode forever.
          //
          // We grant `fullscreen` as an expanded bubble rather than a viewport
          // overlay (see DisplayMode). Per spec the result reports the mode
          // ACTUALLY set, so an unknown/unsupported mode (e.g. `pip`) is
          // answered with the mode we keep rather than an error.
          if (!isRequest) return
          const requested = (msg.params as { mode?: unknown } | undefined)?.mode
          const next: DisplayMode =
            requested === 'fullscreen' || requested === 'inline'
              ? requested
              : displayModeRef.current
          displayModeRef.current = next
          setDisplayMode(next)
          post({ jsonrpc: '2.0', id: msg.id, result: { mode: next } })
          // The request-display-mode RESULT carries only `mode`, so the app has
          // no way to learn the height it must lay its fullscreen surface out
          // against. Follow the grant with the context update.
          post({
            jsonrpc: '2.0',
            method: N_HOST_CONTEXT_CHANGED,
            params: { displayMode: next, containerDimensions: dimensionsFor(next) },
          })
          return
        }

        case M_OPEN_LINK: {
          // The app's "Open in Excalidraw" / menu links. Previously fell to the
          // -32601 default, so the share flow uploaded the diagram and then the
          // tab never opened — a silent dead end for the user.
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
          // App-emitted diagnostics. Silently dropping these (the old behavior,
          // spec-legal for an unknown notification) makes a misbehaving app
          // impossible to debug from outside: excalidraw routes its entire
          // display-mode/editor-lifecycle trace through app.sendLog, so the one
          // signal that explains a stuck app was being discarded. Forward to the
          // console, tagged with the server/tool so multiple apps stay separable.
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
    return () => window.removeEventListener('message', handler)
  }, [])

  // Push a partial hostContext update into the app. Mirrors the inbound bridge's
  // guards — never post into a document that navigated away.
  const notifyHostContext = useCallback((mode: DisplayMode) => {
    const cw = iframeRef.current?.contentWindow
    if (!cw || navigatedRef.current) return
    try {
      // nosemgrep: javascript.browser.security.wildcard-postmessage-configuration.wildcard-postmessage-configuration
      cw.postMessage(
        {
          jsonrpc: '2.0',
          method: N_HOST_CONTEXT_CHANGED,
          // Partial context update — only the changed fields, per spec.
          params: { displayMode: mode, containerDimensions: dimensionsFor(mode) },
        },
        '*',
      )
    } catch { /* frame torn down */ }
  }, [])

  // Host-initiated display-mode change (the header expand/collapse button).
  // The app MUST be told: it may gate an editable surface on the mode, and a
  // host that silently resizes leaves the two out of sync.
  //
  // `next` is derived from the ref rather than inside a setState updater: React
  // may invoke an updater twice (StrictMode), and the notify below is a side
  // effect — running it in the updater double-posted per click in dev.
  const toggleExpanded = useCallback(() => {
    const next: DisplayMode = displayModeRef.current === 'fullscreen' ? 'inline' : 'fullscreen'
    displayModeRef.current = next
    setDisplayMode(next)
    notifyHostContext(next)
  }, [notifyHostContext])

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
        notifyHostContext('fullscreen') // and tell the app the new one
      }, 150)
    }
    window.addEventListener('resize', onResize)
    return () => {
      if (t) clearTimeout(t)
      window.removeEventListener('resize', onResize)
    }
  }, [expanded, notifyHostContext])

  // Must match the `height` granted in dimensionsFor('fullscreen') — a frame
  // shorter than the advertised height would clip the app's own layout.
  // resizeTick is a deliberate recompute trigger, not a value.
  void resizeTick
  const displayHeight = expanded ? expandedHeightPx() : height

  return (
    <div className="my-2 rounded-md border border-border overflow-hidden bg-card">
      <div className="flex items-center justify-between px-3 py-1.5 border-b border-border bg-bg-elevated">
        <span className="text-[12px] font-mono text-muted truncate">
          <span className="text-text">{payload.server}</span>
          <span className="mx-1 opacity-60">/</span>
          {payload.tool}
        </span>
        <IconButtonGroup>
          <IconButton
            onClick={toggleExpanded}
            title={expanded ? 'Collapse' : 'Expand'}
            aria-label={expanded ? 'Collapse app' : 'Expand app'}
          >
            {expanded ? <Minimize2 size={12} /> : <Maximize2 size={12} />}
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
        allow={allow || undefined}
        className="w-full border-none bg-card block"
        style={{ height: displayHeight }}
        title={`${payload.server} / ${payload.tool}`}
      />
    </div>
  )
}
