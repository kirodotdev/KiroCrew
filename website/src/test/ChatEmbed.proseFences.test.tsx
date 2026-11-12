/**
 * `ChatEmbed`'s `proseFences` reaches every code block of its transcript
 * through `ProseFencesContext`, so an app's own fence tag wraps there. The
 * transcript is stubbed to read the context it is drawn under.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, act } from '@testing-library/react'
import React, { useContext } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ProseFencesContext } from '../components/codeBlockProse'

const mockGet = vi.fn()

vi.mock('../app-sdk/index', () => ({
  useAppApi: () => ({ get: mockGet, post: vi.fn() }),
  useAppInfo: () => ({ name: 'test-app', version: '0', permissions: { api: ['/api/chat'], events: [] } }),
}))

vi.mock('../app-sdk/ChatMessageList', () => ({
  default: React.forwardRef(function MockList(_props: unknown, ref: React.Ref<unknown>) {
    React.useImperativeHandle(ref, () => ({ scrollToBottom: vi.fn() }), [])
    const tags = useContext(ProseFencesContext)
    return <div data-testid="prose-tags">{[...tags].join(',')}</div>
  }),
}))

import ChatEmbed from '../app-sdk/ChatEmbed'

let queryClient: QueryClient

async function mount(props: Partial<React.ComponentProps<typeof ChatEmbed>> = {}) {
  await act(async () => {
    render(
      <QueryClientProvider client={queryClient}>
        <ChatEmbed slotKey="slot-1" {...props} />
      </QueryClientProvider>,
    )
  })
}

beforeEach(() => {
  vi.useFakeTimers()
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  mockGet.mockResolvedValue({ messages: [], running: false, title: '' })
})

afterEach(() => {
  vi.useRealTimers()
})

describe('ChatEmbed proseFences', () => {
  it("hands the host's tags to the transcript, in lower case", async () => {
    await mount({ proseFences: ['Ticket-Update', 'reply-draft'] })
    expect(screen.getByTestId('prose-tags')).toHaveTextContent('ticket-update,reply-draft')
  })

  it('without the prop the transcript sees no host tags', async () => {
    await mount()
    expect(screen.getByTestId('prose-tags')).toHaveTextContent('')
  })
})
