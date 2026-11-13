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
 *
 * An Enter press does not count as that input, and it closes the window that
 * earlier key presses opened, as does any terminal input that carries a
 * carriage return or line feed (Ctrl+M, Ctrl+J, a pasted line). The command
 * such input runs, such as `cat` or `curl`, would otherwise let OSC 52 bytes in
 * its output replace the clipboard.
 *
 * Copies are ignored while a reconnect replays saved output. They resume once
 * xterm finishes parsing the output before the server's `replayed` frame.
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
  /** Result of an applied copy; ignored requests report nothing. */
  onResult?: (ok: boolean) => void
}

const INPUT_EVENTS = ['keydown', 'pointerdown', 'pointerup'] as const

/** OSC 52 copy handling for one terminal. */
export interface Osc52CopyHandle extends IDisposable {
  /** Ignore every copy, and every input that would allow one, until `resume`. */
  suspend(): void
  /** Stop ignoring copies. A copy still needs fresh input after this. */
  resume(): void
}

/**
 * Handle OSC 52 copies on `term`. Input is tracked from DOM events on the
 * terminal's own element, so one terminal's input never enables a copy in
 * another. The listeners sit on `document` because a cached terminal may be
 * opened into its element after this runs. Dispose to remove them; the parser
 * handler is also removed with the terminal.
 */
export function attachOsc52Copy(term: Terminal, opts: Osc52Options = {}): Osc52CopyHandle {
  const now = opts.now ?? (() => performance.now())
  const write = opts.write ?? copyToClipboard
  let lastInput = Number.NEGATIVE_INFINITY
  let suspended = false

  const onInput = (e: Event) => {
    if (suspended) return
    const el = term.element
    if (!el || !(e.target instanceof Node) || !el.contains(e.target)) return
    // An Enter press, with any modifier and from either Enter key, runs a
    // command rather than asking a program to copy, so it closes the window
    // that the keys typed before it opened.
    if (e.type === 'keydown' && (e as KeyboardEvent).key === 'Enter') {
      lastInput = Number.NEGATIVE_INFINITY
      return
    }
    lastInput = now()
  }
  for (const type of INPUT_EVENTS) document.addEventListener(type, onInput, true)
  // Input that reaches the program with a carriage return or line feed submits
  // a line, whichever key, shortcut or paste produced it, so it closes the
  // window the same way an Enter press does. xterm emits this after the
  // keydown above, so it always has the last word.
  const dataSub = term.onData((data) => {
    if (/[\r\n]/.test(data)) lastInput = Number.NEGATIVE_INFINITY
  })

  const handler = term.parser.registerOscHandler(52, (data) => {
    const text = decodeOsc52Copy(data)
    if (!suspended && text !== null && now() - lastInput <= OSC52_INPUT_WINDOW_MS) {
      void write(text).then(
        ok => opts.onResult?.(Boolean(ok)),
        () => opts.onResult?.(false),
      ).catch(() => {})
    }
    // Consumed either way, so nothing else in the terminal acts on it.
    return true
  })

  return {
    suspend: () => {
      suspended = true
      lastInput = Number.NEGATIVE_INFINITY
    },
    resume: () => { suspended = false },
    dispose: () => {
      handler.dispose()
      dataSub.dispose()
      for (const type of INPUT_EVENTS) document.removeEventListener(type, onInput, true)
    },
  }
}
