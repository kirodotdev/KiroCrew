// #25962: opening a chat unmounts Customize, so the Services search text must
// still be in the box when Connections mounts again in the same browser tab.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
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

vi.mock('../pages/overview/McpTab', () => ({
  default: () => <p>mcp servers list</p>,
}))

import ConnectionsPage from '../pages/connections/ConnectionsPage'
import { ThemeProvider } from '../hooks/useTheme'
import { createTestStore } from './helpers'

function mount() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <Provider store={createTestStore()}>
        <ThemeProvider>
          <MemoryRouter initialEntries={['/capabilities?tab=mcp']}>
            <ConnectionsPage servicesEnabled />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
}

const searchBox = () => screen.getByRole('textbox', { name: 'Search services' })

beforeEach(() => {
  vi.clearAllMocks()
})

describe('Connections — Services search survives leaving the page (#25962)', () => {
  it('keeps the typed search across an unmount and remount', () => {
    const first = mount()
    fireEvent.change(searchBox(), { target: { value: 'slack' } })
    first.unmount()

    mount()
    expect(searchBox()).toHaveValue('slack')
  })

  it('opens with an empty search in a fresh tab', () => {
    mount()
    expect(searchBox()).toHaveValue('')
  })
})
