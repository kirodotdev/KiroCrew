import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ApiError } from '../api/apiError'
import type { ChatBookmark } from '../api/bookmarks'

const list = vi.fn()
const create = vi.fn()
const remove = vi.fn()
vi.mock('../api/bookmarks', () => ({
  bookmarksApi: {
    list: (...a: unknown[]) => list(...a),
    create: (...a: unknown[]) => create(...a),
    remove: (...a: unknown[]) => remove(...a),
  },
}))

import SessionBookmarks from '../pages/chat/SessionBookmarks'

const bookmark = (over: Partial<ChatBookmark> = {}): ChatBookmark => ({
  id: 'b1',
  slot_key: 'dashboard:chat-1',
  url: 'https://github.com/o/r/pull/7',
  title: 'PR 7',
  origin: 'user',
  created_at: '2026-11-04T00:00:00Z',
  ...over,
})

function renderSection() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <SessionBookmarks slotKey="dashboard:chat-1" />
    </QueryClientProvider>,
  )
}

describe('SessionBookmarks', () => {
  beforeEach(() => {
    list.mockReset()
    create.mockReset()
    remove.mockReset()
  })

  it('lists the session bookmarks as links that open in a new tab', async () => {
    list.mockResolvedValue({ bookmarks: [bookmark(), bookmark({ id: 'b2', url: 'https://example.com/doc', title: '', origin: 'agent' })] })
    renderSection()
    const link = await screen.findByRole('link', { name: /PR 7/ })
    expect(link).toHaveAttribute('href', 'https://github.com/o/r/pull/7')
    expect(link).toHaveAttribute('target', '_blank')
    expect(link).toHaveAttribute('rel', 'noopener noreferrer')
    // An untitled bookmark falls back to its host, and an agent one says who added it.
    expect(screen.getByRole('link', { name: /example\.com/ })).toHaveTextContent('Added by the agent')
    expect(list).toHaveBeenCalledWith('dashboard:chat-1')
  })

  it('shows the empty state when the session keeps none', async () => {
    list.mockResolvedValue({ bookmarks: [] })
    renderSection()
    expect(await screen.findByTestId('bookmarks-empty')).toHaveTextContent('No bookmarks yet')
  })

  it('adds a bookmark from the form and refetches', async () => {
    list.mockResolvedValueOnce({ bookmarks: [] }).mockResolvedValue({ bookmarks: [bookmark()] })
    create.mockResolvedValue(bookmark())
    renderSection()
    fireEvent.click(await screen.findByTestId('bookmark-add-open'))
    fireEvent.change(screen.getByLabelText('Bookmark URL'), { target: { value: ' https://github.com/o/r/pull/7 ' } })
    fireEvent.change(screen.getByLabelText('Bookmark title'), { target: { value: 'PR 7' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(create).toHaveBeenCalledWith({
      slot_key: 'dashboard:chat-1', url: 'https://github.com/o/r/pull/7', title: 'PR 7',
    }))
    expect(await screen.findByRole('link', { name: /PR 7/ })).toBeInTheDocument()
    expect(screen.queryByTestId('bookmark-add-form')).not.toBeInTheDocument()
  })

  it('refuses a URL that is not http or https without calling the API', async () => {
    list.mockResolvedValue({ bookmarks: [] })
    renderSection()
    fireEvent.click(await screen.findByTestId('bookmark-add-open'))
    fireEvent.change(screen.getByLabelText('Bookmark URL'), { target: { value: 'javascript:alert(1)' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByText('Enter a full http or https URL.')).toBeInTheDocument()
    expect(create).not.toHaveBeenCalled()
  })

  it('names the server refusal when the session is full', async () => {
    list.mockResolvedValue({ bookmarks: [] })
    create.mockRejectedValue(new ApiError(409, 'full', JSON.stringify({ code: 'bookmark_limit_reached', error: 'full' })))
    renderSection()
    fireEvent.click(await screen.findByTestId('bookmark-add-open'))
    fireEvent.change(screen.getByLabelText('Bookmark URL'), { target: { value: 'https://example.com' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByText(/already keeps 100 bookmarks/)).toBeInTheDocument()
  })

  it('removes a bookmark by id', async () => {
    list.mockResolvedValueOnce({ bookmarks: [bookmark()] }).mockResolvedValue({ bookmarks: [] })
    remove.mockResolvedValue({ ok: true })
    renderSection()
    fireEvent.click(await screen.findByRole('button', { name: 'Remove bookmark PR 7' }))
    await waitFor(() => expect(remove).toHaveBeenCalledWith('b1'))
    expect(await screen.findByTestId('bookmarks-empty')).toBeInTheDocument()
  })
})
