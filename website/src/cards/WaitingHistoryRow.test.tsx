/**
 * A waiting-on-you ask's history row: the gateway's `waiting` card row is drawn
 * as one quiet line naming the ask and how it came or went, and a malformed row
 * draws nothing.
 */
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { Provider } from 'react-redux'
import { store as reduxStore } from '../store'
import '../api/client'
import { i18nT } from '../i18n/t'
import ConversationCard, { readWaitingHistoryRef } from './ConversationCard'
import type { ChatMessage } from '../types'

const SLOT = 'chat-1'
const TITLE = 'Post the override'

function row(card: Record<string, unknown>): ChatMessage {
  return {
    role: 'card',
    content: `Pinned: ${TITLE}`,
    cls: 'msg msg-card',
    ts: '2026-11-20T10:00:00Z',
    meta: { mid: 'm-1', card: { surface: 'waiting', id: 'wy-0000000000000001', slot: SLOT, key: 'override', title: TITLE, status: 'pinned', ...card } },
  }
}

function renderRow(message: ChatMessage) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <Provider store={reduxStore}>
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <ConversationCard message={message} slot={SLOT} />
        </MemoryRouter>
      </QueryClientProvider>
    </Provider>,
  )
}

describe('reading the row', () => {
  it('reads a waiting row and nothing else', () => {
    expect(readWaitingHistoryRef(row({}))).toEqual({ surface: 'waiting', id: 'wy-0000000000000001', slot: SLOT, title: TITLE, status: 'pinned' })
    expect(readWaitingHistoryRef({ role: 'card', meta: { card: { surface: 'lesson', id: 'x', title: 't', status: 'pinned' } } })).toBeNull()
    expect(readWaitingHistoryRef({ role: 'notice', meta: { card: { surface: 'waiting', id: 'x', title: 't', status: 'pinned' } } })).toBeNull()
    expect(readWaitingHistoryRef(row({ status: 'toString' }))).toBeNull()
    expect(readWaitingHistoryRef(row({ title: '' }))).toBeNull()
    expect(readWaitingHistoryRef(row({ id: '' }))).toBeNull()
  })
})

describe('the row', () => {
  it.each([
    ['pinned', 'components.waitingOnYou.history_pinned'],
    ['answered', 'components.waitingOnYou.history_answered'],
    ['dismissed', 'components.waitingOnYou.history_dismissed'],
    ['cleared', 'components.waitingOnYou.history_withdrawn'],
    ['expired', 'components.waitingOnYou.history_expired'],
    ['retired', 'components.waitingOnYou.history_closed'],
  ])('words a %s ask with its own line', (status, key) => {
    renderRow(row({ status }))
    expect(screen.getByTestId('waiting-history-row')).toHaveAttribute('data-waiting-status', status)
    expect(screen.getByTestId('waiting-history-text')).toHaveTextContent(i18nT(key, { title: TITLE }))
  })

  it('offers nothing to press', () => {
    renderRow(row({ status: 'answered' }))
    const el = screen.getByTestId('waiting-history-row')
    expect(el.querySelector('button, a')).toBeNull()
  })

  it('draws nothing for a malformed waiting row', () => {
    renderRow(row({ title: 42 }))
    expect(screen.queryByTestId('waiting-history-row')).toBeNull()
  })
})
