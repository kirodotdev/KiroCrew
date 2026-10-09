/**
 * The Older Sessions read-only preview: what it draws from a transcript, and
 * that a superseded read cannot paint over the row the user moved on to.
 * The sidebar wiring (activation previews, Resume reopens) is pinned in
 * ChatSidebarCoverage.test.tsx.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render as rtlRender, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'

const mocks = vi.hoisted(() => ({ sessionDetail: vi.fn() }))
vi.mock('../api/client', () => ({ api: mocks }))

import HistoryPreviewModal from '../pages/chat-sidebar/HistoryPreviewModal'
import type { SessionPreview } from '../api/client/sessions'
import { consumeChatHandoff, recordError, __resetErrorJournalForTests } from '../utils/errorReport'

function preview(over: Partial<SessionPreview> = {}): SessionPreview {
  return { key: 'k', title: 'T', messages: [], has_more: false, ...over }
}

/** One QueryClient per test, kept across rerenders, with retries off so a
 *  rejected read settles at once. */
function render(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const wrap = (node: ReactElement) => <QueryClientProvider client={qc}>{node}</QueryClientProvider>
  const view = rtlRender(wrap(ui))
  return { ...view, rerender: (next: ReactElement) => view.rerender(wrap(next)) }
}

beforeEach(() => {
  mocks.sessionDetail.mockReset()
  __resetErrorJournalForTests()
})

describe('HistoryPreviewModal', () => {
  it('draws the conversation and leaves tool and system rows out', async () => {
    mocks.sessionDetail.mockResolvedValue(preview({
      messages: [
        { role: 'user', content: 'what changed?' },
        { role: 'tool', content: 'TOOL-ROW' },
        { role: 'system', content: 'SYSTEM-ROW' },
        { role: 'assistant', content: 'the **parser** changed' },
      ],
    }))
    render(<HistoryPreviewModal target={{ key: 'k', title: 'T' }} onClose={() => {}} onResume={() => {}} />)
    expect(await screen.findByText('what changed?')).toBeTruthy()
    expect(screen.getByText('parser')).toBeTruthy()
    expect(screen.queryByText('TOOL-ROW')).toBeNull()
    expect(screen.queryByText('SYSTEM-ROW')).toBeNull()
    expect(screen.getByText('Read-only preview. This session stays closed until you resume it.')).toBeTruthy()
    expect(mocks.sessionDetail).toHaveBeenCalledWith('k')
  })

  it('shows a transcript skeleton and a status line while it loads', async () => {
    mocks.sessionDetail.mockImplementation(() => new Promise(() => {}))
    render(<HistoryPreviewModal target={{ key: 'k', title: 'T' }} onClose={() => {}} onResume={() => {}} />)
    expect(await screen.findByRole('status')).toHaveTextContent('Loading transcript…')
    expect(document.querySelector('[aria-busy="true"] .skeleton')).not.toBeNull()
  })

  it('says when a long transcript is cut to its newest rows', async () => {
    mocks.sessionDetail.mockResolvedValue(preview({
      has_more: true, messages: [{ role: 'assistant', content: 'tail' }],
    }))
    render(<HistoryPreviewModal target={{ key: 'k', title: 'T' }} onClose={() => {}} onResume={() => {}} />)
    expect(await screen.findByText('Showing the most recent part of this conversation. Resume the session to read all of it.')).toBeTruthy()
  })

  it('says so when there is nothing to show', async () => {
    mocks.sessionDetail.mockResolvedValue(preview())
    render(<HistoryPreviewModal target={{ key: 'k', title: 'T' }} onClose={() => {}} onResume={() => {}} />)
    expect(await screen.findByText('No chat messages to preview. Resume the session to see all of its activity.')).toBeTruthy()
  })

  it('reports a failed read through the shared error notice', async () => {
    mocks.sessionDetail.mockRejectedValue(new Error('HTTP 503 transcript changed'))
    render(<HistoryPreviewModal target={{ key: 'k', title: 'T' }} onClose={() => {}} onResume={() => {}} />)
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent("Couldn't load this transcript.")
    expect(alert).toHaveTextContent('HTTP 503 transcript changed')
    // Nothing was shown, so reopening is not the emphasized way out.
    expect(screen.getByRole('button', { name: 'Resume session' }).className).not.toContain('bg-accent')
    expect(screen.getByRole('button', { name: 'Try again' }).className).toContain('bg-accent')
    mocks.sessionDetail.mockResolvedValue(preview({ messages: [{ role: 'assistant', content: 'second try' }] }))
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
    expect(await screen.findByText('second try')).toBeTruthy()
    expect(mocks.sessionDetail).toHaveBeenCalledTimes(2)
  })

  it('names a known failure code in plain words', async () => {
    mocks.sessionDetail.mockRejectedValue(
      Object.assign(new Error('HTTP 503'), { body: JSON.stringify({ error: 'transcript changed while reading', code: 'transcript_changed' }) }),
    )
    render(<HistoryPreviewModal target={{ key: 'k', title: 'T' }} onClose={() => {}} onResume={() => {}} />)
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('The session changed while it was being read.')
    expect(alert).not.toHaveTextContent('HTTP 503')
  })

  it.each([
    ['HTTP 503', 'history_corpus_unreadable'],
    ['HTTP 400', 'no_conversation_log'],
  ])('says an unreadable transcript in plain words (%s %s)', async (status, code) => {
    mocks.sessionDetail.mockRejectedValue(
      Object.assign(new Error(status), { body: JSON.stringify({ error: 'unreadable', code }) }),
    )
    render(<HistoryPreviewModal target={{ key: 'k', title: 'T' }} onClose={() => {}} onResume={() => {}} />)
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent("This conversation can't be read right now. Resume the session to open it.")
    expect(alert).not.toHaveTextContent(status)
  })

  it('keeps the structured report when a known failure gets plain wording', async () => {
    recordError({
      source: 'api',
      message: 'HTTP 503',
      status: 503,
      code: 'transcript_changed',
      endpoint: '/api/sessions/k',
    })
    mocks.sessionDetail.mockRejectedValue(
      Object.assign(new Error('HTTP 503'), { body: JSON.stringify({ error: 'transcript changed while reading', code: 'transcript_changed' }) }),
    )
    render(<HistoryPreviewModal target={{ key: 'k', title: 'T' }} onClose={() => {}} onResume={() => {}} />)

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('The session changed while it was being read.')
    fireEvent.click(screen.getByRole('button', { name: 'Ask the agent' }))
    const prompt = consumeChatHandoff()
    expect(prompt).toContain('/api/sessions/k')
    expect(prompt).toContain('503')
    expect(prompt).toContain('transcript_changed')
  })

  it('hands the target to onResume and disables Resume while offline', async () => {
    mocks.sessionDetail.mockResolvedValue(preview())
    const onResume = vi.fn()
    const { rerender } = render(
      <HistoryPreviewModal target={{ key: 'k', title: 'T' }} onClose={() => {}} onResume={onResume} resumeDisabled />,
    )
    const button = await screen.findByRole('button', { name: 'Resume session' })
    expect((button as HTMLButtonElement).disabled).toBe(true)
    rerender(<HistoryPreviewModal target={{ key: 'k', title: 'T' }} onClose={() => {}} onResume={onResume} />)
    expect(screen.getByRole('button', { name: 'Resume session' }).className).toContain('bg-accent')
    fireEvent.click(screen.getByRole('button', { name: 'Resume session' }))
    expect(onResume).toHaveBeenCalledWith({ key: 'k', title: 'T' })
  })

  it('reads again when the same row is reopened', async () => {
    // The app's client sets `staleTime: Infinity`, so a cached entry would be
    // served forever; `gcTime: 0` drops it on close and the reopen refetches.
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
    const wrap = (node: ReactElement | null) => <QueryClientProvider client={qc}>{node}</QueryClientProvider>
    mocks.sessionDetail
      .mockResolvedValueOnce(preview({ messages: [{ role: 'assistant', content: 'first open' }] }))
      .mockResolvedValueOnce(preview({ messages: [{ role: 'assistant', content: 'grown transcript' }] }))
    const modal = <HistoryPreviewModal target={{ key: 'k', title: 'T' }} onClose={() => {}} onResume={() => {}} />
    const view = rtlRender(wrap(modal))
    expect(await screen.findByText('first open')).toBeTruthy()
    view.rerender(wrap(null))
    // The close drops the entry on a macrotask, as a real close-then-reopen does.
    await new Promise(r => setTimeout(r, 0))
    view.rerender(wrap(modal))
    expect(await screen.findByText('grown transcript')).toBeTruthy()
    expect(mocks.sessionDetail).toHaveBeenCalledTimes(2)
  })

  it('shows the redacted preview title once the read lands', async () => {
    // The row title is the unredacted one the session list serves; the preview
    // endpoint redacts for display, so the loaded title is what must show.
    mocks.sessionDetail.mockResolvedValue(preview({ title: 'token [redacted]' }))
    render(
      <HistoryPreviewModal
        target={{ key: 'k', title: 'token sk-live-123' }}
        onClose={() => {}}
        onResume={() => {}}
      />,
    )
    expect(await screen.findByText('token [redacted]')).toBeTruthy()
    expect(screen.queryByText('token sk-live-123')).toBeNull()
    expect(screen.getByRole('dialog', { name: 'Preview of token [redacted]' })).toBeTruthy()
  })

  it('uses the generic transcript label while the read is in flight', async () => {
    mocks.sessionDetail.mockImplementation(() => new Promise(() => {}))
    render(
      <HistoryPreviewModal
        target={{ key: 'k', title: 'token sk-live-123' }}
        onClose={() => {}}
        onResume={() => {}}
      />,
    )
    expect(await screen.findByRole('dialog', { name: 'Preview of Transcript' })).toBeTruthy()
    expect(screen.queryByText('token sk-live-123')).toBeNull()
  })

  it('ignores a superseded read that answers late', async () => {
    let resolveFirst: (p: SessionPreview) => void = () => {}
    mocks.sessionDetail
      .mockImplementationOnce(() => new Promise<SessionPreview>(r => { resolveFirst = r }))
      .mockResolvedValueOnce(preview({ key: 'b', messages: [{ role: 'assistant', content: 'second row' }] }))
    const { rerender } = render(
      <HistoryPreviewModal target={{ key: 'a', title: 'A' }} onClose={() => {}} onResume={() => {}} />,
    )
    rerender(<HistoryPreviewModal target={{ key: 'b', title: 'B' }} onClose={() => {}} onResume={() => {}} />)
    expect(await screen.findByText('second row')).toBeTruthy()
    resolveFirst(preview({ key: 'a', messages: [{ role: 'assistant', content: 'first row' }] }))
    await waitFor(() => expect(mocks.sessionDetail).toHaveBeenCalledTimes(2))
    await new Promise(r => setTimeout(r, 0))
    expect(screen.queryByText('first row')).toBeNull()
    expect(screen.getByText('second row')).toBeTruthy()
  })
})
