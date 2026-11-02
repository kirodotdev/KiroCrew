/**
 * The live-browser frame wire format and the in-memory frame ring.
 *
 * A frame is ONE binary websocket message from the gateway relay
 * (`browser_harness/screencast.py`): a 4-byte big-endian header length, a
 * UTF-8 JSON header, then the JPEG bytes. The header names the session the
 * frame belongs to, its sequence number (which the card acks so the relay can
 * pace the screencast), the JPEG's size and the password-box rectangles the
 * card paints over before anyone sees the frame.
 *
 * Pure functions only, no DOM and no React: `useWebSocket` calls
 * `decodeBrowserFrame` on every binary message, and the card's canvas paints
 * what the ring holds. Every field is checked before it is trusted, because
 * the header is whatever answered the socket: a frame that fails a check is
 * dropped, never half-applied.
 */

/** The JSON header in front of the JPEG. Mirrors the wire contract. */
export interface BrowserFrameHeader {
  type: 'browser_frame'
  session_key: string
  /**
   * Which screencast the frame came from. `seq` restarts at 1 with every
   * stream (a new turn, a tab switch, a gateway restart), so the store scopes
   * its ack dedup to `(stream, seq)`. Empty when an older relay omits it.
   */
  stream: string
  seq: number
  /** JPEG width and height in pixels: the coordinate space takeover input uses. */
  w: number
  h: number
  /** Password-field boxes `[x, y, w, h]` in frame pixels, painted over by the card. */
  mask: MaskRect[]
}

export type MaskRect = readonly [number, number, number, number]

/** One decoded frame: its checked header and the JPEG bytes, not yet an image. */
export interface DecodedBrowserFrame {
  header: BrowserFrameHeader
  jpeg: Uint8Array
  /** Wall-clock ms when the frame was decoded; the replay keeps its spacing from this. */
  receivedAt: number
}

/** The header length prefix is 4 bytes; a header can be no larger than this. */
export const MAX_HEADER_BYTES = 64 * 1024

const HEADER_PREFIX_BYTES = 4

/** A frame dimension the relay could plausibly send; anything else is a bad header. */
const MAX_FRAME_EDGE = 16_384
/** The relay's stream id is 8 hex chars; anything much longer is not a header we wrote. */
const MAX_STREAM_ID_CHARS = 64

function isFiniteInt(v: unknown, min: number, max: number): v is number {
  return typeof v === 'number' && Number.isInteger(v) && v >= min && v <= max
}

function readMask(raw: unknown, w: number, h: number): MaskRect[] | null {
  if (raw === undefined || raw === null) return []
  if (!Array.isArray(raw)) return null
  const out: MaskRect[] = []
  for (const r of raw) {
    if (!Array.isArray(r) || r.length !== 4) return null
    const [x, y, rw, rh] = r as unknown[]
    if (!isFiniteInt(x, 0, w) || !isFiniteInt(y, 0, h) || !isFiniteInt(rw, 0, w) || !isFiniteInt(rh, 0, h)) return null
    out.push([x, y, rw, rh])
  }
  return out
}

/**
 * Decode one binary websocket message into a frame, or `null` when the bytes
 * are not a well-formed browser frame. The caller drops a `null` silently: a
 * malformed frame is a bug upstream, and a frame that paints wrong is worse
 * than a frame that never shows.
 */
export function decodeBrowserFrame(buf: ArrayBuffer, now: number = Date.now()): DecodedBrowserFrame | null {
  if (buf.byteLength < HEADER_PREFIX_BYTES + 2) return null
  const view = new DataView(buf)
  const headerLen = view.getUint32(0, false)
  if (headerLen === 0 || headerLen > MAX_HEADER_BYTES || HEADER_PREFIX_BYTES + headerLen > buf.byteLength) return null
  let parsed: unknown
  try {
    parsed = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(new Uint8Array(buf, HEADER_PREFIX_BYTES, headerLen)))
  } catch {
    return null
  }
  if (!parsed || typeof parsed !== 'object') return null
  const h = parsed as Record<string, unknown>
  if (h.type !== 'browser_frame') return null
  if (typeof h.session_key !== 'string' || !h.session_key) return null
  if (h.stream !== undefined && (typeof h.stream !== 'string' || h.stream.length > MAX_STREAM_ID_CHARS)) return null
  if (!isFiniteInt(h.seq, 0, Number.MAX_SAFE_INTEGER)) return null
  if (!isFiniteInt(h.w, 1, MAX_FRAME_EDGE) || !isFiniteInt(h.h, 1, MAX_FRAME_EDGE)) return null
  const mask = readMask(h.mask, h.w, h.h)
  if (mask === null) return null
  const jpeg = new Uint8Array(buf, HEADER_PREFIX_BYTES + headerLen)
  if (jpeg.byteLength === 0) return null
  return {
    header: { type: 'browser_frame', session_key: h.session_key, stream: typeof h.stream === 'string' ? h.stream : '', seq: h.seq, w: h.w, h: h.h, mask },
    jpeg,
    receivedAt: now,
  }
}

/**
 * Encode a frame the way the relay does. Used by the tests and by nothing in
 * production: the dashboard only ever reads frames.
 */
export function encodeBrowserFrame(header: Omit<BrowserFrameHeader, 'type' | 'mask' | 'stream'> & { mask?: MaskRect[]; stream?: string }, jpeg: Uint8Array): ArrayBuffer {
  const json = new TextEncoder().encode(JSON.stringify({ type: 'browser_frame', ...header, stream: header.stream ?? '', mask: header.mask ?? [] }))
  const out = new Uint8Array(HEADER_PREFIX_BYTES + json.byteLength + jpeg.byteLength)
  new DataView(out.buffer).setUint32(0, json.byteLength, false)
  out.set(json, HEADER_PREFIX_BYTES)
  out.set(jpeg, HEADER_PREFIX_BYTES + json.byteLength)
  return out.buffer
}

/**
 * The frames a turn keeps for Replay, bounded by count AND bytes so a long
 * session at 60 fps cannot grow the heap without limit. Memory only, for the
 * life of the page (RFC open question 1): a reload loses the replay, never a
 * screenshot the agent wrote to disk.
 */
export class FrameRing {
  private frames: DecodedBrowserFrame[] = []
  private bytes = 0

  constructor(
    public readonly maxFrames: number = DEFAULT_RING_FRAMES,
    public readonly maxBytes: number = DEFAULT_RING_BYTES,
  ) {}

  push(frame: DecodedBrowserFrame): void {
    this.frames.push(frame)
    this.bytes += frame.jpeg.byteLength
    while (this.frames.length > this.maxFrames || (this.bytes > this.maxBytes && this.frames.length > 1)) {
      const dropped = this.frames.shift()
      if (dropped) this.bytes -= dropped.jpeg.byteLength
    }
  }

  /** Drop every frame: the store evicts the rings of turns too old to replay. */
  clear(): void {
    this.frames = []
    this.bytes = 0
  }

  get length(): number { return this.frames.length }
  get byteLength(): number { return this.bytes }
  get last(): DecodedBrowserFrame | null { return this.frames[this.frames.length - 1] ?? null }
  /** A snapshot of the frames, oldest first, for a replay to walk. */
  all(): readonly DecodedBrowserFrame[] { return this.frames.slice() }
}

/** 10 minutes at 30 fps rounded down, or 48 MB, whichever bites first. */
export const DEFAULT_RING_FRAMES = 1800
export const DEFAULT_RING_BYTES = 48 * 1024 * 1024
