import { useEffect, useMemo, useState } from 'react'

import { useAppSelector } from '../store'

/**
 * Shape of the `kirocrew-computer-use-frame` CustomEvent detail. Mirrors the WS
 * `computer_use_frame` payload the gateway builds in
 * `computer_use/screencast.py::build_frame_payload`, where every field is bounded
 * to an explicit charset / range before it reaches the wire.
 */
export interface ComputerUseFrameDetail {
  /** Base64 JPEG — always the already-downscaled frame the model itself saw. */
  data: string
  /** Always `'jpeg'`; the gateway refuses any other encoding on this path. */
  format?: string
  width?: number | null
  height?: number | null
  /** Opaque session id, used only to look the title up in the slot store. */
  session_key?: string
  /** Display name of the mirrored application (never the window title). */
  app?: string
}

export interface ComputerUseFrameState {
  /** Latest frame as a `data:` URI, or null before the first frame arrives. */
  frame: string | null
  /** Wall-clock ms of the last frame, or null. */
  lastTs: number | null
  /** Opaque session key carried on the wire (client-side lookup only). */
  sessionKey: string | null
  /** Human title for `sessionKey`, resolved from the trusted slot store. */
  sessionName: string | null
  /** Mirrored application's display name, if the frame named one. */
  appName: string | null
}

/** Window event the WS layer dispatches for each `computer_use_frame` message. */
export const COMPUTER_USE_FRAME_EVENT = 'kirocrew-computer-use-frame'

/**
 * Subscribe to the computer-use live view (PiP) frame stream.
 *
 * Each frame is a screenshot the agent's own `computer_get_state` call already
 * captured and already received — the gateway relays those exact bytes rather
 * than capturing anything extra, and it drops the frame entirely when the window
 * held a password field or when the governance ceiling denies the `screenshot`
 * observation channel. So the panel showing nothing is a valid, expected state.
 *
 * Presentation-agnostic: this hook owns the frame state and the session-title
 * lookup only. `ComputerUseLiveView` owns the window chrome.
 */
export function useComputerUseFrame(): ComputerUseFrameState {
  const [frame, setFrame] = useState<string | null>(null)
  const [lastTs, setLastTs] = useState<number | null>(null)
  const [sessionKey, setSessionKey] = useState<string | null>(null)
  const [appName, setAppName] = useState<string | null>(null)

  useEffect(() => {
    const onFrame = (e: Event) => {
      const detail = (e as CustomEvent<ComputerUseFrameDetail>).detail
      if (!detail?.data) return
      // The media type is a LITERAL, not read from the payload: the gateway
      // refuses any encoding but JPEG on this path, so trusting `detail.format`
      // here would add a way to influence the data-URI type for no benefit.
      setFrame(`data:image/jpeg;base64,${detail.data}`)
      setLastTs(Date.now())
      setSessionKey(detail.session_key || null)
      setAppName(detail.app || null)
    }
    window.addEventListener(COMPUTER_USE_FRAME_EVENT, onFrame)
    return () => window.removeEventListener(COMPUTER_USE_FRAME_EVENT, onFrame)
  }, [])

  // Resolve the driving session's display title from the client's own slot store.
  // Only the opaque key rides the wire; the title (user/agent-set text) is
  // already in the trusted store and never crosses the frame payload.
  const slots = useAppSelector(s => s.dashboard.slots)
  const sessionName = useMemo(
    () => (sessionKey ? slots.find(s => s.key === sessionKey)?.title || null : null),
    [slots, sessionKey],
  )

  return { frame, lastTs, sessionKey, sessionName, appName }
}
