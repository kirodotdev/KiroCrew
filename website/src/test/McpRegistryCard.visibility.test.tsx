import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

/* ── Mocks: must run before importing the component ── */
const H = vi.hoisted(() => {
  class ApiError extends Error {
    status: number
    constructor(status: number, message: string) {
      super(message)
      this.name = 'ApiError'
      this.status = status
    }
  }
  return {
    api: {
      capabilityMcpRegistry: vi.fn(),
      capabilityMcpInstall: vi.fn(),
      capabilityMcpUninstall: vi.fn(),
      mcpServers: vi.fn(),
    },
    ApiError,
  }
})
vi.mock('../api/client', () => ({ api: H.api, ApiError: H.ApiError }))
vi.mock('../providers', () => ({
  useProvider: () => ({ labels: { pluginRegistryName: 'Packages' } }),
}))
vi.mock('../components/McpDetailModal', () => ({ default: () => null }))

import McpRegistryCard from '../components/McpRegistryCard'

function renderWithQuery() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  return render(<QueryClientProvider client={qc}><McpRegistryCard /></QueryClientProvider>)
}

beforeEach(() => {
  Object.values(H.api).forEach(m => 'mockReset' in m && m.mockReset())
  H.api.mcpServers.mockResolvedValue([])
})

describe('McpRegistryCard — Browse Integrations visibility', () => {
  it('hides the whole section when no integration endpoint is configured (registry 503)', async () => {
    H.api.capabilityMcpRegistry.mockRejectedValue(
      new H.ApiError(503, 'capability manager not available')
    )
    const { container } = renderWithQuery()
    // Nothing renders — no heading, no "Failed to load registry" card.
    await waitFor(() => expect(H.api.capabilityMcpRegistry).toHaveBeenCalled())
    await waitFor(() => expect(screen.queryByText('Browse Integrations')).toBeNull())
    expect(screen.queryByText('Failed to load registry')).toBeNull()
    expect(container).toBeEmptyDOMElement()
  })

  it('also hides on a 404 (endpoint not routed)', async () => {
    H.api.capabilityMcpRegistry.mockRejectedValue(new H.ApiError(404, 'not found'))
    renderWithQuery()
    await waitFor(() => expect(H.api.capabilityMcpRegistry).toHaveBeenCalled())
    await waitFor(() => expect(screen.queryByText('Browse Integrations')).toBeNull())
  })

  it('shows Browse Integrations when the registry is reachable', async () => {
    H.api.capabilityMcpRegistry.mockResolvedValue({
      servers: [{ id: 'slack-mcp', installed: '', title: 'Slack', tier: 'Recommended', description: 'Slack MCP' }],
    })
    renderWithQuery()
    expect(await screen.findByText('Browse Integrations')).toBeInTheDocument()
    expect(await screen.findByText('Slack')).toBeInTheDocument()
  })
})
