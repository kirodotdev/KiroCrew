/** The search preview rings its hit's bubble on the bubble's own edge with
 *  no fill, fills the matched words the way the chat's find does, and nothing
 *  fades either while it is open. */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, screen, waitFor } from '@testing-library/react'

import { installHighlightApiStub, registeredRanges } from '../../test/highlightApiStub'
import { renderWithProviders } from '../../test/helpers'
import { SEARCH_HL_MATCH } from '../../utils/domHighlight'

const mocks = vi.hoisted(() => ({ chatHistoryAround: vi.fn() }))

vi.mock('../../api/client', async (importOriginal) => {
  const real = await importOriginal<typeof import('../../api/client')>()
  return {
    ...real,
    api: new Proxy(real.api, {
      get(target, prop, receiver) {
        if (prop === 'chatHistoryAround') return mocks.chatHistoryAround
        return Reflect.get(target, prop, receiver)
      },
    }),
  }
})

import type { ChatHistoryHit } from '../../api/client'
import ChatHistoryPreview from './ChatHistoryPreview'

const rows = (n: number) => ({
  messages: [
    { role: 'user', content: `plum question ${n}`, cls: 'msg msg-u', ts: `2026-11-20T10:00:0${n}Z`, meta: { mid: `q-${n}` } },
    { role: 'assistant', content: `the plum answer ${n}`, cls: 'msg msg-a', ts: `2026-11-20T10:00:1${n}Z`, meta: { mid: `a-${n}` } },
  ],
  start: 0,
  end: 2,
  total: 2,
  has_earlier: false,
})

const hit = (n: number): ChatHistoryHit => ({ index: 1, role: 'assistant', ts: `2026-11-20T10:00:1${n}Z`, mid: `a-${n}`, snippet: '' })

installHighlightApiStub()
beforeEach(() => {
  mocks.chatHistoryAround.mockReset().mockImplementation((_slot: string, _s: number, _e: number, _sig: unknown, mid?: string) => Promise.resolve(rows(Number(mid?.slice(2) ?? 1))))
})
afterEach(() => {
  vi.useRealTimers()
  cleanup()
})

const preview = (n: number, query = 'plum') => (
  <ChatHistoryPreview key={n} slotKey="chat-7" hit={hit(n)} query={query} onBack={vi.fn()} onHandoff={vi.fn()} />
)
const target = () => screen.getByTestId('chat-history-preview').querySelector<HTMLElement>('[data-jump-target]')
const filled = () => registeredRanges(SEARCH_HL_MATCH).map(r => r.toString())

describe('ChatHistoryPreview marks', () => {
  it('rings the hit bubble on its edge with no fill, fills the words, and both stay as time passes', async () => {
    // Faked from the start, so a timer the preview set while it drew would fire
    // in the jump below. Real time still runs, so the reads resolve.
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const view = renderWithProviders(preview(1))
    await waitFor(() => expect(target()).not.toBeNull())
    // The matched words get the chat find's own fill, in every row shown.
    await waitFor(() => expect(filled()).toEqual(['plum', 'plum']))
    expect(screen.queryByTestId('chat-history-match-outline')).toBeNull()
    const scroller = screen.getByTestId('chat-history-preview')
    // The ring is on the hit's own bubble edge, drawn inside it, so nothing
    // that clips the row can cut it and ringing moves nothing.
    expect(target()?.querySelector('.message-bubble')).not.toBeNull()
    for (const c of ['outline-2', 'outline-accent', '-outline-offset-2']) {
      expect(scroller.className).toContain(`[&_[data-jump-target]_.message-bubble]:${c}`)
    }
    // A reply's text has a user bubble's padding and radius, unfilled, on every row.
    for (const c of ['px-4', 'py-2', 'rounded-xl']) {
      expect(scroller.className).toContain(`[&_.message-bubble:not(.user-bubble)]:${c}`)
    }
    // A row with no bubble is ringed itself.
    expect(scroller.className).toContain('[&_[data-jump-target]:not(:has(.message-bubble))]:outline-2')
    // The transcript's landing flash would fade it; the preview turns it off.
    expect(scroller.className).toContain('[&_[data-jump-target]]:animate-none')
    expect(scroller.className).not.toMatch(/\]:bg-|\]:(?:m|-m)[xy]?-/)
    expect(target()).toHaveTextContent('the plum answer 1')

    // Well past the transcript's 5s landing fade and any other flash.
    act(() => { vi.advanceTimersByTime(60_000) })
    expect(target()).toHaveTextContent('the plum answer 1')
    expect(filled()).toEqual(['plum', 'plum'])
    vi.useRealTimers()

    // Another result: its own row is the one ringed.
    view.rerender(preview(2))
    await waitFor(() => expect(target()).toHaveTextContent('the plum answer 2'))
    expect(screen.queryByText('the plum answer 1')).toBeNull()
  })

  it('fills no words once the query is cleared, and nothing once the preview closes', async () => {
    const view = renderWithProviders(preview(1))
    await waitFor(() => expect(filled()).toEqual(['plum', 'plum']))
    view.rerender(preview(1, ''))
    await waitFor(() => expect(filled()).toEqual([]))
    view.unmount()
    expect(screen.queryByTestId('chat-history-preview')).toBeNull()
  })
})
