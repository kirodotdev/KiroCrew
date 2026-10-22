import { createElement, useEffect, useRef, type ReactNode, type RefObject } from 'react'

import { useConfirm } from '../components/ConfirmDialog'
import { i18nT } from '../i18n/t'
import { hasGesture, OPEN_MESSAGE_TYPE, openExternal } from './useFrameOpenLink'

/** Longest URL a page may ask its host to open. A real link is far shorter; the
 *  cap keeps the confirm readable and the message cheap to check. */
export const MAX_WEB_LINK_LENGTH = 2048

/**
 * The web URL an open message asks for, or null when it is not one the link
 * bridge honours.
 *
 * Only an absolute `http:` or `https:` URL passes. `javascript:`, `data:`,
 * `blob:`, `file:`, `mailto:` and every custom scheme are refused, and so is a
 * URL carrying a user name or password, whose `user@host` form makes the host
 * part hard to read in the confirm. The returned value is the PARSED form, so
 * the URL the person reads in the confirm is exactly the one that opens.
 */
export function webUrlOf(data: unknown): string | null {
  if (!data || typeof data !== 'object') return null
  const msg = data as { type?: unknown; url?: unknown }
  if (msg.type !== OPEN_MESSAGE_TYPE || typeof msg.url !== 'string') return null
  if (!msg.url || msg.url.length > MAX_WEB_LINK_LENGTH) return null
  let parsed: URL
  try {
    parsed = new URL(msg.url)
  } catch {
    return null
  }
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return null
  if (parsed.username || parsed.password) return null
  return parsed.href
}

/**
 * The host half of the link bridge for frames that hold agent-written pages
 * (saved artifacts, their thumbnails, a teammate's shared artifact, chat widgets).
 *
 * Those frames are `sandbox="allow-scripts"` with no popup grant, so a page cannot
 * open a window itself. `buildSrcdoc({ linkBridge: true })` turns a `_blank` or
 * bare web link click, and a `window.open` call, into the same
 * `kirocrew-dashboard:open` message the crew pages use. This hook answers it:
 *
 * - only from one of the given frames (`event.source`);
 * - only for a web URL (`webUrlOf`);
 * - only while the person has just clicked or pressed a key in that frame
 *   (`hasGesture`), so a page posting on its own opens nothing;
 * - and only after the person confirms a dialog that shows the full URL, so a
 *   page that ties an open to an unrelated click still cannot open anything the
 *   person did not see and accept.
 *
 * Render the returned node once, next to the frames.
 */
export function useFrameLinkBridge(
  frames: ReadonlyArray<RefObject<HTMLIFrameElement | null>>,
  open: (url: string) => void = openExternal,
  gesture: (frame: HTMLIFrameElement) => boolean = hasGesture,
): ReactNode {
  const { confirm, confirmDialog } = useConfirm()
  const framesRef = useRef(frames)
  framesRef.current = frames
  const openRef = useRef(open)
  openRef.current = open
  const gestureRef = useRef(gesture)
  gestureRef.current = gesture
  const confirmRef = useRef(confirm)
  confirmRef.current = confirm
  useEffect(() => {
    const onMessage = (event: MessageEvent) => {
      if (!event.source) return
      const mine = framesRef.current.find((f) => f.current?.contentWindow === event.source)?.current
      if (!mine) return
      const url = webUrlOf(event.data)
      if (!url || !gestureRef.current(mine)) return
      void confirmRef.current({
        title: i18nT('hooks.frameLinkBridge.title'),
        body: createElement(
          'code',
          { className: 'block break-all font-mono text-xs text-[var(--text)]', translate: 'no' },
          url,
        ),
        confirmLabel: i18nT('hooks.frameLinkBridge.open'),
        danger: false,
        primary: true,
      }).then((ok) => {
        if (ok) openRef.current(url)
      })
    }
    window.addEventListener('message', onMessage)
    return () => window.removeEventListener('message', onMessage)
  }, [])
  return confirmDialog
}
