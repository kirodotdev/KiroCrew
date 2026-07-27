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

/**
 * Subscribe to the live browse-mirror frame stream.
 *
 * Frames arrive as `kirocrew-browser-frame` window events (dispatched from the
 * WS `browser_frame` message in useWebSocket) — each is a screenshot the agent
 * (or the proxy's idle active-pump) captured, forwarded by the Playwright MCP
 * proxy. This hook is presentation-agnostic: it owns only the frame state and
 * the session-title lookup — used by the floating `BrowserLiveView` window.
 */
export function useBrowserFrame(): BrowserFrameState {
  const [frame, setFrame] = useState<string | null>(null)
  const [lastTs, setLastTs] = useState<number | null>(null)
  const [sessionKey, setSessionKey] = useState<string | null>(null)

  useEffect(() => {
    const onFrame = (e: Event) => {
      const d = (e as CustomEvent<BrowserFrameDetail>).detail
      if (!d?.data) return
      setFrame(`data:image/${d.format || 'jpeg'};base64,${d.data}`)
      setLastTs(Date.now())
      setSessionKey(d.session_key || null)
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
