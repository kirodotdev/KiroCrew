/**
 * The Context tab's prompt-trace read follows Developer Mode in BOTH directions.
 *
 * React Query keeps a query's last error after `enabled` flips false, so a
 * failed poll (a gateway restart) followed by switching Developer Mode off
 * would otherwise leave a permanent error notice about a section that is no
 * longer shown and no longer polled to clear it. The notice is gated on the
 * mode exactly like the data it describes.
 */
import { describe, it, expect, afterEach, vi } from 'vitest'
import { render, screen, cleanup, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

let devMode = true
vi.mock('../hooks/useDevMode', () => ({ useDevMode: () => devMode }))
vi.mock('../api/client', () => ({
  api: {
    telemetryContextTrace: async () => ({
      slot: 'chat-1',
      turns: [],
      totals: {},
      injected_chars: 0,
      user_chars: 0,
      peak_context_used: 0,
      context_window: 0,
      window_days: 14,
    }),
    telemetryPromptTrace: async () => {
      throw new Error('prompt trace unreachable')
    },
  },
}))

import { ContextBreakdownTab } from '../pages/ContextBreakdownPanel'

afterEach(cleanup)

const tab = (client: QueryClient) => (
  <QueryClientProvider client={client}>
    <ContextBreakdownTab slot="chat-1" />
  </QueryClientProvider>
)

describe('ContextBreakdownTab prompt-trace error notice', () => {
  it('shows a failed prompt-trace read while Developer Mode is on, and drops it when the mode goes off', async () => {
    devMode = true
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const { rerender } = render(tab(client))
    // Fixed user-vocabulary text, not the server's message.
    await waitFor(() => expect(screen.getByText(/Couldn't load this session's prompt text/)).toBeTruthy())
    expect(screen.queryByText(/prompt trace unreachable/)).toBeNull()
    // The mode flips off on the SAME client: the query is disabled but React
    // Query retains its last error. The notice must go with the section it
    // described.
    devMode = false
    rerender(tab(client))
    expect(client.getQueryState(['prompt-trace', 'chat-1'])?.error).toBeTruthy()
    expect(screen.queryByText(/Couldn't load this session's prompt text/)).toBeNull()
  })

  it('drops the prompt-trace cache entry once no tab shows it, so a same-name replacement starts empty', async () => {
    devMode = true
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
    const { unmount } = render(tab(client))
    await waitFor(() => expect(client.getQueryState(['prompt-trace', 'chat-1'])).toBeTruthy())
    unmount()
    // gcTime 0: the entry is collected as soon as its last observer leaves, so
    // a new tab reusing the slot name cannot be served the closed one's text.
    await waitFor(() => expect(client.getQueryState(['prompt-trace', 'chat-1'])).toBeUndefined())
  })
})
