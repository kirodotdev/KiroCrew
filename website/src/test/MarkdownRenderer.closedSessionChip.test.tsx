import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent, screen } from '@testing-library/react'

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
    await vi.waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent(`Couldn't open ${CLOSED}.`))
    expect(open).not.toHaveBeenCalled()
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
