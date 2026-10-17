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
  default: () => <div data-testid="chat-message-list" />,
}))

import ChatEmbed from '../app-sdk/ChatEmbed'

let queryClient: QueryClient

function renderWithProviders(ui: React.ReactElement) {
  return render(React.createElement(QueryClientProvider, { client: queryClient }, ui))
}

beforeEach(() => {
  vi.restoreAllMocks()
  Element.prototype.scrollIntoView = vi.fn()
  mockGet.mockResolvedValue({ messages: [], running: false, title: '' })
  mockPost.mockResolvedValue({})
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('ChatEmbed framing', () => {
  it('renders the title strip by default (frameless off)', async () => {
    await act(async () => {
      renderWithProviders(<ChatEmbed slotKey="my-slot" />)
    })
    // The title strip is present when NOT frameless.
    expect(screen.getByTestId('chat-embed-title')).toBeInTheDocument()
    // Input still renders.
    expect(screen.getByLabelText('Chat message')).toBeInTheDocument()
  })

  it('shows "New session" for an untitled slot, keeping the slot key only on hover', async () => {
    await act(async () => {
      renderWithProviders(<ChatEmbed slotKey="coder-abc123" />)
    })
    // React Query delivers the first read through its notification timer.
    await act(async () => { await new Promise((r) => setTimeout(r, 0)) })
    const header = screen.getByTestId('chat-embed-title')
    expect(header).toHaveTextContent('New session')
    expect(header).toHaveAttribute('title', 'coder-abc123')
    // The internal id never appears as visible text.
    expect(screen.queryByText('coder-abc123')).toBeNull()
  })

  it('hides the title strip when frameless is set', async () => {
    await act(async () => {
      renderWithProviders(<ChatEmbed slotKey="my-slot" frameless />)
    })
    // No title strip, but the chat + input still render.
    expect(screen.queryByTestId('chat-embed-title')).toBeNull()
    expect(screen.queryByText('New session')).toBeNull()
    expect(screen.getByLabelText('Chat message')).toBeInTheDocument()
    expect(screen.getByTestId('chat-message-list')).toBeInTheDocument()
  })
})
