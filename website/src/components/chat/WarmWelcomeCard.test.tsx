import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { Provider } from 'react-redux'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ChatMessage } from '../../types'
import type { RootState } from '../../store'
import dashboardReducer from '../../store/dashboardSlice'

vi.mock('../MarkdownRenderer', async (orig) => ({
  ...(await orig<typeof import('../MarkdownRenderer')>()),
  default: ({ content }: { content: string }) => <span data-testid="md">{content}</span>,
}))

import WarmWelcomeCard, { warmWelcomeOf, warmWelcomeVisibleText } from './WarmWelcomeCard'

const row = (brief: Record<string, unknown>): ChatMessage =>
  ({ role: 'assistant', content: '', meta: { kind: 'warm_task_brief_result', brief } }) as unknown as ChatMessage

const ITEM = 'it_00000001'
const BRIEF = {
  version: 4,
  count_line: 'Waiting on you: 1',
  framing: '',
  cards: [{
    kind: 'item',
    item_id: ITEM,
    checked: 'checked 06:01',
    title: 'Pick the layout',
    tags: [{ text: 'conflicts', bad: true }, { text: 'open', bad: false }],
    pr: { label: 'acme/w#7', url: 'https://github.com/acme/w/pull/7' },
    read: 'GitHub 06:00',
    said: {
      by: 'Suggested 05:30', why: 'Only you know.', pick: 'One column', pick_why: 'fits 320 px', pick_label: 'Recommended',
      options: [{ label: 'Two columns', outcome: 'wide screens' }, { label: 'One column', outcome: '' }],
    },
  }],
  changes_title: 'Since you left',
  changes: [{ text: 'Item 2', tags: [{ text: 'blocked', bad: true }], source: 'worker report 05:02' }],
  more: '+2 more on the Board',
  checks: [],
  board: { text: 'Board: 1 waiting on you', link: 'Open the Board →', conductor: 'crew-a' },
  body: '',
}
const SLOT = 'crew-a'

function setup(brief: Record<string, unknown> = BRIEF) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const store = configureStore({
    reducer: { dashboard: dashboardReducer },
    preloadedState: { dashboard: { slots: [] } as unknown as RootState['dashboard'] },
  })
  return render(
    <Provider store={store}><QueryClientProvider client={qc}><MemoryRouter><WarmWelcomeCard welcome={warmWelcomeOf(row(brief))!} slotKey={SLOT} /></MemoryRouter></QueryClientProvider></Provider>,
  )
}

describe('WarmWelcomeCard', () => {
  it('reads only a version 4 welcome', () => {
    expect(warmWelcomeOf(row({ ...BRIEF, version: 3 }))).toBeNull()
    expect(warmWelcomeOf(row({ ...BRIEF, count_line: '' }))).toBeNull()
    expect(warmWelcomeOf(row(BRIEF))?.cards[0].itemId).toBe(ITEM)
  })

  it('draws a PR pill only for a GitHub pull-request URL', () => {
    const bad = { ...BRIEF, cards: [{ ...BRIEF.cards[0], pr: { label: 'x', url: 'https://evil.example/pull/7' } }] }
    expect(warmWelcomeOf(row(bad))?.cards[0].pr).toBeNull()
  })

  it("shows the gateway's facts and the conductor's words with their time", () => {
    setup()
    expect(screen.getByTestId('warm-welcome-count')).toHaveTextContent('Waiting on you: 1Pick the layout')
    expect(screen.getByText('conflicts')).toHaveAttribute('data-bad', 'true')
    expect(screen.getByText(/Suggested 05:30/)).toBeInTheDocument()
    expect(screen.getByTestId('warm-welcome-changes')).toHaveTextContent('+2 more on the Board')
    expect(screen.getByRole('link', { name: 'Open the Board →' })).toHaveAttribute('href', '/crew-board?conductor=crew-a')
    expect(warmWelcomeVisibleText(warmWelcomeOf(row(BRIEF))!)).toContain('One column (Recommended)')
  })

  it('draws the conductor options as plain text rows, never as buttons', () => {
    setup()
    const rows = Array.from(document.querySelectorAll('.ww-options li'))
    expect(rows.map((r) => r.textContent)).toEqual(['Two columns: wide screens', 'One column (Recommended)'])
    for (const r of rows) {
      expect(r.querySelector('button, a, [role="button"]')).toBeNull()
      expect(r.className).toBe('')
    }
  })

  it('the search text holds each shown label exactly as often as the card draws it', () => {
    const noWhy = { ...BRIEF, cards: [{ ...BRIEF.cards[0], said: { ...BRIEF.cards[0].said, pick_why: '' } }] }
    setup(noWhy)
    const drawn = (document.querySelector('[data-testid="warm-welcome"]')?.textContent ?? '').split('One column').length - 1
    const indexed = warmWelcomeVisibleText(warmWelcomeOf(row(noWhy))!).split('One column').length - 1
    expect(indexed).toBe(drawn)
    expect(indexed).toBe(1)
  })

  it('a card is static once posted and says when it was checked', () => {
    setup()
    expect(screen.getByTestId('warm-welcome-checked')).toHaveTextContent('checked 06:01')
    expect(warmWelcomeVisibleText(warmWelcomeOf(row(BRIEF))!)).toContain('checked 06:01')
  })
})
