import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, cleanup, fireEvent, act } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { Terminal } from '@xterm/xterm'
import TerminalKeyBar from '../components/TerminalKeyBar'
import { SOFT_KEYS } from '../utils/terminalKeys'

function makeTerm() {
  const textarea = document.createElement('textarea')
  document.body.appendChild(textarea)
  const seen: KeyboardEvent[] = []
  textarea.addEventListener('keydown', e => seen.push(e))
  const paste = vi.fn()
  // Layout stub: offsetParent non-null = laid out / visible. Tests flip it to
  // null to simulate a display:none host (instance switch hides the whole
  // dashboard) — same mock shape as the CliPanel
  // refit-guard tests.
  const element = { offsetParent: {} as unknown }
  return { term: { textarea, paste, element } as unknown as Terminal & { paste: typeof paste }, textarea, seen, element }
}

/**
 * A term whose buffer + selection APIs behave like xterm's: `selectLines(a, b)`
 * records an inclusive line range and `selectAll()` records the whole buffer,
 * and getSelection() derives from whichever ran last — so the Select key's
 * staged create/select-all/wrap logic can be driven end to end. `lines` are the
 * buffer rows (index 0 = top of scrollback); a trailing '' models an empty tail
 * row the first tap must skip.
 *
 * `cursor` places the terminal's cursor at an absolute buffer row (baseY +
 * cursorY). Stage 1 selects the bottommost non-empty line STRICTLY ABOVE the
 * cursor row (the live shell reprints a prompt on the cursor row, which the tap
 * must skip), falling back to the cursor row itself. Defaults to the last row,
 * which — with a trailing '' tail — reproduces the pre-cursor behaviour so the
 * unchanged tests keep passing.
 */
function makeSelectableTerm(
  lines: (string | { text: string; isWrapped?: boolean })[],
  cursor?: { baseY: number; cursorY: number },
  view?: { viewportY: number; rows: number },
) {
  const base = makeTerm()
  // Normalise the row fixtures: a bare string is an unwrapped row; a
  // {text, isWrapped} models a wrapped continuation row (xterm flags every
  // physical row after the first of a wrapped logical line with isWrapped).
  const rows = lines.map(l => (typeof l === 'string' ? { text: l, isWrapped: false } : { text: l.text, isWrapped: l.isWrapped ?? false }))
  const lineTexts = rows.map(r => r.text)
  let sel: { start: number; end: number } | null = null
  const selectionListeners = new Set<() => void>()
  const emitSelectionChange = () => { for (const l of [...selectionListeners]) l() }
  // Cursor defaults to the last buffer row: with a trailing empty tail row that
  // reproduces "search from the bottom" (the tail is skipped as empty, nothing
  // sits above it to skip), so fixtures that don't care about the prompt row
  // behave exactly as before.
  const cur = cursor ?? { baseY: 0, cursorY: rows.length - 1 }
  const buffer = {
    active: {
      length: rows.length,
      baseY: cur.baseY,
      cursorY: cur.cursorY,
      // Scroll position: defaults to the live bottom (viewportY === baseY);
      // a `view` override models a user scrolled up into scrollback.
      viewportY: view?.viewportY ?? cur.baseY,
      getLine: (row: number) =>
        row >= 0 && row < rows.length
          ? { translateToString: (_trim?: boolean) => rows[row].text, isWrapped: rows[row].isWrapped }
          : undefined,
    },
  }
  const term = base.term as unknown as {
    buffer: typeof buffer
    selectLines: (a: number, b: number) => void
    selectAll: () => void
    clearSelection: () => void
    getSelection: () => string
    onSelectionChange: (cb: () => void) => { dispose: () => void }
  }
  term.buffer = buffer
  // Visible viewport height; only meaningful together with `view.viewportY`.
  ;(term as unknown as { rows?: number }).rows = view?.rows
  const selectLines = vi.fn((a: number, b: number) => { sel = { start: a, end: b }; emitSelectionChange() })
  term.selectLines = selectLines
  const selectAll = vi.fn(() => { sel = { start: 0, end: rows.length - 1 }; emitSelectionChange() })
  term.selectAll = selectAll
  term.clearSelection = () => { sel = null; emitSelectionChange() }
  term.getSelection = () =>
    sel ? lineTexts.slice(sel.start, sel.end + 1).join('\n') : ''
  // xterm's onSelectionChange: register a listener, get back a disposable. The
  // harness fires it whenever the modelled selection changes (select/clear), so
  // the component's "clear the stage when the buffer's selection empties" path
  // can be driven end to end.
  term.onSelectionChange = (cb: () => void) => {
    selectionListeners.add(cb)
    return { dispose: () => { selectionListeners.delete(cb) } }
  }
  return {
    ...base,
    selectLines,
    selectAll,
    clearSelection: () => term.clearSelection(),
    emitSelectionChange,
  }
}

afterEach(cleanup)

describe('TerminalKeyBar', () => {
  it('renders one labelled button per soft key', () => {
    const { term } = makeTerm()
    render(<TerminalKeyBar term={term} />)
    for (const k of SOFT_KEYS) {
      expect(screen.getByRole('button', { name: k.aria })).toBeTruthy()
    }
  })

  it('sends the key to the terminal when tapped', async () => {
    const { term, seen } = makeTerm()
    render(<TerminalKeyBar term={term} />)
    await userEvent.click(screen.getByRole('button', { name: 'Tab' }))
    expect(seen.map(e => e.key)).toEqual(['Tab'])
  })

  /**
   * The four arrows are drawn as icons, not characters. U+2190..U+2193 are not
   * all present in the terminal font stack, so the browser fell back per glyph
   * and the arrows rendered at visibly different sizes next to each other.
   */
  it('draws the arrows as icons and the named keys as text', () => {
    const { term } = makeTerm()
    render(<TerminalKeyBar term={term} />)
    for (const name of ['Left arrow', 'Down arrow', 'Up arrow', 'Right arrow']) {
      const btn = screen.getByRole('button', { name })
      expect(btn.querySelector('svg')).toBeTruthy()
      expect(btn.textContent).toBe('')
    }
    for (const [name, text] of [['Escape', 'esc'], ['Tab', 'tab'], ['Control C', 'ctrl-c']]) {
      const btn = screen.getByRole('button', { name })
      expect(btn.querySelector('svg')).toBeNull()
      expect(btn.textContent).toBe(text)
    }
  })

  /**
   * All four arrow icons share one box size — that is the whole point. Only the
   * sizing classes are compared: lucide appends a per-icon class of its own
   * (`lucide-arrow-left`), so the full class strings legitimately differ.
   */
  it('sizes every arrow icon identically', () => {
    const { term } = makeTerm()
    render(<TerminalKeyBar term={term} />)
    const sizes = ['Left arrow', 'Down arrow', 'Up arrow', 'Right arrow'].map(name => {
      const cls = screen.getByRole('button', { name }).querySelector('svg')!.getAttribute('class') ?? ''
      return cls.split(/\s+/).filter(c => /^[hw]-/.test(c)).sort().join(' ')
    })
    expect(sizes).toEqual(['h-4 w-4', 'h-4 w-4', 'h-4 w-4', 'h-4 w-4'])
  })

  /**
   * The whole point of the bar is to be usable while typing. A tap that blurs
   * xterm's textarea dismisses the on-screen keyboard, so pressing Tab would
   * cost the user the keyboard they were using.
   */
  it('cancels pointerdown so the press never moves focus off the terminal', async () => {
    const { term, textarea } = makeTerm()
    render(<TerminalKeyBar term={term} />)
    const btn = screen.getByRole('button', { name: 'Tab' })
    const blur = vi.fn()
    textarea.addEventListener('blur', blur)
    textarea.focus()

    const ev = new PointerEvent('pointerdown', { bubbles: true, cancelable: true })
    btn.dispatchEvent(ev)

    expect(ev.defaultPrevented).toBe(true)
    expect(blur).not.toHaveBeenCalled()
  })

  /**
   * Paste soft key. Desktop paste works only because Ctrl/Cmd+V lands on
   * xterm's hidden textarea — an event a touch keyboard never fires — so the
   * bar carries an explicit Paste key that reads the clipboard and hands the
   * text to term.paste() (which owns newline normalization and bracketed-paste
   * wrapping, keeping multi-line pastes safe for opted-in shells).
   */
  describe('paste key', () => {
    const origClipboard = Object.getOwnPropertyDescriptor(navigator, 'clipboard')
    afterEach(() => {
      // The stub is per-test; leaking it would poison later-added tests.
      if (origClipboard) Object.defineProperty(navigator, 'clipboard', origClipboard)
      else delete (navigator as unknown as Record<string, unknown>).clipboard
    })
    function stubClipboard(readText: (() => Promise<string>) | undefined) {
      Object.defineProperty(navigator, 'clipboard', {
        configurable: true,
        value: readText ? { readText } : undefined,
      })
    }

    it('reads the clipboard and delivers multi-line text via term.paste', async () => {
      const { term } = makeTerm()
      const text = 'echo one\necho two\n'
      stubClipboard(() => Promise.resolve(text))
      render(<TerminalKeyBar term={term} />)

      await userEvent.click(screen.getByRole('button', { name: 'Paste' }))

      // Delivered UNSPLIT: term.paste receives the whole payload once, so
      // xterm's own bracketed-paste handling governs multi-line safety.
      expect(term.paste).toHaveBeenCalledTimes(1)
      expect(term.paste).toHaveBeenCalledWith(text)
    })

    it('surfaces a denied clipboard read with the remedy, not a generic failure', async () => {
      const { term } = makeTerm()
      stubClipboard(() => Promise.reject(new DOMException('denied', 'NotAllowedError')))
      render(<TerminalKeyBar term={term} />)

      await userEvent.click(screen.getByRole('button', { name: 'Paste' }))

      expect(term.paste).not.toHaveBeenCalled()
      const failed = await screen.findByRole('button', { name: 'Allow clipboard access' })
      expect(failed.textContent).toContain('Allow clipboard access')
    })

    it('surfaces a non-permission rejection as a plain failure', async () => {
      const { term } = makeTerm()
      stubClipboard(() => Promise.reject(new Error('boom')))
      render(<TerminalKeyBar term={term} />)

      await userEvent.click(screen.getByRole('button', { name: 'Paste' }))

      expect(term.paste).not.toHaveBeenCalled()
      expect(await screen.findByRole('button', { name: 'Paste failed' })).toBeTruthy()
    })

    /**
     * A missing clipboard API is a PERMANENT failure for the page (plain-HTTP
     * LAN access — the exact self-hosted-dashboard-from-a-phone case this bar
     * exists for). It must name the HTTPS remedy, not wear the same
     * transient-looking face as a one-off glitch the user retries forever.
     */
    it('names the HTTPS remedy when the clipboard API is missing (non-secure context)', async () => {
      const { term } = makeTerm()
      stubClipboard(undefined)
      render(<TerminalKeyBar term={term} />)

      await userEvent.click(screen.getByRole('button', { name: 'Paste' }))

      expect(term.paste).not.toHaveBeenCalled()
      expect(await screen.findByRole('button', { name: 'Paste needs a secure (HTTPS) connection' })).toBeTruthy()
    })

    it('cancels pointerdown so pasting never dismisses the on-screen keyboard', () => {
      const { term } = makeTerm()
      stubClipboard(() => Promise.resolve(''))
      render(<TerminalKeyBar term={term} />)

      const ev = new PointerEvent('pointerdown', { bubbles: true, cancelable: true })
      screen.getByRole('button', { name: 'Paste' }).dispatchEvent(ev)

      expect(ev.defaultPrevented).toBe(true)
    })

    /**
     * The clipboard read is async (on iOS the permission callout can hold it
     * open for seconds). A paste that resolves after the terminal went
     * invisible must be dropped: a tab switch sets display:none on the
     * wrapper containing the terminal, so its offsetParent goes null — and a
     * newline-terminated clipboard would execute in a shell the user cannot
     * see.
     */
    it('drops a paste that resolves after the hosting tab went hidden', async () => {
      const { term, element } = makeTerm()
      let resolveRead!: (t: string) => void
      stubClipboard(() => new Promise<string>(res => { resolveRead = res }))
      render(<TerminalKeyBar term={term} />)

      await userEvent.click(screen.getByRole('button', { name: 'Paste' }))
      element.offsetParent = null // tab switch: display:none on the pane wrapper
      resolveRead('rm -rf ./scratch\n')
      await Promise.resolve() // let the .then callback run

      expect(term.paste).not.toHaveBeenCalled()
    })

    it('drops a paste that resolves after the bar unmounted', async () => {
      const { term } = makeTerm()
      let resolveRead!: (t: string) => void
      stubClipboard(() => new Promise<string>(res => { resolveRead = res }))
      const { unmount } = render(<TerminalKeyBar term={term} />)

      await userEvent.click(screen.getByRole('button', { name: 'Paste' }))
      unmount()
      resolveRead('echo late\n')
      await Promise.resolve()

      expect(term.paste).not.toHaveBeenCalled()
    })

    /**
     * Switching to a remote instance hides the whole local dashboard by
     * toggling `display` far above this component (InstancesViewport) — a
     * paste resolving then would type into the hidden local shell. The gate
     * reads the terminal's own layout (offsetParent === null when
     * display:none / detached), which covers this case and the plain tab
     * switch with one check.
     */
    it('drops a paste that resolves after the whole dashboard was hidden (instance switch)', async () => {
      const { term, element } = makeTerm()
      let resolveRead!: (t: string) => void
      stubClipboard(() => new Promise<string>(res => { resolveRead = res }))
      render(<TerminalKeyBar term={term} />)

      await userEvent.click(screen.getByRole('button', { name: 'Paste' }))
      element.offsetParent = null // instance switch: display:none far above the pane
      resolveRead('rm -rf ./scratch\n')
      await Promise.resolve()

      expect(term.paste).not.toHaveBeenCalled()
    })

    /** A terminal that was never opened (or was disposed mid-read) has no
     *  element at all — the layout gate must fail closed, not throw or
     *  deliver. */
    it('fails closed when the terminal has no element', async () => {
      const { term } = makeTerm()
      delete (term as unknown as { element?: unknown }).element
      stubClipboard(() => Promise.resolve('echo orphan\n'))
      render(<TerminalKeyBar term={term} />)

      await userEvent.click(screen.getByRole('button', { name: 'Paste' }))

      expect(term.paste).not.toHaveBeenCalled()
    })

    /**
     * role=button is children-presentational in ARIA: a live region nested
     * inside the button gets pruned and never announces. And the button is
     * never focused (pointerdown is cancelled to keep the on-screen keyboard
     * up), so a swapped aria-label would not re-announce either. The status
     * region must therefore live OUTSIDE the button.
     */
    it('announces the failure from a status region outside the button', async () => {
      const { term } = makeTerm()
      stubClipboard(() => Promise.reject(new DOMException('denied', 'NotAllowedError')))
      render(<TerminalKeyBar term={term} />)

      await userEvent.click(screen.getByRole('button', { name: 'Paste' }))

      // Copy and Paste each own a status region; select the one that announced.
      const status = (await screen.findAllByRole('status')).find(
        (el) => el.textContent !== '',
      )!
      expect(status.closest('button')).toBeNull()
      expect(status.textContent).toBe('Allow clipboard access')
    })

    it('names an empty clipboard rather than claiming a paste failure', async () => {
      const { term } = makeTerm()
      stubClipboard(() => Promise.resolve(''))
      render(<TerminalKeyBar term={term} />)

      await userEvent.click(screen.getByRole('button', { name: 'Paste' }))

      expect(term.paste).not.toHaveBeenCalled()
      expect(await screen.findByRole('button', { name: 'Clipboard is empty' })).toBeTruthy()
    })

    /** Paste is the bar's only non-keycap control and the clipboard glyph is
     *  copy/paste-ambiguous on touch, where `title` never shows — the label
     *  must be visible text, like the sibling keycaps. */
    it('shows a visible text label next to the icon in the idle state', () => {
      const { term } = makeTerm()
      stubClipboard(() => Promise.resolve(''))
      render(<TerminalKeyBar term={term} />)

      expect(screen.getByRole('button', { name: 'Paste' }).textContent).toBe('Paste')
    })

    /**
     * At real phone widths the key caps + Paste exceed the viewport. The key
     * caps must scroll in their OWN region while Paste stays pinned outside
     * it — a toolbar that scrolls as a whole hides the Paste key in the
     * horizontal overflow with no scroll affordance, on exactly the devices
     * this bar exists for.
     */
    it('pins the paste key outside the scrollable key-cap region', () => {
      const { term } = makeTerm()
      stubClipboard(() => Promise.resolve(''))
      render(<TerminalKeyBar term={term} />)

      const caps = screen.getByTestId('terminal-key-caps')
      expect(caps.className).toContain('overflow-x-auto')
      expect(caps.className).toContain('flex-1')
      for (const k of SOFT_KEYS) {
        expect(caps.contains(screen.getByRole('button', { name: k.aria }))).toBe(true)
      }
      expect(caps.contains(screen.getByRole('button', { name: 'Paste' }))).toBe(false)
      // The toolbar root must NOT be the scroll container anymore.
      expect(screen.getByRole('toolbar').className).not.toContain('overflow-x-auto')
    })

    /**
     * Width discipline must be CONTAINER-relative: the terminal can live in a
     * narrow docked pane inside a wide window, so a viewport-relative cap
     * (vw) lets a failure-state label expand past the pane and be clipped by
     * the pane's overflow-hidden — hiding exactly the remedy it exists to
     * show. The region caps against the toolbar and the label truncates
     * inside it.
     */
    it('caps the paste region against the toolbar, never the viewport', () => {
      const { term } = makeTerm()
      stubClipboard(() => Promise.resolve(''))
      render(<TerminalKeyBar term={term} />)

      const btn = screen.getByRole('button', { name: 'Paste' })
      const region = btn.parentElement!
      expect(region.className).toContain('max-w-[70%]')
      expect(region.className).toContain('min-w-0')
      expect(btn.className).toContain('max-w-full')
      expect(region.className).not.toContain('vw')
      expect(btn.className).not.toContain('vw')
    })

    it('ignores taps while a clipboard read is already in flight', async () => {
      const { term } = makeTerm()
      const readText = vi.fn(() => new Promise<string>(() => {})) // never resolves
      stubClipboard(readText)
      render(<TerminalKeyBar term={term} />)

      const btn = screen.getByRole('button', { name: 'Paste' })
      await userEvent.click(btn)
      await userEvent.click(btn)

      expect(readText).toHaveBeenCalledTimes(1)
    })

    it('reverts the failed state after a beat so the key stays retryable', async () => {
      vi.useFakeTimers()
      try {
        const { term } = makeTerm()
        stubClipboard(undefined) // sync failure path, no promise to await
        render(<TerminalKeyBar term={term} />)

        fireEvent.click(screen.getByRole('button', { name: 'Paste' }))
        expect(screen.getByRole('button', { name: 'Paste needs a secure (HTTPS) connection' })).toBeTruthy()

        act(() => { vi.advanceTimersByTime(4100) })
        expect(screen.getByRole('button', { name: 'Paste' })).toBeTruthy()
        expect(screen.queryByRole('button', { name: 'Paste needs a secure (HTTPS) connection' })).toBeNull()
      } finally {
        vi.useRealTimers()
      }
    })
  })

  /**
   * Copy soft key — the other half of the touch clipboard story. xterm's
   * drag-selection is a mouse gesture that never fires on touch, so CliPanel's
   * selection toolbar (its only Copy affordance, wired to mouseup) never
   * appears; touch devices could paste since #5571 but had no way to copy OUT.
   * This key reads term.getSelection() and writes it to the clipboard.
   */
  describe('copy key', () => {
    const origClipboard = Object.getOwnPropertyDescriptor(navigator, 'clipboard')
    afterEach(() => {
      if (origClipboard) Object.defineProperty(navigator, 'clipboard', origClipboard)
      else delete (navigator as unknown as Record<string, unknown>).clipboard
    })
    function stubWrite(writeText: ((t: string) => Promise<void>) | undefined) {
      Object.defineProperty(navigator, 'clipboard', {
        configurable: true,
        value: writeText ? { writeText } : undefined,
      })
    }
    function withSelection(term: Terminal, text: string) {
      ;(term as unknown as { getSelection: () => string }).getSelection = () => text
    }

    it('writes the current selection to the clipboard', async () => {
      const { term } = makeTerm()
      withSelection(term, 'echo one\necho two')
      const writeText = vi.fn(() => Promise.resolve())
      stubWrite(writeText)
      render(<TerminalKeyBar term={term} />)

      await userEvent.click(screen.getByRole('button', { name: 'Copy' }))

      expect(writeText).toHaveBeenCalledTimes(1)
      expect(writeText).toHaveBeenCalledWith('echo one\necho two')
    })

    /**
     * A successful copy must clear the selection — the Select key's
     * restart-from-bottom design assumes it, and without it a repeat
     * Select→Copy cycle re-grabs the stale prior selection. The clear happens
     * on the success path, after the staleness gate.
     */
    it('clears the selection after a successful copy', async () => {
      const { term } = makeTerm()
      withSelection(term, 'text')
      const clearSelection = vi.fn()
      ;(term as unknown as { clearSelection: () => void }).clearSelection = clearSelection
      stubWrite(() => Promise.resolve())
      render(<TerminalKeyBar term={term} />)

      await userEvent.click(screen.getByRole('button', { name: 'Copy' }))

      expect(clearSelection).toHaveBeenCalledTimes(1)
    })

    /**
     * A successful copy also clears the Select stage announcement: the
     * selection it described is gone, so leaving "· tap for all" live beside
     * the Copy status would falsely imply the next Select tap continues the
     * cycle (it actually restarts at stage 1). Stage a selection with its
     * announcement, copy, and assert the select announcement is gone while the
     * "Copied" beat is present.
     */
    it('clears the select stage announcement after a successful copy', async () => {
      // Cursor on a virtual prompt row below the buffer, so stage 1 selects the
      // bottommost output line (row 1) above it — the live-shell shape.
      const { term } = makeSelectableTerm(['top', 'bottom'], { baseY: 0, cursorY: 2 })
      stubWrite(() => Promise.resolve())
      render(<TerminalKeyBar term={term} />)

      // Stage a selection AND its live announcement via a Select tap.
      await userEvent.click(screen.getByRole('button', { name: 'Select' }))
      expect(screen.getByRole('button', { name: 'Line · tap for all' })).toBeTruthy()

      await userEvent.click(screen.getByRole('button', { name: 'Copy' }))

      // The stale select announcement is gone; the Copy success beat is shown.
      expect(screen.queryByRole('button', { name: 'Line · tap for all' })).toBeNull()
      expect(screen.getByRole('button', { name: 'Copied' })).toBeTruthy()
    })

    /**
     * A failed/rejected copy must NOT clear the selection: the text never
     * reached the clipboard, so the user keeps their selection to retry.
     */
    it('does not clear the selection when the copy fails', async () => {
      const { term } = makeTerm()
      withSelection(term, 'text')
      const clearSelection = vi.fn()
      ;(term as unknown as { clearSelection: () => void }).clearSelection = clearSelection
      stubWrite(() => Promise.reject(new DOMException('denied', 'NotAllowedError')))
      render(<TerminalKeyBar term={term} />)

      await userEvent.click(screen.getByRole('button', { name: 'Copy' }))
      // Let the rejection propagate.
      await screen.findByRole('button', { name: 'Allow clipboard access' })

      expect(clearSelection).not.toHaveBeenCalled()
    })

    /**
     * On the common first-run permission deny, the failure remedy ("Allow
     * clipboard access") and the still-standing Select stage label ("Line · tap
     * for all") would share the 390px row and both truncate, muting the
     * decision-critical remedy. The failure path must collapse the VISIBLE
     * select label back to idle while KEEPING the stage machinery intact — so
     * the next Select tap still advances to stage 2 rather than restarting.
     */
    it('resets the select label AND stage together on copy failure', async () => {
      const { term, selectLines, selectAll } = makeSelectableTerm(['top', 'bottom'], { baseY: 0, cursorY: 2 })
      stubWrite(() => Promise.reject(new DOMException('denied', 'NotAllowedError')))
      render(<TerminalKeyBar term={term} />)

      // Stage 1 via Select puts the "Line · tap for all" label up.
      await userEvent.click(screen.getByRole('button', { name: 'Select' }))
      expect(screen.getByRole('button', { name: 'Line · tap for all' })).toBeTruthy()

      // Copy fails: the remedy shows and the select label collapses.
      await userEvent.click(screen.getByRole('button', { name: 'Copy' }))
      await screen.findByRole('button', { name: 'Allow clipboard access' })
      expect(screen.queryByRole('button', { name: 'Line · tap for all' })).toBeNull()

      // The stage ref reset WITH the label: a button reading "Select" must
      // never selectAll (the accidental buffer-wide grab). The next tap
      // honestly restarts at stage 1, re-selecting the bottommost line.
      await userEvent.click(screen.getByRole('button', { name: 'Select' }))
      expect(selectAll).not.toHaveBeenCalled()
      expect(selectLines).toHaveBeenLastCalledWith(1, 1)
      expect(selectLines).toHaveBeenCalledTimes(2)
    })

    /**
     * The three keys share ONE truncating toolbar row, so all transients live
     * in a single bar-level slot (First Principles review counted the pairs
     * the earlier pairwise clearing missed: paste↔select and paste↔copy).
     * These pin the structural mutual exclusion for those two pairs.
     */
    it('clears a live paste error when Select is tapped (paste↔select)', async () => {
      const { term } = makeSelectableTerm(['out', '$ '], { baseY: 0, cursorY: 1 })
      // No clipboard at all → Paste shows its needs-HTTPS remedy.
      Object.defineProperty(navigator, 'clipboard', { value: undefined, configurable: true })
      render(<TerminalKeyBar term={term} />)

      await userEvent.click(screen.getByRole('button', { name: 'Paste' }))
      expect(await screen.findByRole('button', { name: 'Paste needs a secure (HTTPS) connection' })).toBeTruthy()

      await userEvent.click(screen.getByRole('button', { name: 'Select' }))

      // The paste transient yielded the row to the Select stage label.
      expect(screen.queryByRole('button', { name: 'Paste needs a secure (HTTPS) connection' })).toBeNull()
      expect(screen.getByRole('button', { name: 'Line · tap for all' })).toBeTruthy()
    })

    it('replaces a live paste error when Copy shows its own transient (paste↔copy)', async () => {
      const { term } = makeTerm()
      Object.defineProperty(navigator, 'clipboard', { value: undefined, configurable: true })
      render(<TerminalKeyBar term={term} />)

      await userEvent.click(screen.getByRole('button', { name: 'Paste' }))
      expect(await screen.findByRole('button', { name: 'Paste needs a secure (HTTPS) connection' })).toBeTruthy()

      // No selection → Copy shows its guidance transient; the single slot
      // means it REPLACES the paste error instead of standing beside it.
      await userEvent.click(screen.getByRole('button', { name: 'Copy' }))
      expect(await screen.findByRole('button', { name: 'Long-press a line or tap Select, then Copy' })).toBeTruthy()
      expect(screen.queryByRole('button', { name: 'Paste needs a secure (HTTPS) connection' })).toBeNull()
    })

    /**
     * Integration of Copy and Select: after a successful copy clears the
     * selection, the next Select tap restarts from the bottommost line rather
     * than extending the prior (now-copied) selection. Uses the selectable
     * harness so clearSelection actually empties the modelled selection.
     */
    it('lets the next Select tap restart from the bottom after a successful copy', async () => {
      const { term, selectLines, selectAll } = makeSelectableTerm(['top', 'bottom'], { baseY: 0, cursorY: 2 })
      const writeText = vi.fn(() => Promise.resolve())
      stubWrite(writeText)
      render(<TerminalKeyBar term={term} />)
      const select = screen.getByRole('button', { name: 'Select' })

      await userEvent.click(select) // stage 1: bottommost = row 1
      expect(selectLines).toHaveBeenLastCalledWith(1, 1)
      await userEvent.click(select) // stage 2: selectAll → whole buffer
      expect(selectAll).toHaveBeenCalledTimes(1)

      await userEvent.click(screen.getByRole('button', { name: 'Copy' }))
      expect(writeText).toHaveBeenCalledWith('top\nbottom')

      await userEvent.click(select) // selection cleared by copy → restart from bottom
      expect(selectLines).toHaveBeenLastCalledWith(1, 1)
    })

    /**
     * A writeText can resolve LATE (held open by an iOS permission callout).
     * If the user builds a NEW selection via Select while the old copy is in
     * flight, the completion must NOT clear that newer selection or its stage —
     * the clipboard holds the OLD text, so erasing the new selection would
     * silently destroy work. Only a selection identical to the copied one is
     * cleared.
     */
    it('does not clear a newer selection when a delayed copy completes', async () => {
      const { term, selectLines, selectAll } = makeSelectableTerm(['top', 'bottom'], { baseY: 0, cursorY: 2 })
      let resolveWrite!: () => void
      const writeText = vi.fn(() => new Promise<void>((res) => { resolveWrite = res }))
      stubWrite(writeText)
      render(<TerminalKeyBar term={term} />)
      const select = screen.getByRole('button', { name: 'Select' })

      await userEvent.click(select) // stage 1: row 1 selected ("top\nbottom"[1])
      await userEvent.click(screen.getByRole('button', { name: 'Copy' })) // write starts, held open
      expect(writeText).toHaveBeenCalledWith('bottom')

      // While the write is pending the user advances the selection: stage 2
      // selects the whole buffer — a NEW selection the clipboard does not hold.
      await userEvent.click(screen.getByRole('button', { name: /tap for all/ }))
      expect(selectAll).toHaveBeenCalledTimes(1)
      expect(term.getSelection()).toBe('top\nbottom')

      resolveWrite()
      await act(async () => { await Promise.resolve() })

      // The delayed completion left the newer selection and its stage intact.
      expect(term.getSelection()).toBe('top\nbottom')
      expect(screen.getByRole('button', { name: 'All · tap for line' })).toBeTruthy()
      // And the next Select tap wraps around (stage was preserved, not reset).
      await userEvent.click(screen.getByRole('button', { name: 'All · tap for line' }))
      expect(selectLines).toHaveBeenLastCalledWith(1, 1)
    })

    /**
     * A bare clipboard write leaves nothing on screen, so on touch the user
     * cannot tell the copy took. A successful copy must surface a transient
     * "Copied" beat — visible on the key AND announced from the sibling status
     * region — then auto-revert to the idle "Copy" label.
     */
    it('shows a transient "Copied" success state after a successful copy', async () => {
      vi.useFakeTimers()
      try {
        const { term } = makeTerm()
        withSelection(term, 'text')
        stubWrite(() => Promise.resolve())
        render(<TerminalKeyBar term={term} />)

        await act(async () => {
          fireEvent.click(screen.getByRole('button', { name: 'Copy' }))
          await Promise.resolve() // let the writeText().then run
        })
        expect(screen.getByRole('button', { name: 'Copied' })).toBeTruthy()

        act(() => { vi.advanceTimersByTime(4100) })
        expect(screen.getByRole('button', { name: 'Copy' })).toBeTruthy()
        expect(screen.queryByRole('button', { name: 'Copied' })).toBeNull()
      } finally {
        vi.useRealTimers()
      }
    })

    /** The success beat is announced from the status region OUTSIDE the button
     *  — role=button prunes a nested live region, same as the failure path. */
    it('announces the copy success from a region outside the button', async () => {
      const { term } = makeTerm()
      withSelection(term, 'text')
      stubWrite(() => Promise.resolve())
      render(<TerminalKeyBar term={term} />)

      await userEvent.click(screen.getByRole('button', { name: 'Copy' }))

      const statuses = await screen.findAllByRole('status')
      const done = statuses.find(s => s.textContent === 'Copied')
      expect(done).toBeTruthy()
      expect(done!.closest('button')).toBeNull()
    })

    /**
     * A tap with nothing selected must NOT copy the whole scrollback — an
     * accidental buffer-wide copy on touch is worse than a no-op. The key says
     * "select text first" and never touches the clipboard.
     */
    it('never copies the whole buffer when nothing is selected', async () => {
      const { term } = makeTerm()
      withSelection(term, '')
      const writeText = vi.fn(() => Promise.resolve())
      stubWrite(writeText)
      render(<TerminalKeyBar term={term} />)

      await userEvent.click(screen.getByRole('button', { name: 'Copy' }))

      expect(writeText).not.toHaveBeenCalled()
      expect(await screen.findByRole('button', { name: 'Long-press a line or tap Select, then Copy' })).toBeTruthy()
    })

    it('surfaces a denied clipboard write with the remedy, not a generic failure', async () => {
      const { term } = makeTerm()
      withSelection(term, 'secret')
      stubWrite(() => Promise.reject(new DOMException('denied', 'NotAllowedError')))
      render(<TerminalKeyBar term={term} />)

      await userEvent.click(screen.getByRole('button', { name: 'Copy' }))

      const failed = await screen.findByRole('button', { name: 'Allow clipboard access' })
      expect(failed.textContent).toContain('Allow clipboard access')
    })

    it('surfaces a non-permission rejection as a plain failure', async () => {
      const { term } = makeTerm()
      withSelection(term, 'text')
      stubWrite(() => Promise.reject(new Error('boom')))
      render(<TerminalKeyBar term={term} />)

      await userEvent.click(screen.getByRole('button', { name: 'Copy' }))

      expect(await screen.findByRole('button', { name: 'Copy failed' })).toBeTruthy()
    })

    /**
     * A missing clipboard API is a PERMANENT failure for the page (plain-HTTP
     * LAN access — the self-hosted-dashboard-from-a-phone case). It must name
     * the HTTPS remedy, and it must decide that BEFORE reporting no-selection
     * would be wrong: here there IS a selection, so the HTTPS branch is what
     * should show.
     */
    it('names the HTTPS remedy when the clipboard API is missing (non-secure context)', async () => {
      const { term } = makeTerm()
      withSelection(term, 'text')
      stubWrite(undefined)
      render(<TerminalKeyBar term={term} />)

      await userEvent.click(screen.getByRole('button', { name: 'Copy' }))

      expect(await screen.findByRole('button', { name: 'Copy needs a secure (HTTPS) connection' })).toBeTruthy()
    })

    it('cancels pointerdown so copying never dismisses the on-screen keyboard', () => {
      const { term } = makeTerm()
      withSelection(term, 'text')
      stubWrite(() => Promise.resolve())
      render(<TerminalKeyBar term={term} />)

      const ev = new PointerEvent('pointerdown', { bubbles: true, cancelable: true })
      screen.getByRole('button', { name: 'Copy' }).dispatchEvent(ev)

      expect(ev.defaultPrevented).toBe(true)
    })

    /**
     * The clipboard write is async (iOS can hold it open on a permission
     * callout). A write that resolves after the pane went hidden must not flip
     * the key's status back to idle behind the user's back. offsetParent goes
     * null when display:none / detached — the same gate Paste uses.
     */
    it('ignores a write that resolves after the hosting tab went hidden', async () => {
      const { term, element } = makeTerm()
      withSelection(term, 'text')
      let resolveWrite!: () => void
      stubWrite(() => new Promise<void>(res => { resolveWrite = res }))
      render(<TerminalKeyBar term={term} />)

      await userEvent.click(screen.getByRole('button', { name: 'Copy' }))
      element.offsetParent = null // tab switch: display:none on the pane wrapper
      resolveWrite()
      await Promise.resolve()

      // No throw, no status flip — the late resolve is dropped.
      expect(screen.getByRole('button', { name: 'Copy' })).toBeTruthy()
    })

    it('does not throw when the terminal has no element on a late resolve', async () => {
      const { term } = makeTerm()
      withSelection(term, 'text')
      let resolveWrite!: () => void
      stubWrite(() => new Promise<void>(res => { resolveWrite = res }))
      render(<TerminalKeyBar term={term} />)

      await userEvent.click(screen.getByRole('button', { name: 'Copy' }))
      delete (term as unknown as { element?: unknown }).element
      resolveWrite()
      await Promise.resolve()

      expect(screen.getByRole('button', { name: 'Copy' })).toBeTruthy()
    })

    /**
     * role=button is children-presentational: the live region must live
     * OUTSIDE the button to announce, same as Paste.
     */
    it('announces the status from a region outside the button', async () => {
      const { term } = makeTerm()
      withSelection(term, '')
      stubWrite(() => Promise.resolve())
      render(<TerminalKeyBar term={term} />)

      await userEvent.click(screen.getByRole('button', { name: 'Copy' }))

      const statuses = await screen.findAllByRole('status')
      const copyStatus = statuses.find(s => s.textContent === 'Long-press a line or tap Select, then Copy')
      expect(copyStatus).toBeTruthy()
      expect(copyStatus!.closest('button')).toBeNull()
    })

    it('shows a visible text label next to the icon in the idle state', () => {
      const { term } = makeTerm()
      withSelection(term, '')
      stubWrite(() => Promise.resolve())
      render(<TerminalKeyBar term={term} />)

      expect(screen.getByRole('button', { name: 'Copy' }).textContent).toBe('Copy')
    })

    it('ignores taps while a clipboard write is already in flight', async () => {
      const { term } = makeTerm()
      withSelection(term, 'text')
      const writeText = vi.fn(() => new Promise<void>(() => {})) // never resolves
      stubWrite(writeText)
      render(<TerminalKeyBar term={term} />)

      const btn = screen.getByRole('button', { name: 'Copy' })
      await userEvent.click(btn)
      await userEvent.click(btn)

      expect(writeText).toHaveBeenCalledTimes(1)
    })

    it('reverts the status after a beat so the key stays usable', async () => {
      vi.useFakeTimers()
      try {
        const { term } = makeTerm()
        withSelection(term, '')
        stubWrite(() => Promise.resolve())
        render(<TerminalKeyBar term={term} />)

        fireEvent.click(screen.getByRole('button', { name: 'Copy' }))
        expect(screen.getByRole('button', { name: 'Long-press a line or tap Select, then Copy' })).toBeTruthy()

        act(() => { vi.advanceTimersByTime(4100) })
        expect(screen.getByRole('button', { name: 'Copy' })).toBeTruthy()
        expect(screen.queryByRole('button', { name: 'Long-press a line or tap Select, then Copy' })).toBeNull()
      } finally {
        vi.useRealTimers()
      }
    })
  })

  /**
   * Select soft key — the SOURCE half of the touch copy story. xterm's
   * drag-selection is mouse-only and never fires on touch, so before this key
   * there was no way to CREATE the selection Copy reads. Select builds one out
   * of the buffer itself and runs a STAGED CYCLE, announced at every stage:
   * tap 1 = the last line, tap 2 = the whole buffer, tap 3 = wrap back to the
   * last line. After the selection clears the next tap restarts at stage 1.
   */
  describe('select key', () => {
    it('renders a Select key before Copy in the clipboard region', () => {
      const { term } = makeSelectableTerm(['$ echo hi', 'hi', ''])
      render(<TerminalKeyBar term={term} />)
      const bar = screen.getByTestId('terminal-key-bar')
      const select = screen.getByRole('button', { name: 'Select' })
      const copy = screen.getByRole('button', { name: 'Copy' })
      expect(bar.contains(select)).toBe(true)
      // Select precedes Copy in DOM order.
      expect(select.compareDocumentPosition(copy) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    })

    /** Stage 1: the first tap selects the bottommost non-empty buffer line and
     *  announces the front-loaded stage label. */
    it('selects the bottommost non-empty buffer line on the first tap and announces it', async () => {
      const { term, selectLines } = makeSelectableTerm(['$ echo hi', 'hi', ''])
      render(<TerminalKeyBar term={term} />)

      await userEvent.click(screen.getByRole('button', { name: 'Select' }))

      // Row 2 is the empty tail row; the bottommost NON-empty line is row 1.
      expect(selectLines).toHaveBeenCalledTimes(1)
      expect(selectLines).toHaveBeenCalledWith(1, 1)
      // The SHORT stage label on the button makes the staged behaviour
      // discoverable on screen (the full teaching sentence is in the sr-only
      // live region, asserted separately below).
      expect(screen.getByRole('button', { name: 'Line · tap for all' })).toBeTruthy()
    })

    /**
     * A live shell reprints a fresh prompt (`$ `) after every command and the
     * cursor rests ON that prompt row. Stage 1 must select the OUTPUT line above
     * the prompt, not the prompt itself — the PR's headline copy-an-error case.
     * Buffer: [output, `$ ` prompt], cursor on the prompt row (row 1); the tap
     * must select the output row (0), skipping the prompt row at the cursor.
     */
    it('skips the live prompt row at the cursor and selects the output line above it', async () => {
      const { term, selectLines } = makeSelectableTerm(
        ['bash: command not found', '$ '],
        { baseY: 0, cursorY: 1 }, // cursor on the reprinted prompt row
      )
      render(<TerminalKeyBar term={term} />)

      await userEvent.click(screen.getByRole('button', { name: 'Select' }))

      // Row 1 is the live prompt at the cursor; the selected line is the output
      // row 0 STRICTLY ABOVE it, not the prompt.
      expect(selectLines).toHaveBeenCalledTimes(1)
      expect(selectLines).toHaveBeenCalledWith(0, 0)
      expect(screen.getByRole('button', { name: 'Line · tap for all' })).toBeTruthy()
    })

    /**
     * Fallback: when NO non-empty line exists above the cursor (a bare prompt
     * with no output yet), stage 1 selects the cursor row itself rather than
     * no-opping on a non-empty buffer.
     */
    it('falls back to the cursor row when nothing non-empty sits above it', async () => {
      const { term, selectLines } = makeSelectableTerm(
        ['$ '], // only the prompt line, cursor on it, nothing above
        { baseY: 0, cursorY: 0 },
      )
      render(<TerminalKeyBar term={term} />)

      await userEvent.click(screen.getByRole('button', { name: 'Select' }))

      expect(selectLines).toHaveBeenCalledTimes(1)
      expect(selectLines).toHaveBeenCalledWith(0, 0)
      expect(screen.getByRole('button', { name: 'Line · tap for all' })).toBeTruthy()
    })

    /**
     * The empty-line-skipping behaviour still holds ABOVE the cursor: blank rows
     * between the output and the prompt (and the prompt row at the cursor) are
     * all skipped, landing on the last non-empty output line.
     */
    it('skips empty lines above the cursor and lands on the last output line', async () => {
      const { term, selectLines } = makeSelectableTerm(
        ['error: boom', '', '$ '], // output, a blank row, then the prompt at cursor
        { baseY: 0, cursorY: 2 },
      )
      render(<TerminalKeyBar term={term} />)

      await userEvent.click(screen.getByRole('button', { name: 'Select' }))

      // Row 2 (prompt) is at the cursor and row 1 is blank; the tap skips both
      // and selects the output row 0.
      expect(selectLines).toHaveBeenCalledTimes(1)
      expect(selectLines).toHaveBeenCalledWith(0, 0)
    })

    /**
     * Scrollback anchoring: a user scrolled UP into scrollback is looking at
     * earlier output — stage 1 must select what they can SEE, not the line
     * next to the off-screen prompt at the buffer bottom (which would make
     * Copy confirm "Copied" for text they never saw).
     */
    it('anchors stage 1 at the visible viewport bottom when scrolled into scrollback', async () => {
      // 10 rows; live bottom has the prompt at row 9. The user scrolled up so
      // the viewport shows rows 2..5 (viewportY=2, rows=4).
      const { term, selectLines } = makeSelectableTerm(
        ['r0', 'r1', 'r2', 'r3', 'r4', 'r5', 'r6', 'r7', 'r8', '$ '],
        { baseY: 6, cursorY: 3 }, // absolute cursor row 9 (prompt)
        { viewportY: 2, rows: 4 }, // visible rows 2..5
      )
      render(<TerminalKeyBar term={term} />)

      await userEvent.click(screen.getByRole('button', { name: 'Select' }))

      // Anchor = viewport bottom (row 5) — content the user is LOOKING at, so
      // it is included (not skipped like the live prompt): row 5 is selected.
      expect(selectLines).toHaveBeenLastCalledWith(5, 5)
    })

    it('keeps the cursor anchor at the live bottom (viewportY === baseY)', async () => {
      const { term, selectLines } = makeSelectableTerm(
        ['out', '$ '],
        { baseY: 0, cursorY: 1 },
        { viewportY: 0, rows: 2 }, // not scrolled: viewportY === baseY
      )
      render(<TerminalKeyBar term={term} />)

      await userEvent.click(screen.getByRole('button', { name: 'Select' }))

      // Live-bottom behaviour unchanged: skip the prompt, select the output.
      expect(selectLines).toHaveBeenLastCalledWith(0, 0)
    })

    /**
     * A single logical line can wrap across several physical rows at phone
     * widths (~40 cols) — the headline error-message / URL cases. xterm flags
     * every continuation row with isWrapped===true. Stage 1 must select the
     * WHOLE logical line, not just the found row (which would copy only its
     * last fragment). Buffer: a 3-row wrapped logical line (rows 0–2), then a
     * `$ ` prompt at the cursor (row 3). The found row is the bottom fragment
     * (row 2); expansion must reach up to row 0 and select the full span.
     */
    it('selects the entire wrapped logical line on the first tap', async () => {
      const { term, selectLines } = makeSelectableTerm(
        [
          'bash: a very long error message that ',
          { text: 'wraps across three physical rows at ', isWrapped: true },
          { text: 'this narrow phone width', isWrapped: true },
          '$ ',
        ],
        { baseY: 0, cursorY: 3 }, // cursor on the reprinted prompt row
      )
      render(<TerminalKeyBar term={term} />)

      await userEvent.click(screen.getByRole('button', { name: 'Select' }))

      // The bottom fragment (row 2) is found first (strictly above the prompt);
      // expansion walks UP through the two isWrapped continuation rows to the
      // logical line's start (row 0), selecting the whole 3-row span.
      expect(selectLines).toHaveBeenCalledTimes(1)
      expect(selectLines).toHaveBeenCalledWith(0, 2)
      expect(screen.getByRole('button', { name: 'Line · tap for all' })).toBeTruthy()
    })

    /**
     * Expansion also walks DOWN: when the found row is the START of a wrapped
     * logical line whose continuation rows sit BELOW it, those continuations
     * must be included too. Buffer: a `$ ` prompt (row 0), then a wrapped
     * 2-row logical line (rows 1–2, row 2 flagged isWrapped), cursor below the
     * buffer so the found row is the logical line's first row (row 1).
     */
    it('includes wrapped continuation rows below the found row', async () => {
      const { term, selectLines } = makeSelectableTerm(
        [
          '$ echo hi',
          'the output line that is long enough to ',
          { text: 'wrap onto a second physical row', isWrapped: true },
        ],
        { baseY: 0, cursorY: 3 }, // cursor below the buffer → search from the bottom
      )
      render(<TerminalKeyBar term={term} />)

      await userEvent.click(screen.getByRole('button', { name: 'Select' }))

      // Bottommost non-empty row is the continuation (row 2); expansion walks UP
      // to the logical start (row 1). Equivalently, starting from row 1 it walks
      // DOWN to include row 2 — either way the full 1–2 span is selected.
      expect(selectLines).toHaveBeenCalledTimes(1)
      expect(selectLines).toHaveBeenCalledWith(1, 2)
    })

    /**
     * The prompt-skip must skip the cursor's whole LOGICAL line, not just its
     * last physical row (GPT review). A long command wraps onto the cursor
     * row: rows 1–2 are the active command (`$ …` + continuation), cursor on
     * row 2. Starting the search at `cursorRow - 1` would find the command's
     * own fragment (row 1) and expand back down through the cursor row —
     * Copy would write the ACTIVE COMMAND instead of the prior output. The
     * search must start above the logical line's top and select row 0.
     */
    it('skips the whole wrapped active command, selecting prior output', async () => {
      const { term, selectLines } = makeSelectableTerm(
        [
          'previous output line',
          '$ a very long command still being typed ',
          { text: 'that wraps onto a second physical row', isWrapped: true },
        ],
        { baseY: 0, cursorY: 2 }, // cursor on the wrapped command's continuation row
      )
      render(<TerminalKeyBar term={term} />)

      await userEvent.click(screen.getByRole('button', { name: 'Select' }))

      // Rows 1–2 (the cursor's logical line) are skipped entirely; the prior
      // output at row 0 is selected.
      expect(selectLines).toHaveBeenCalledTimes(1)
      expect(selectLines).toHaveBeenCalledWith(0, 0)
    })

    /**
     * The cursor-row fallback (no non-empty line strictly above the cursor) also
     * expands to the full logical line. Buffer: a wrapped 2-row logical line at
     * rows 0–1, cursor ON row 1 (the continuation), nothing non-empty above the
     * cursor row itself — the fallback selects the cursor row and expands up.
     */
    it('expands the logical line on the cursor-row fallback', async () => {
      const { term, selectLines } = makeSelectableTerm(
        [
          'a long single logical line that wraps ',
          { text: 'onto this continuation row', isWrapped: true },
        ],
        { baseY: 0, cursorY: 1 }, // cursor on the continuation row; nothing strictly above is reachable as a distinct line
      )
      render(<TerminalKeyBar term={term} />)

      await userEvent.click(screen.getByRole('button', { name: 'Select' }))

      // Rows 0–1 are one logical line and the cursor sits on it, so the
      // strictly-above search starts past the top of the buffer and finds
      // nothing — the cursor-row fallback selects the anchor row and expands
      // UP through the isWrapped boundary to row 0 — the whole logical line.
      expect(selectLines).toHaveBeenCalledTimes(1)
      expect(selectLines).toHaveBeenCalledWith(0, 1)
    })

    /**
     * Stage 2: the second tap selects the ENTIRE buffer (term.selectAll) and
     * announces the "all selected" stage label. The intermediate output-block
     * stage was removed (its empty-line-gap boundary was fragile and served no
     * distinct user need), so the cycle is a clean two stages.
     */
    it('selects the entire buffer on the second tap and announces it', async () => {
      const { term, selectLines, selectAll } = makeSelectableTerm(['a', 'b', 'c'], { baseY: 0, cursorY: 3 })
      render(<TerminalKeyBar term={term} />)
      const btn = screen.getByRole('button', { name: 'Select' })

      await userEvent.click(btn) // stage 1: bottommost non-empty = row 2
      expect(selectLines).toHaveBeenLastCalledWith(2, 2)
      await userEvent.click(btn) // stage 2: selectAll
      expect(selectAll).toHaveBeenCalledTimes(1)
      expect(screen.getByRole('button', { name: 'All · tap for line' })).toBeTruthy()
      // Stage 2 uses selectAll, not a second selectLines call.
      expect(selectLines).toHaveBeenCalledTimes(1)
    })

    /**
     * Stage 3: a third tap WRAPS AROUND to stage 1 (a fresh bottommost line) —
     * this is the overshoot recovery, and it re-announces the last-line label.
     */
    it('wraps around to the last line on the third tap (overshoot recovery)', async () => {
      const { term, selectLines, selectAll } = makeSelectableTerm(['a', 'b', 'c'], { baseY: 0, cursorY: 3 })
      render(<TerminalKeyBar term={term} />)
      const btn = screen.getByRole('button', { name: 'Select' })

      await userEvent.click(btn) // stage 1: row 2
      await userEvent.click(btn) // stage 2: selectAll
      await userEvent.click(btn) // stage 3: wrap → bottommost non-empty = row 2
      expect(selectAll).toHaveBeenCalledTimes(1)
      expect(selectLines).toHaveBeenLastCalledWith(2, 2)
      expect(selectLines).toHaveBeenCalledTimes(2) // stage 1 + wraparound
      expect(screen.getByRole('button', { name: 'Line · tap for all' })).toBeTruthy()
    })

    it('restarts at stage 1 after the selection is cleared', async () => {
      const { term, selectLines, selectAll, clearSelection } = makeSelectableTerm(['top', 'bottom'], { baseY: 0, cursorY: 2 })
      render(<TerminalKeyBar term={term} />)
      const btn = screen.getByRole('button', { name: 'Select' })

      await userEvent.click(btn) // stage 1: bottommost = row 1
      expect(selectLines).toHaveBeenLastCalledWith(1, 1)
      await userEvent.click(btn) // stage 2: selectAll
      expect(selectAll).toHaveBeenCalledTimes(1)

      clearSelection() // e.g. a successful Copy cleared the selection
      await userEvent.click(btn) // next tap restarts at stage 1, from the bottom
      expect(selectLines).toHaveBeenLastCalledWith(1, 1)
    })

    /**
     * A Select tap owns the shared status region: it must clear a live copy
     * status so the "Tap Select, then Copy" guidance does not stay up beside
     * the expanding Select announcement (both truncate at 390px otherwise).
     * Raise the no-selection guidance via a Copy tap, then tap Select and
     * assert the guidance is gone while the stage announcement is present.
     */
    it('clears a live copy status when Select is tapped', async () => {
      const origClipboard = Object.getOwnPropertyDescriptor(navigator, 'clipboard')
      // No selection → Copy shows the "Tap Select, then Copy" guidance.
      Object.defineProperty(navigator, 'clipboard', {
        configurable: true,
        value: { writeText: () => Promise.resolve() },
      })
      try {
        const { term } = makeSelectableTerm(['top', 'bottom'], { baseY: 0, cursorY: 2 })
        render(<TerminalKeyBar term={term} />)

        await userEvent.click(screen.getByRole('button', { name: 'Copy' }))
        expect(screen.getByRole('button', { name: 'Long-press a line or tap Select, then Copy' })).toBeTruthy()

        await userEvent.click(screen.getByRole('button', { name: 'Select' }))

        // The copy guidance is cleared; the Select stage announcement is up.
        expect(screen.queryByRole('button', { name: 'Long-press a line or tap Select, then Copy' })).toBeNull()
        expect(screen.getByRole('button', { name: 'Line · tap for all' })).toBeTruthy()
      } finally {
        if (origClipboard) Object.defineProperty(navigator, 'clipboard', origClipboard)
        else delete (navigator as unknown as Record<string, unknown>).clipboard
      }
    })

    /**
     * The stage label is PERSISTENT while the selection is live — it does NOT
     * self-revert on a timer. An earlier revision reverted it after 4000ms
     * while the cycle stage (selectStageRef) persisted, so a user returning
     * after >4s saw a button reading "Select", tapped it expecting a fresh
     * stage-1 line, and got selectAll() instead (the accidental buffer-wide
     * grab the copy_no_selection rationale warns against). The label must keep
     * describing the stage for as long as the buffer holds that selection.
     */
    it('keeps the stage label visible past 4000ms while the selection is live', async () => {
      vi.useFakeTimers()
      try {
        const { term } = makeSelectableTerm(['a', 'b'])
        render(<TerminalKeyBar term={term} />)

        fireEvent.click(screen.getByRole('button', { name: 'Select' }))
        expect(screen.getByRole('button', { name: 'Line · tap for all' })).toBeTruthy()

        // Well past the old 4000ms revert — the label must still be showing,
        // because the selection is still live.
        act(() => { vi.advanceTimersByTime(8000) })
        expect(screen.getByRole('button', { name: 'Line · tap for all' })).toBeTruthy()
        expect(screen.queryByRole('button', { name: 'Select' })).toBeNull()
      } finally {
        vi.useRealTimers()
      }
    })

    /**
     * When the user clears the selection through the terminal itself (ESC, a
     * viewport tap, a reflow) xterm fires onSelectionChange with an empty
     * selection. The stage label AND the cycle stage must reset together so the
     * button stops claiming a stage the buffer no longer has and the next tap
     * restarts at stage 1.
     */
    it('resets the label and stage when the selection clears via onSelectionChange', async () => {
      const { term, selectLines, emitSelectionChange } = makeSelectableTerm(['top', 'bottom'], { baseY: 0, cursorY: 2 })
      render(<TerminalKeyBar term={term} />)
      const btn = screen.getByRole('button', { name: 'Select' })

      await userEvent.click(btn) // stage 1
      expect(screen.getByRole('button', { name: 'Line · tap for all' })).toBeTruthy()

      // The terminal reports the selection was cleared out from under us.
      act(() => { term.clearSelection?.(); emitSelectionChange() })

      // Label reverted to idle, and the next tap restarts at stage 1 (row 1),
      // not stage 2 — the stage reset alongside the label.
      expect(screen.getByRole('button', { name: 'Select' })).toBeTruthy()
      expect(screen.queryByRole('button', { name: 'Line · tap for all' })).toBeNull()
      await userEvent.click(btn)
      expect(selectLines).toHaveBeenLastCalledWith(1, 1)
    })

    it('selects nothing when every buffer line is empty', async () => {
      const { term, selectLines, selectAll } = makeSelectableTerm(['', '', ''])
      render(<TerminalKeyBar term={term} />)

      await userEvent.click(screen.getByRole('button', { name: 'Select' }))

      expect(selectLines).not.toHaveBeenCalled()
      expect(selectAll).not.toHaveBeenCalled()
    })

    /** The stage's FULL teaching sentence lives in a status region OUTSIDE the
     *  button (the visible button label is the SHORT form), the same live-region
     *  machinery Copy's "Copied" beat uses — that is what makes the multi-stage
     *  behaviour discoverable to a screen reader. */
    it('announces the stage label from a status region outside the button', async () => {
      const { term } = makeSelectableTerm(['a', 'b'])
      render(<TerminalKeyBar term={term} />)

      await userEvent.click(screen.getByRole('button', { name: 'Select' }))

      // The sr-only region reads the same compact label the button shows (the
      // separate full-sentence variant was merged away — one string, 13 locales).
      const status = (await screen.findAllByRole('status')).find(
        (el) => el.textContent === 'Line · tap for all',
      )
      expect(status).toBeTruthy()
      expect(status!.closest('button')).toBeNull()
    })

    it('shows a visible text label next to the icon in the idle state', () => {
      const { term } = makeSelectableTerm(['x'])
      render(<TerminalKeyBar term={term} />)
      expect(screen.getByRole('button', { name: 'Select' }).textContent).toBe('Select')
    })

    it('cancels pointerdown so selecting never dismisses the on-screen keyboard', () => {
      const { term } = makeSelectableTerm(['x'])
      render(<TerminalKeyBar term={term} />)

      const ev = new PointerEvent('pointerdown', { bubbles: true, cancelable: true })
      screen.getByRole('button', { name: 'Select' }).dispatchEvent(ev)

      expect(ev.defaultPrevented).toBe(true)
    })
  })
})
