import { useEffect, useRef, type RefObject } from 'react'
import { FRAME_ALIVE_MESSAGE_TYPE, FRAME_PING_MESSAGE_TYPE } from '../lib/widgetSrcdoc'

/** How long a frame has to answer the ping sent on its `load`. The reply is one
 *  message round trip to a document that has already loaded, so this only has
 *  to cover a busy main thread. */
export const FRAME_ALIVE_TIMEOUT_MS = 4000

/** Re-mints in a row with no answer before the hook stops trying. Any answer
 *  resets the count. */
export const FRAME_ALIVE_MAX_RECOVERIES = 3

/** Re-mint a sandbox-doc frame the browser reloaded onto a spent URL.
 *
 *  A sandbox-doc URL is single-use (`serve_sandbox_doc`), so when the browser
 *  reloads the frame by itself (a gateway restart, a dropped and restored
 *  remote connection, a tab woken from sleep) the reload gets a 404 and the
 *  frame goes white. The parent cannot read that 404: the document has an
 *  opaque origin. So on every `load` of the frame the host pings it, and the
 *  host script `buildSrcdoc({ aliveReply: true })` puts in the document
 *  answers. No answer within `FRAME_ALIVE_TIMEOUT_MS` means the frame holds
 *  something else, and `recover` mints a new URL.
 *
 *  The deadline starts on `load`, because a ping sent before a document loads
 *  can be lost. A frame moved on screen after loading off screen fires no new
 *  `load`, so `alreadyLoaded` says so and the deadline starts at mount instead:
 *  that off-screen load can itself have been a 404. Otherwise the mount ping
 *  has no deadline; an answer to it only resets the count.
 *
 *  `armed` must be false for a frame whose sandbox withholds `allow-scripts`:
 *  its document cannot answer.
 *  shortcut: a static (no-script) frame is not recovered, upgrade if one shows
 *  up white after a reload.
 */
export function useFrameAliveRecovery(
  frameRef: RefObject<HTMLIFrameElement | null>,
  url: string | null,
  armed: boolean,
  recover: () => void,
  /** The frame's document for `url` already fired `load` before it was mounted here. */
  alreadyLoaded = false,
): void {
  const recoverRef = useRef(recover)
  recoverRef.current = recover
  // Read at mount only: it describes how this url's element arrived.
  const alreadyLoadedRef = useRef(alreadyLoaded)
  alreadyLoadedRef.current = alreadyLoaded
  const triesRef = useRef(0)

  useEffect(() => {
    const el = frameRef.current
    if (!armed || !url || !el) return
    let timer: number | null = null
    const stop = () => {
      if (timer !== null) window.clearTimeout(timer)
      timer = null
    }
    const ping = () => {
      // '*' is required: the frame's origin is opaque and has no other name. The
      // message carries only its type, to this frame's own window.
      // nosemgrep: javascript.browser.security.wildcard-postmessage-configuration.wildcard-postmessage-configuration
      el.contentWindow?.postMessage({ type: FRAME_PING_MESSAGE_TYPE }, '*')
    }
    const onLoad = () => {
      stop()
      timer = window.setTimeout(() => {
        timer = null
        if (triesRef.current >= FRAME_ALIVE_MAX_RECOVERIES) return
        triesRef.current += 1
        recoverRef.current()
      }, FRAME_ALIVE_TIMEOUT_MS)
      ping()
    }
    const onMessage = (event: MessageEvent) => {
      if (!el.contentWindow || event.source !== el.contentWindow) return
      const data = event.data as { type?: unknown } | null
      if (!data || data.type !== FRAME_ALIVE_MESSAGE_TYPE) return
      stop()
      triesRef.current = 0
    }
    el.addEventListener('load', onLoad)
    window.addEventListener('message', onMessage)
    if (alreadyLoadedRef.current) onLoad()
    else ping()
    return () => {
      stop()
      el.removeEventListener('load', onLoad)
      window.removeEventListener('message', onMessage)
    }
  }, [frameRef, url, armed])
}
