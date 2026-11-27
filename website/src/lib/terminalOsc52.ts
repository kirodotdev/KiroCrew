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
 * A copy is applied only when the user's own input reached the program in this
 * terminal shortly before it, which is when a program's copy command follows
 * the user's own selection. That input is a key press xterm sends to the
 * program (`onKey`), or a pointer press/release without Shift or Alt while the
 * program has turned on mouse tracking. A plain click or drag, a key the dashboard keeps for a
 * shortcut, a paste, and a reply the terminal sends by itself never open the
 * window. Output that arrives with no such recent input leaves the clipboard
 * unchanged.
 *
 * Input that carries a carriage return or line feed (Enter, Ctrl+M, Ctrl+J, a
 * pasted line) closes the window, as do Ctrl+C, Ctrl+D, Ctrl+Z and Ctrl+\.
 * Each runs, ends or stops a command such as `cat` or `curl`, whose output
 * could otherwise carry OSC 52 bytes that replace the clipboard.
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

const POINTER_EVENTS = ['pointerdown', 'pointerup'] as const

/** Enter, Ctrl+M, Ctrl+J, Ctrl+C, Ctrl+D, Ctrl+Z and Ctrl+\ as the program
 *  receives them: each runs, ends or stops a command. */
const CLOSES_WINDOW = /[\r\n\x03\x04\x1a\x1c]/

/** OSC 52 copy handling for one terminal. */
export interface Osc52CopyHandle extends IDisposable {
  /** Ignore every copy, and every input that would allow one, until `resume`. */
  suspend(): void
  /** Stop ignoring copies. A copy still needs fresh input after this. */
  resume(): void
  /** Close the input window: the dashboard sent the program a command line
   *  that did not pass through xterm. */
  closeWindow(): void
}

/**
 * Handle OSC 52 copies on `term`. Keys come from the terminal's own `onKey`,
 * and pointer input from DOM events on the terminal's own element, so one
 * terminal's input never enables a copy in another. The pointer listeners sit
 * on `document` because a cached terminal may be opened into its element after
 * this runs. Dispose to remove them; the parser handler is also removed with
 * the terminal.
 */
export function attachOsc52Copy(term: Terminal, opts: Osc52Options = {}): Osc52CopyHandle {
  const now = opts.now ?? (() => performance.now())
  const write = opts.write ?? copyToClipboard
  let lastInput = Number.NEGATIVE_INFINITY
  let suspended = false

  const close = () => { lastInput = Number.NEGATIVE_INFINITY }

  // A key xterm sends to the program. A key the dashboard's key handler keeps
  // (a shortcut, a paste chord) never fires this.
  const keySub = term.onKey(({ key }) => {
    if (suspended) return
    if (CLOSES_WINDOW.test(key)) close()
    else lastInput = now()
  })

  // A press or release only reaches the program while it tracks the mouse;
  // otherwise it is a plain click or a selection drag in the dashboard.
  // xterm keeps a Shift (or macOS Option) press for its own selection, so a
  // press with either modifier is treated as not reaching the program.
  const onPointer = (e: Event) => {
    if (suspended || term.modes.mouseTrackingMode === 'none') return
    if (e instanceof MouseEvent && (e.shiftKey || e.altKey)) return
    const el = term.element
    if (!el || !(e.target instanceof Node) || !el.contains(e.target)) return
    lastInput = now()
  }
  for (const type of POINTER_EVENTS) document.addEventListener(type, onPointer, true)

  // Any input that reaches the program and runs, ends or stops a command
  // closes the window, whichever key, shortcut or paste produced it. xterm
  // emits this after `onKey`, so it always has the last word.
  const dataSub = term.onData((data) => {
    if (CLOSES_WINDOW.test(data)) close()
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
      close()
    },
    resume: () => { suspended = false },
    closeWindow: close,
    dispose: () => {
      handler.dispose()
      keySub.dispose()
      dataSub.dispose()
      for (const type of POINTER_EVENTS) document.removeEventListener(type, onPointer, true)
    },
  }
}
