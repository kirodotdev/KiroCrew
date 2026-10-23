import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent, screen } from '@testing-library/react'
import i18next from 'i18next'
import '../i18n/all'

import MarkdownRenderer from '../components/MarkdownRenderer'
import { ClosedSessionCtx, type ClosedSessionActions } from '../components/markdown/contexts'
import { resetClosedSessionProbes } from '../components/markdown/linkTargets'

// #9915: a link to a CLOSED session (on disk, not open) must reach it. The open
// roster cannot say it exists, so the chip asks the gateway about that one key.
const OPEN = 'chat-1-1784661000'
const CLOSED = 'chat-7-1784661951'
const STEM = `dashboard_${CLOSED}`
const roster = () => new Map([[OPEN, 'Open one']])

let lookup: ReturnType<typeof vi.fn>
let open: ReturnType<typeof vi.fn>
let onSessionOpen: ReturnType<typeof vi.fn>

beforeEach(() => {
  resetClosedSessionProbes()
  lookup = vi.fn(async (key: string) => (key === CLOSED ? { key: STEM, title: 'Earlier work' } : null))
  open = vi.fn(async () => 'opened' as const)
  onSessionOpen = vi.fn()
})

function renderWith(content: string, opts: { sessions?: Map<string, string> | undefined; closed?: ClosedSessionActions } = {}) {
  const sessions = 'sessions' in opts ? opts.sessions : roster()
  return render(
    <ClosedSessionCtx.Provider value={opts.closed ?? { lookup, open }}>
      <MarkdownRenderer content={content} onSessionOpen={onSessionOpen} sessions={sessions} />
    </ClosedSessionCtx.Provider>,
  )
}

describe('closed-session chip', () => {
  it('a backticked closed key becomes a chip that resumes it', async () => {
    renderWith(`Earlier: \`${CLOSED}\`.`)
    await vi.waitFor(() => expect(screen.getByText(CLOSED)).toHaveAttribute('data-session-key', CLOSED))
    const chip = screen.getByText(CLOSED)
    fireEvent.click(chip)
    await vi.waitFor(() => expect(open).toHaveBeenCalledWith({ key: STEM, title: 'Earlier work' }))
    expect(onSessionOpen).not.toHaveBeenCalled()
    expect(lookup).toHaveBeenCalledWith(CLOSED)
  })

  it('a ?sid= link to a closed session resumes it on click', async () => {
    renderWith(`See [the old run](/chat?sid=${CLOSED}).`)
    const link = screen.getByText('the old run').closest('a')!
    await vi.waitFor(() => expect(link.getAttribute('title')).toContain('Earlier work'))
    fireEvent.click(link)
    await vi.waitFor(() => expect(open).toHaveBeenCalledWith({ key: STEM, title: 'Earlier work' }))
  })

  it('an untitled closed session is named "Untitled session" in the current language, from a raw cached row (#23084)', async () => {
    lookup.mockImplementation(async (key: string) => (key === CLOSED ? { key: STEM, title: '' } : null))
    const first = renderWith(`See [the old run](/chat?sid=${CLOSED}).`)
    const link = () => screen.getByText('the old run').closest('a')!
    await vi.waitFor(() => expect(link().getAttribute('title')).toContain('Untitled session'))
    first.unmount()
    try {
      // The probe answer outlives the mount; a remount in another language
      // must name it in that language, not the one it was first rendered in.
      await i18next.changeLanguage('zh-CN')
      renderWith(`See [the old run](/chat?sid=${CLOSED}).`)
      await vi.waitFor(() => expect(link().getAttribute('title')).toContain('未命名会话'))
      expect(link().getAttribute('title')).not.toContain('Untitled session')
      expect(lookup).toHaveBeenCalledTimes(1)
    } finally {
      await i18next.changeLanguage('en')
    }
  })

  it('an open session still switches through the roster, with no probe', () => {
    renderWith(`Now: \`${OPEN}\`.`)
    fireEvent.click(screen.getByText(OPEN))
    expect(onSessionOpen).toHaveBeenCalledWith(OPEN)
    expect(lookup).not.toHaveBeenCalled()
  })

  it('a key the gateway does not know stays plain text', async () => {
    renderWith('Forged: `chat-9-1700000000`.')
    await vi.waitFor(() => expect(lookup).toHaveBeenCalledWith('chat-9-1700000000'))
    await Promise.resolve()
    expect(screen.getByText('chat-9-1700000000')).not.toHaveAttribute('data-session-key')
  })

  it('a short name is never probed: it cannot say which past session it means', () => {
    renderWith('Short: `chat-7`.')
    expect(lookup).not.toHaveBeenCalled()
  })

  it('offline (roster withheld) does not probe', () => {
    renderWith(`Earlier: \`${CLOSED}\`.`, { sessions: undefined })
    expect(lookup).not.toHaveBeenCalled()
  })

  it('without the page provider, a closed key stays plain as before', () => {
    renderWith(`Earlier: \`${CLOSED}\`.`, { closed: {} })
    expect(screen.getByText(CLOSED)).not.toHaveAttribute('data-session-key')
  })

  it('probes each key once across renders', async () => {
    const first = renderWith(`\`${CLOSED}\``)
    await vi.waitFor(() => expect(screen.getByText(CLOSED)).toHaveAttribute('data-session-key', CLOSED))
    first.unmount()
    renderWith(`\`${CLOSED}\``)
    expect(screen.getByText(CLOSED)).toHaveAttribute('data-session-key', CLOSED)
    expect(lookup).toHaveBeenCalledTimes(1)
  })

  it('a session the click finds deleted drops its chip', async () => {
    renderWith(`Earlier: \`${CLOSED}\`.`)
    await vi.waitFor(() => expect(screen.getByText(CLOSED)).toHaveAttribute('data-session-key', CLOSED))
    open.mockResolvedValue('gone')
    fireEvent.click(screen.getByText(CLOSED))
    await vi.waitFor(() => expect(screen.getByText(CLOSED)).not.toHaveAttribute('data-session-key'))
    expect(open).toHaveBeenCalledTimes(1)
  })

  it('a failed probe raises nothing until the reader clicks', async () => {
    lookup.mockRejectedValueOnce(new Error('offline'))
    renderWith(`Earlier: \`${CLOSED}\`.`)
    await vi.waitFor(() => expect(screen.getByText(CLOSED)).toHaveAttribute('data-session-key', CLOSED))
    expect(screen.queryByRole('alert')).toBeNull()
    expect(screen.queryByText(`Couldn't open ${CLOSED}.`)).toBeNull()
  })

  it('a click on a chip whose probe failed shows the inline notice, and retry opens it', async () => {
    lookup.mockRejectedValueOnce(new Error('offline'))
    renderWith(`Earlier: \`${CLOSED}\`.`)
    await vi.waitFor(() => expect(screen.getByText(CLOSED)).toHaveAttribute('data-session-key', CLOSED))
    fireEvent.click(screen.getByText(CLOSED))
    expect(screen.getByRole('alert')).toHaveTextContent(`Couldn't open ${CLOSED}.`)
    expect(open).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
    await vi.waitFor(() => expect(open).toHaveBeenCalledWith({ key: STEM, title: 'Earlier work' }))
    await vi.waitFor(() => expect(screen.queryByRole('alert')).toBeNull())
    expect(lookup).toHaveBeenCalledTimes(2)
  })

  it('a retry that fails again keeps the notice up', async () => {
    lookup.mockRejectedValue(new Error('offline'))
    renderWith(`Earlier: \`${CLOSED}\`.`)
    await vi.waitFor(() => expect(screen.getByText(CLOSED)).toHaveAttribute('data-session-key', CLOSED))
    fireEvent.click(screen.getByText(CLOSED))
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
    await vi.waitFor(() => expect(lookup).toHaveBeenCalledTimes(2))
    await vi.waitFor(() => expect(screen.getByRole('button', { name: 'Try again' })).not.toBeDisabled())
    expect(screen.getByRole('alert')).toHaveTextContent(`Couldn't open ${CLOSED}.`)
    expect(open).not.toHaveBeenCalled()
  })

  it('a ?sid= link whose probe failed shows the notice beside the link on click', async () => {
    lookup.mockRejectedValueOnce(new Error('offline'))
    renderWith(`See [the old run](/chat?sid=${CLOSED}).`)
    const link = screen.getByText('the old run').closest('a')!
    // Once the probe has answered (failed), the link is live again, not muted.
    await vi.waitFor(() => expect(link).not.toHaveClass('text-muted'))
    expect(screen.queryByRole('alert')).toBeNull()
    fireEvent.click(link)
    await vi.waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent("Couldn't open the old run."))
    expect(open).not.toHaveBeenCalled()
  })

  it('the notice on a prose-labelled ?sid= link names the link text, not the key', async () => {
    lookup.mockRejectedValueOnce(new Error('offline'))
    renderWith(`See [the old run](/chat?sid=${CLOSED}).`)
    const link = screen.getByText('the old run').closest('a')!
    await vi.waitFor(() => expect(link).not.toHaveClass('text-muted'))
    fireEvent.click(link)
    await vi.waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent("Couldn't open the old run."))
    expect(screen.queryByText(`Couldn't open ${CLOSED}.`)).toBeNull()
  })

  it('the notice prefers the probed title over the link text', async () => {
    open.mockResolvedValueOnce('failed')
    renderWith(`See [the old run](/chat?sid=${CLOSED}).`)
    const link = screen.getByText('the old run').closest('a')!
    await vi.waitFor(() => expect(link.getAttribute('title')).toContain('Earlier work'))
    fireEvent.click(link)
    await vi.waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent("Couldn't open Earlier work."))
  })

  it('a ?sid= link with no prose label falls back to the key', async () => {
    lookup.mockRejectedValueOnce(new Error('offline'))
    renderWith(`See [${CLOSED}](/chat?sid=${CLOSED}).`)
    const link = screen.getByText(CLOSED).closest('a')!
    await vi.waitFor(() => expect(link).not.toHaveClass('text-muted'))
    fireEvent.click(link)
    await vi.waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent(`Couldn't open ${CLOSED}.`))
  })

  it('the full stop after a ?sid= link stays next to the link; the notice renders below the paragraph', async () => {
    lookup.mockRejectedValueOnce(new Error('offline'))
    renderWith(`See [the old run](/chat?sid=${CLOSED}). Then more.`)
    const link = screen.getByText('the old run').closest('a')!
    await vi.waitFor(() => expect(link).not.toHaveClass('text-muted'))
    // Before any click the sentence reads exactly as authored.
    const p = link.parentElement!
    expect(p).toHaveTextContent('See the old run. Then more.')
    expect(link.nextSibling?.textContent).toBe('.')
    fireEvent.click(link)
    await vi.waitFor(() => expect(screen.getByRole('alert')).toBeTruthy())
    const notice = document.querySelector('[data-closed-session-notice]')!
    // The paragraph is untouched: period, then the rest of the sentence.
    expect(link.nextSibling?.textContent).toBe('.')
    expect(link.nextSibling?.nextSibling?.textContent).toBe(' Then more.')
    expect(p).toHaveTextContent('See the old run. Then more.')
    // The notice sits in the slot right after the paragraph.
    expect(notice.parentElement).toBe(p.nextElementSibling)
    expect(p.nextElementSibling).toHaveAttribute('data-paragraph-notices')
  })

  it('a link in mid-sentence leaves the sentence whole; the notice renders below the paragraph', async () => {
    lookup.mockRejectedValueOnce(new Error('offline'))
    renderWith(`See [the old run](/chat?sid=${CLOSED}), then read its last message.`)
    const link = screen.getByText('the old run').closest('a')!
    await vi.waitFor(() => expect(link).not.toHaveClass('text-muted'))
    fireEvent.click(link)
    await vi.waitFor(() => expect(screen.getByRole('alert')).toBeTruthy())
    const p = link.parentElement!
    expect(p).toHaveTextContent('See the old run, then read its last message.')
    expect(p.querySelector('[data-closed-session-notice]')).toBeNull()
    expect(p.nextElementSibling!.querySelector('[data-closed-session-notice]')).not.toBeNull()
  })

  it('a paragraph without a session reference draws no notice slot', () => {
    renderWith('Read [the docs](https://example.com/docs). Then more.')
    const p = screen.getByText('the docs').closest('p')!
    expect(p.nextElementSibling).toBeNull()
    expect(document.querySelector('[data-paragraph-notices]')).toBeNull()
  })

  it('without a paragraph slot (a tight list item) the notice renders in place, after the trailing punctuation', async () => {
    lookup.mockRejectedValueOnce(new Error('offline'))
    renderWith(`- See [the old run](/chat?sid=${CLOSED}). Then more.`)
    const link = screen.getByText('the old run').closest('a')!
    await vi.waitFor(() => expect(link).not.toHaveClass('text-muted'))
    fireEvent.click(link)
    await vi.waitFor(() => expect(screen.getByRole('alert')).toBeTruthy())
    const notice = document.querySelector('[data-closed-session-notice]')!
    expect(link.closest('li')).not.toBeNull()
    expect(link.nextSibling?.textContent).toBe('.')
    expect(link.nextSibling?.nextSibling).toBe(notice)
    expect(notice.nextSibling?.textContent).toBe(' Then more.')
  })

  it('a link with no trailing punctuation keeps the rest of the sentence whole', async () => {
    lookup.mockRejectedValueOnce(new Error('offline'))
    renderWith(`See [the old run](/chat?sid=${CLOSED}) for details`)
    const link = screen.getByText('the old run').closest('a')!
    await vi.waitFor(() => expect(link).not.toHaveClass('text-muted'))
    fireEvent.click(link)
    await vi.waitFor(() => expect(screen.getByRole('alert')).toBeTruthy())
    expect(link.nextSibling?.textContent).toBe(' for details')
    expect(link.parentElement).toHaveTextContent(/for details$/)
  })

  it('a retry that finds the session deleted drops every reference to it, notices included', async () => {
    lookup.mockRejectedValue(new Error('offline'))
    renderWith(`See [the old run](/chat?sid=${CLOSED}). Also \`${CLOSED}\`.`)
    const link = screen.getByText('the old run').closest('a')!
    await vi.waitFor(() => expect(link).not.toHaveClass('text-muted'))
    await vi.waitFor(() => expect(screen.getByText(CLOSED)).toHaveAttribute('data-session-key', CLOSED))
    fireEvent.click(link)
    fireEvent.click(screen.getByText(CLOSED))
    await vi.waitFor(() => expect(screen.getAllByRole('alert')).toHaveLength(2))
    lookup.mockResolvedValueOnce(null)
    open.mockResolvedValueOnce('gone')
    fireEvent.click(screen.getAllByRole('button', { name: 'Try again' })[0])
    await vi.waitFor(() => expect(open).toHaveBeenCalledWith({ key: CLOSED, title: 'the old run' }))
    // Both references learn the session is gone: no notice still offers Try again.
    await vi.waitFor(() => expect(screen.queryAllByRole('alert')).toHaveLength(0))
    expect(link).toHaveClass('text-muted')
    expect(screen.getByText(CLOSED)).not.toHaveAttribute('data-session-key')
  })

  it('punctuation after an ordinary link is left where it was', () => {
    renderWith('Read [the docs](https://example.com/docs). Then more.')
    const link = screen.getByText('the docs').closest('a')!
    expect(link.parentElement).toHaveTextContent('Read the docs. Then more.')
    expect(link.nextSibling?.textContent).toBe('. Then more.')
  })

  it('an authored trailing marker in raw HTML is dropped, not printed', async () => {
    lookup.mockRejectedValueOnce(new Error('offline'))
    renderWith(`See <a href="/chat?sid=${CLOSED}" data-session-trailing="BOGUS">the old run</a>.`)
    const link = screen.getByText('the old run').closest('a')!
    await vi.waitFor(() => expect(link).not.toHaveClass('text-muted'))
    expect(link.parentElement).toHaveTextContent('See the old run.')
    expect(link.parentElement?.textContent).not.toContain('BOGUS')
    expect(link.nextSibling?.textContent).toBe('.')
  })

  it('a retry that finds the session deleted goes through the page\'s deleted banner', async () => {
    lookup.mockRejectedValueOnce(new Error('offline'))
    renderWith(`See [the old run](/chat?sid=${CLOSED}).`)
    const link = screen.getByText('the old run').closest('a')!
    await vi.waitFor(() => expect(link).not.toHaveClass('text-muted'))
    fireEvent.click(link)
    await vi.waitFor(() => expect(screen.getByRole('alert')).toBeTruthy())
    // Deleted between the first probe and the retry: the fresh lookup is a 404.
    lookup.mockResolvedValueOnce(null)
    open.mockResolvedValueOnce('gone')
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
    // The same hand-off a click on a deleted chip takes, so the page says
    // "was deleted" instead of the chip silently vanishing.
    await vi.waitFor(() => expect(open).toHaveBeenCalledWith({ key: CLOSED, title: 'the old run' }))
    await vi.waitFor(() => expect(screen.queryByRole('alert')).toBeNull())
    expect(link).toHaveClass('text-muted')
  })

  it('a backticked chip whose retry finds the session deleted takes the same deleted hand-off, named by its key', async () => {
    lookup.mockRejectedValueOnce(new Error('offline'))
    renderWith(`Earlier: \`${CLOSED}\`.`)
    await vi.waitFor(() => expect(screen.getByText(CLOSED)).toHaveAttribute('data-session-key', CLOSED))
    fireEvent.click(screen.getByText(CLOSED))
    await vi.waitFor(() => expect(screen.getByRole('alert')).toBeTruthy())
    lookup.mockResolvedValueOnce(null)
    open.mockResolvedValueOnce('gone')
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
    // The chip's visible text is the key, so the key is the name it hands on.
    await vi.waitFor(() => expect(open).toHaveBeenCalledWith({ key: CLOSED, title: CLOSED }))
    await vi.waitFor(() => expect(screen.getByText(CLOSED)).not.toHaveAttribute('data-session-key'))
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('the notice is a line of its own, not an inline run between two sentences', async () => {
    lookup.mockRejectedValueOnce(new Error('offline'))
    renderWith(`See [the old run](/chat?sid=${CLOSED}). Then more.`)
    const link = screen.getByText('the old run').closest('a')!
    await vi.waitFor(() => expect(link).not.toHaveClass('text-muted'))
    fireEvent.click(link)
    await vi.waitFor(() => expect(screen.getByRole('alert')).toBeTruthy())
    const notice = document.querySelector('[data-closed-session-notice]')!
    expect(notice).toHaveClass('flex')
    expect(notice).not.toHaveClass('inline-flex')
  })

  it('the full stop after a backticked session key stays with the chip; the notice follows it', async () => {
    lookup.mockRejectedValueOnce(new Error('offline'))
    renderWith(`Earlier: \`${CLOSED}\`. Then more.`)
    await vi.waitFor(() => expect(screen.getByText(CLOSED)).toHaveAttribute('data-session-key', CLOSED))
    const chip = screen.getByText(CLOSED)
    expect(chip.parentElement).toHaveTextContent(`Earlier: ${CLOSED}. Then more.`)
    expect(chip).not.toHaveAttribute('data-session-trailing')
    fireEvent.click(chip)
    await vi.waitFor(() => expect(screen.getByRole('alert')).toBeTruthy())
    const notice = document.querySelector('[data-closed-session-notice]')!
    expect(chip.nextSibling?.textContent).toBe('.')
    expect(chip.nextSibling?.nextSibling?.textContent).toBe(' Then more.')
    expect(notice.parentElement).toBe(chip.parentElement!.nextElementSibling)
  })

  it('punctuation after an ordinary code span is left where it was', () => {
    renderWith('Run `npm test`. Then more.')
    const code = screen.getByText('npm test')
    expect(code).not.toHaveAttribute('data-session-trailing')
    const texts = Array.from(code.parentElement!.childNodes).filter(n => n.nodeType === Node.TEXT_NODE).map(n => n.textContent)
    expect(texts).toEqual(['Run ', '. Then more.'])
  })

  it('a click whose own check fails shows the inline notice', async () => {
    open.mockResolvedValueOnce('failed')
    renderWith(`Earlier: \`${CLOSED}\`.`)
    await vi.waitFor(() => expect(screen.getByText(CLOSED)).toHaveAttribute('data-session-key', CLOSED))
    fireEvent.click(screen.getByText(CLOSED))
    await vi.waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent(`Couldn't open ${CLOSED}.`))
    expect(screen.getByRole('button', { name: 'Try again' })).toBeTruthy()
  })

  it("one chip's failed click-time check does not drop another chip for the same key", async () => {
    open.mockResolvedValueOnce('failed')
    renderWith(`First \`${CLOSED}\` and again \`${CLOSED}\`.`)
    await vi.waitFor(() => expect(screen.getAllByText(CLOSED).every(el => el.getAttribute('data-session-key') === CLOSED)).toBe(true))
    const [first, second] = screen.getAllByText(CLOSED)
    fireEvent.click(first)
    await vi.waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent(`Couldn't open ${CLOSED}.`))
    expect(screen.getAllByRole('alert')).toHaveLength(1)
    expect(second).toHaveAttribute('data-session-key', CLOSED)
    fireEvent.click(second)
    await vi.waitFor(() => expect(open).toHaveBeenCalledTimes(2))
  })

  it('the inline notice holds two actions: the hand-off and Try again', async () => {
    lookup.mockRejectedValueOnce(new Error('offline'))
    renderWith(`Earlier: \`${CLOSED}\`.`)
    await vi.waitFor(() => expect(screen.getByText(CLOSED)).toHaveAttribute('data-session-key', CLOSED))
    fireEvent.click(screen.getByText(CLOSED))
    const notice = document.querySelector('[data-closed-session-notice]')!
    expect(notice.querySelectorAll('button')).toHaveLength(2)
    expect(screen.getByRole('button', { name: 'Try again' })).toBeTruthy()
  })
})
