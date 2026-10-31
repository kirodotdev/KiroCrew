/**
 * The message protocol between the dashboard and a link-preview frame.
 *
 * An app's card runs in an iframe that is `sandbox="allow-scripts"` with no
 * `allow-same-origin` (served by `apps/link_preview_frame.py`), so its origin is
 * opaque and `postMessage` is the only channel. Every message carries
 * `{ [LINK_PREVIEW_TAG]: 1, type }`.
 *
 * Dashboard to frame (the frame's bootstrap turns these into provider calls):
 *
 * - `init { capability, theme }` once the frame has loaded
 * - `capability { capability }` a renewed capability for `ctx.fetch`
 * - `show { url? | text?, rect, theme, modifiers }` the hovered link or text, where
 *   it is, and the modifier keys held when the hover began
 * - `leave`, `hide { reason }`, `theme { theme }`, `dispose`
 * - `key { key, code, shift }` a key pressed in the dashboard while a card shows,
 *   so a card's own shortcuts work without a click into it first. Never while
 *   the dashboard's focus is in a text field, and never with Ctrl, Alt or Meta.
 *
 * Frame to dashboard (read by `readFrameMessage`, which drops anything else):
 *
 * - `ready`, `failed { message }` the module activated, or did not
 * - `open`, `close` show or hide this frame
 * - `resize { width, height }` the card's size; the dashboard clamps it
 * - `pin { pinned }` a pinned card stays when the pointer leaves or the chat scrolls
 * - `drag { dx, dy }` move the card by this much
 * - `dock { edge }` stick the card to the left or right edge, or `null` to undock
 * - `textPatternsOff { ids }`, `escape`, `capabilityExpired`
 * - `regions { rects }` overlay mode, for a provider that manages several windows
 *   of its own (pinned cards, a dock, dialogs): the frame covers the viewport and
 *   is clipped to these rects, so everywhere else the page stays clickable. Its
 *   first `regions` puts the frame in this mode; `open`, `close`, `resize`, `pin`,
 *   `drag` and `dock` are then ignored, since the provider moves its own windows.
 *
 * The dashboard positions and sizes the frame. Nothing the frame says reaches the
 * dashboard's DOM: a message only moves, sizes, clips, shows or hides that one
 * iframe.
 *
 * @module linkPreview/linkPreviewProtocol
 */

export const LINK_PREVIEW_TAG = 'kirocrew-link-preview'

export interface AnchorRect {
  left: number
  top: number
  width: number
  height: number
}

export interface Modifiers {
  alt: boolean
  shift: boolean
  ctrl: boolean
  meta: boolean
}

export type ToFrame =
  | { type: 'init'; capability: string | null; theme: 'dark' | 'light' }
  | { type: 'capability'; capability: string | null }
  | { type: 'show'; url?: string; text?: string; rect: AnchorRect; theme: 'dark' | 'light'; modifiers?: Modifiers }
  | { type: 'leave' }
  | { type: 'hide'; reason: string }
  | { type: 'theme'; theme: 'dark' | 'light' }
  | { type: 'dispose' }
  | { type: 'key'; key: string; code: string; shift: boolean }

export type FromFrame =
  | { type: 'ready' }
  | { type: 'failed'; message: string }
  | { type: 'open' }
  | { type: 'close' }
  | { type: 'resize'; width: number; height: number }
  | { type: 'pin'; pinned: boolean }
  | { type: 'drag'; dx: number; dy: number }
  | { type: 'dock'; edge: 'left' | 'right' | null }
  | { type: 'textPatternsOff'; ids: string[] }
  | { type: 'escape' }
  | { type: 'capabilityExpired' }
  | { type: 'regions'; rects: AnchorRect[] }

/** At most this many rects in one `regions` message; the rest are dropped. */
export const MAX_REGIONS = 32

const finite = (n: unknown): number => (typeof n === 'number' && Number.isFinite(n) ? n : 0)

/** The frame message in `data`, or null when it is not one this protocol defines. */
export function readFrameMessage(data: unknown): FromFrame | null {
  if (!data || typeof data !== 'object') return null
  const m = data as Record<string, unknown>
  if (m[LINK_PREVIEW_TAG] !== 1 || typeof m.type !== 'string') return null
  switch (m.type) {
    case 'ready':
    case 'open':
    case 'close':
    case 'escape':
    case 'capabilityExpired':
      return { type: m.type }
    case 'failed':
      return { type: 'failed', message: typeof m.message === 'string' ? m.message.slice(0, 200) : '' }
    case 'resize':
      return { type: 'resize', width: finite(m.width), height: finite(m.height) }
    case 'pin':
      return { type: 'pin', pinned: m.pinned === true }
    case 'drag':
      return { type: 'drag', dx: finite(m.dx), dy: finite(m.dy) }
    case 'dock':
      return { type: 'dock', edge: m.edge === 'left' || m.edge === 'right' ? m.edge : null }
    case 'textPatternsOff':
      return {
        type: 'textPatternsOff',
        ids: Array.isArray(m.ids) ? m.ids.filter((x): x is string => typeof x === 'string').slice(0, 64) : [],
      }
    case 'regions': {
      const raw = Array.isArray(m.rects) ? m.rects.slice(0, MAX_REGIONS) : []
      const rects: AnchorRect[] = []
      for (const r of raw) {
        if (!r || typeof r !== 'object') continue
        const q = r as Record<string, unknown>
        const rect = { left: finite(q.left), top: finite(q.top), width: finite(q.width), height: finite(q.height) }
        if (rect.width > 0 && rect.height > 0) rects.push(rect)
      }
      return { type: 'regions', rects }
    }
    default:
      return null
  }
}

export function toFrame(msg: ToFrame): Record<string, unknown> {
  return { [LINK_PREVIEW_TAG]: 1, ...msg }
}
