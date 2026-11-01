/**
 * What every app frame's messages share, whichever point the frame serves.
 *
 * An app frame (`apps/app_frame.py`) is `sandbox="allow-scripts"` with no
 * `allow-same-origin`, so its origin is opaque and `postMessage` is the only
 * channel. Every message either way carries `{ [APP_FRAME_TAG]: 1, type }`.
 * The point-specific messages are in `linkPreview/linkPreviewProtocol.ts` and
 * `messageActions/messageActionProtocol.ts`.
 *
 * A frame's messages are bounded: one larger than its point's byte cap, or past
 * its burst allowance (`BURST_CAPACITY`, refilled at `BURST_PER_SECOND`), is
 * dropped unread.
 *
 * @module appFrame/frameProtocol
 */

export const APP_FRAME_TAG = 'kirocrew-app-frame'

/** Messages a frame may send in a burst, and how fast that allowance refills. */
export const BURST_CAPACITY = 120
export const BURST_PER_SECOND = 60

/** The serialized size of a message, or Infinity when it is not plain data. */
export function messageBytes(data: unknown): number {
  try {
    const s = JSON.stringify(data)
    return typeof s === 'string' ? s.length : Infinity
  } catch {
    return Infinity
  }
}

/** A frame's burst allowance. */
export interface RateState {
  tokens: number
  /** When it was last refilled (`performance.now()`). */
  refilledAt: number
}

export function freshRate(): RateState {
  return { tokens: BURST_CAPACITY, refilledAt: performance.now() }
}

/** Spend one message of the allowance; false when it is used up. */
export function takeMessage(rate: RateState, now = performance.now()): boolean {
  rate.tokens = Math.min(BURST_CAPACITY, rate.tokens + (Math.max(0, now - rate.refilledAt) / 1000) * BURST_PER_SECOND)
  rate.refilledAt = now
  if (rate.tokens < 1) return false
  rate.tokens -= 1
  return true
}

/** The tagged message fields in `data`, or null when it is not an app-frame message. */
export function readTagged(data: unknown): (Record<string, unknown> & { type: string }) | null {
  if (!data || typeof data !== 'object') return null
  const m = data as Record<string, unknown>
  if (m[APP_FRAME_TAG] !== 1 || typeof m.type !== 'string') return null
  return m as Record<string, unknown> & { type: string }
}

export function tagged(msg: { type: string } & Record<string, unknown>): Record<string, unknown> {
  return { [APP_FRAME_TAG]: 1, ...msg }
}

/** A finite number, or 0. */
export const finite = (n: unknown): number => (typeof n === 'number' && Number.isFinite(n) ? n : 0)

// ---------------------------------------------------------------------------
// The invocation contract (click-driven points)
// ---------------------------------------------------------------------------

/** Largest message a frame on the invocation contract may send, as JSON text. */
export const MAX_HOST_MESSAGE_BYTES = 64 * 1024
/** The most text a `copy` offer may carry, in UTF-16 units. */
export const MAX_OFFER_CHARS = 32000
/** The longest URL an `open` offer may carry. */
export const MAX_OFFER_URL = 2048

export type OfferKind = 'copy' | 'open'
/** Why the host refused an offer, as `result.error { code }`. */
export type OfferRefusal = 'stale' | 'too_large' | 'not_allowed'

/**
 * Frame-to-host messages of the invocation contract, plus the lifecycle ones
 * every frame sends. A point's own messages are read by the point.
 */
export type InvocationMessage =
  | { type: 'ready' }
  | { type: 'failed'; message: string }
  | { type: 'capabilityExpired' }
  | { type: 'escape' }
  | { type: 'invocation.resize'; invocationId: string; width: number; height: number }
  | { type: 'invocation.close'; invocationId: string }
  | { type: 'invocation.error'; invocationId: string; message: string }
  | { type: 'result.offer'; invocationId: string; seq: number; kind: OfferKind | null; value: string }

const str = (v: unknown): string => (typeof v === 'string' ? v : '')

/** The invocation-contract message in `data`, or null when it is not one. */
export function readInvocationMessage(data: unknown): InvocationMessage | null {
  const m = readTagged(data)
  if (!m) return null
  const invocationId = str(m.invocationId)
  switch (m.type) {
    case 'ready':
    case 'escape':
    case 'capabilityExpired':
      return { type: m.type }
    case 'failed':
      return { type: 'failed', message: str(m.message).slice(0, 200) }
    case 'invocation.resize':
      return { type: 'invocation.resize', invocationId, width: finite(m.width), height: finite(m.height) }
    case 'invocation.close':
      return { type: 'invocation.close', invocationId }
    case 'invocation.error':
      return { type: 'invocation.error', invocationId, message: str(m.message).slice(0, 200) }
    case 'result.offer':
      return {
        type: 'result.offer',
        invocationId,
        seq: typeof m.seq === 'number' && Number.isInteger(m.seq) && m.seq > 0 ? m.seq : -1,
        kind: m.kind === 'copy' || m.kind === 'open' ? m.kind : null,
        value: str(m.value),
      }
    default:
      return null
  }
}

/** Why an offer's value is refused, or null when the host can draw its button. */
export function offerRefusal(kind: OfferKind | null, value: string): Exclude<OfferRefusal, 'stale'> | null {
  if (kind === 'copy') return value.length > MAX_OFFER_CHARS ? 'too_large' : null
  if (kind === 'open') {
    if (value.length > MAX_OFFER_URL) return 'too_large'
    try {
      const u = new URL(value)
      return u.protocol === 'https:' && !u.username && !u.password ? null : 'not_allowed'
    } catch {
      return 'not_allowed'
    }
  }
  return 'not_allowed'
}

/** Text the host shows from a frame: no C0 controls but tab and newline, no bidi overrides. */
export function displaySafe(text: string): string {
  return text.replace(/[\u0000-\u0008\u000b-\u001f\u007f\u202a-\u202e\u2066-\u2069]/g, '')
}
