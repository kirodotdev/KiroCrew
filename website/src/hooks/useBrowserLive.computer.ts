/**
 * Computer use in the live card: what turns the gateway's `computer_use_frame`
 * event and a turn's `computer_*` tool rows into a record the browser card
 * already knows how to draw.
 *
 * The frame is the JPEG a computer tool already captured and downscaled for the
 * model (`computer_get_state`, and the snapshot `computer_launch_app` returns); the gateway relays exactly those bytes and drops the frame
 * for a window holding a password field, so there is no stream, no ack and no
 * mask here. `readComputerFrame` checks one event field by field and turns it
 * into the browser card's frame shape, so the canvas, the frame ring and the
 * replay take it unchanged. The store folds it in (`applyComputerFrame` in
 * `useBrowserLive`).
 *
 * The step strip is the turn's computer tool calls, in order, labelled by the
 * tool's own name (`computer_click` reads "click"). The name is the trusted
 * `meta.tool_name` when the gateway stamped one, else the row's pill text; the
 * arguments are never read.
 */

import type { ChatMessage } from '../types'
import type { DecodedBrowserFrame } from './useBrowserLive.frame'

/** The stream id a desktop frame carries in the browser frame header. */
export const COMPUTER_STREAM = 'computer'

/** Same bounds as the gateway's `computer_use/screencast.py`. */
const MAX_FRAME_B64_CHARS = 4_000_000
const MAX_EDGE = 16_384
const SESSION_KEY_RE = /^[A-Za-z0-9_.:-]{1,256}$/
const APP_RE = /^[A-Za-z0-9 ._+&()-]{1,96}$/
const B64_RE = /^[A-Za-z0-9+/]*={0,2}$/

/** One checked desktop frame, plus the application it shows. */
export interface ComputerFrame {
  frame: DecodedBrowserFrame
  /** The application's display name (never the window title), or ''. */
  app: string
}

const edge = (v: unknown): number | null => (typeof v === 'number' && Number.isInteger(v) && v > 0 && v <= MAX_EDGE ? v : null)

/** Check a `computer_use_frame` payload; `null` for one the card must not draw. */
export function readComputerFrame(raw: unknown, seq: number, now: number = Date.now()): ComputerFrame | null {
  if (!raw || typeof raw !== 'object') return null
  const d = raw as Record<string, unknown>
  if (typeof d.session_key !== 'string' || !SESSION_KEY_RE.test(d.session_key)) return null
  if (d.format !== undefined && d.format !== 'jpeg') return null
  const data = d.data
  if (typeof data !== 'string' || !data || data.length > MAX_FRAME_B64_CHARS || data.length % 4 !== 0 || !B64_RE.test(data)) return null
  const w = edge(d.width)
  const h = edge(d.height)
  if (w === null || h === null) return null
  let jpeg: Uint8Array
  try {
    const bin = atob(data)
    jpeg = new Uint8Array(bin.length)
    for (let i = 0; i < bin.length; i++) jpeg[i] = bin.charCodeAt(i)
  } catch {
    return null
  }
  if (jpeg.byteLength === 0) return null
  return {
    frame: {
      header: { type: 'browser_frame', session_key: d.session_key, stream: COMPUTER_STREAM, seq, w, h, mask: [] },
      jpeg,
      receivedAt: now,
    },
    app: typeof d.app === 'string' && APP_RE.test(d.app) ? d.app : '',
  }
}

const COMPUTER_ROW_RE = /^🔧\s*(?:@(?:[\w.-]+\/)?)?(computer_[a-z_]+)(?:\s|$)/
const NAMESPACED_COMPUTER_RE = /(?:^|\/|::|___)(computer_[a-z_]+)$/

/** The computer tool a row called (`computer_click`), or '' for any other row. */
export function computerToolName(msg: ChatMessage): string {
  if (msg.role !== 'tool') return ''
  const trusted = msg.meta?.tool_name
  if (typeof trusted === 'string' && trusted) return NAMESPACED_COMPUTER_RE.exec(trusted)?.[1] ?? ''
  return COMPUTER_ROW_RE.exec(msg.content)?.[1] ?? ''
}

export function isComputerToolMessage(msg: ChatMessage): boolean {
  return computerToolName(msg) !== ''
}

/** The step label for a computer tool: its name without the prefix, words spaced. */
export function computerStepLabel(tool: string): string {
  return tool.replace(/^computer_/, '').replace(/_/g, ' ')
}
