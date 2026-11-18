/**
 * This tab's half of `dashboard_capture`: a Bot reading its own live page.
 *
 * The gateway sends a `dashboard_capture` frame (`{ request_id, slug }`) to every
 * owner tab. A tab answers only when ALL of these hold, and otherwise stays silent:
 *
 * - it shows that crewmate's LIVE page (the hook is armed only there);
 * - it is VISIBLE (`document.visibilityState`) and the page's frame is RENDERED
 *   (`onScreen`: not in a side-panel view the person moved away from, and not under
 *   the reply-thread panel or a dialog), so the sign below is on a screen;
 * - the gateway asked: nothing here reads or sends the page on its own.
 *
 * While it answers it shows a sign ("{{crew}} is reading this page") for at least
 * `SIGN_MIN_MS`. The page's frame is an opaque origin, so it is asked by message
 * (`dashboard_frame._CAPTURE_JS`) and its answer is cut to the gateway's caps here;
 * the gateway refuses anything larger. The frame never reads what the person typed into a
 * text field; nothing here writes the page.
 */
import { useEffect, useRef, useState, type RefObject } from 'react'
import { api } from '../../api/client'
import { subscribeDashboardCapture } from '../../hooks/useWebSocket'

/** Mirrors `dashboard_frame.CAPTURE_MESSAGE_TYPE` / `CAPTURED_MESSAGE_TYPE`. */
export const CAPTURE_MESSAGE_TYPE = 'kirocrew-dashboard:capture'
export const CAPTURED_MESSAGE_TYPE = 'kirocrew-dashboard:captured'
/** Mirrors `dashboard_capture.TEXT_MAX_CHARS`, `FIELDS_MAX`, `FIELD_LABEL_MAX`, `FIELD_VALUE_MAX`. */
export const CAPTURE_TEXT_MAX = 16000
export const CAPTURE_FIELDS_MAX = 64
export const CAPTURE_LABEL_MAX = 120
export const CAPTURE_VALUE_MAX = 500
/** How long the frame has to answer. The gateway waits five seconds in all. */
export const FRAME_ANSWER_MS = 1500
/** The sign stays at least this long, so a fast capture is still seen. */
export const SIGN_MIN_MS = 2500

type Field = { label: string; value: string }
export type CaptureReply =
  | { request_id: string; slug: string; status: 'captured'; text: string; fields: Field[]; truncated: boolean }
  | { request_id: string; slug: string; status: 'unavailable'; reason: 'static_page' | 'not_loaded' }

function cut(value: unknown, max: number): [string, boolean] {
  const s = typeof value === 'string' ? value : ''
  return s.length > max ? [s.slice(0, max), true] : [s, false]
}

/** The reply for the frame's answer *data*, cut to the caps, or null when it is not one. */
export function capturedReply(requestId: string, slug: string, data: unknown): CaptureReply | null {
  if (!data || typeof data !== 'object') return null
  const d = data as { text?: unknown; fields?: unknown; truncated?: unknown }
  if (typeof d.text !== 'string') return null
  const [text, textCut] = cut(d.text, CAPTURE_TEXT_MAX)
  let truncated = textCut || d.truncated === true
  const raw = Array.isArray(d.fields) ? d.fields : []
  if (raw.length > CAPTURE_FIELDS_MAX) truncated = true
  const fields: Field[] = []
  for (const item of raw.slice(0, CAPTURE_FIELDS_MAX)) {
    if (!item || typeof item !== 'object') continue
    const [label, labelCut] = cut((item as Field).label, CAPTURE_LABEL_MAX)
    const [value, valueCut] = cut((item as Field).value, CAPTURE_VALUE_MAX)
    truncated = truncated || labelCut || valueCut
    fields.push({ label, value })
  }
  return { request_id: requestId, slug, status: 'captured', text, fields, truncated }
}

function nonce(): string {
  const bytes = new Uint8Array(12)
  crypto.getRandomValues(bytes)
  return Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('')
}

/** Ask *win* for its page; resolves with the frame's answer, or null after `FRAME_ANSWER_MS`. */
function askFrame(win: Window): Promise<unknown> {
  return new Promise(resolve => {
    const asked = nonce()
    const done = (value: unknown) => {
      window.removeEventListener('message', onMessage)
      window.clearTimeout(timer)
      resolve(value)
    }
    const onMessage = (event: MessageEvent) => {
      if (event.source !== win) return
      const data = event.data as { type?: unknown; nonce?: unknown } | null
      if (!data || data.type !== CAPTURED_MESSAGE_TYPE || data.nonce !== asked) return
      done(data)
    }
    const timer = window.setTimeout(() => done(null), FRAME_ANSWER_MS)
    window.addEventListener('message', onMessage)
    // An opaque origin cannot be named, so the target is '*'; the post reaches this
    // one frame's contentWindow and carries a nonce, nothing of the page.
    // nosemgrep: javascript.browser.security.wildcard-postmessage-configuration.wildcard-postmessage-configuration
    win.postMessage({ type: CAPTURE_MESSAGE_TYPE, nonce: asked }, '*')
  })
}

/** Where `uncovered` looks, as fractions of the part of the frame inside the window. */
const PROBE_POINTS = [[0.5, 0.5], [0.25, 0.25], [0.75, 0.25], [0.25, 0.75], [0.75, 0.75]]

/** Whether the frame is the topmost thing at every probe point. A thread panel or a
 *  dialog is drawn over a Dashboard that stays mounted, and neither the page nor the
 *  sign above it is seen under one. */
function uncovered(el: HTMLElement): boolean {
  // shortcut: a browser without `elementFromPoint` (only jsdom) skips this check; none ships without it.
  if (typeof document.elementFromPoint !== 'function') return true
  const r = el.getBoundingClientRect()
  const left = Math.max(r.left, 0)
  const top = Math.max(r.top, 0)
  const right = Math.min(r.right, window.innerWidth)
  const bottom = Math.min(r.bottom, window.innerHeight)
  if (right <= left || bottom <= top) return false
  return PROBE_POINTS.every(([x, y]) => document.elementFromPoint(left + (right - left) * x, top + (bottom - top) * y) === el)
}

/** Whether *el* is rendered on screen now: not inside a `hidden` or `display: none`
 *  pane (a side-panel view the person moved away from keeps the Dashboard mounted),
 *  not `visibility: hidden`, not zero-sized, and not covered by another layer. */
export function onScreen(el: HTMLElement | null): boolean {
  if (!el || el.getClientRects().length === 0) return false
  if (typeof el.checkVisibility === 'function'
    && !el.checkVisibility({ visibilityProperty: true, opacityProperty: true })) return false
  return uncovered(el)
}

/** Answer *slug*'s capture requests from *frameRef*'s page; true while the sign is up. */
export function useDashboardLiveCapture({ slug, frameRef, runsScript }: {
  /** The crewmate whose LIVE page this tab shows, or '' to answer nothing. */
  slug: string
  frameRef: RefObject<HTMLIFrameElement | null>
  runsScript: boolean
}): boolean {
  const [capturing, setCapturing] = useState(false)
  const runsScriptRef = useRef(runsScript)
  runsScriptRef.current = runsScript
  useEffect(() => {
    if (!slug) return
    let alive = true
    let hide: number | null = null
    const off = subscribeDashboardCapture(slug, requestId => {
      // On a screen the person is looking at, or silent: the browser tab in front AND
      // this page's own frame rendered, so the sign below is actually seen.
      if (document.visibilityState !== 'visible' || !onScreen(frameRef.current)) return
      const started = Date.now()
      if (hide !== null) window.clearTimeout(hide)
      setCapturing(true)
      const win = frameRef.current?.contentWindow
      const reply: Promise<CaptureReply> = !win
        ? Promise.resolve({ request_id: requestId, slug, status: 'unavailable', reason: 'not_loaded' })
        : !runsScriptRef.current
          ? Promise.resolve({ request_id: requestId, slug, status: 'unavailable', reason: 'static_page' })
          : askFrame(win).then(data => capturedReply(requestId, slug, data)
            ?? { request_id: requestId, slug, status: 'unavailable', reason: 'not_loaded' })
      reply
        .then(body => api.memberDashboardCapture(slug, body))
        // A refused answer (another tab was first, or the request expired) only
        // means the Bot hears from that tab or is told no tab answered.
        .catch(() => undefined)
        .finally(() => {
          if (!alive) return
          hide = window.setTimeout(() => {
            hide = null
            if (alive) setCapturing(false)
          }, Math.max(0, SIGN_MIN_MS - (Date.now() - started)))
        })
    })
    return () => {
      alive = false
      off()
      if (hide !== null) window.clearTimeout(hide)
      setCapturing(false)
    }
  }, [slug, frameRef])
  return capturing
}
