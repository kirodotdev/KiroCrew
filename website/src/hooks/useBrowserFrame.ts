import { useEffect, useMemo, useState } from 'react'

import { useAppSelector } from '../store'

/** Shape of the `kirocrew-browser-frame` CustomEvent detail (mirrors the WS
 *  `browser_frame` payload built by the gateway's `build_frame_payload`). */
export interface BrowserFrameDetail {
  data: string
  format?: string
  device_width?: number | null
  device_height?: number | null
  session_key?: string
}

export interface BrowserFrameState {
  /** Latest frame as a `data:` URI, or null before the first frame arrives. */
  frame: string | null
  /** Wall-clock ms of the last frame, or null. */
  lastTs: number | null
  /** Opaque session key carried on the frame wire (client-side lookup only). */
  sessionKey: string | null
  /** Human title for `sessionKey` resolved from the slot store, if known. The
   *  raw key never renders — the title is looked up from the trusted store. */
  sessionName: string | null
}

// Module-scope last-frame buffer. Frames are live-only window events with no
// replay, so a panel mounting AFTER a frame arrived (the common case: the
// Browser tab auto-opens when the agent starts browsing) would otherwise show
// nothing until the next frame. Buffering the latest frame lets a freshly
// mounted hook paint immediately. Session scoping + the live-TTL in the panel
// still gate what actually renders, so a stale or cross-session cached frame
// never displays.
let cachedFrame: string | null = null
let cachedTs: number | null = null
let cachedSessionKey: string | null = null

// Populate the buffer from the moment this module loads — BEFORE any component
// using the hook mounts. The very first frame arrives as the Browser tab is
// auto-opening (the panel's own per-mount listener isn't installed yet), so
// without this module-load listener that first frame would be missed and the
// freshly mounted panel would paint blank until the next pump frame. The hook's
// useState initializers read this buffer, so a late-mounting panel paints the
// last frame immediately. Session scoping + the panel's live-TTL still gate
// what actually renders.
if (typeof window !== 'undefined') {
  window.addEventListener('kirocrew-browser-frame', (e: Event) => {
    const d = (e as CustomEvent<BrowserFrameDetail>).detail
    if (!d?.data) return
    cachedFrame = `data:image/${d.format || 'jpeg'};base64,${d.data}`
    cachedTs = Date.now()
    cachedSessionKey = d.session_key || null
  })
}

/**
 * Subscribe to the live browse-mirror frame stream.
 *
 * Frames arrive as `kirocrew-browser-frame` window events (dispatched from the
 * WS `browser_frame` message in useWebSocket) — each is a screenshot the agent
 * (or the proxy's idle active-pump) captured, forwarded by the Playwright MCP
 * proxy. This hook is presentation-agnostic: it owns only the frame state and
 * the session-title lookup — used by the Browser panel's live mirror
 * (WebPreviewPanel).
 */
export function useBrowserFrame(): BrowserFrameState {
  const [frame, setFrame] = useState<string | null>(() => cachedFrame)
  const [lastTs, setLastTs] = useState<number | null>(() => cachedTs)
  const [sessionKey, setSessionKey] = useState<string | null>(() => cachedSessionKey)

  useEffect(() => {
    const onFrame = (e: Event) => {
      const d = (e as CustomEvent<BrowserFrameDetail>).detail
      if (!d?.data) return
      const uri = `data:image/${d.format || 'jpeg'};base64,${d.data}`
      const ts = Date.now()
      const sk = d.session_key || null
      // Buffer at module scope so a panel that mounts AFTER this frame (e.g. the
      // Browser tab auto-opening when the agent starts browsing) paints it
      // immediately instead of waiting for the next active-pump frame.
      cachedFrame = uri
      cachedTs = ts
      cachedSessionKey = sk
      setFrame(uri)
      setLastTs(ts)
      setSessionKey(sk)
    }
    window.addEventListener('kirocrew-browser-frame', onFrame)
    return () => window.removeEventListener('kirocrew-browser-frame', onFrame)
  }, [])

  // Resolve the mirrored session's display title from the client's own slot
  // store. Only the opaque session key rides the frame wire; the title (which
  // is user/agent-set text) never crosses it — it's already in the trusted store.
  const slots = useAppSelector(s => s.dashboard.slots)
  const sessionName = useMemo(
    () => (sessionKey ? slots.find(s => s.key === sessionKey)?.title || null : null),
    [slots, sessionKey],
  )

  return { frame, lastTs, sessionKey, sessionName }
}
