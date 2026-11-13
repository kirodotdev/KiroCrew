/**
 * Creating, loading and renewing one app frame, for every point that draws one.
 *
 * The frame is `sandbox="allow-scripts"` (never `allow-same-origin`), loads the
 * gateway's frame document (`/app-frame/...`, minted per frame by a dashboard
 * session), and gets its capability for `ctx.fetch` in `init`. A frame that loads
 * a second document navigated itself away from the gateway's and is closed. The
 * point decides where the frame sits and what it is told.
 *
 * @module appFrame/sandboxFrame
 */
import { BASE_PATH, withBase } from '../lib/basePath'
import { APP_FRAME_TAG, tagged } from './frameProtocol'
import { relayNonce, serveFrameRelay } from './frameRelay'

/** The only sandbox grant. Never `allow-same-origin`: with `allow-scripts` it would
 *  let the frame remove its own sandbox. */
export const FRAME_SANDBOX = 'allow-scripts'
/** Every minted frame URL starts here (`apps/app_frame.py`). */
export const FRAME_PATH_PREFIX = '/app-frame/'

export interface Minted {
  frame: string
  /** The frame's own id; a renewal names it so the new capability is that frame's. */
  frameId?: string
  capability: string | null
  ttl: number
  /** The frame document itself, for `srcdoc` (a first mint only; a renewal has none). */
  document?: string
}

/** Stands for this page's origin in a srcdoc frame document's policy (`SRCDOC_SELF`
 *  in `apps/app_frame.py`); replaced with `location.origin` before it is set. */
export const SRCDOC_SELF = '__kirocrew_frame_self__'
/** After a frame loads by URL, how long its document has to say `boot` before
 *  the srcdoc fallback replaces it. The ready deadline still covers the whole load. */
export const BOOT_GRACE_MS = 1500

/** A point name (the manifest key under `contributes`) and an entry id. Mirrors
 *  `POINT_RE` / `POINT_ID_RE` in `apps/app_frame.py`. */
export const POINT_RE = /^[a-z][A-Za-z]{0,31}$/
export const POINT_ID_RE = /^[a-z][a-z0-9_-]{0,63}$/

/**
 * Mint the frame URL and capability for one `(point, id)` of an app
 * (`POST /api/apps/<app>/frames/<point>/<id>`, a dashboard session only); with
 * `frameId`, a renewal for that frame. `request` names the one act the mint is for (a
 * message action sends its `role` and `format`): the gateway checks it against
 * the live grant and refuses with 403 when it is outside it, so a tab holding an
 * older grant cannot act on a reach the owner has since taken back.
 */
export async function mintAppFrame(
  appName: string,
  point: string,
  pointId: string,
  frameId?: string,
  request?: Readonly<Record<string, string>>,
): Promise<Minted> {
  if (!POINT_RE.test(point) || !POINT_ID_RE.test(pointId)) throw new Error('app frame: not a point entry')
  const params = new URLSearchParams(request)
  params.delete('frame')
  if (frameId) params.set('frame', frameId)
  const qs = params.toString()
  const q = qs ? `?${qs}` : ''
  const path = `/api/apps/${encodeURIComponent(appName)}/frames/${point}/${pointId}${q}`
  const res = await fetch(withBase(path), { method: 'POST', credentials: 'same-origin' })
  if (!res.ok) throw new Error(`app frame mint failed: ${res.status}`)
  const data = (await res.json()) as Partial<Minted>
  if (typeof data.frame !== 'string' || !data.frame.startsWith(FRAME_PATH_PREFIX)) {
    throw new Error('app frame mint returned no frame')
  }
  return {
    frame: data.frame,
    ...(typeof data.frameId === 'string' ? { frameId: data.frameId } : {}),
    capability: typeof data.capability === 'string' ? data.capability : null,
    ttl: typeof data.ttl === 'number' && data.ttl > 0 ? data.ttl : 3600,
    ...(typeof data.document === 'string' && data.document ? { document: data.document } : {}),
  }
}

/** The app a minted `/app-frame/<app>/...` path is for. */
export function frameAppName(frame: string): string {
  const seg = frame.slice(FRAME_PATH_PREFIX.length).split('/')[0] ?? ''
  try {
    return decodeURIComponent(seg)
  } catch {
    return ''
  }
}

/** A fresh, unloaded frame with every attribute the model requires. */
export function createSandboxedFrame(title: string): HTMLIFrameElement {
  const el = document.createElement('iframe')
  el.setAttribute('sandbox', FRAME_SANDBOX)
  // No cookies or other credentials on the frame's own requests, where supported.
  el.setAttribute('credentialless', '')
  el.setAttribute('referrerpolicy', 'no-referrer')
  el.setAttribute('allow', '')
  el.setAttribute('title', title)
  return el
}

/** Post one tagged message to a frame's window. */
export function postToFrame(el: HTMLIFrameElement, msg: { type: string } & Record<string, unknown>): void {
  // An opaque origin cannot be named as a target, hence '*'. The window is this
  // iframe's own. The one secret sent, the capability, is the frame's own: bound
  // to this app, this frame id, this point and this client, and the app's code
  // inside the frame already holds it.
  // nosemgrep: javascript.browser.security.wildcard-postmessage-configuration.wildcard-postmessage-configuration
  try { el.contentWindow?.postMessage(tagged(msg), '*') } catch { /* frame gone */ }
}

/**
 * Point `el` at its minted document, put it in `root`, and send `init` on its first
 * load. A second load means the frame navigated away: `onNavigatedAway` closes it.
 */
export function loadMintedFrame(
  el: HTMLIFrameElement,
  root: HTMLElement,
  minted: Minted,
  opts: { theme: () => 'dark' | 'light'; onNavigatedAway: () => void },
): void {
  let loads = 0
  let booted = false
  let fellBack = false
  let bootTimer: number | null = null
  const relay = relayNonce()
  const doc = minted.document
  const sendInit = () => postToFrame(el, { type: 'init', capability: minted.capability, theme: opts.theme(), relay, base: BASE_PATH })
  const stopWatching = () => {
    window.removeEventListener('message', onBoot)
    if (bootTimer !== null) window.clearTimeout(bootTimer)
    bootTimer = null
  }
  // The gateway's document says `boot` while it parses. One listener, removed on
  // the first boot, on the fallback, or when the frame navigates away.
  function onBoot(e: MessageEvent): void {
    if (e.source !== el.contentWindow) return
    const m = e.data as Record<string, unknown> | null
    if (!m || typeof m !== 'object' || m[APP_FRAME_TAG] !== 1 || m.type !== 'boot') return
    booted = true
    const waiting = bootTimer !== null
    stopWatching()
    // A boot after the URL load finished, inside the grace period: init now.
    if (waiting && loads === 1 && !fellBack) sendInit()
  }
  if (doc) window.addEventListener('message', onBoot)
  // The fallback: the document the mint returned, set as `srcdoc`. Used only when
  // the URL load did not run the gateway's document, which is what an
  // authenticating reverse proxy in front of this page causes (the frame's own,
  // cookieless request gets its login page). Same sandbox; its policy is a <meta>
  // copy of the frame's, with `'self'` spelled as this page's origin.
  const fallBack = () => {
    bootTimer = null
    if (!el.isConnected) { stopWatching(); return }
    if (booted || fellBack || !doc) return
    fellBack = true
    loads = 0
    stopWatching()
    // `srcdoc` takes precedence over `src`: one navigation, to this document.
    el.srcdoc = doc.split(SRCDOC_SELF).join(window.location.origin)
  }
  el.addEventListener('load', () => {
    loads += 1
    if (loads === 1) {
      if (doc && !booted && !fellBack) {
        // Loaded, but not (yet) the gateway's document: give a slow parse a
        // moment, then fall back.
        bootTimer = window.setTimeout(fallBack, BOOT_GRACE_MS)
        return
      }
      sendInit()
      return
    }
    stopWatching()
    // The frame's contents are no longer the gateway's document.
    opts.onNavigatedAway()
  })
  // The URL is the gateway's own: mintAppFrame accepts only a same-origin
  // `/app-frame/` path, and the frame is sandboxed to an opaque origin.
  // nosemgrep: semgrep.kirocrew.frontend-external-script-inject
  el.src = withBase(minted.frame)
  // In the document only once it has its real URL, so its first load is the frame's.
  root.appendChild(el)
  // Requests the frame cannot make itself go through this page (`frameRelay.ts`),
  // whichever document it ended up with. When the frame leaves the document (even
  // before its first load), the relay's sweep ends the boot watch with it.
  serveFrameRelay(el, frameAppName(minted.frame), relay, stopWatching)
}

/** The dashboard's theme as a frame is told it. */
export function currentTheme(): 'dark' | 'light' {
  const root = document.documentElement
  return root.classList.contains('dark') || /dark/.test(root.dataset.theme ?? '') ? 'dark' : 'light'
}

/** How often to renew a capability that lives `ttl` seconds: well before it lapses. */
export function renewIntervalMs(ttl: number): number {
  return Math.max(60, Math.floor(ttl / 2)) * 1000
}
