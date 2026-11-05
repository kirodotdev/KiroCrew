/**
 * ChatEmbed hands a host's `renderers` to its transcript: an entry claiming a
 * row draws it, and every row the entry does not claim keeps its built-in
 * rendering. The host entry here is the documented restyling pattern: it
 * claims exactly the rows the built-in assistant entry draws (so the live
 * streaming tail stays visible and a compaction notice keeps its card) and
 * wraps the built-in bubble. The REAL ChatMessageList and registry render;
 * only the app SDK's api hooks (the slot poll) are stubbed.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, act, waitFor } from '@testing-library/react'
import React from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import {
  defaultMessageRenderers,
  resolveRenderer,
  type MessageRenderer,
} from '../app-sdk/messageRenderers'

const mockGet = vi.fn()
const mockPost = vi.fn()

vi.mock('../app-sdk/index', () => ({
  useAppApi: () => ({ get: mockGet, post: mockPost }),
  useAppInfo: () => ({ name: 'test-app', version: '0', permissions: { api: ['/api/chat'], events: [] } }),
}))

import ChatEmbed from '../app-sdk/ChatEmbed'

const SETTLED = {
  running: false,
  title: 'Embed',
  messages: [
    { role: 'user', content: 'What is the plan?', cls: '', ts: '2026-01-01T00:00:01Z' },
    { role: 'assistant', kind: 'compaction', content: 'Context summary body', cls: '', ts: '2026-01-01T00:00:02Z' },
    { role: 'assistant', content: 'Ship the small fix first.', cls: '', ts: '2026-01-01T00:00:03Z' },
  ],
}

const STREAMING = {
  running: true,
  title: 'Embed',
  messages: [
    { role: 'user', content: 'What is the plan?', cls: '', ts: '2026-01-01T00:00:01Z' },
    { role: 'streaming', content: 'Still writing the reply', cls: '', ts: '2026-01-01T00:00:02Z' },
  ],
}

const BUILTIN_ASSISTANT = defaultMessageRenderers.find(r => r.id === 'assistant')!

// Module constant: the list memoizes the merged registry on this identity.
const RENDERERS: readonly MessageRenderer[] = [{
  id: 'host-assistant',
  roles: BUILTIN_ASSISTANT.roles,
  match: m => resolveRenderer(m, defaultMessageRenderers) === BUILTIN_ASSISTANT,
  render: (m, ctx) => BUILTIN_ASSISTANT.render(m, {
    ...ctx,
    wrapper: (children, isUser) => ctx.wrapper(
      <div data-testid="host-bubble">
        <span data-testid="host-avatar">A</span>
        {children}
      </div>,
      isUser,
    ),
  }),
}]

beforeEach(() => {
  vi.clearAllMocks()
  Element.prototype.scrollIntoView = vi.fn()
  mockPost.mockResolvedValue({})
})

async function mount(ui: React.ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  await act(async () => {
    render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>)
  })
  await waitFor(() => expect(screen.getByText('What is the plan?')).toBeTruthy())
}

describe('ChatEmbed renderers', () => {
  it('draws the claimed reply through the host entry and keeps the built-in rows for the rest', async () => {
    mockGet.mockResolvedValue(SETTLED)
    await mount(<ChatEmbed slotKey="s" renderers={RENDERERS} />)
    const bubbles = await screen.findAllByTestId('host-bubble')
    expect(bubbles).toHaveLength(1)
    expect(bubbles[0].textContent).toContain('Ship the small fix first.')
    // The user row and the compaction notice are not swallowed by the host entry.
    expect(screen.getByText('What is the plan?').closest('[data-testid="host-bubble"]')).toBeNull()
    expect(screen.queryByText('Context summary body')?.closest('[data-testid="host-bubble"]') ?? null).toBeNull()
  })

  it('still shows the live streaming reply, through the host entry', async () => {
    mockGet.mockResolvedValue(STREAMING)
    await mount(<ChatEmbed slotKey="s" renderers={RENDERERS} />)
    const bubble = await screen.findByTestId('host-bubble')
    expect(bubble.textContent).toContain('Still writing the reply')
  })

  it('renders the built-in reply row when no renderers are passed', async () => {
    mockGet.mockResolvedValue(SETTLED)
    await mount(<ChatEmbed slotKey="s" />)
    await waitFor(() => expect(screen.getByText('Ship the small fix first.')).toBeTruthy())
    expect(screen.queryByTestId('host-bubble')).toBeNull()
  })
})
