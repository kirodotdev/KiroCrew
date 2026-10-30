/**
 * A message one crewmate sent to another, drawn in both threads: the row reads
 * its reference off the card meta, names the other crewmate with the direction,
 * shows the text as plain text, and a malformed row draws nothing.
 */
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { Provider } from 'react-redux'
import { store as reduxStore } from '../store'
import '../api/client'
import { i18nT } from '../i18n/t'
import ConversationCard, { readPeerMessageRef } from './ConversationCard'
import type { ChatMessage } from '../types'

const SLOT = 'dm-natey'

function row(card: Record<string, unknown>): ChatMessage {
  return {
    role: 'card',
    content: 'From mate: Can you check the build?',
    cls: 'msg msg-card',
    ts: '2026-10-30T10:00:00Z',
    meta: {
      mid: 'm-1',
      card: { surface: 'peer', id: 'p1', slot: SLOT, direction: 'in', peer: 'mate', text: 'Can you check the build?', hop: 1, ...card },
    },
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
  it('reads a peer row and nothing else', () => {
    expect(readPeerMessageRef(row({}))).toEqual({
      surface: 'peer', id: 'p1', slot: SLOT, direction: 'in', peer: 'mate', text: 'Can you check the build?', hop: 1,
    })
    expect(readPeerMessageRef(row({ direction: 'sideways' }))).toBeNull()
    expect(readPeerMessageRef(row({ peer: '' }))).toBeNull()
    expect(readPeerMessageRef(row({ text: '' }))).toBeNull()
    expect(readPeerMessageRef({ role: 'user', meta: row({}).meta })).toBeNull()
    expect(readPeerMessageRef({ role: 'card', meta: { card: { surface: 'lesson', id: 'x', rule: 'r' } } })).toBeNull()
  })
})

describe('drawing the row', () => {
  it('names the sender in the target thread and the target in the sender thread', () => {
    const { unmount } = renderRow(row({}))
    expect(screen.getByTestId('peer-message-row').getAttribute('data-peer-direction')).toBe('in')
    expect(screen.getByTestId('peer-message-label').textContent).toBe(i18nT('components.peerMessage.from', { name: 'mate' }))
    unmount()
    renderRow(row({ direction: 'out', peer: 'natey' }))
    expect(screen.getByTestId('peer-message-label').textContent).toBe(i18nT('components.peerMessage.to', { name: 'natey' }))
  })

  it('shows the text as plain text, not as markup', () => {
    renderRow(row({ text: '<b>bold</b>\n**not markdown**' }))
    const text = screen.getByTestId('peer-message-text')
    expect(text.textContent).toBe('<b>bold</b>\n**not markdown**')
    expect(text.querySelector('b')).toBeNull()
  })

  it('draws nothing for a malformed row', () => {
    const { container } = renderRow(row({ id: '' }))
    expect(container.querySelector('[data-testid="peer-message-row"]')).toBeNull()
  })
})
