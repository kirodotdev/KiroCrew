/**
 * The session menu chevron sits on the same side of the title on every
 * viewport. The desktop header renders the icon-only trigger AHEAD of the
 * title (ChatTitleOverlay: chevron pill, then SessionTitleControl), so the
 * phone's titled trigger must put the chevron ahead of its title too.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'

vi.mock('../api/client', () => ({
  api: {
    slackChannels: vi.fn().mockResolvedValue([]),
    mcpActive: vi.fn().mockResolvedValue([]),
    chatFolders: vi.fn().mockResolvedValue([]),
  },
}))

import { ChatHeaderMenu } from '../pages/chat/ChatHeaderMenu'

describe('ChatHeaderMenu titled trigger', () => {
  it('puts the chevron before the title, matching the desktop header', () => {
    const store = createTestStore()
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <Provider store={store}>
          <ThemeProvider>
            <MemoryRouter>
              <ChatHeaderMenu activeSlot="chat-1" triggerLabel="My Session" />
            </MemoryRouter>
          </ThemeProvider>
        </Provider>
      </QueryClientProvider>,
    )
    const trigger = screen.getByTestId('session-title-menu')
    const chevron = trigger.querySelector('svg')
    const title = trigger.querySelector('.session-header-title')
    expect(chevron).not.toBeNull()
    expect(title?.textContent).toBe('My Session')
    expect(chevron!.compareDocumentPosition(title!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })
})
