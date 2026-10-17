/**
 * A lesson the agent saved, drawn as one quiet row with a link to the Memory
 * page: the row reads its reference off the card meta, words an enrichment as
 * "updated", and a malformed row draws nothing.
 */
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { Provider } from 'react-redux'
import { store as reduxStore } from '../store'
import '../api/client'
import { i18nT } from '../i18n/t'
import ConversationCard, { readLessonSavedRef } from './ConversationCard'
import { LESSONS_ROUTE } from './LessonSavedRow'
import type { ChatMessage } from '../types'

const SLOT = 'chat-1'

function row(card: Record<string, unknown>): ChatMessage {
  return {
    role: 'card',
    content: 'Lesson saved: always use pnpm',
    cls: 'msg msg-card',
    ts: '2026-10-14T10:00:00Z',
    meta: { mid: 'm-1', card: { surface: 'lesson', id: 'l1', slot: SLOT, rule: 'always use pnpm', status: 'inserted', ...card } },
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
  it('reads a lesson row and nothing else', () => {
    expect(readLessonSavedRef(row({}))).toEqual({ surface: 'lesson', id: 'l1', slot: SLOT, rule: 'always use pnpm', status: 'saved' })
    expect(readLessonSavedRef(row({ status: 'enriched' }))?.status).toBe('updated')
    expect(readLessonSavedRef({ role: 'card', meta: { card: { surface: 'self', id: 'x', rule: 'r' } } })).toBeNull()
    expect(readLessonSavedRef({ role: 'notice', meta: { card: { surface: 'lesson', id: 'x', rule: 'r' } } })).toBeNull()
    expect(readLessonSavedRef(row({ rule: '' }))).toBeNull()
    expect(readLessonSavedRef(row({ id: '' }))).toBeNull()
  })
})

describe('the row', () => {
  it('names the saved rule and links to the lessons', () => {
    renderRow(row({}))
    const el = screen.getByTestId('lesson-saved-row')
    expect(el).toHaveAttribute('data-lesson-status', 'saved')
    expect(screen.getByTestId('lesson-saved-text')).toHaveTextContent(
      i18nT('components.lessonSaved.saved', { rule: 'always use pnpm' }),
    )
    const link = screen.getByTestId('lesson-saved-review')
    expect(link).toHaveTextContent(i18nT('components.lessonSaved.review'))
    expect(link).toHaveAttribute('href', LESSONS_ROUTE)
  })

  it('words an enrichment as an update', () => {
    renderRow(row({ status: 'enriched' }))
    expect(screen.getByTestId('lesson-saved-row')).toHaveAttribute('data-lesson-status', 'updated')
    expect(screen.getByTestId('lesson-saved-text')).toHaveTextContent(
      i18nT('components.lessonSaved.updated', { rule: 'always use pnpm' }),
    )
  })

  it('draws nothing for a malformed lesson row', () => {
    renderRow(row({ rule: 42 }))
    expect(screen.queryByTestId('lesson-saved-row')).toBeNull()
  })
})
