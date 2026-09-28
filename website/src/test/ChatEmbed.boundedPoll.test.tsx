/**
 * ChatEmbed's bounded poll (chat-core P5-e): the slot-detail read asks for one
 * page of the newest rows, and the transcript's earlier-history bar widens the
 * page up to the handler's ceiling. Pinned so the unbounded per-second re-read
 * of a whole thread (#10005) cannot come back silently.
 */
import { render, screen, fireEvent, act, waitFor } from '@testing-library/react'
import React from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const mockGet = vi.fn()
const mockPost = vi.fn()

vi.mock('../app-sdk/index', () => ({
  useAppApi: () => ({ get: mockGet, post: mockPost }),
}))

interface MockListProps {
  messages: unknown[]
  transcript?: {
    sessionId: string
    followOutput?: boolean
    initialPlacement?: 'top' | 'bottom'
    earlier?: { hasMore: boolean; loading: boolean; failed: boolean; onLoad: () => void; handOff?: boolean }
    aboveRows?: React.ReactNode
  }
}

// The list is mocked down to the wiring this embed hands it: the paging bar's
// state and the mount identity. The real component is covered by its own tests.
vi.mock('../app-sdk/ChatMessageList', () => ({
  default: ({ messages, transcript }: MockListProps) => (
    <div
      data-testid="chat-message-list"
      data-count={messages.length}
      data-session={transcript?.sessionId}
      data-follow={String(transcript?.followOutput)}
      data-placement={transcript?.initialPlacement}
      data-handoff={String(transcript?.earlier?.handOff)}
    >
      {transcript?.aboveRows}
      {transcript?.earlier?.hasMore && (
        <button
          data-testid="load-earlier-messages"
          aria-busy={transcript.earlier.loading}
          data-failed={String(transcript.earlier.failed)}
          onClick={() => transcript.earlier!.onLoad()}
        >
          earlier
        </button>
      )}
    </div>
  ),
}))

import ChatEmbed, { EMBED_PAGE_LIMIT, EMBED_PAGE_LIMIT_MAX } from '../app-sdk/ChatEmbed'

let queryClient: QueryClient

function renderEmbed(ui: React.ReactElement) {
  return render(React.createElement(QueryClientProvider, { client: queryClient }, ui))
}

const url = (slot: string, limit: number) => '/api/chat/slots/' + encodeURIComponent(slot) + '?limit=' + limit

/** Let React Query settle a resolved fetch into state. */
const flush = () => act(async () => { await Promise.resolve(); await Promise.resolve() })

beforeEach(() => {
  vi.restoreAllMocks()
  vi.clearAllMocks()
  mockGet.mockResolvedValue({ messages: [], running: false, title: '', has_more: false })
  mockPost.mockResolvedValue({})
  queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
})

describe('ChatEmbed bounded poll', () => {
  it('reads one page of the newest rows, never the whole slot', async () => {
    await act(async () => { renderEmbed(<ChatEmbed slotKey="slot-1" />) })
    expect(mockGet).toHaveBeenCalledWith(url('slot-1', EMBED_PAGE_LIMIT))
    expect(mockGet).not.toHaveBeenCalledWith('/api/chat/slots/slot-1')
  })

  it('offers to load earlier only while the server reports more, and widens the page per press', async () => {
    mockGet.mockResolvedValue({ messages: [], running: false, title: '', has_more: true })
    await act(async () => { renderEmbed(<ChatEmbed slotKey="slot-1" />) })

    const bar = await screen.findByTestId('load-earlier-messages')
    await act(async () => { fireEvent.click(bar) })
    expect(mockGet).toHaveBeenCalledWith(url('slot-1', EMBED_PAGE_LIMIT * 2))

    await flush()
    const again = await screen.findByTestId('load-earlier-messages')
    await act(async () => { fireEvent.click(again) })
    expect(mockGet).toHaveBeenCalledWith(url('slot-1', EMBED_PAGE_LIMIT_MAX))

    // At the handler's ceiling a wider ask would be clamped to the same page,
    // so the bar stops offering even though the server still has older rows.
    await act(async () => { await Promise.resolve() })
    expect(screen.queryByTestId('load-earlier-messages')).toBeNull()
  })

  it('shows no earlier bar when the page already holds the whole history', async () => {
    mockGet.mockResolvedValue({ messages: [], running: false, title: '', has_more: false })
    await act(async () => { renderEmbed(<ChatEmbed slotKey="slot-1" />) })
    await act(async () => { await Promise.resolve() })
    expect(screen.queryByTestId('load-earlier-messages')).toBeNull()
  })

  it('starts a new slot over at one page', async () => {
    mockGet.mockResolvedValue({ messages: [], running: false, title: '', has_more: true })
    let view!: ReturnType<typeof renderEmbed>
    await act(async () => { view = renderEmbed(<ChatEmbed slotKey="slot-1" />) })
    const { rerender } = view
    await flush()
    const bar = await screen.findByTestId('load-earlier-messages')
    await act(async () => { fireEvent.click(bar) })
    expect(mockGet).toHaveBeenCalledWith(url('slot-1', EMBED_PAGE_LIMIT * 2))

    mockGet.mockClear()
    await act(async () => {
      rerender(React.createElement(QueryClientProvider, { client: queryClient }, <ChatEmbed slotKey="slot-2" />))
    })
    expect(mockGet).toHaveBeenCalledWith(url('slot-2', EMBED_PAGE_LIMIT))
    expect(mockGet).not.toHaveBeenCalledWith(url('slot-2', EMBED_PAGE_LIMIT * 2))
  })

  it('keeps the settled page on screen when a widen fails, and Retry re-reads that limit', async () => {
    const page = { messages: [{ role: 'user', content: 'hi', cls: '' }], running: false, title: '', has_more: true }
    mockGet.mockImplementation((u: string) => (u.endsWith('?limit=200') ? Promise.resolve(page) : Promise.reject(new Error('boom'))))
    await act(async () => { renderEmbed(<ChatEmbed slotKey="slot-1" />) })
    // The bar mounts only once the first page has settled, so waiting for it
    // is waiting for the read — a fixed number of microtask turns is not.
    const first = await screen.findByTestId('load-earlier-messages')
    expect(screen.getByTestId('chat-message-list').getAttribute('data-count')).toBe('1')

    await act(async () => { fireEvent.click(first) })
    // The narrower page is still the transcript; the bar reports the failure.
    await waitFor(() => expect(screen.getByTestId('load-earlier-messages').getAttribute('data-failed')).toBe('true'))
    expect(screen.getByTestId('chat-message-list').getAttribute('data-count')).toBe('1')
    const bar = screen.getByTestId('load-earlier-messages')

    mockGet.mockClear()
    await act(async () => { fireEvent.click(bar) })
    expect(mockGet).toHaveBeenCalledWith(url('slot-1', EMBED_PAGE_LIMIT * 2))
    expect(mockGet).not.toHaveBeenCalledWith(url('slot-1', EMBED_PAGE_LIMIT_MAX))
  })

  it('reports a failed first read as an error with retry, not as an empty session', async () => {
    mockGet.mockRejectedValue(new Error('down'))
    await act(async () => { renderEmbed(<ChatEmbed slotKey="slot-1" />) })
    await flush()
    expect(await screen.findByTestId('chat-embed-load-error')).toBeInTheDocument()
    expect(screen.queryByText('Session ready. Type a message to start.')).toBeNull()
    mockGet.mockClear()
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Retry' })) })
    expect(mockGet).toHaveBeenCalledWith(url('slot-1', EMBED_PAGE_LIMIT))
  })

  it('turns the history bar\'s ask-the-agent hand-off off: the embed draft is unsaved local state', async () => {
    mockGet.mockResolvedValue({ messages: [], running: false, title: '', has_more: true })
    await act(async () => { renderEmbed(<ChatEmbed slotKey="slot-1" />) })
    await flush()
    expect(screen.getByTestId('chat-message-list').getAttribute('data-handoff')).toBe('false')
  })

  it('mounts the transcript per mode: pinned-at-bottom for startAtBottom, top-anchored otherwise', async () => {
    await act(async () => { renderEmbed(<ChatEmbed slotKey="slot-1" startAtBottom />) })
    let list = screen.getByTestId('chat-message-list')
    expect(list.getAttribute('data-session')).toBe('embed:slot-1')
    expect(list.getAttribute('data-follow')).toBe('true')
    expect(list.getAttribute('data-placement')).toBe('bottom')

    await act(async () => { renderEmbed(<ChatEmbed slotKey="slot-3" />) })
    list = screen.getAllByTestId('chat-message-list').at(-1)!
    expect(list.getAttribute('data-follow')).toBe('false')
    expect(list.getAttribute('data-placement')).toBe('top')
  })
})
