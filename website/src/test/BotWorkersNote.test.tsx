// A server reaches a Bot's workers only when it is added to that Bot. The MCP
// Servers page and the Connections page both add servers, so both say so.
import type { ReactElement } from 'react'
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'

vi.mock('../api/client', () => ({
  ApiError: class ApiError extends Error {},
  api: {
    mcpServers: vi.fn(async () => []),
    mcpProbe: vi.fn(async () => []),
    connectionsStatus: vi.fn(async () => ({ schema_version: 1, connections: [] })),
    connectionsPremint: vi.fn(async () => ({ ok: true, preminting: [] })),
    connectionsMintState: vi.fn(async () => ({ state: 'idle' })),
  },
}))

// The panes under the note have their own specs; this one pins the note.
vi.mock('../pages/settings/McpManagement', () => ({ McpManagement: () => <div /> }))
vi.mock('../pages/overview/McpTab', () => ({ default: () => <div /> }))
vi.mock('../components/RestartButton', () => ({ default: () => <div /> }))
vi.mock('../pages/connections/OAuthAppPanel', async () => ({
  OAuthDraftsContext: (await vi.importActual<typeof import('react')>('react')).createContext<(slug: string, dirty: boolean) => void>(() => {}),
  OAuthClientWrittenContext: (await vi.importActual<typeof import('react')>('react')).createContext<(slugs: string[]) => void>(() => {}),
  OAuthDraftValuesContext: (await vi.importActual<typeof import('react')>('react')).createContext({ get: () => undefined, set: () => {} }),
  OAuthAppPanel: () => <div />,
}))

import McpPage from '../pages/McpPage'
import ConnectionsPage from '../pages/connections/ConnectionsPage'
import { ThemeProvider } from '../hooks/useTheme'
import { createTestStore } from './helpers'

const NOTE = "A server added here does not reach Bots' workers. A Bot's workers get a server only when it is added to that Bot."

function mount(page: ReactElement) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <Provider store={createTestStore()}>
        <ThemeProvider>
          <MemoryRouter initialEntries={['/capabilities?tab=mcp']}>{page}</MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
}

describe('the Bot workers note', () => {
  it('shows on the MCP Servers page', () => {
    mount(<McpPage embedded />)
    expect(screen.getByText(NOTE)).toBeInTheDocument()
  })

  it('shows on the Connections page above its services', () => {
    mount(<ConnectionsPage servicesEnabled />)
    expect(screen.getByText(NOTE)).toBeInTheDocument()
  })

  it('is not on the Connections page while its service cards are off', () => {
    mount(<ConnectionsPage servicesEnabled={false} />)
    expect(screen.queryByText(NOTE)).not.toBeInTheDocument()
  })
})
