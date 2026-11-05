import type { IDisposable, Terminal } from '@xterm/xterm'
import { copyToClipboard } from '../utils/clipboard'

/**
 * OSC 52 ("set selection data") support for the dashboard terminal: a program
 * running in the terminal, such as a multiplexer, sends `ESC ] 52 ; Pc ; Pd BEL`
 * where `Pd` is the base64 of the UTF-8 text to copy.
 *
 * This handles copying only. A `Pd` of `?` asks the terminal to send the
 * clipboard back; that request is never answered, and nothing here reads the
 * clipboard.
 *
 * A copy is applied only when this terminal received a key press or a pointer
 * press/release shortly before it, which is when a program's copy command
 * follows the user's own selection. Output that arrives with no recent input on
 * this terminal leaves the clipboard unchanged.
 */

/** Largest decoded copy accepted, in bytes. */
export const OSC52_MAX_BYTES = 1024 * 1024

/** How long after the user's last input on this terminal a copy is applied. */
export const OSC52_INPUT_WINDOW_MS = 2000

/** Selection targets that mean "the clipboard" in practice: clipboard,
 *  primary, the xterm "select" default, and the numbered cut buffers. */
const TARGETS = /^[cps0-7]*$/
const BASE64 = /^[A-Za-z0-9+/]*={0,2}$/

/**
 * The text an OSC 52 payload asks to copy, or `null` when the payload is not a
 * copy this terminal applies: a read request (`?`), an empty payload, an
 * unknown target, malformed base64, text that is not valid UTF-8, or a payload
 * over `OSC52_MAX_BYTES`.
 *
 * `data` is everything after `52;`, i.e. `Pc;Pd`.
 */
export function decodeOsc52Copy(data: string): string | null {
  const sep = data.indexOf(';')
  if (sep < 0) return null
  const targets = data.slice(0, sep)
  const payload = data.slice(sep + 1)
  if (!TARGETS.test(targets)) return null
  if (payload === '' || payload === '?') return null
  // Reject on the encoded length first so an oversized payload is never decoded.
  if (payload.length > Math.ceil(OSC52_MAX_BYTES / 3) * 4) return null
  if (payload.length % 4 !== 0 || !BASE64.test(payload)) return null
  let binary: string
  try {
    binary = atob(payload)
  } catch {
    return null
  }
  if (binary.length > OSC52_MAX_BYTES) return null
  const bytes = Uint8Array.from(binary, ch => ch.charCodeAt(0))
  try {
    // `atob` yields one character per byte; decoding those bytes as UTF-8 is
    // what keeps non-ASCII text intact.
    // `ignoreBOM` keeps a leading U+FEFF as part of the copied text.
    return new TextDecoder('utf-8', { fatal: true, ignoreBOM: true }).decode(bytes)
  } catch {
    return null
  }
}

interface Osc52Options {
  /** Clock, for tests. */
  now?: () => number
  /** Clipboard writer, for tests. */
  write?: (text: string) => Promise<unknown>
}

const INPUT_EVENTS = ['keydown', 'pointerdown', 'pointerup'] as const

/**
 * Handle OSC 52 copies on `term`. Input is tracked from DOM events on the
 * terminal's own element, so one terminal's input never enables a copy in
 * another. The listeners sit on `document` because a cached terminal may be
 * opened into its element after this runs. Dispose to remove them; the parser
 * handler is also removed with the terminal.
 */
export function attachOsc52Copy(term: Terminal, opts: Osc52Options = {}): IDisposable {
  const now = opts.now ?? (() => performance.now())
  const write = opts.write ?? copyToClipboard
  let lastInput = Number.NEGATIVE_INFINITY

  const onInput = (e: Event) => {
    const el = term.element
    if (el && e.target instanceof Node && el.contains(e.target)) lastInput = now()
  }
  for (const type of INPUT_EVENTS) document.addEventListener(type, onInput, true)

  const handler = term.parser.registerOscHandler(52, (data) => {
    const text = decodeOsc52Copy(data)
    if (text !== null && now() - lastInput <= OSC52_INPUT_WINDOW_MS) {
      void write(text).catch(() => {})
    }
    // Consumed either way, so nothing else in the terminal acts on it.
    return true
  })

  return {
    dispose: () => {
      handler.dispose()
      for (const type of INPUT_EVENTS) document.removeEventListener(type, onInput, true)
    },
  }
}
