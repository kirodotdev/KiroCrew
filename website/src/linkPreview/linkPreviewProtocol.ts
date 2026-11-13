/**
 * The message protocol between the dashboard and a link-preview frame.
 *
 * An app's card runs in an app frame (`apps/app_frame.py`): `sandbox="allow-scripts"`
 * with no `allow-same-origin`, so its origin is opaque and `postMessage` is the
 * only channel. What every app frame shares (the tag, the rate bound) is in
 * `appFrame/frameProtocol.ts`.
 *
 * Dashboard to frame (the frame's bootstrap turns these into provider calls):
 *
 * - `init { capability, theme }` once the frame has loaded
 * - `capability { capability }` a renewed capability for `ctx.fetch`
 * - `show { invocation, url? | text?, rect, theme, modifiers }` the hovered link or
 *   text, where it is, and the modifier keys held when the hover began.
 *   `invocation` numbers the shows of one frame; a card message (`open`, `close`,
 *   `resize`, `pin`, `drag`, `dock`) carries the one it answers, and one for an
 *   older show is dropped.
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
 * A message larger than `MAX_MESSAGE_BYTES`, or past a frame's burst allowance
 * (`BURST_CAPACITY`, refilled at `BURST_PER_SECOND`), is dropped unread.
 *
 * The dashboard positions and sizes the frame. Nothing the frame says reaches the
 * dashboard's DOM: a message only moves, sizes, clips, shows or hides that one
 * iframe.
 *
 * @module linkPreview/linkPreviewProtocol
 */

import { finite, readTagged, tagged } from '../appFrame/frameProtocol'

export { BURST_CAPACITY, BURST_PER_SECOND, messageBytes } from '../appFrame/frameProtocol'

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
  | { type: 'show'; invocation: number; url?: string; text?: string; rect: AnchorRect; theme: 'dark' | 'light'; modifiers?: Modifiers }
  | { type: 'leave' }
  | { type: 'hide'; reason: string }
  | { type: 'theme'; theme: 'dark' | 'light' }
  | { type: 'dispose' }
  | { type: 'key'; key: string; code: string; shift: boolean }

export type FromFrame =
  | { type: 'ready' }
  | { type: 'failed'; message: string }
  | { type: 'open'; invocation: number }
  | { type: 'close'; invocation: number }
  | { type: 'resize'; invocation: number; width: number; height: number }
  | { type: 'pin'; invocation: number; pinned: boolean }
  | { type: 'drag'; invocation: number; dx: number; dy: number }
  | { type: 'dock'; invocation: number; edge: 'left' | 'right' | null }
  | { type: 'textPatternsOff'; ids: string[] }
  | { type: 'escape' }
  | { type: 'capabilityExpired' }
  | { type: 'regions'; rects: AnchorRect[] }

/** At most this many rects in one `regions` message; the rest are dropped. */
export const MAX_REGIONS = 32
/** Largest message a frame may send, as JSON text. */
export const MAX_MESSAGE_BYTES = 8192
/** The frame message in `data`, or null when it is not one this protocol defines. */
export function readFrameMessage(data: unknown): FromFrame | null {
  const m = readTagged(data)
  if (!m) return null
  const invocation = typeof m.invocation === 'number' && Number.isInteger(m.invocation) && m.invocation >= 0 ? m.invocation : -1
  switch (m.type) {
    case 'ready':
    case 'escape':
    case 'capabilityExpired':
      return { type: m.type }
    case 'open':
    case 'close':
      return { type: m.type, invocation }
    case 'failed':
      return { type: 'failed', message: typeof m.message === 'string' ? m.message.slice(0, 200) : '' }
    case 'resize':
      return { type: 'resize', invocation, width: finite(m.width), height: finite(m.height) }
    case 'pin':
      return { type: 'pin', invocation, pinned: m.pinned === true }
    case 'drag':
      return { type: 'drag', invocation, dx: finite(m.dx), dy: finite(m.dy) }
    case 'dock':
      return { type: 'dock', invocation, edge: m.edge === 'left' || m.edge === 'right' ? m.edge : null }
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
  return tagged(msg)
}
