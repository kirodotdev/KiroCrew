import { useEffect, useRef, useState } from 'react'
import type { Terminal } from '@xterm/xterm'
import { ClipboardPaste } from 'lucide-react'
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
 *
 * The async key (Paste) drops late deliveries by reading the terminal's own
 * layout: a clipboard read can take seconds on iOS (the permission callout),
 * and a paste that resolves after the terminal went invisible — tab switch,
 * remote-instance switch, pane closed — must be DROPPED, not delivered. In
 * every one of those the terminal's host is display:none or detached, so
 * `term.element.offsetParent` is null; a newline-terminated clipboard would
 * otherwise execute in a shell the user cannot see.
 */
type PasteError = 'paste_failed' | 'paste_clipboard_empty' | 'paste_permission_needed' | 'paste_needs_https'

/** Literal-key lookup (runtime-assembled catalog keys are forbidden by
 *  dynamicKeys.test.ts, which is what keeps the dead-key scan meaningful). */
function pasteErrorLabel(k: PasteError): string {
  switch (k) {
    case 'paste_clipboard_empty': return i18nT('components.terminalKeyBar.paste_clipboard_empty')
    case 'paste_permission_needed': return i18nT('components.terminalKeyBar.paste_permission_needed')
    case 'paste_needs_https': return i18nT('components.terminalKeyBar.paste_needs_https')
    default: return i18nT('components.terminalKeyBar.paste_failed')
  }
}

export default function TerminalKeyBar({ term }: { term: Terminal }) {
  // Paste is a soft key too, for the same reason the others exist: desktop
  // paste rides Ctrl/Cmd+V into xterm's hidden textarea, an event a touch
  // keyboard never fires, and no long-press callout surfaces over the xterm
  // viewport. It differs from SOFT_KEYS in being async (clipboard permission)
  // and fallible, so it is rendered inline rather than forced into TermKey.
  // Which failure message the key shows, keyed by cause — the UX review's
  // point: the code already distinguishes these branches, and folding them
  // into one generic string leaves the deny-path user (the common iOS
  // first-run) with no visible remedy.
  const [pasteError, setPasteError] = useState<PasteError | null>(null)
  const resetTimer = useRef<ReturnType<typeof setTimeout>>()
  const aliveRef = useRef(true)
  // In-flight guard: a second tap while the clipboard read is pending would
  // issue a second read and a duplicate delivery into the PTY; a late success
  // could also clear a failure state the user has not read yet.
  const busyRef = useRef(false)
  useEffect(() => {
    aliveRef.current = true
    return () => { aliveRef.current = false; clearTimeout(resetTimer.current) }
  }, [])

  const handlePaste = () => {
    if (busyRef.current) return
    // A denied permission, a non-secure context, and an empty clipboard must
    // all be VISIBLE: silently doing nothing reads as a broken key on exactly
    // the devices this bar exists for. The failed state reverts after a beat
    // so the key stays usable for a retry once the user grants permission.
    const fail = (key: PasteError) => {
      busyRef.current = false
      if (!aliveRef.current) return
      setPasteError(key)
      clearTimeout(resetTimer.current)
      resetTimer.current = setTimeout(() => setPasteError(null), 4000)
    }
    // Optional-chain the whole path: `navigator.clipboard` is undefined in
    // non-secure contexts, and `readText` is missing on engines that ship
    // write-only clipboard support. This failure is PERMANENT for the page
    // (plain-HTTP LAN access) — naming the remedy matters, because a generic
    // "Paste failed" that auto-reverts reads as a transient glitch and the
    // user retries forever.
    const read = navigator.clipboard?.readText?.bind(navigator.clipboard)
    if (!read) { fail('paste_needs_https'); return }
    busyRef.current = true
    read()
      .then(text => {
        // Staleness gate: between the tap and the clipboard resolving, the
        // user may have switched tabs, switched to a remote instance, or
        // closed this pane — in every one of those the terminal's host is
        // display:none or detached, so its offsetParent is null (the same
        // guard CliPanel's refit path uses). A missing element (never
        // opened, or disposed mid-read) fails closed the same way. This
        // layout read subsumes the tab-visibility prop the component once
        // took: the pane's `visible` toggles display on the wrapper that
        // CONTAINS the terminal, so the DOM already answers the question.
        if (!aliveRef.current || !term.element?.offsetParent) {
          busyRef.current = false
          return
        }
        // An empty clipboard pastes nothing — say so rather than no-op.
        if (!text) { fail('paste_clipboard_empty'); return }
        busyRef.current = false
        setPasteError(null)
        // term.paste routes through the same onData → PTY socket pipeline as
        // typing, converts newlines to carriage returns, and wraps the payload
        // in bracketed-paste markers whenever the running application enabled
        // that mode — which is what keeps a multi-line paste from executing
        // line-by-line in shells/editors that opt in.
        term.paste(text)
      })
      // One catch for both legs: a rejected read AND a throw out of
      // term.paste (disposed terminal) — otherwise the latter is an unhandled
      // rejection. A NotAllowedError is the permission prompt saying no, and
      // its message must name the remedy, not just the failure.
      .catch((err: unknown) => {
        const denied = err instanceof DOMException && err.name === 'NotAllowedError'
        fail(denied ? 'paste_permission_needed' : 'paste_failed')
      })
  }

  return (
    <div
      data-testid="terminal-key-bar"
      role="toolbar"
      aria-label={i18nT('components.terminalKeyBar.terminal_keys')}
      className="flex shrink-0 items-center gap-1 border-t border-border py-1"
    >
      {/* The key caps scroll in their OWN region (flex-1 min-w-0) so the
          Paste region stays pinned on-screen: at real phone widths
          (375–393px) the caps + Paste exceed the viewport, and a toolbar
          that scrolls as a whole hides the new key in the horizontal
          overflow with no scroll affordance. */}
      <div data-testid="terminal-key-caps" className="flex min-w-0 flex-1 items-center gap-1 overflow-x-auto">
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
      {/* Separated region (right-aligned, behind a divider): the clipboard
          action group, deliberately a DIFFERENT visual group from the key-cap
          row — Paste is a clipboard action, not a key press, and
          max-two-buttons-per-row's cap is per visual group ("Controls in a
          genuinely different row or a separated region" do not count), so the
          pre-existing key-cap row does not grow.
          Width discipline is CONTAINER-relative, never viewport-relative: the
          terminal can live in a narrow docked pane inside a wide window, so a
          vw cap would let a failure label expand past the pane and be clipped
          by the pane's overflow-hidden. max-w-[70%] caps against the toolbar
          itself and min-w-0 lets the button truncate inside it. */}
      <div className="flex min-w-0 max-w-[70%] shrink items-center border-l border-border pl-2">
        <button
          type="button"
          aria-label={pasteError ? pasteErrorLabel(pasteError) : i18nT('components.terminalKeyBar.paste')}
          title={pasteError ? pasteErrorLabel(pasteError) : i18nT('components.terminalKeyBar.paste')}
          // Same focus preservation as the other keys: a blur would dismiss the
          // on-screen keyboard mid-composition.
          onPointerDown={e => e.preventDefault()}
          onClick={handlePaste}
          className="flex min-w-[2.25rem] max-w-full items-center justify-center gap-1 whitespace-nowrap rounded-md border border-border bg-bg-elevated px-2 py-1.5 font-mono text-[13px] text-text active:bg-bg-hover"
        >
          {/* h-4 w-4, matching the sibling arrow icons this bar already pins
              (see the icon-sizing test) — `lucide-inline`'s 1em box would draw
              this one icon smaller than its neighbours. */}
          <ClipboardPaste className={`h-4 w-4 ${pasteError ? 'text-danger' : ''}`} aria-hidden="true" />
          {/* Always-visible text label: Paste is the bar's only non-keycap
              control and the clipboard glyph alone is copy/paste-ambiguous;
              `title` never shows on touch. In a failure state the label
              becomes the cause-specific message (not color alone). Hidden
              from the accessible name so the sibling status region below is
              not double-read. */}
          <span aria-hidden="true" className={`min-w-0 truncate ${pasteError ? 'text-danger' : ''}`}>
            {pasteError ? pasteErrorLabel(pasteError) : i18nT('components.terminalKeyBar.paste')}
          </span>
        </button>
        {/* Announcement lives OUTSIDE the button: role=button is
            children-presentational in ARIA, so a live region nested inside it
            gets pruned and never fires — and the button is never focused
            (pointerdown is cancelled to keep the on-screen keyboard up), so a
            swapped aria-label would not be re-announced either. */}
        <span role="status" aria-live="polite" className="sr-only">
          {pasteError ? pasteErrorLabel(pasteError) : ''}
        </span>
      </div>
    </div>
  )
}
