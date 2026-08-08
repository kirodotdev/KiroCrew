import type { Terminal } from '@xterm/xterm'
import { SOFT_KEYS, pressTerminalKey } from '../utils/terminalKeys'
import { i18nT } from '../i18n/t'
/**
 * A row of soft keys for the keys a touch keyboard omits (Tab, Escape, arrows,
 * ^C). Rendered only on touch devices by the caller.
 *
 * This bar is also what keeps the right-swipe Tab gesture compliant: WCAG 2.5.1
 * requires every path-based gesture to have a single-pointer alternative, so the
 * swipe may accelerate Tab but must never be the only way to press it.
 *
 * It sits in the terminal pane's flow, BELOW the terminal, and never overlays
 * it — the shell's prompt lives on the bottom row, so an overlay would cover the
 * line being typed.
 */
export default function TerminalKeyBar({ term }: { term: Terminal }) {
  return (
    <div
      data-testid="terminal-key-bar"
      role="toolbar"
      aria-label={i18nT('components.terminalKeyBar.terminal_keys')}
      className="flex shrink-0 items-center gap-1 overflow-x-auto border-t border-border py-1"
    >
      {SOFT_KEYS.map(k => {
        const Icon = k.icon
        return (
          <button
            key={k.aria}
            type="button"
            aria-label={k.aria}
            title={k.aria}
            // Keep the press from moving focus off xterm's textarea: a blur closes
            // the on-screen keyboard, so tapping Tab would dismiss the very
            // keyboard the user is typing on.
            onPointerDown={e => e.preventDefault()}
            onClick={() => pressTerminalKey(term, k)}
            className="flex min-w-[2.25rem] shrink-0 items-center justify-center whitespace-nowrap rounded-md border border-border bg-bg-elevated px-2 py-1.5 font-mono text-[13px] text-text active:bg-bg-hover"
          >
            {Icon ? <Icon className="h-4 w-4" aria-hidden="true" /> : k.label}
          </button>
        )
      })}
    </div>
  )
}
