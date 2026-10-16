/**
 * A failed MCP sign-in row inside ChatEmbed, through the REAL ChatMessageList,
 * renderer registry and McpOAuthBanner.
 *
 * The "Ask the agent" hand-off navigates to the main chat, which unmounts the
 * embed and drops its unsaved composer draft, so the embed mounts its list with
 * `handOff={false}`. The mcp_oauth renderer must honour that switch like the
 * tool rows do; the banner's own default (on) is what every other host keeps.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, act } from '@testing-library/react'
import React from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ChatMessage } from '../types'
import { renderMcpOAuthMessage } from '../pages/chat/McpOAuthBanner'

const mockGet = vi.fn()
const mockPost = vi.fn()

vi.mock('../app-sdk/index', () => ({
  useAppApi: () => ({ get: mockGet, post: mockPost }),
  useAppInfo: () => ({ name: 'test-app', version: '0', permissions: { api: ['/api/chat'], events: [] } }),
}))

import ChatEmbed from '../app-sdk/ChatEmbed'

const FAILED_SIGN_IN: ChatMessage = {
  role: 'mcp_oauth',
  content: '',
  cls: '',
  ts: '1',
  meta: { server_name: 'zzq-server', failed: true, error: 'token exchange refused' },
}

let queryClient: QueryClient

async function settleUntil(predicate: () => boolean, maxTicks = 50) {
  for (let i = 0; i < maxTicks && !predicate(); i++) {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10)
    })
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.useFakeTimers()
  Element.prototype.scrollIntoView = vi.fn()
  mockGet.mockResolvedValue({ messages: [FAILED_SIGN_IN], running: false, title: '' })
  mockPost.mockResolvedValue({})
  queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
})

afterEach(() => {
  vi.restoreAllMocks()
  vi.useRealTimers()
})

describe('ChatEmbed failed MCP sign-in row', () => {
  it('offers no "Ask the agent" hand-off inside the embed', async () => {
    await act(async () => {
      render(
        React.createElement(
          QueryClientProvider,
          { client: queryClient },
          <ChatEmbed slotKey="slot-1" />,
        ),
      )
      await vi.advanceTimersByTimeAsync(0)
    })
    await settleUntil(() => screen.queryByText(/token exchange refused/) !== null)

    // Non-vacuity: the failed banner itself is on screen, only its hand-off is gone.
    expect(screen.getByText(/authentication failed: token exchange refused/i)).toBeInTheDocument()
    expect(screen.getByText('zzq-server')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /ask the agent/i })).toBeNull()
  })

  it('keeps the hand-off on the failed banner for a host that leaves it on', () => {
    render(<>{renderMcpOAuthMessage(FAILED_SIGN_IN)}</>)
    expect(screen.getByText(/authentication failed: token exchange refused/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /ask the agent/i })).toBeInTheDocument()
  })
})
