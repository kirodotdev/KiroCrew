import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, act } from '@testing-library/react'
import React from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const mockGet = vi.fn()
const mockPost = vi.fn()

vi.mock('../app-sdk/index', () => ({
  useAppApi: () => ({ get: mockGet, post: mockPost }),
  useAppInfo: () => ({ name: 'test-app', version: '0', permissions: { api: ['/api/chat'], events: [] } }),
}))

vi.mock('../app-sdk/ChatMessageList', () => ({
  default: ({ transcript }: { transcript?: { aboveRows?: React.ReactNode } }) => (
    <div data-testid="chat-message-list">{transcript?.aboveRows}</div>
  ),
}))

import ChatEmbed from '../app-sdk/ChatEmbed'

let queryClient: QueryClient

function wrap(ui: React.ReactElement) {
  return React.createElement(QueryClientProvider, { client: queryClient }, ui)
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((r) => { resolve = r })
  return { promise, resolve }
}

const EARLIER = [{ role: 'user', content: 'Earlier turn', cls: '' }]
const READY = 'Session ready. Type a message to start.'

/** React Query delivers a settled read through its notification timer. */
async function settle() {
  await act(async () => { await new Promise((r) => setTimeout(r, 0)) })
}

async function mount(ui: React.ReactElement) {
  const view = await act(async () => render(wrap(ui)))
  await settle()
  return view
}

async function refetch() {
  await act(async () => { await queryClient.refetchQueries() })
  await settle()
}

beforeEach(() => {
  vi.restoreAllMocks()
  vi.clearAllMocks()
  Element.prototype.scrollIntoView = vi.fn()
  mockPost.mockResolvedValue({})
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
})

afterEach(() => {
  queryClient.clear()
  vi.restoreAllMocks()
})

describe('ChatEmbed header title', () => {
  it('does not call a session new while its first read is pending, then shows the title in place', async () => {
    const first = deferred<unknown>()
    mockGet.mockReturnValueOnce(first.promise)
    await mount(<ChatEmbed slotKey="coder-abc123" />)

    const header = screen.getByTestId('chat-embed-title')
    expect(header).toHaveTextContent(/^Loading session…$/)
    expect(header).toHaveAttribute('aria-busy', 'true')
    expect(header).toHaveAttribute('title', 'coder-abc123')
    expect(screen.queryByText('New session')).toBeNull()
    expect(screen.queryByText('coder-abc123')).toBeNull()

    await act(async () => { first.resolve({ messages: EARLIER, running: false, title: 'Trip planning' }) })
    await settle()
    expect(screen.getByTestId('chat-embed-title')).toBe(header)
    expect(header).toHaveTextContent(/^Trip planning$/)
    expect(header).not.toHaveAttribute('aria-busy')
  })

  it('reads "New session" for a loaded untitled session with no history', async () => {
    mockGet.mockResolvedValue({ messages: [], running: false })
    await mount(<ChatEmbed slotKey="coder-fresh" />)
    expect(screen.getByTestId('chat-embed-title')).toHaveTextContent(/^New session$/)
  })

  it.each([
    { name: 'rows on the page', page: { messages: EARLIER } },
    { name: 'only older rows beyond the page', page: { messages: [], has_more: true } },
  ])('reads "Untitled session" for an untitled session with $name', async ({ page }) => {
    mockGet.mockResolvedValue({ ...page, running: false, title: '' })
    await mount(<ChatEmbed slotKey="coder-resumed" />)
    const header = screen.getByTestId('chat-embed-title')
    expect(header).toHaveTextContent(/^Untitled session$/)
    expect(screen.queryByText('New session')).toBeNull()
    expect(screen.queryByText('coder-resumed')).toBeNull()
  })

  it('replaces "Untitled session" with a later title without remounting', async () => {
    mockGet.mockResolvedValue({ messages: EARLIER, running: false })
    await mount(<ChatEmbed slotKey="coder-resumed" />)
    const header = screen.getByTestId('chat-embed-title')
    expect(header).toHaveTextContent(/^Untitled session$/)

    mockGet.mockResolvedValue({ messages: EARLIER, running: false, title: 'Trip planning' })
    await refetch()
    expect(screen.getByTestId('chat-embed-title')).toBe(header)
    expect(header).toHaveTextContent(/^Trip planning$/)
  })

  it('reads "Untitled session" once a fresh session has messages, then takes its title in place', async () => {
    mockGet.mockResolvedValue({ messages: [], running: false })
    await mount(<ChatEmbed slotKey="coder-fresh" />)
    const header = screen.getByTestId('chat-embed-title')
    expect(header).toHaveTextContent(/^New session$/)

    mockGet.mockResolvedValue({ messages: EARLIER, running: true })
    await refetch()
    expect(header).toHaveTextContent(/^Untitled session$/)

    mockGet.mockResolvedValue({ messages: EARLIER, running: false, title: 'Trip planning' })
    await refetch()
    expect(screen.getByTestId('chat-embed-title')).toBe(header)
    expect(header).toHaveTextContent(/^Trip planning$/)
  })

  it('shows the ready line only once an empty page has loaded', async () => {
    const first = deferred<unknown>()
    mockGet.mockReturnValueOnce(first.promise)
    await mount(<ChatEmbed slotKey="coder-fresh" />)
    expect(screen.getByTestId('chat-embed-title')).toHaveTextContent(/^Loading session…$/)
    expect(screen.queryByText(READY)).toBeNull()

    await act(async () => { first.resolve({ messages: [], running: false }) })
    await settle()
    expect(screen.getByTestId('chat-embed-title')).toHaveTextContent(/^New session$/)
    expect(screen.getByText(READY)).toBeInTheDocument()
  })

  it('shows the loading label, not the previous title, while a new slot is first read', async () => {
    mockGet.mockResolvedValue({ messages: EARLIER, running: false, title: 'Trip planning' })
    const view = await mount(<ChatEmbed slotKey="coder-one" />)
    expect(screen.getByTestId('chat-embed-title')).toHaveTextContent(/^Trip planning$/)

    const second = deferred<unknown>()
    mockGet.mockReturnValueOnce(second.promise)
    await act(async () => { view.rerender(wrap(<ChatEmbed slotKey="coder-two" />)) })
    await settle()
    const header = screen.getByTestId('chat-embed-title')
    expect(header).toHaveTextContent(/^Loading session…$/)
    expect(header).toHaveAttribute('title', 'coder-two')

    await act(async () => { second.resolve({ messages: EARLIER, running: false }) })
    await settle()
    expect(header).toHaveTextContent(/^Untitled session$/)
  })

  it('uses the age-neutral label when the first read fails', async () => {
    mockGet.mockRejectedValue(new Error('offline'))
    await mount(<ChatEmbed slotKey="coder-gone" />)
    const header = screen.getByTestId('chat-embed-title')
    expect(header).toHaveTextContent(/^Untitled session$/)
    expect(header).not.toHaveAttribute('aria-busy')
  })
})
