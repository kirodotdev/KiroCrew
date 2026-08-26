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

      const status = await screen.findByRole('status')
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
})
