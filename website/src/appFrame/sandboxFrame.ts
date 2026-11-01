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
import { withBase } from '../lib/basePath'
import { tagged } from './frameProtocol'

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
}

/** A point name (the manifest key under `contributes`) and an entry id. Mirrors
 *  `POINT_RE` / `POINT_ID_RE` in `apps/app_frame.py`. */
export const POINT_RE = /^[a-z][A-Za-z]{0,31}$/
export const POINT_ID_RE = /^[a-z][a-z0-9_-]{0,63}$/

/** Mint the frame URL and capability for one `(point, id)` of an app
 *  (`POST /api/apps/<app>/frames/<point>/<id>`, a dashboard session only); with
 *  `frameId`, a renewal for that frame. */
export async function mintAppFrame(appName: string, point: string, pointId: string, frameId?: string): Promise<Minted> {
  if (!POINT_RE.test(point) || !POINT_ID_RE.test(pointId)) throw new Error('app frame: not a point entry')
  const q = frameId ? `?frame=${encodeURIComponent(frameId)}` : ''
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
  el.addEventListener('load', () => {
    loads += 1
    if (loads === 1) {
      postToFrame(el, { type: 'init', capability: minted.capability, theme: opts.theme() })
      return
    }
    // The frame's contents are no longer the gateway's document.
    opts.onNavigatedAway()
  })
  // The URL is the gateway's own: mintAppFrame accepts only a same-origin
  // `/app-frame/` path, and the frame is sandboxed to an opaque origin.
  // nosemgrep: semgrep.kirocrew.frontend-external-script-inject
  el.src = withBase(minted.frame)
  // In the document only once it has its real URL, so its first load is the frame's.
  root.appendChild(el)
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
