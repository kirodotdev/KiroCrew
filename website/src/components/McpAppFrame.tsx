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
const PROTOCOL_VERSION = '2026-01-26'
const M_INITIALIZE = 'ui/initialize'
const M_TOOLS_CALL = 'tools/call'
const N_INITIALIZED = 'ui/notifications/initialized'
const N_TOOL_INPUT = 'ui/notifications/tool-input'
const N_TOOL_RESULT = 'ui/notifications/tool-result'
const N_SIZE_CHANGED = 'ui/notifications/size-changed'

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
 * `ui/notifications/size-changed`, and relays `tools/call` requests to the
 * gateway via `POST /api/mcp-apps/call` (the gateway enforces that only
 * app-visible tools are callable). Every other not-yet-supported request is
 * rejected with a JSON-RPC method-not-found error. Every inbound message is
 * authenticated by `event.source === iframe.contentWindow` before it is acted on.
 */
export default function McpAppFrame({ payload }: { payload: McpAppRenderPayload }) {
  const iframeRef = useRef<HTMLIFrameElement>(null)
  const [height, setHeight] = useState(DEFAULT_HEIGHT)
  const [expanded, setExpanded] = useState(false)

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
                hostCapabilities: {},
                hostContext: {
                  theme: 'dark',
                  platform: 'web',
                  displayMode: 'inline',
                  availableDisplayModes: ['inline'],
                  containerDimensions: { maxHeight: MAX_HEIGHT },
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

  const toggleExpanded = useCallback(() => setExpanded((v) => !v), [])
  const displayHeight = expanded ? MAX_HEIGHT : height

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
