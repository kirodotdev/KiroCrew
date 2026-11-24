/**
 * The XR page's live panels on the dashboard's one socket.
 *
 * A panel of `/xr?view=pages` can show one of the dashboard's own pages live: the gateway draws the page in a tab of
 * its own and sends pictures (`pages/xr/workspace/streamSource.ts`). Those pictures and the panel's few messages
 * ride the socket this tab already has. This module is the seam: the socket's owner (`useWebSocket`) installs a
 * sender when the socket opens and hands over every `xr_panel` frame and every binary panel picture; a panel's
 * source listens.
 *
 * A leaf: it imports nothing, so the first-load bundle pays a few lines for it and nothing of three.js.
 */

/** The header before every panel picture: which panel, which picture, and the page's size in CSS pixels. */
export interface XrPanelPicture {
  panel: string
  stream: string
  seq: number
  w: number
  h: number
  css: [number, number]
  /** Boxes the gateway painted over (secret fields), in picture pixels. */
  mask: [number, number, number, number][]
  /** How many pictures of another shape than the page's the gateway dropped for this panel so far. */
  misshapen?: number
  /** For the readout: from the hub's send of the picture before to its ack, in ms; the pace it stands at; whether the page is held back. */
  round_ms?: number | null
  pace?: number
  held?: boolean
}

export interface XrPanelListener {
  /** The socket is open (again): what the gateway held for the socket before is gone. */
  open(): void
  /** The socket closed. */
  close(): void
  /** One `xr_panel` message from the gateway: `{ op, id, ... }`. */
  message(data: Record<string, unknown>): void
  /** One picture. `bytes` is the JPEG or PNG. */
  picture(header: XrPanelPicture, bytes: Uint8Array): void
}

type Sender = (payload: object) => void

const FRAME_TYPE = 'xr_panel_frame'
const MESSAGE_TYPE = 'xr_panel'
const MAX_HEADER_BYTES = 64 * 1024

let sender: Sender | null = null
const listeners = new Set<XrPanelListener>()

/** Installed by the socket's owner when it opens; `null` when it closes. */
export function setXrPanelSender(next: Sender | null): void {
  sender = next
  for (const l of [...listeners]) { if (next) l.open(); else l.close() }
}

/** Whether there is a socket to send on now. */
export function xrPanelLinkOpen(): boolean { return sender !== null }

/** Send one panel message. `false` when there is no socket. */
export function sendXrPanel(data: object): boolean {
  if (!sender) return false
  sender({ type: MESSAGE_TYPE, data })
  return true
}

export function listenXrPanels(listener: XrPanelListener): () => void {
  listeners.add(listener)
  return () => { listeners.delete(listener) }
}

/** One `xr_panel` JSON frame from the socket. */
export function applyXrPanelFrame(data: unknown): void {
  if (!data || typeof data !== 'object') return
  for (const l of [...listeners]) l.message(data as Record<string, unknown>)
}

const isInt = (v: unknown, min: number, max: number): v is number => typeof v === 'number' && Number.isInteger(v) && v >= min && v <= max

/** Reads a binary frame's header. `null` when it is not a whole panel picture. */
export function decodeXrPanelPicture(buf: ArrayBuffer): { header: XrPanelPicture; bytes: Uint8Array } | null {
  if (buf.byteLength < 6) return null
  const length = new DataView(buf).getUint32(0, false)
  if (length === 0 || length > MAX_HEADER_BYTES || 4 + length >= buf.byteLength) return null
  let h: Record<string, unknown>
  try {
    h = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(new Uint8Array(buf, 4, length))) as Record<string, unknown>
  } catch {
    return null
  }
  if (!h || h.type !== FRAME_TYPE || typeof h.panel !== 'string' || typeof h.stream !== 'string') return null
  if (!isInt(h.seq, 0, Number.MAX_SAFE_INTEGER) || !isInt(h.w, 1, 8192) || !isInt(h.h, 1, 8192)) return null
  const css = h.css
  if (!Array.isArray(css) || css.length !== 2 || !isInt(css[0], 1, 8192) || !isInt(css[1], 1, 8192)) return null
  const mask = Array.isArray(h.mask) ? h.mask.filter((b): b is [number, number, number, number] => Array.isArray(b) && b.length === 4 && b.every(v => isInt(v, 0, 8192))) : []
  const misshapen = isInt(h.misshapen, 0, Number.MAX_SAFE_INTEGER) ? h.misshapen : undefined
  const round_ms = isInt(h.round_ms, 0, 600000) ? h.round_ms : null
  const pace = isInt(h.pace, 0, 1000) ? h.pace : undefined
  const held = typeof h.held === 'boolean' ? h.held : undefined
  return { header: { panel: h.panel, stream: h.stream, seq: h.seq, w: h.w, h: h.h, css: [css[0], css[1]], mask, misshapen, round_ms, pace, held }, bytes: new Uint8Array(buf, 4 + length) }
}

/**
 * One binary frame from the socket. `true` when it was a panel picture, so the socket's owner hands it to nobody
 * else. With no panel listening nothing is parsed: the live browser card's frames pass at no cost.
 */
export function applyXrPanelBinary(buf: ArrayBuffer): boolean {
  if (listeners.size === 0) return false
  const picture = decodeXrPanelPicture(buf)
  if (!picture) return false
  for (const l of [...listeners]) l.picture(picture.header, picture.bytes)
  return true
}
