import { createElement, useCallback, useState, type ReactNode, type RefObject } from 'react'

import { FrameLinkChip } from '../components/FrameLinkChip'
import { safeHttpUrl } from '../lib/safeUrl'
import { ACTIVATION_LIFESPAN_MS } from '../lib/widgetSrcdoc'
import { openInternalLink, wouldOpenInternalLink } from '../utils/internalNav'
import { hasGesture, OPEN_MESSAGE_TYPE, openExternal, useFrameOpenLink } from './useFrameOpenLink'

/**
 * The dashboard's own last click or key press (`performance.now()`).
 *
 * `hasGesture` sees a live activation and the frame focused, but a live
 * activation also follows a click or key in the dashboard, and a page's script
 * can focus its own frame: a click on an artifact row followed by the page
 * focusing itself would pass. Input inside an agent-page frame never reaches
 * these window listeners (the frame is another origin), so anything recorded is
 * the dashboard's own. Starts at load time, not never: this module can load just
 * after the click that opened the page.
 */
export const hostInput = { lastAt: typeof performance === 'undefined' ? 0 : performance.now() }

/**
 * Where focus last came in from the dashboard (`frame`), and the frame the pointer
 * is over (`pointer`), as the dashboard sees them.
 *
 * Focus moving from the dashboard into a frame blurs the dashboard's window, so
 * `frame` is the iframe focused at that moment; the window's focus clears it.
 * Focus moving from one frame to another fires nothing here, so a frame that is
 * focused now but is not `frame` got focus from another frame, whose input the
 * dashboard cannot see. The pointer entering a frame is a `pointerover` on that
 * iframe, at `pointerAt` (`performance.now()`); entering the dashboard's own
 * content clears it.
 */
export const focusEntry: {
  frame: HTMLIFrameElement | null
  pointer: HTMLIFrameElement | null
  pointerAt: number
} = {
  frame: null,
  pointer: null,
  pointerAt: -Infinity,
}

if (typeof window !== 'undefined') {
  for (const type of ['keydown', 'mousedown', 'pointerdown', 'pointerup', 'touchend']) {
    window.addEventListener(type, () => { hostInput.lastAt = performance.now() }, { capture: true, passive: true })
  }
  window.addEventListener('blur', () => {
    const active = document.activeElement
    focusEntry.frame = active instanceof HTMLIFrameElement ? active : null
    watchEntry()
  })
  window.addEventListener('focus', () => { focusEntry.frame = null })
  window.addEventListener('pointerover', (e) => {
    const over = e.target instanceof HTMLIFrameElement ? e.target : null
    focusEntry.pointer = over
    focusEntry.pointerAt = over ? performance.now() : -Infinity
    // The pointer reached another frame, where it may click: a page that takes focus
    // back afterwards must not inherit the entry.
    if (over && focusEntry.frame && over !== focusEntry.frame) focusEntry.frame = null
    checkEntry()
  }, { capture: true, passive: true })
  window.addEventListener('message', checkEntry)
}

/** How often the dashboard looks, while focus is in a frame, whether it is still the
 *  frame it came into. */
export const FOCUS_ENTRY_CHECK_MS = 200

let entryWatch: ReturnType<typeof setInterval> | null = null

/**
 * Forget where focus came in once focus is seen anywhere else.
 *
 * Focus moving from that frame to another fires nothing in the dashboard, so the
 * entry would outlive it: a click in the other frame, then the first frame focusing
 * itself again, would read as tied. So the entry is checked against the focused
 * element whenever the dashboard hears anything (the pointer moving over a frame, a
 * frame's message) and every FOCUS_ENTRY_CHECK_MS while it is set. Seen elsewhere
 * once, it is gone until focus comes in from the dashboard again.
 */
export function checkEntry(): void {
  if (focusEntry.frame && document.activeElement !== focusEntry.frame) focusEntry.frame = null
  if (!focusEntry.frame && entryWatch !== null) {
    clearInterval(entryWatch)
    entryWatch = null
  }
}

function watchEntry(): void {
  if (focusEntry.frame && entryWatch === null) entryWatch = setInterval(checkEntry, FOCUS_ENTRY_CHECK_MS)
}

/** The message a bridged frame's shim posts on each click or key in that frame. */
export const INPUT_MESSAGE_TYPE = 'kirocrew-dashboard:input'

/**
 * The last click or key each bridged frame reported, by its window.
 *
 * A click in one frame gives the dashboard a live activation too, so a sibling
 * page that focuses itself would pass `hasGesture` on that click. Each frame's
 * shim reports its own input, and a report from any window but the asking
 * frame's means the click may not be the asking frame's. A report never opens
 * anything: a page that sends false ones only turns its siblings' opens into the
 * button.
 */
export const frameInputs = new Map<MessageEventSource, number>()
if (typeof window !== 'undefined') {
  window.addEventListener('message', (event: MessageEvent) => {
    const data = event.data as { type?: unknown } | null
    if (!event.source || !data || typeof data !== 'object' || data.type !== INPUT_MESSAGE_TYPE) return
    const now = performance.now()
    frameInputs.set(event.source, now)
    for (const [source, at] of frameInputs) {
      if (now - at > 2 * ACTIVATION_LIFESPAN_MS) frameInputs.delete(source)
    }
  })
}

/** The latest input a sibling frame reported, not the asking frame's own. */
function siblingInputAt(asker: MessageEventSource | null): number {
  let at = -Infinity
  for (const [source, t] of frameInputs) if (source !== asker && t > at) at = t
  return at
}

/** The latest input that is not the asking frame's own: the dashboard's or a sibling frame's. */
function foreignInputAt(asker: MessageEventSource | null): number {
  return Math.max(hostInput.lastAt, siblingInputAt(asker))
}

/**
 * Whether the dashboard can tie the live click to *frame*. All must hold:
 *
 * - focus came into this frame straight from the dashboard, not from another
 *   frame (`focusEntry.frame`);
 * - the pointer is not over another frame (`focusEntry.pointer`);
 * - no click or key outside this frame is still live: none in the dashboard
 *   (`hostInput`, a Tab into the frame included) and none reported by another
 *   frame (`frameInputs`) within an activation's lifespan.
 *
 * Nothing here is the frame's own word, which its script could forge. Without a
 * tie the bridge shows the "Open <host>" button instead of opening.
 */
export function tiedToFrame(frame: HTMLIFrameElement, now: number = performance.now()): boolean {
  if (focusEntry.frame !== frame) return false
  if (focusEntry.pointer && focusEntry.pointer !== frame) return false
  return now - foreignInputAt(frame.contentWindow) >= ACTIVATION_LIFESPAN_MS
}

/**
 * Whether the dashboard can tie the live click to *frame* for an in-app
 * navigation (`openInternalLink`), which only moves this window to a page on the
 * in-app list, behind the page's leave check.
 *
 * `tiedToFrame` refuses any click within an activation's lifespan of the last
 * click or key in the dashboard, so a link clicked a few seconds after opening the
 * page, or after clicking its tab, got the "Open" button first. Here a dashboard
 * click or key does not count against the frame when it came BEFORE the pointer
 * entered this frame: the person clicked in the dashboard, then moved onto the
 * frame. All must hold:
 *
 * - focus came into this frame straight from the dashboard (`focusEntry.frame`);
 * - the pointer is over this frame (`focusEntry.pointer`), and entered it after
 *   the dashboard's last click or key (`focusEntry.pointerAt`), so a key pressed in
 *   the dashboard with the pointer resting over the frame still does not tie;
 * - no sibling frame reported a click or key within an activation's lifespan.
 *
 * A web URL, or any open in a new tab, still needs `tiedToFrame`.
 */
export function pointerTiedToFrame(frame: HTMLIFrameElement, now: number = performance.now()): boolean {
  if (focusEntry.frame !== frame || focusEntry.pointer !== frame) return false
  if (!(focusEntry.pointerAt > hostInput.lastAt)) return false
  return now - siblingInputAt(frame.contentWindow) >= ACTIVATION_LIFESPAN_MS
}

/**
 * The web URL an open message asks for, or null when it is not one the link
 * bridge honours.
 *
 * A path (`/artifacts/<slug>`, `/chat?sid=<key>`) is a page of this dashboard and
 * resolves against its origin. The URL rule is then `safeHttpUrl`: only an
 * `http:` or `https:` URL with no user name or password passes, so `javascript:`,
 * `data:`, `blob:`, `file:`, `mailto:` and every custom scheme are refused. The
 * returned value is the PARSED form, so the URL that opens is exactly the one the
 * link's hover title shows.
 */
export function webUrlOf(data: unknown): string | null {
  if (!data || typeof data !== 'object') return null
  const msg = data as { type?: unknown; url?: unknown }
  if (msg.type !== OPEN_MESSAGE_TYPE || typeof msg.url !== 'string') return null
  let href: string
  try {
    href = new URL(msg.url, window.location.origin).href
  } catch {
    return null
  }
  // The URL rule is the dashboard's one web-link gate (safeHttpUrl).
  return safeHttpUrl(href)
}

/**
 * Whether an open message asked for a new tab: the shim sets `tab` for a middle
 * or modified click. A page can set it itself, which only keeps the new-tab
 * open every URL had before in-app links existed.
 */
function wantsNewTab(data: unknown): boolean {
  return !!data && typeof data === 'object' && (data as { tab?: unknown }).tab === true
}

/**
 * Move focus from the frame to the element that holds it, so the next open needs
 * a new click in the frame while a keyboard user keeps their place: one Tab goes
 * back into the page. The holder is made focusable from script only
 * (`tabindex="-1"`) when it is not already; with no holder the frame is blurred.
 */
function focusHostOf(frame: HTMLIFrameElement): void {
  const host = frame.parentElement
  if (!host) {
    frame.blur()
    return
  }
  if (!host.hasAttribute('tabindex')) host.setAttribute('tabindex', '-1')
  host.focus({ preventScroll: true })
  if (document.activeElement === frame) frame.blur()
}

/** A ref that always holds the latest *value*, for use inside stable callbacks. */
function useLatest<T>(value: T): { current: T } {
  const [ref] = useState(() => ({ current: value }))
  ref.current = value
  return ref
}

/**
 * The host half of the link bridge for frames that hold agent-written pages
 * (saved artifacts, a teammate's shared artifact, chat widgets). Render what it
 * returns: the "Open <host>" button, or nothing.
 *
 * Those frames are `sandbox="allow-scripts"` with no popup grant, so a page cannot
 * open a window itself. `buildSrcdoc({ linkBridge: true })` turns a `_blank` or
 * bare web link click, and a `window.open` call, into the same
 * `kirocrew-dashboard:open` message the crew pages use, and gives each web link a
 * hover title with its URL. This hook answers the message:
 *
 * - only from one of the given frames (`event.source`);
 * - only for a web URL (`webUrlOf`);
 * - only while the person has just clicked or pressed a key with that frame
 *   focused (`hasGesture`), so a page posting on its own opens nothing;
 * - at once when that click is tied to the frame (`tiedToFrame`), the way a link
 *   in a session opens;
 * - in place, through the router, for a page of this dashboard that
 *   `openInternalLink` accepts, unless the click asked for a new tab; in a new
 *   tab for everything else;
 * - otherwise through an "Open <host>" button by the frame (`FrameLinkChip`),
 *   whose click is the person's own in the dashboard, so a page cannot open a
 *   link on a click made elsewhere;
 * - once per click: after an open focus moves to the frame's holder, one Tab from
 *   the page, and the button takes focus when it appears, so a page posting again
 *   from the same click fails the gesture test until the next real click.
 */
export function useFrameLinkBridge(
  frames: ReadonlyArray<RefObject<HTMLIFrameElement | null>>,
  open: (url: string) => void = openExternal,
  gesture: (frame: HTMLIFrameElement) => boolean = hasGesture,
  tied: (frame: HTMLIFrameElement) => boolean = tiedToFrame,
  pointerTied: (frame: HTMLIFrameElement) => boolean = pointerTiedToFrame,
): ReactNode {
  const [chip, setChip] = useState<{ url: string; frame: HTMLIFrameElement; tab: boolean } | null>(null)
  const openRef = useLatest(open)
  const tiedRef = useLatest(tied)
  const pointerTiedRef = useLatest(pointerTied)
  // A page of this dashboard opens in place (`openInternalLink`), unless the
  // click asked for a new tab (a middle or modified click, which the shim marks
  // `tab`). Every other URL, and any URL when no in-app navigator is installed,
  // goes to `open`.
  const go = useCallback((url: string, tab: boolean) => {
    if (!tab && openInternalLink(url)) return
    openRef.current(url)
  }, [openRef])
  // The frame, URL and gesture checks are useFrameOpenLink's own listener; this
  // hook adds the web-URL rule, the tie and the once-per-click focus move.
  const answer = useCallback((url: string, frame?: HTMLIFrameElement, data?: unknown) => {
    if (!frame) return
    const tab = wantsNewTab(data)
    // An in-app navigation may also be tied by the pointer's entry into the frame
    // (`pointerTiedToFrame`); a web URL or a new tab needs the full tie.
    const inApp = !tab && wouldOpenInternalLink(url)
    if (tiedRef.current(frame) || (inApp && pointerTiedRef.current(frame))) {
      setChip(null)
      go(url, tab)
      focusHostOf(frame)
      return
    }
    setChip({ url, frame, tab })
  }, [go, tiedRef, pointerTiedRef])
  useFrameOpenLink(frames, webUrlOf, answer, gesture)
  if (!chip) return null
  const { url, frame, tab } = chip
  return createElement(FrameLinkChip, {
    url,
    frame,
    onOpen: () => {
      setChip(null)
      go(url, tab)
      focusHostOf(frame)
    },
    onDismiss: () => {
      const hadFocus = document.activeElement instanceof HTMLElement
        && document.activeElement.closest('[data-frame-link-chip]') !== null
      setChip(null)
      if (hadFocus) focusHostOf(frame)
    },
  })
}
