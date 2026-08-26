import type { Terminal } from '@xterm/xterm'
import { ArrowLeft, ArrowDown, ArrowUp, ArrowRight, type LucideIcon } from 'lucide-react'

/**
 * Keys a touch keyboard cannot produce, delivered to xterm as synthetic events.
 *
 * Mobile keyboards ship no Tab, no Escape, no arrows and no Ctrl, which locks a
 * phone user out of shell completion, vi, history recall and interrupting a
 * runaway command. Each entry below is one such key.
 */
export interface TermKey {
  /**
   * What the soft key shows. Named keys are spelled out (`esc`, `tab`,
   * `ctrl-c`) because their glyphs are not widely recognised; the four
   * movement keys show `icon` instead and keep the arrow character here only
   * as documentation of what the key is.
   */
  label: string
  /**
   * Drawn in place of `label` when set. The arrows are icons, not characters,
   * because U+2190..U+2193 are not all present in the terminal font stack: the
   * browser falls back per glyph, so the four arrows rendered at visibly
   * different sizes and weights next to each other.
   */
  icon?: LucideIcon
  /** Screen-reader name, when it differs from the visible label. */
  aria: string
  /** `KeyboardEvent.key`. */
  key: string
  /** `KeyboardEvent.keyCode` — see `pressTerminalKey` for why this matters. */
  keyCode: number
  ctrl?: boolean
}

export const SOFT_KEYS: readonly TermKey[] = [
  { label: 'esc',    aria: 'Escape',      key: 'Escape',     keyCode: 27 },
  { label: 'tab',    aria: 'Tab',         key: 'Tab',        keyCode: 9  },
  { label: '←',      aria: 'Left arrow',  key: 'ArrowLeft',  keyCode: 37, icon: ArrowLeft },
  { label: '↓',      aria: 'Down arrow',  key: 'ArrowDown',  keyCode: 40, icon: ArrowDown },
  { label: '↑',      aria: 'Up arrow',    key: 'ArrowUp',    keyCode: 38, icon: ArrowUp },
  { label: '→',      aria: 'Right arrow', key: 'ArrowRight', keyCode: 39, icon: ArrowRight },
  { label: 'ctrl-c', aria: 'Control C',   key: 'c',          keyCode: 67, ctrl: true },
] as const

/**
 * Press a key on a terminal as if it had been typed.
 *
 * Dispatches a synthetic `keydown` at xterm's hidden textarea rather than
 * writing the key's bytes to the PTY. The two are NOT equivalent: xterm invokes
 * its `attachCustomKeyEventHandler` from that textarea's keydown listener, and
 * `TerminalCompletion` owns that slot. Going through the event therefore keeps
 * the whole existing pipeline intact — an open completion menu claims Tab and
 * accepts a path, and only when no menu is up does xterm fall through to
 * writing `\t` for the shell's own completion. Writing bytes directly would
 * bypass the menu and silently disable it on touch devices.
 *
 * `keyCode` is set because xterm's key evaluator switches on it, not on `key`
 * (its Tab branch is `case 9`). `keyCode` is a legacy field that the DOM spec
 * omits from `KeyboardEventInit`, but every engine still honours it there;
 * `terminalKeys.test.ts` pins that so a future engine cannot break these keys
 * quietly.
 *
 * Returns false when the terminal has not been opened yet (no textarea), which
 * is the one state where there is nothing to dispatch at.
 */
export function pressTerminalKey(term: Terminal, k: TermKey): boolean {
  const ta = term.textarea
  if (!ta) return false
  // A soft key is pressed with the pointer, which leaves focus wherever it was;
  // xterm only routes keys it receives on this textarea.
  ta.focus()
  ta.dispatchEvent(new KeyboardEvent('keydown', {
    key: k.key,
    code: k.key,
    keyCode: k.keyCode,
    which: k.keyCode,
    ctrlKey: Boolean(k.ctrl),
    bubbles: true,
    cancelable: true,
  } as KeyboardEventInit))
  return true
}
