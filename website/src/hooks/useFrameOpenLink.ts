import { useEffect, useRef, type RefObject } from 'react'

/**
 * A sandboxed page asking its host to open a link: the listener under
 * `useFrameLinkBridge`, which every agent-page frame (crew Dashboard pages,
 * artifacts, widgets) uses.
 *
 * Those frames are `sandbox="allow-scripts"` with no `allow-popups`, so a
 * `target="_blank"` link inside them is a dead click -- deliberately: a page an
 * agent wrote must not open windows. The page posts
 * `{type: 'kirocrew-dashboard:open', url}` and the host acts only when all hold:
 *
 * - the message came from one of THIS host's own frames (`event.source`), so
 *   another window posting the same shape is ignored;
 * - the caller's `accept` rule names a URL (`webUrlOf` for the bridge);
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

/** Whether the person just made a gesture inside *frame*. No API counts as no.
 *  Shared with InstancesViewport's pane image-copy relay, which needs the same
 *  "this click happened in that frame" test. */
export function hasGesture(frame: HTMLIFrameElement): boolean {
  return Boolean(navigator.userActivation?.isActive) && document.activeElement === frame
}

export function openExternal(url: string): void {
  window.open(url, '_blank', 'noopener,noreferrer')
}

/**
 * Honour open messages from the given frames. `accept` is the URL rule
 * (`webUrlOf` in `useFrameLinkBridge`). `open` and `gesture` are injectable for
 * tests; `open` receives the asking frame as well.
 */
export function useFrameOpenLink(
  frames: ReadonlyArray<RefObject<HTMLIFrameElement | null>>,
  accept: (data: unknown) => string | null,
  open: (url: string, frame?: HTMLIFrameElement) => void = openExternal,
  gesture: (frame: HTMLIFrameElement) => boolean = hasGesture,
): void {
  const framesRef = useRef(frames)
  framesRef.current = frames
  const openRef = useRef(open)
  openRef.current = open
  const gestureRef = useRef(gesture)
  gestureRef.current = gesture
  const acceptRef = useRef(accept)
  acceptRef.current = accept
  useEffect(() => {
    const onMessage = (event: MessageEvent) => {
      if (!event.source) return
      const mine = framesRef.current.find((f) => f.current?.contentWindow === event.source)?.current
      if (!mine) return
      const url = acceptRef.current(event.data)
      if (url && gestureRef.current(mine)) openRef.current(url, mine)
    }
    window.addEventListener('message', onMessage)
    return () => window.removeEventListener('message', onMessage)
  }, [])
}
