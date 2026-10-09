import { useEffect, useRef, type RefObject } from 'react'

/**
 * A sandboxed crew page asking its host to open one pull request.
 *
 * The crew frames are `sandbox="allow-scripts"` with no `allow-popups`, so a
 * `target="_blank"` link inside them is a dead click -- deliberately: a page an
 * agent wrote must not open windows. This is the one narrow way out. The page
 * posts `{type: 'kirocrew-dashboard:open', url}` and the host opens the URL only
 * when BOTH hold:
 *
 * - the message came from one of THIS host's own frames (`event.source`), so
 *   another window posting the same shape is ignored;
 * - the URL is exactly a GitHub pull request page. No other host, no query, no
 *   fragment, no fallback: anything else is dropped silently;
 * - the person just clicked or pressed a key IN THAT FRAME: the host has a live user
 *   activation and its focused element is that very iframe. Activation alone is
 *   page-wide (typing in the composer sets it), so focus is what ties the gesture
 *   to the frame. A page posting on its own, on a loop, opens nothing.
 *
 * The open is `window.open(url, '_blank', 'noopener,noreferrer')`, the path every
 * other external link here takes (McpAppFrame's open-link, the share and login
 * links): the desktop app's window-open handler hands it to the system browser,
 * and a plain browser opens a tab with no `opener` back into the dashboard.
 */
export const OPEN_MESSAGE_TYPE = 'kirocrew-dashboard:open'

/** The only URLs a crew page may ask its host to open. */
export const PR_URL_RE = /^https:\/\/github\.com\/[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+\/pull\/[0-9]+\/?$/

/** The URL an open message asks for, or null when the message is not one this host honours. */
export function openUrlOf(data: unknown): string | null {
  if (!data || typeof data !== 'object') return null
  const msg = data as { type?: unknown; url?: unknown }
  if (msg.type !== OPEN_MESSAGE_TYPE || typeof msg.url !== 'string') return null
  return PR_URL_RE.test(msg.url) ? msg.url : null
}

/** Whether the person just made a gesture inside *frame*. No API counts as no. */
function hasGesture(frame: HTMLIFrameElement): boolean {
  return Boolean(navigator.userActivation?.isActive) && document.activeElement === frame
}

function openExternal(url: string): void {
  window.open(url, '_blank', 'noopener,noreferrer')
}

/**
 * Honour open messages from the given frames. `open` and `gesture` are injectable
 * for tests only.
 */
export function useFrameOpenLink(
  frames: ReadonlyArray<RefObject<HTMLIFrameElement | null>>,
  open: (url: string) => void = openExternal,
  gesture: (frame: HTMLIFrameElement) => boolean = hasGesture,
): void {
  const framesRef = useRef(frames)
  framesRef.current = frames
  const openRef = useRef(open)
  openRef.current = open
  const gestureRef = useRef(gesture)
  gestureRef.current = gesture
  useEffect(() => {
    const onMessage = (event: MessageEvent) => {
      if (!event.source) return
      const mine = framesRef.current.find((f) => f.current?.contentWindow === event.source)?.current
      if (!mine) return
      const url = openUrlOf(event.data)
      if (url && gestureRef.current(mine)) openRef.current(url)
    }
    window.addEventListener('message', onMessage)
    return () => window.removeEventListener('message', onMessage)
  }, [])
}
