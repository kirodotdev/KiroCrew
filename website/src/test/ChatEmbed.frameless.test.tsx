import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, act } from '@testing-library/react'
import React from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const mockGet = vi.fn()
const mockPost = vi.fn()

vi.mock('../app-sdk/index', () => ({
  useAppApi: () => ({ get: mockGet, post: mockPost }),
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
    // The title strip shows title || slotKey — present when NOT frameless.
    expect(screen.getByText('my-slot')).toBeInTheDocument()
    // Input still renders.
    expect(screen.getByLabelText('Chat message')).toBeInTheDocument()
  })

  it('hides the title strip when frameless is set', async () => {
    await act(async () => {
      renderWithProviders(<ChatEmbed slotKey="my-slot" frameless />)
    })
    // No title strip → slotKey text is absent, but the chat + input still render.
    expect(screen.queryByText('my-slot')).toBeNull()
    expect(screen.getByLabelText('Chat message')).toBeInTheDocument()
    expect(screen.getByTestId('chat-message-list')).toBeInTheDocument()
  })
})
