import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, fireEvent, act, screen } from '@testing-library/react'
import MarkdownRenderer, { COPY_FAILED_FLASH_MS } from '../components/MarkdownRenderer'
import { OPEN_DELAY_MS } from '../components/InstantTip'
import { copyToClipboard } from '../utils/clipboard'

// Resolves TRUE: the chip gates its confirmation on the helper's boolean, so a
// mock that resolved `undefined` would look like a refused clipboard write.
vi.mock('../utils/clipboard', () => ({ copyToClipboard: vi.fn(async () => true) }))

beforeEach(() => { vi.mocked(copyToClipboard).mockClear(); vi.mocked(copyToClipboard).mockResolvedValue(true) })
afterEach(() => { vi.useRealTimers() })

describe('InlineCode click-to-copy (non-path chips)', () => {
  it('copies the inline code text on click', async () => {
    render(<MarkdownRenderer content={'Run `npm test` please.'} />)
    const code = await screen.findByText('npm test')
    expect(code.tagName).toBe('CODE')
    expect(code).toHaveAttribute('role', 'button')

    fireEvent.click(code)
    expect(copyToClipboard).toHaveBeenCalledWith('npm test')
  })

  it('copies on Enter keydown', async () => {
    render(<MarkdownRenderer content={'Try `curl -s https://example.com`.'} />)
    const code = await screen.findByText('curl -s https://example.com')
    fireEvent.keyDown(code, { key: 'Enter' })
    expect(copyToClipboard).toHaveBeenCalledWith('curl -s https://example.com')
  })

  it('copies on Space keydown', async () => {
    render(<MarkdownRenderer content={'Set `NODE_ENV=production`.'} />)
    const code = await screen.findByText('NODE_ENV=production')
    fireEvent.keyDown(code, { key: ' ' })
    expect(copyToClipboard).toHaveBeenCalledWith('NODE_ENV=production')
  })

  it('names the action in its tooltip, on hover and on keyboard focus alike', async () => {
    vi.useFakeTimers()
    render(<MarkdownRenderer content={'Use `--verbose` flag.'} />)
    const code = screen.getByText('--verbose')
    // No native title: the shared instant tooltip replaces it, so a keyboard
    // user gets the cue too (a `title` never shows on focus).
    expect(code).not.toHaveAttribute('title')

    fireEvent.mouseEnter(code)
    act(() => { vi.advanceTimersByTime(OPEN_DELAY_MS) })
    expect(screen.getByRole('tooltip')).toHaveTextContent('Click to copy')
    fireEvent.mouseLeave(code)
    expect(screen.queryByRole('tooltip')).toBeNull()

    fireEvent.focus(code)
    expect(screen.getByRole('tooltip')).toHaveTextContent('Click to copy')
    expect(code).toHaveAttribute('aria-describedby', screen.getByRole('tooltip').id)
  })

  it('confirms the copy in the tooltip and to assistive tech, then clears both after 1500ms', async () => {
    vi.useFakeTimers()
    render(<MarkdownRenderer content={'Use `--verbose` flag.'} />)
    const code = screen.getByText('--verbose')
    const status = screen.getByRole('status')
    expect(status).toHaveTextContent('')

    fireEvent.focus(code)
    await act(async () => { fireEvent.click(code) })
    expect(screen.getByRole('tooltip')).toHaveTextContent('Copied!')
    expect(status).toHaveTextContent('Copied!')

    act(() => { vi.advanceTimersByTime(1500) })
    expect(screen.getByRole('tooltip')).toHaveTextContent('Click to copy')
    expect(status).toHaveTextContent('')
  })

  it('reports a refused clipboard write in the same bubble, in error tone, instead of claiming a copy', async () => {
    vi.mocked(copyToClipboard).mockResolvedValue(false)
    render(<MarkdownRenderer content={'Use `--verbose` flag.'} />)
    const code = screen.getByText('--verbose')
    // No standing alert region: the failure's accessible surface is the
    // ErrorNotice itself (errors-use-error-notice), which does not exist yet.
    expect(document.querySelectorAll('[role="alert"]')).toHaveLength(0)
    fireEvent.focus(code)
    await act(async () => { fireEvent.click(code) })
    expect(copyToClipboard).toHaveBeenCalledWith('--verbose')
    // No confirmation anywhere...
    expect(screen.getByRole('tooltip')).not.toHaveTextContent('Copied!')
    expect(screen.getByRole('status')).toHaveTextContent('')
    // ...the failure lands where the confirmation would have: in the bubble,
    // rendered through ErrorNotice (icon, danger tone, role="alert"), and NOT in
    // the text flow — the paragraph carries no notice, so the prose never shifts.
    const notice = screen.getByTestId('md-chip-copy-error')
    expect(notice).toHaveTextContent('Copy failed')
    expect(screen.getByRole('tooltip').contains(notice)).toBe(true)
    expect(code.parentElement!.querySelector('[data-testid="md-chip-copy-error"]')).toBeNull()
    // Announced ONCE, through the notice: it is accessible (no aria-hidden
    // ancestor) and the ONLY alert in the document — no sr-only copy of it.
    expect(notice.closest('[aria-hidden="true"]')).toBeNull()
    const liveAlerts = screen.getAllByRole('alert')
    expect(liveAlerts).toHaveLength(1)
    expect(liveAlerts[0]).toBe(notice)
    expect(document.querySelectorAll('[role="alert"]')).toHaveLength(1)

    // A later successful copy clears it and confirms.
    vi.mocked(copyToClipboard).mockResolvedValue(true)
    await act(async () => { fireEvent.click(code) })
    expect(screen.queryByTestId('md-chip-copy-error')).toBeNull()
    expect(document.querySelectorAll('[role="alert"]')).toHaveLength(0)
    expect(screen.getByRole('tooltip')).toHaveTextContent('Copied!')
    expect(screen.getByRole('status')).toHaveTextContent('Copied!')
  })

  it('the confirmation survives the pointer leaving, and the bubble closes when the flash ends', async () => {
    // The bubble is the only visible confirmation, and a mouse user moves on
    // right after clicking: a leave must not take the 1.5s "Copied!" with it.
    // A real click also FOCUSES the chip; that focus must not keep the bubble
    // open once the flash ends (the harness caught exactly this).
    vi.useFakeTimers()
    render(<MarkdownRenderer content={'Use `--verbose` flag.'} />)
    const code = screen.getByText('--verbose')
    fireEvent.mouseEnter(code)
    act(() => { vi.advanceTimersByTime(OPEN_DELAY_MS) })
    fireEvent.focus(code)
    await act(async () => { fireEvent.click(code) })
    expect(screen.getByRole('tooltip')).toHaveTextContent('Copied!')

    fireEvent.mouseLeave(code)
    expect(screen.getByRole('tooltip')).toHaveTextContent('Copied!')
    act(() => { vi.advanceTimersByTime(1499) })
    expect(screen.getByRole('tooltip')).toHaveTextContent('Copied!')
    // The flash's own timer ends it; the pointer is gone, so the bubble goes too.
    act(() => { vi.advanceTimersByTime(1) })
    expect(screen.queryByRole('tooltip')).toBeNull()
    expect(screen.getByRole('status')).toHaveTextContent('')
  })

  it('a click inside the hover-intent window still gets its bubble: the outcome opens it', async () => {
    vi.useFakeTimers()
    render(<MarkdownRenderer content={'Use `--verbose` flag.'} />)
    const code = screen.getByText('--verbose')
    fireEvent.mouseEnter(code)
    expect(screen.queryByRole('tooltip')).toBeNull()
    await act(async () => { fireEvent.click(code) })
    expect(screen.getByRole('tooltip')).toHaveTextContent('Copied!')
  })

  it('the failure holds the bubble through a mouse leave, then clears itself and the bubble closes', async () => {
    vi.useFakeTimers()
    vi.mocked(copyToClipboard).mockResolvedValue(false)
    render(<MarkdownRenderer content={'Use `--verbose` flag.'} />)
    const code = screen.getByText('--verbose')
    fireEvent.mouseEnter(code)
    act(() => { vi.advanceTimersByTime(OPEN_DELAY_MS) })
    await act(async () => { fireEvent.click(code) })
    expect(screen.getByRole('tooltip')).toHaveTextContent('Copy failed')

    fireEvent.mouseLeave(code)
    expect(screen.getByRole('tooltip')).toHaveTextContent('Copy failed')
    // Longer than the confirmation: a failure is the outcome the user did not
    // expect, so it gets the time to be noticed and read.
    act(() => { vi.advanceTimersByTime(1500) })
    expect(screen.getByRole('tooltip')).toHaveTextContent('Copy failed')
    act(() => { vi.advanceTimersByTime(COPY_FAILED_FLASH_MS - 1500) })
    expect(screen.queryByRole('tooltip')).toBeNull()
    expect(screen.queryByTestId('md-chip-copy-error')).toBeNull()
  })

  it('a refused retry inside the confirmation window replaces "Copied!" with the failure, never shows both', async () => {
    vi.useFakeTimers()
    render(<MarkdownRenderer content={'Use `--verbose` flag.'} />)
    const code = screen.getByText('--verbose')
    const status = screen.getByRole('status')
    // Focus opens the tooltip at once (no intent timer), so every timer counted
    // below belongs to an outcome flash.
    fireEvent.focus(code)
    const idle = vi.getTimerCount()

    await act(async () => { fireEvent.click(code) })
    expect(screen.getByRole('tooltip')).toHaveTextContent('Copied!')
    expect(status).toHaveTextContent('Copied!')
    expect(vi.getTimerCount()).toBe(idle + 1) // the 1.5s clear is pending

    // 500ms in, still confirming, the retry is refused: the text the user just
    // asked for is NOT on the clipboard, so the confirmation has to go.
    act(() => { vi.advanceTimersByTime(500) })
    vi.mocked(copyToClipboard).mockResolvedValue(false)
    await act(async () => { fireEvent.click(code) })
    expect(screen.getByRole('tooltip')).toHaveTextContent('Copy failed')
    expect(screen.getByRole('tooltip')).not.toHaveTextContent('Copied!')
    expect(status).toHaveTextContent('')
    // The failure is announced exactly once, through the notice itself — the
    // document's one alert; no sr-only copy of it anywhere.
    const alerts = screen.getAllByRole('alert')
    expect(alerts).toHaveLength(1)
    expect(alerts[0]).toBe(screen.getByTestId('md-chip-copy-error'))
    expect(document.querySelectorAll('[role="alert"]')).toHaveLength(1)
    // One timer — the failure's own flash. The stale confirmation's clear is
    // cancelled, not left to fire.
    expect(vi.getTimerCount()).toBe(idle + 1)

    // Where the stale clear would have fired, nothing changes: no late flip.
    act(() => { vi.advanceTimersByTime(1500) })
    expect(status).toHaveTextContent('')
    expect(screen.getByRole('tooltip')).toHaveTextContent('Copy failed')
    // The failure's flash ends; still focused, so the bubble stays with the prompt.
    act(() => { vi.advanceTimersByTime(COPY_FAILED_FLASH_MS - 1500) })
    expect(screen.getByRole('tooltip')).toHaveTextContent('Click to copy')
    expect(screen.queryByTestId('md-chip-copy-error')).toBeNull()
    expect(document.querySelectorAll('[role="alert"]')).toHaveLength(0)
    expect(vi.getTimerCount()).toBe(idle)
  })

  it('a stale settlement from a superseded press cannot overwrite the latest outcome, in either order', async () => {
    // Two presses in flight at once: the write the user pressed LAST is the
    // one whose outcome the chip reports, whichever promise settles first.
    vi.useFakeTimers()
    const deferred = () => {
      let resolve!: (ok: boolean) => void
      const promise = new Promise<boolean>(r => { resolve = r })
      return { promise, resolve }
    }
    render(<MarkdownRenderer content={'Use `--verbose` flag.'} />)
    const code = screen.getByText('--verbose')
    const status = screen.getByRole('status')
    fireEvent.focus(code)

    // Order 1: the latest press succeeds, then the earlier one is refused late.
    const first = deferred()
    const second = deferred()
    vi.mocked(copyToClipboard).mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise)
    fireEvent.click(code)
    fireEvent.click(code)
    expect(copyToClipboard).toHaveBeenCalledTimes(2)
    await act(async () => { second.resolve(true) })
    expect(status).toHaveTextContent('Copied!')
    await act(async () => { first.resolve(false) })
    expect(status).toHaveTextContent('Copied!')
    expect(screen.getByRole('tooltip')).toHaveTextContent('Copied!')
    expect(screen.queryByTestId('md-chip-copy-error')).toBeNull()
    act(() => { vi.advanceTimersByTime(1500) })
    expect(status).toHaveTextContent('')

    // Order 2: the latest press is refused, then the earlier one succeeds late.
    const third = deferred()
    const fourth = deferred()
    vi.mocked(copyToClipboard).mockReturnValueOnce(third.promise).mockReturnValueOnce(fourth.promise)
    fireEvent.click(code)
    fireEvent.click(code)
    await act(async () => { fourth.resolve(false) })
    expect(screen.getByTestId('md-chip-copy-error')).toHaveTextContent('Copy failed')
    await act(async () => { third.resolve(true) })
    expect(screen.getByTestId('md-chip-copy-error')).toHaveTextContent('Copy failed')
    expect(status).toHaveTextContent('')
    expect(screen.getByRole('tooltip')).not.toHaveTextContent('Copied!')
  })

  it('press order is global: an older press on ANOTHER chip that settles late cannot evict the latest failure', async () => {
    // Chip A's write hangs (a permission prompt), the user moves on and presses
    // chip B, which is refused and shows its failure. A then succeeds. The user
    // pressed B last, so B's failure is the outcome on show; A's late success
    // must not close it, and must not announce "Copied!" for a press the user
    // has moved on from. Per-chip attempt numbers alone cannot see this.
    vi.useFakeTimers()
    const deferred = () => {
      let resolve!: (ok: boolean) => void
      const promise = new Promise<boolean>(r => { resolve = r })
      return { promise, resolve }
    }
    render(<MarkdownRenderer content={'Run `npm test` then `npm run build`.'} />)
    const a = screen.getByText('npm test')
    const b = screen.getByText('npm run build')
    const slow = deferred()
    vi.mocked(copyToClipboard).mockReturnValueOnce(slow.promise).mockResolvedValueOnce(false)
    fireEvent.mouseEnter(a)
    fireEvent.click(a)
    fireEvent.mouseLeave(a)
    fireEvent.mouseEnter(b)
    await act(async () => { fireEvent.click(b) })
    expect(copyToClipboard).toHaveBeenCalledTimes(2)
    const notice = screen.getByTestId('md-chip-copy-error')
    expect(screen.getByRole('tooltip').contains(notice)).toBe(true)
    expect(b).toHaveAttribute('aria-describedby', screen.getByRole('tooltip').id)

    await act(async () => { slow.resolve(true) })
    const tips = screen.getAllByRole('tooltip')
    expect(tips).toHaveLength(1)
    expect(tips[0].contains(screen.getByTestId('md-chip-copy-error'))).toBe(true)
    expect(tips[0]).not.toHaveTextContent('Copied!')
    for (const region of screen.getAllByRole('status')) expect(region).toHaveTextContent('')
    // B's failure still runs its own flash to the end.
    act(() => { vi.advanceTimersByTime(COPY_FAILED_FLASH_MS - 1) })
    expect(screen.getByTestId('md-chip-copy-error')).toBeInTheDocument()
  })

  it('only a press moves on from a press: another chip re-rendering or unmounting does not drop a pending one', async () => {
    // A streaming transcript rewrites and remounts chips constantly. Chip A's
    // write is pending; chip B's text changes, then B disappears entirely. A's
    // settlement must still land: nobody pressed anything else.
    vi.useFakeTimers()
    const deferred = () => {
      let resolve!: (ok: boolean) => void
      const promise = new Promise<boolean>(r => { resolve = r })
      return { promise, resolve }
    }
    const { rerender } = render(<MarkdownRenderer content={'Run `npm test` then `npm run build`.'} />)
    const a = screen.getByText('npm test')
    const slow = deferred()
    vi.mocked(copyToClipboard).mockReturnValueOnce(slow.promise)
    fireEvent.mouseEnter(a)
    fireEvent.click(a)
    rerender(<MarkdownRenderer content={'Run `npm test` then `npm run lint`.'} />)
    rerender(<MarkdownRenderer content={'Run `npm test` now.'} />)
    await act(async () => { slow.resolve(true) })
    expect(screen.getByRole('tooltip')).toHaveTextContent('Copied!')
    expect(screen.getByRole('status')).toHaveTextContent('Copied!')
  })

  it('a failure the pointer never saw still opens the bubble at the chip, once', async () => {
    // Clicked, then the pointer left before the write settled: the refusal
    // still owes its bubble (and its single announcement) at the chip.
    vi.useFakeTimers()
    vi.mocked(copyToClipboard).mockResolvedValue(false)
    render(<MarkdownRenderer content={'Use `--verbose` flag.'} />)
    const code = screen.getByText('--verbose')
    fireEvent.mouseEnter(code)
    fireEvent.click(code)
    fireEvent.mouseLeave(code)
    expect(screen.queryByRole('tooltip')).toBeNull()
    await act(async () => {})
    expect(screen.getByRole('tooltip')).toHaveTextContent('Copy failed')
    expect(screen.getAllByRole('alert')).toHaveLength(1)
    act(() => { vi.advanceTimersByTime(COPY_FAILED_FLASH_MS) })
    expect(screen.queryByRole('tooltip')).toBeNull()
  })

  it('a retry that fails again shows the failure again, even after a scroll or Escape closed the bubble', async () => {
    // Same kind of outcome twice, bubble closed in between with the pointer
    // still on the chip: the transcript's auto-scroll hides it (the position is
    // stale), or Escape did. The retry's outcome must still render — the text
    // is still not on the clipboard — and announce once.
    vi.useFakeTimers()
    vi.mocked(copyToClipboard).mockResolvedValue(false)
    render(<MarkdownRenderer content={'Use `--verbose` flag.'} />)
    const code = screen.getByText('--verbose')
    fireEvent.mouseEnter(code)
    act(() => { vi.advanceTimersByTime(OPEN_DELAY_MS) })
    await act(async () => { fireEvent.click(code) })
    expect(screen.getByRole('tooltip')).toHaveTextContent('Copy failed')

    fireEvent.scroll(window)
    expect(screen.queryByRole('tooltip')).toBeNull()
    await act(async () => { fireEvent.click(code) })
    expect(screen.getByRole('tooltip')).toHaveTextContent('Copy failed')
    expect(screen.getAllByRole('alert')).toHaveLength(1)

    fireEvent.keyDown(window, { key: 'Escape' })
    expect(screen.queryByRole('tooltip')).toBeNull()
    await act(async () => { fireEvent.click(code) })
    expect(screen.getByRole('tooltip')).toHaveTextContent('Copy failed')
    expect(screen.getAllByRole('alert')).toHaveLength(1)
  })

  it('moving to the next chip does not take a held outcome: the neighbour\'s hint waits for the flash', async () => {
    // One bubble at a time, held ones first. A "Copy failed" lives ONLY in
    // the bubble now, and a mouse user moves on right after clicking: the
    // neighbour's hint must not evict the only sign the write was refused.
    vi.useFakeTimers()
    vi.mocked(copyToClipboard).mockResolvedValue(false)
    render(<MarkdownRenderer content={'Run `npm test` then `npm run build`.'} />)
    const first = screen.getByText('npm test')
    const second = screen.getByText('npm run build')
    fireEvent.mouseEnter(first)
    act(() => { vi.advanceTimersByTime(OPEN_DELAY_MS) })
    await act(async () => { fireEvent.click(first) })
    expect(screen.getByTestId('md-chip-copy-error')).toBeInTheDocument()
    fireEvent.mouseLeave(first)
    fireEvent.mouseEnter(second)
    act(() => { vi.advanceTimersByTime(OPEN_DELAY_MS) })
    // Still one bubble, still the failure.
    let tips = screen.getAllByRole('tooltip')
    expect(tips).toHaveLength(1)
    expect(tips[0].contains(screen.getByTestId('md-chip-copy-error'))).toBe(true)
    // The flash ends with the pointer resting on the neighbour: its hint,
    // which yielded, opens — one bubble throughout.
    act(() => { vi.advanceTimersByTime(COPY_FAILED_FLASH_MS) })
    tips = screen.getAllByRole('tooltip')
    expect(tips).toHaveLength(1)
    expect(tips[0]).toHaveTextContent('Click to copy')
    expect(second).toHaveAttribute('aria-describedby', tips[0].id)
  })

  it('a new press supersedes a held neighbour: the latest outcome owns the one bubble', async () => {
    vi.useFakeTimers()
    render(<MarkdownRenderer content={'Run `npm test` then `npm run build`.'} />)
    const first = screen.getByText('npm test')
    const second = screen.getByText('npm run build')
    fireEvent.mouseEnter(first)
    act(() => { vi.advanceTimersByTime(OPEN_DELAY_MS) })
    await act(async () => { fireEvent.click(first) })
    expect(screen.getByRole('tooltip')).toHaveTextContent('Copied!')
    fireEvent.mouseLeave(first)
    fireEvent.mouseEnter(second)
    await act(async () => { fireEvent.click(second) })
    const tips = screen.getAllByRole('tooltip')
    expect(tips).toHaveLength(1)
    expect(tips[0]).toHaveTextContent('Copied!')
    expect(second).toHaveAttribute('aria-describedby', tips[0].id)
    expect(first).not.toHaveAttribute('aria-describedby', tips[0].id)
  })

  it('declares its action for the stylesheet, and a raw-HTML claim cannot forge another', async () => {
    // index.css keys the chip colours on this attribute (the Kiro inline-code
    // rule outranks the `text-accent` utility), so a copy chip must say "copy"
    // and only the renderer may say anything else.
    render(<MarkdownRenderer content={'Run `ls -la` and <code data-chip-action="open">not a path</code>.'} />)
    expect(screen.getByText('ls -la')).toHaveAttribute('data-chip-action', 'copy')
    expect(screen.getByText('not a path')).toHaveAttribute('data-chip-action', 'copy')
  })

  it('a raw-HTML class cannot redress a chip: the renderer owns the cue classes', () => {
    // `class` is allowlisted on every element, so a message could carry
    // `<code class="text-accent hover:underline">`: on the nine non-Kiro themes
    // that is the navigate look on a chip whose click copies — the very
    // confusion this cue exists to end — and `class=""` would strip the cursor,
    // the underline and the mono face. Neither reaches the chip: the `code`
    // override destructures `className` out (it only routes fenced blocks)
    // before `InlineCode` builds `safeProps`. Pinned so a refactor that forwards
    // it cannot pass quietly.
    render(<MarkdownRenderer content={'Run <code class="text-accent hover:underline">rm -rf /tmp</code> then <code class="">ls</code>.'} />)
    const forged = screen.getByRole('button', { name: 'Copy rm -rf /tmp' })
    expect(forged.className).not.toMatch(/text-accent|hover:underline/)
    expect(forged.className).toMatch(/cursor-copy/)
    expect(forged.className).toMatch(/decoration-dotted/)
    const emptied = screen.getByRole('button', { name: 'Copy ls' })
    expect(emptied.className).toMatch(/cursor-copy/)
    expect(emptied.className).toMatch(/decoration-dotted/)
    expect(emptied.className).toMatch(/font-mono/)
  })

  it('an outcome belongs to the text that earned it: a chip whose text changes drops it', async () => {
    // A streaming transcript or an editable preview can rewrite a span while its
    // copy is flashing or still pending. The clipboard holds the OLD text, so
    // the new text must not wear "Copied!" — and a settlement for the old text
    // must not confirm the new one.
    vi.useFakeTimers()
    const deferred = () => {
      let resolve!: (ok: boolean) => void
      const promise = new Promise<boolean>(r => { resolve = r })
      return { promise, resolve }
    }
    const { rerender } = render(<MarkdownRenderer content={'Use `--verbose` flag.'} />)
    let code = screen.getByText('--verbose')
    fireEvent.focus(code)
    await act(async () => { fireEvent.click(code) })
    expect(screen.getByRole('status')).toHaveTextContent('Copied!')

    rerender(<MarkdownRenderer content={'Use `--quiet` flag.'} />)
    code = screen.getByText('--quiet')
    expect(screen.getByRole('status')).toHaveTextContent('')
    expect(screen.queryByRole('tooltip')?.textContent ?? '').not.toContain('Copied!')

    // Pending at the swap: the old text's settlement is dropped, not shown.
    const pending = deferred()
    vi.mocked(copyToClipboard).mockReturnValueOnce(pending.promise)
    fireEvent.focus(code)
    fireEvent.click(code)
    rerender(<MarkdownRenderer content={'Use `--silent` flag.'} />)
    await act(async () => { pending.resolve(true) })
    expect(screen.getByRole('status')).toHaveTextContent('')
    expect(screen.queryByTestId('md-chip-copy-error')).toBeNull()
  })

  it('is focusable via tabIndex', async () => {
    render(<MarkdownRenderer content={'Check `FOO_BAR` env var.'} />)
    const code = await screen.findByText('FOO_BAR')
    expect(code).toHaveAttribute('tabindex', '0')
  })

  it('wears code styling, not link styling: neutral colour, copy cursor, a DOTTED underline, never the accent', async () => {
    // The dotted underline is the "hover me" affordance the rest of the app
    // uses for a term with an explanation (CrewLogPanel, OAuthRelayAffordance),
    // and it is not a link's solid one; the accent and the hover-solid
    // underline belong to the chips whose click navigates.
    render(<MarkdownRenderer content={'Run `ls -la`.'} />)
    const code = await screen.findByText('ls -la')
    expect(code.className).toContain('font-mono')
    expect(code.className).toContain('bg-bg-elevated')
    expect(code.className).toContain('cursor-copy')
    expect(code.className).toContain('decoration-dotted')
    expect(code.className).not.toContain('cursor-pointer')
    expect(code.className).not.toContain('text-accent')
    expect(code.className).not.toContain('hover:underline')
  })
})
